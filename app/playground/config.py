"""TRY_* settings for the /try playground, read from the environment and validated at startup."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..ratelimit import ConfigError, parse_rate_limit

ROOT = Path(__file__).resolve().parent.parent.parent
Limit = tuple[int, float] | None


@dataclass(frozen=True)
class PlaygroundConfig:
    mode: str = "off"                       # off | on
    dir: Path = ROOT / "var" / "try"
    ttl_hours: int = 24
    max_active: int = 200
    max_live_users: int = 500
    max_members: int = 4
    cache_size: int = 32                    # sandboxes kept open in memory (the rest reload from disk)
    create_limit: Limit = (5, 3600.0)       # per client IP
    join_limit: Limit = (10, 3600.0)        # per client IP
    agent_limit: Limit = (20, 600.0)        # per sandbox
    agent_limit_ip: Limit = (30, 600.0)     # per client IP
    public_base_url: str = ""

    @property
    def enabled(self) -> bool:
        return self.mode == "on"

    @property
    def ttl_s(self) -> float:
        return self.ttl_hours * 3600.0

    @classmethod
    def from_env(cls, env: dict | None = None, nexconn_mode: str = "off") -> "PlaygroundConfig":
        e = os.environ if env is None else env
        mode = (e.get("TRY_MODE") or "off").strip().lower()
        if mode not in ("off", "on"):
            raise ConfigError(f"TRY_MODE={mode!r}: use off or on")
        if mode == "on" and nexconn_mode not in ("mock", "live"):
            raise ConfigError("TRY_MODE=on needs NEXCONN_MODE=mock or live (the playground's approval room "
                              "is a Nexconn chat)")
        raw_dir = (e.get("TRY_DIR") or "").strip()
        return cls(
            mode=mode,
            dir=Path(raw_dir) if raw_dir else ROOT / "var" / "try",
            ttl_hours=_pos_int(e, "TRY_TTL_HOURS", 24, 24 * 90),
            max_active=_pos_int(e, "TRY_MAX_ACTIVE", 200, 100_000),
            max_live_users=_pos_int(e, "TRY_MAX_LIVE_USERS", 500, 10_000_000, allow_zero=True),
            max_members=_pos_int(e, "TRY_MAX_MEMBERS", 4, 29),
            cache_size=_pos_int(e, "TRY_CACHE_SIZE", 32, 10_000),
            create_limit=parse_rate_limit(e.get("TRY_CREATE_LIMIT"), "TRY_CREATE_LIMIT", "5/3600"),
            join_limit=parse_rate_limit(e.get("TRY_JOIN_LIMIT"), "TRY_JOIN_LIMIT", "10/3600"),
            agent_limit=parse_rate_limit(e.get("TRY_AGENT_LIMIT"), "TRY_AGENT_LIMIT", "20/600"),
            agent_limit_ip=parse_rate_limit(e.get("TRY_AGENT_LIMIT_IP"), "TRY_AGENT_LIMIT_IP", "30/600"),
            public_base_url=(e.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
        )


def _pos_int(e, name: str, default: int, maximum: int, allow_zero: bool = False) -> int:
    raw = e.get(name)
    if raw is None or not str(raw).strip():
        return default
    v = str(raw).strip()
    lo = 0 if allow_zero else 1
    if not re.fullmatch(r"[0-9]{1,9}", v) or not lo <= int(v) <= maximum:
        raise ConfigError(f"{name}={raw!r}: use a whole number from {lo} to {maximum}")
    return int(v)
