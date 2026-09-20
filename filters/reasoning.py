"""
title: Reasoning
author: Teriyake
version: 1.0.0
description: Reasoning effort control, one model per function.
"""

from typing import Optional

from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Filter execution priority. Lower runs first.",
        )
        reasoning_kwarg: str = Field(
            default="",
            description=(
                "The effort kwarg written to chat_template_kwargs "
                "(e.g. 'reasoning_effort', 'thinking_level'). "
                "Only sent when available_efforts is not empty; "
                "leave empty for toggle-only models."
            ),
        )
        available_efforts: str = Field(
            default="",
            description=(
                "Comma-separated reasoning effort levels, lowest to highest "
                "(e.g. 'low, medium, high'). These populate the user's effort "
                "dropdown exactly (no 'Off' option — on/off is a separate "
                "Thinking toggle). Leave empty for a toggle-only model."
            ),
        )
        default_effort: str = Field(
            default="",
            description=(
                "Preselected effort when the user has not saved a choice. "
                "Shown in the user's field description, not as a dropdown "
                "option. For toggle-only models, use 'off' or 'on' — it "
                "becomes the default state of the Thinking toggle."
            ),
        )
        enable_reasoning_kwarg: str = Field(
            default="",
            description=(
                "Optional boolean kwarg that toggles thinking itself "
                "(e.g. 'enable_thinking'). Written to chat_template_kwargs. "
                "Leave empty if the model doesn't need one."
            ),
        )
        default_preserve_thinking: bool = Field(
            default=False,
            description=(
                "Default state of the 'Preserve thinking' checkbox; shown "
                "in the checkbox description and applied when the user has "
                "not saved a choice yet."
            ),
        )
        preserve_thinking_kwarg: str = Field(
            default="",
            description=(
                "Optional boolean kwarg that preserves previous reasoning "
                "context between turns (e.g. 'preserve_thinking'). Written "
                "to chat_template_kwargs. Leave empty if the model doesn't "
                "support it — the checkbox is then hidden from the user."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        # Makes the filter appear as a toggleable chip in Open WebUI.
        self.toggle = True
        # NOTE: A hosted SVG URL is recommended over a base64 data URI to
        # avoid bloating function-list API payloads.
        self.icon = (
            "data:image/svg+xml,"
            "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
            "stroke='%23841000' stroke-width='2.35' stroke-linecap='round' stroke-linejoin='round'%3E"
            "%3Cpath d='M12 2a7 7 0 0 0-7 7c0 2.5 1.3 4.7 3.2 6 .6.4.8 1.1.8 1.8v.2h6v-.2c0-.7.2-1.4.8-1.8A7.002 7.002 0 0 0 19 9a7 7 0 0 0-7-7z'/%3E"
            "%3Cpath d='M9 18h6'/%3E"
            "%3Cpath d='M10 21.5h4'/%3E"
            "%3C/svg%3E"
        )

    # --- Current config ----------------------------------------------------

    def _cfg(self) -> dict:
        """Current admin config, read permissively from `self.valves`.

        Works whether Open WebUI stored a Valves model or a raw dict, and
        `available_efforts` as a CSV string or a list. Each field is coerced
        independently so one malformed value can never collapse the config.
        """
        raw = self.valves
        if raw is None:
            raw = {}
        elif not isinstance(raw, dict):
            try:
                raw = raw.model_dump()
            except Exception:
                raw = {}

        def s(key: str) -> str:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) else ""

        efforts_raw = raw.get("available_efforts", "")
        if isinstance(efforts_raw, str):
            parts = [p.strip() for p in efforts_raw.split(",")]
        elif isinstance(efforts_raw, (list, tuple)):
            parts = [str(p).strip() for p in efforts_raw]
        else:
            parts = []
        efforts: list[str] = []
        for p in parts:
            if p and p not in efforts:
                efforts.append(p)

        return {
            "efforts": efforts,
            "reasoning_kwarg": s("reasoning_kwarg"),
            "default_effort": s("default_effort"),
            "enable_reasoning_kwarg": s("enable_reasoning_kwarg"),
            "preserve_thinking_kwarg": s("preserve_thinking_kwarg"),
            "default_preserve_thinking": self._as_bool(
                raw.get("default_preserve_thinking", False)
            ),
        }

    # --- Derived defaults --------------------------------------------------

    @staticmethod
    def _effort_default(cfg: dict) -> str:
        efforts = cfg["efforts"]
        d = cfg["default_effort"]
        if d in efforts:
            return d
        return efforts[0] if efforts else ""

    @staticmethod
    def _thinking_default(cfg: dict) -> bool:
        return cfg["default_effort"] != "off"

    # --- User valves (dynamic schema, no cache) ----------------------------

    @property
    def UserValves(self):
        """UserValves class for the *current* valve configuration.

        Rebuilt from the saved valves on every access (no cache), so the
        modal always reflects the config as of the last request.

        Fields:
          - 'thinking' toggle (always)
          - 'reasoning_effort' dropdown — efforts only, default shown in
            the description rather than as an option
          - 'preserve_thinking' toggle — only when the model supports it
        """
        cfg = self._cfg()
        efforts = cfg["efforts"]
        effort_default = self._effort_default(cfg)
        thinking_default = self._thinking_default(cfg)
        preserve_default = cfg["default_preserve_thinking"]

        ns: dict = {}
        ann: dict = {}

        ns["thinking"] = Field(
            default=thinking_default,
            description=(
                "Enable thinking " f"(default: {'on' if thinking_default else 'off'})."
            ),
        )
        ann["thinking"] = bool

        if efforts:
            ns["reasoning_effort"] = Field(
                default=effort_default,
                description=f"Reasoning effort (default: {effort_default}).",
                json_schema_extra={"enum": efforts},
            )
            ann["reasoning_effort"] = str

        if cfg["preserve_thinking_kwarg"]:
            ns["preserve_thinking"] = Field(
                default=preserve_default,
                description=(
                    "Preserve previous reasoning context between turns "
                    f"(default: {'on' if preserve_default else 'off'})."
                ),
            )
            ann["preserve_thinking"] = bool

        ns["__annotations__"] = ann
        return type("UserValves", (BaseModel,), ns)

    # --- Helpers -------------------------------------------------------------

    @staticmethod
    def _valve_value(source, key, default=None):
        """Read a valve from either a dict or a model instance."""
        if isinstance(source, dict):
            return source.get(key, default)
        return getattr(source, key, default)

    @staticmethod
    def _as_bool(value) -> bool:
        """Truthiness that treats the strings 'off'/'false'/'0' as False."""
        if isinstance(value, str):
            return value.strip().lower() in ("on", "true", "1", "yes")
        return bool(value)

    # --- Request hook ------------------------------------------------------

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> dict:
        cfg = self._cfg()
        efforts = cfg["efforts"]
        effort_default = self._effort_default(cfg)

        # Open WebUI's generic top-level field must never reach the provider;
        # everything is written to chat_template_kwargs instead, so this
        # removal-only pop is always safe.
        body.pop("reasoning_effort", None)

        # --- Resolve the user's selection ---
        # A missing value means "user hasn't chosen yet", which falls back
        # to the configured default below.
        thinking = None
        effort = None
        preserve = None
        if __user__ is not None:
            uv = self._valve_value(__user__, "valves")
            if uv is not None:
                thinking = self._valve_value(uv, "thinking", None)
                effort = self._valve_value(uv, "reasoning_effort", None)
                preserve = self._valve_value(uv, "preserve_thinking", None)

        thinking = (
            self._thinking_default(cfg) if thinking is None else self._as_bool(thinking)
        )

        note = ""
        if efforts:
            if effort not in efforts:
                if effort not in (None, ""):
                    note = (
                        f" (saved selection '{effort}' unavailable, " "using default)"
                    )
                effort = effort_default
        else:
            effort = ""

        preserve = (
            cfg["default_preserve_thinking"]
            if preserve is None
            else self._as_bool(preserve)
        )

        # --- Apply: all kwargs go to chat_template_kwargs ---
        # The dict is created lazily, only when a kwarg actually needs it.
        template_kwargs = None

        def _tk() -> dict:
            nonlocal template_kwargs
            if template_kwargs is None:
                existing = body.get("chat_template_kwargs")
                if not isinstance(existing, dict):
                    existing = {}
                    body["chat_template_kwargs"] = existing
                template_kwargs = existing
            return template_kwargs

        if thinking:
            if cfg["enable_reasoning_kwarg"]:
                _tk()[cfg["enable_reasoning_kwarg"]] = True
            # Effort kwarg only exists when the model has effort levels;
            # toggle-only models carry just the enable flag.
            if cfg["reasoning_kwarg"] and efforts:
                _tk()[cfg["reasoning_kwarg"]] = effort
            label = effort.capitalize() if efforts else "On"
        else:
            if cfg["enable_reasoning_kwarg"]:
                _tk()[cfg["enable_reasoning_kwarg"]] = False
            if cfg["reasoning_kwarg"] and efforts:
                # Remove a pre-existing value without creating the dict.
                existing = body.get("chat_template_kwargs")
                if isinstance(existing, dict):
                    existing.pop(cfg["reasoning_kwarg"], None)
            label = "Off"

        if cfg["preserve_thinking_kwarg"]:
            _tk()[cfg["preserve_thinking_kwarg"]] = preserve

        # --- Status feedback ---
        if __event_emitter__:
            description = f"Reasoning: {label}"
            if thinking and note:
                description += note
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": True,
                        "hidden": False,
                    },
                }
            )
        return body
