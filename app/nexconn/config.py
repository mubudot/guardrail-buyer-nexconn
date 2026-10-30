"""Settings for the Nexconn chat approval channel, read from the environment.

NEXCONN_MODE
    off   (default) no chat channel; the app behaves exactly as before.
    mock  in-memory stand-in for Nexconn (no account, no network): the approval room
          lives in the server process and the /chat page talks to it. For tests and an
          offline demo.
    live  the real Nexconn Platform Chat API (server side) + the Nexconn Web Chat SDK
          (browser side). Needs NEXCONN_APP_KEY and NEXCONN_APP_SECRET from a free
          Nexconn Console app.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Data centers documented at docs.nexconn.ai/platform-chat-api/prepare-to-use-api/base-url.
API_HOSTS = {
    "SG": ("api.sg-light-api.com", "api-b.sg-light-api.com"),
    "NA": ("api.us-light-api.com", "api-b.us-light-api.com"),
    "SA": ("api.sau-light-api.com", "api-b.sau-light-api.com"),
}
SDK_VERSION = "26.2.1"  # @nexconn/chat and @nexconn/engine must be the same version
DEFAULT_SDK_BASE = "https://cdn.jsdelivr.net/npm"
_ID = re.compile(r"[A-Za-z0-9]{1,64}")  # Nexconn user / group ids: letters and digits, max 64


class NexconnConfigError(ValueError):
    """Invalid NEXCONN_* settings (raised at startup, never mid-request)."""


@dataclass(frozen=True)
class NexconnConfig:
    mode: str = "off"
    app_key: str = ""
    app_secret: str = field(default="", repr=False)
    area: str = "SG"
    group_id: str = "grbapprovals"
    group_name: str = "GuardRail approvals"
    bot_user_id: str = "grbagent"
    approver_ids: tuple[str, ...] = ("approver1",)
    sdk_base: str = DEFAULT_SDK_BASE
    webhook_max_skew_s: int = 300
    public_base_url: str = ""

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def api_hosts(self) -> tuple[str, ...]:
        return API_HOSTS[self.area]

    def public(self) -> dict:
        """Settings safe to show in /health and to the browser (never the App Secret)."""
        return {"mode": self.mode, "area": self.area, "group_id": self.group_id,
                "bot_user_id": self.bot_user_id, "approver_ids": list(self.approver_ids),
                "app_key_set": bool(self.app_key), "sdk_version": SDK_VERSION}

    @classmethod
    def from_env(cls, env: dict | None = None) -> "NexconnConfig":
        e = os.environ if env is None else env
        mode = (e.get("NEXCONN_MODE") or "off").strip().lower()
        if mode not in ("off", "mock", "live"):
            raise NexconnConfigError(f"NEXCONN_MODE={mode!r}: use off, mock or live")
        area = (e.get("NEXCONN_AREA") or "SG").strip().upper()
        if area not in API_HOSTS:
            raise NexconnConfigError(f"NEXCONN_AREA={area!r}: use one of {', '.join(API_HOSTS)}")
        approvers = tuple(a.strip() for a in (e.get("NEXCONN_APPROVER_IDS") or "approver1").split(",")
                          if a.strip())
        cfg = cls(
            mode=mode,
            app_key=(e.get("NEXCONN_APP_KEY") or "").strip(),
            app_secret=(e.get("NEXCONN_APP_SECRET") or "").strip(),
            area=area,
            group_id=(e.get("NEXCONN_GROUP_ID") or "grbapprovals").strip(),
            group_name=(e.get("NEXCONN_GROUP_NAME") or "GuardRail approvals").strip()[:64],
            bot_user_id=(e.get("NEXCONN_BOT_USER_ID") or "grbagent").strip(),
            approver_ids=approvers,
            sdk_base=(e.get("NEXCONN_SDK_BASE") or DEFAULT_SDK_BASE).strip().rstrip("/"),
            webhook_max_skew_s=_int(e.get("NEXCONN_WEBHOOK_MAX_SKEW_S"), 300, "NEXCONN_WEBHOOK_MAX_SKEW_S"),
            public_base_url=(e.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.enabled:
            return
        for name, value in (("NEXCONN_GROUP_ID", self.group_id), ("NEXCONN_BOT_USER_ID", self.bot_user_id)):
            if not _ID.fullmatch(value):
                raise NexconnConfigError(f"{name}={value!r}: letters and digits only, 1-64 characters")
        if not self.approver_ids:
            raise NexconnConfigError("NEXCONN_APPROVER_IDS is empty: name at least one approver user id")
        for a in self.approver_ids:
            if not _ID.fullmatch(a):
                raise NexconnConfigError(f"NEXCONN_APPROVER_IDS entry {a!r}: letters and digits only, 1-64")
            if a == self.bot_user_id:
                raise NexconnConfigError("the agent bot user cannot also be an approver")
        if not self.sdk_base.startswith(("https://", "/")):
            raise NexconnConfigError("NEXCONN_SDK_BASE must be an https:// URL or a same-origin /path")
        if self.mode == "live" and not (self.app_key and self.app_secret):
            raise NexconnConfigError("NEXCONN_MODE=live needs NEXCONN_APP_KEY and NEXCONN_APP_SECRET "
                                     "(Nexconn Console -> Key Management); use NEXCONN_MODE=mock to try "
                                     "the chat flow without an account")


def _int(value: str | None, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    if not re.fullmatch(r"[0-9]{1,6}", value.strip()) or int(value) < 1:
        raise NexconnConfigError(f"{name}={value!r}: use a positive whole number of seconds")
    return int(value)
