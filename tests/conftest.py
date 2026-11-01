import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

os.environ["GRB_NO_AUTOAPP"] = "1"
os.environ["GRB_NO_DOTENV"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.guardrails import Policy  # noqa: E402
from app.paypal_client import MockPayPal  # noqa: E402
from app.service import BuyerService, Catalog  # noqa: E402


@pytest.fixture
def policy():
    return Policy.load(ROOT / "policy.yaml")


@pytest.fixture
def catalog():
    return Catalog.load(ROOT / "data" / "catalog.json")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("UTC"))

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(policy, catalog, clock):
    return BuyerService(policy, catalog, MockPayPal(), ":memory:", clock=clock)


# Settings a user may have exported in their shell (e.g. the README's offline block).
# Tests must not depend on them; tests that need one set it explicitly.
_APP_ENV = ("AGENT_MODE", "PAYPAL_MODE", "PAYPAL_MOCK_APPROVAL", "PAYPAL_AUTH", "PAYPAL_CLIENT_ID",
            "PAYPAL_CLIENT_SECRET", "PAYPAL_BASE_URL", "PUBLIC_BASE_URL", "APPROVER_TOKEN", "DB_PATH",
            "CLI_DB_PATH", "POLICY_PATH", "CATALOG_PATH", "MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            "OPENAI_MODEL", "OPENAI_MAX_TOKENS_PARAM", "PAYPAL_APPROVAL_TTL_MIN", "DEMO_MODE", "AGENT_RATE_LIMIT",
            "STATE_SECRET", "GRB_INSTANCE", "NEXCONN_MODE", "NEXCONN_APP_KEY", "NEXCONN_APP_SECRET",
            "NEXCONN_AREA", "NEXCONN_GROUP_ID", "NEXCONN_GROUP_NAME", "NEXCONN_BOT_USER_ID",
            "NEXCONN_APPROVER_IDS", "NEXCONN_SDK_BASE", "NEXCONN_WEBHOOK_MAX_SKEW_S", "NEXCONN_RELAY",
            "NEXCONN_FALLBACK", "NEXCONN_PROBE_EVERY_S",
            "TRY_MODE", "TRY_DIR", "TRY_TTL_HOURS", "TRY_MAX_ACTIVE", "TRY_MAX_LIVE_USERS", "TRY_MAX_MEMBERS",
            "TRY_CACHE_SIZE", "TRY_CREATE_LIMIT", "TRY_JOIN_LIMIT", "TRY_AGENT_LIMIT", "TRY_AGENT_LIMIT_IP")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for k in _APP_ENV:
        monkeypatch.delenv(k, raising=False)
