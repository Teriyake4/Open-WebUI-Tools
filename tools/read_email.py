"""
title: Read Email
author: Teriyake
description: Read-only access to up to two personal IMAP mail accounts (Gmail, Outlook.com, M365). Each user fills in their own accounts and app passwords in the per-user valves; no mail is ever sent, moved or deleted.
version: 1.0.0
required_open_webui_version: 0.6.3
"""

import asyncio
import email
import imaplib
import json
import re
from email.header import decode_header
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ToolError(Exception):
    """Expected, user-actionable error whose message is safe to return to the LLM."""


class _HTMLToText(HTMLParser):
    """Small stdlib-only HTML -> text converter for email bodies."""

    SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode_header(value: Optional[str]) -> str:
    """Decode a possibly RFC 2047 encoded header value to plain text."""
    if not value:
        return ""
    parts = decode_header(value)
    out: List[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _parse_folder_name(entry: bytes) -> str:
    """Extract the folder name from one IMAP LIST response entry."""
    text = entry.decode("utf-8", errors="replace").strip()
    # Quoted name, e.g. b'(\\HasNoChildren) "/" "Sent Items"'
    m = re.match(r'^\([^)]*\)\s+\S+\s+"(.*)"\s*$', text)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    # Atom name, e.g. b'(\\Noselect) * INBOX'
    m = re.match(r'^\([^)]*\)\s+\S+\s+(\S+)\s*$', text)
    if m:
        return m.group(1)
    raise ToolError(f"Could not parse IMAP folder entry: {text!r}")


def _imap_folder_arg(folder: str) -> str:
    """imaplib writes string arguments to the wire verbatim, so IMAP-quote names that are not plain atoms."""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", folder):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tokenize_imap_search(query: str) -> List[str]:
    """Split an IMAP search string into wire tokens, keeping quoted values quoted."""
    tokens: List[str] = []
    cur: List[str] = []
    in_quotes = False
    for ch in (query or "").strip():
        if ch == '"':
            in_quotes = not in_quotes
            cur.append(ch)
        elif ch == " " and not in_quotes:
            tok = "".join(cur).strip()
            if tok:
                tokens.append(tok)
            cur = []
        else:
            cur.append(ch)
    tok = "".join(cur).strip()
    if tok:
        tokens.append(tok)
    if in_quotes:
        raise ToolError("IMAP search query has unbalanced quotes around a value.")
    return tokens or ["ALL"]


def _extract_body(msg) -> "tuple[str, str]":
    """Return (body_text, body_type) for a message; prefers text/plain, else strips HTML."""
    best: Optional[tuple] = None
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        priority = 0 if ctype == "text/plain" else 1
        if best is None or priority < best[0]:
            best = (priority, ctype, text)
    if best is None:
        return ("", "none")
    _, ctype, text = best
    if ctype == "text/html":
        parser = _HTMLToText()
        try:
            parser.feed(text)
            text = parser.text()
        except Exception:
            pass
    return (text.strip(), "text" if ctype == "text/plain" else "html")


class Tools:
    class UserValves(BaseModel):
        account1_label: str = Field(
            default="personal",
            description="Short name for account 1, used as the 'account' argument (e.g. 'personal').",
        )
        account1_host: str = Field(
            default="imap.gmail.com",
            description="IMAP host for account 1 (Gmail: imap.gmail.com, Outlook.com/M365: outlook.office365.com).",
        )
        account1_port: int = Field(default=993, description="IMAP port for account 1 (993 for SSL).")
        account1_username: str = Field(default="", description="Full email address for account 1.")
        account1_app_password: str = Field(
            default="",
            description="App password for account 1 (Google app password / Microsoft app password). Never the real account password.",
        )
        account2_label: str = Field(
            default="work",
            description="Short name for account 2, used as the 'account' argument (e.g. 'work'). Leave account 2 empty to disable it.",
        )
        account2_host: str = Field(
            default="outlook.office365.com",
            description="IMAP host for account 2.",
        )
        account2_port: int = Field(default=993, description="IMAP port for account 2 (993 for SSL).")
        account2_username: str = Field(default="", description="Full email address for account 2.")
        account2_app_password: str = Field(
            default="",
            description="App password for account 2. Leave blank if account 2 is not used.",
        )

    def __init__(self):
        self.user_valves = self.UserValves()

    # ------------------------------------------------------------------
    # Internal helpers (underscore-prefixed, never exposed to the model)
    # ------------------------------------------------------------------

    def _resolve_account(self, account: str, user_valves) -> Dict[str, Any]:
        accounts: Dict[str, Dict[str, Any]] = {}
        for prefix in ("account1", "account2"):
            label = str(getattr(user_valves, prefix + "_label") or "").strip().lower()
            host = str(getattr(user_valves, prefix + "_host") or "").strip()
            username = str(getattr(user_valves, prefix + "_username") or "").strip()
            if not (label and host and username):
                continue
            accounts[label] = {
                "label": label,
                "host": host,
                "port": int(getattr(user_valves, prefix + "_port") or 993),
                "username": username,
                "app_password": str(getattr(user_valves, prefix + "_app_password") or ""),
            }
        if not accounts:
            raise ToolError(
                "No mail account is configured yet. Fill in host, username and app password "
                "for account 1 in the tool's per-user valves (settings of this tool)."
            )
        wanted = (account or "").strip().lower()
        if wanted in accounts:
            acc = accounts[wanted]
        elif len(accounts) == 1:
            acc = next(iter(accounts.values()))
        else:
            raise ToolError(
                f"Unknown account '{account}'. Configured accounts: {', '.join(sorted(accounts))}."
            )
        if not acc["app_password"]:
            raise ToolError(
                f"Account '{acc['label']}' has no app password configured yet. "
                "Set it in the tool's per-user valves."
            )
        return acc

    @staticmethod
    def _connect(acc: Dict[str, Any]) -> imaplib.IMAP4_SSL:
        try:
            conn = imaplib.IMAP4_SSL(acc["host"], acc["port"])
            conn.sock.settimeout(30)
            conn.login(acc["username"], acc["app_password"])
        except imaplib.IMAP4.error as e:
            raise ToolError(
                f"IMAP login failed for account '{acc['label']}' ({acc['host']}): {e}. "
                "Check host, username and app password in the tool's valves. "
                "Gmail: enable 2FA and create an App Password. "
                "Microsoft/Outlook: enable IMAP for the account and use an app password or sign-in with IMAP."
            ) from e
        except OSError as e:
            raise ToolError(f"Could not reach IMAP host {acc['host']}:{acc['port']}: {e}") from e
        return conn

    @staticmethod
    def _select_folder(conn: imaplib.IMAP4_SSL, folder: str) -> None:
        status, _ = conn.select(_imap_folder_arg(folder), readonly=True)
        if status[0] != "OK":
            raise ToolError(
                f"Could not open folder '{folder}' (it may not exist). "
                "Call list_folders to see the exact folder names."
            )

    @staticmethod
    def _search_uids(conn: imaplib.IMAP4_SSL, query: str) -> List[int]:
        tokens = _tokenize_imap_search(query)
        status, data = conn.uid("SEARCH", None, *tokens)
        if status != "OK":
            raise ToolError(f"IMAP search failed: {data}")
        raw = data[0] if data and data[0] else b""
        return [int(u) for u in raw.split()]

    @staticmethod
    def _fetch_uid(conn: imaplib.IMAP4_SSL, uid: int, part_spec: str) -> Optional[bytes]:
        status, data = conn.uid("FETCH", uid, part_spec)
        if status != "OK":
            raise ToolError(f"IMAP fetch failed: {data}")
        if not data or data[0] in (None, b"NIL", "NIL"):
            return None
        item = data[0]
        if isinstance(item, tuple):
            return item[1] if item[1] is not None else b""
        return None

    @staticmethod
    def _safe_logout(conn: imaplib.IMAP4_SSL) -> None:
        try:
            conn.logout()
        except Exception:
            pass

    def _list_folders(self, acc: Dict[str, Any]) -> Dict[str, Any]:
        conn = self._connect(acc)
        try:
            status, data = conn.list()
            if status != "OK":
                raise ToolError(f"IMAP LIST failed: {data}")
            folders = set()
            for entry in data:
                if not entry:
                    continue
                try:
                    folders.add(_parse_folder_name(entry))
                except ToolError:
                    continue
            return {"account": acc["label"], "folders": sorted(folders)}
        finally:
            self._safe_logout(conn)

    def _search(self, acc: Dict[str, Any], folder: str, query: str, limit: int) -> Dict[str, Any]:
        conn = self._connect(acc)
        try:
            self._select_folder(conn, folder)
            uids = self._search_uids(conn, query)
            uids.sort(reverse=True)  # UIDs grow with arrival, so newest first
            total = len(uids)
            uids = uids[:limit]
            emails_list: List[Dict[str, Any]] = []
            for uid in uids:
                raw = self._fetch_uid(conn, uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
                if raw:
                    msg = email.message_from_bytes(raw)
                    emails_list.append(
                        {
                            "uid": uid,
                            "subject": _decode_header(msg.get("Subject")),
                            "from": _decode_header(msg.get("From")),
                            "date": msg.get("Date") or "",
                        }
                    )
                else:
                    emails_list.append({"uid": uid, "subject": "", "from": "", "date": ""})
            return {
                "account": acc["label"],
                "folder": folder,
                "query": query,
                "total_matches": total,
                "returned": len(emails_list),
                "emails": emails_list,
            }
        finally:
            self._safe_logout(conn)

    def _read(self, acc: Dict[str, Any], folder: str, uid: int, max_chars: int) -> Dict[str, Any]:
        conn = self._connect(acc)
        try:
            self._select_folder(conn, folder)
            raw = self._fetch_uid(conn, uid, "(BODY.PEEK[])")
            if raw is None:
                raise ToolError(
                    f"No message with UID {uid} in folder '{folder}'. UIDs are stable but can "
                    "disappear if a message is deleted on the server; run search_emails again to "
                    "get a current UID."
                )
            msg = email.message_from_bytes(raw) if raw else email.message()
            body, body_type = _extract_body(msg)
            truncated = False
            if len(body) > max_chars:
                body = body[:max_chars]
                truncated = True
            attachments = []
            for part in msg.walk():
                if part.get_content_disposition() != "attachment":
                    continue
                fname = _decode_header(part.get_filename())
                if fname:
                    attachments.append(fname)
            return {
                "account": acc["label"],
                "folder": folder,
                "uid": uid,
                "subject": _decode_header(msg.get("Subject")),
                "from": _decode_header(msg.get("From")),
                "to": _decode_header(msg.get("To")),
                "cc": _decode_header(msg.get("Cc")),
                "date": msg.get("Date") or "",
                "body": body,
                "body_type": body_type,
                "truncated": truncated,
                "attachments": attachments,
            }
        finally:
            self._safe_logout(conn)

    # ------------------------------------------------------------------
    # Public tool methods (exposed to the model; docstrings are read by the LLM)
    # ------------------------------------------------------------------

    async def list_folders(self, account: str, __user__: Optional[dict] = None) -> str:
        """List the folders (mailboxes) of one mail account.

        Call this first to learn the exact folder names (e.g. 'INBOX', 'Sent', 'Archive') to use with search_emails and read_email_content.

        :param account: Name of the configured mail account from the tool's per-user valves, e.g. 'personal' or 'work'.
        :param __user__: Internal user context (injected by Open WebUI, not set by the model).
        :return: JSON object {"account": ..., "folders": [...]}, or {"error": ...} on failure.
        """
        try:
            valves = (__user__ or {}).get("valves") or self.user_valves
            acc = self._resolve_account(account, valves)
            result = await asyncio.to_thread(self._list_folders, acc)
            return json.dumps(result, ensure_ascii=False)
        except ToolError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    async def search_emails(
        self,
        account: str,
        folder: str = "INBOX",
        query: str = "ALL",
        limit: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """Search for emails in a folder and return their metadata (not full bodies).

        'query' uses IMAP search syntax, e.g. 'ALL', 'FROM "boss@company.com"', 'SUBJECT "invoice"', 'SINCE 01-Jan-2025', or combined: 'FROM "alice@example.com" SUBJECT "report" SINCE 01-Jan-2025'. Dates use IMAP format DD-Mon-YYYY with English months (e.g. 01-Jan-2025). Results are the most recent matches first. Use the returned 'uid' with read_email_content to open a message.

        :param account: Name of the configured mail account from the tool's per-user valves, e.g. 'personal' or 'work'.
        :param folder: Folder name, e.g. 'INBOX'. Use list_folders to discover folder names.
        :param query: IMAP search expression.
        :param limit: Maximum number of messages to return (1-50, default 10).
        :param __user__: Internal user context (injected by Open WebUI, not set by the model).
        :return: JSON object {"account", "folder", "query", "total_matches", "returned", "emails": [{"uid", "subject", "from", "date"}]}, or {"error": ...} on failure.
        """
        try:
            limit = max(1, min(int(limit), 50))
            valves = (__user__ or {}).get("valves") or self.user_valves
            acc = self._resolve_account(account, valves)
            result = await asyncio.to_thread(self._search, acc, folder, query, limit)
            return json.dumps(result, ensure_ascii=False)
        except ToolError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    async def read_email_content(
        self,
        account: str,
        uid: int,
        folder: str = "INBOX",
        max_chars: int = 100000,
        __user__: Optional[dict] = None,
    ) -> str:
        """Read the full content of one email by the UID returned by search_emails.

        The message is fetched with BODY.PEEK, so the mail server does not mark it as read. HTML-only messages are converted to plain text. If a message has no text (e.g. only attachments), body may be empty and 'attachments' lists the attachment file names.

        :param account: Name of the configured mail account from the tool's per-user valves, e.g. 'personal' or 'work'.
        :param uid: UID of the message, as returned by search_emails (UIDs are per-folder).
        :param folder: Folder the UID belongs to; must be the same folder used in search_emails.
        :param max_chars: Truncate the body to this many characters (default 100000, capped at 500000).
        :param __user__: Internal user context (injected by Open WebUI, not set by the model).
        :return: JSON object {"account", "folder", "uid", "subject", "from", "to", "cc", "date", "body", "body_type", "truncated", "attachments"}, or {"error": ...} on failure.
        """
        try:
            max_chars = max(1000, min(int(max_chars), 500000))
            valves = (__user__ or {}).get("valves") or self.user_valves
            acc = self._resolve_account(account, valves)
            result = await asyncio.to_thread(self._read, acc, folder, uid, max_chars)
            return json.dumps(result, ensure_ascii=False)
        except ToolError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
