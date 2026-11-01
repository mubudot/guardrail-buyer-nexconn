"""The /try page's pure modules in plain node: friendly-errors.js (HTTP status -> plain sentence),
try-model.js (invite / checkout-return parsing, the step checklist, the daily-cap bar, same-origin pay
links), the mock transport's configurable base path, and the shared room module's wiring. Skipped
without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def run(harness: str, *mods: str) -> dict:
    r = subprocess.run([NODE, "--input-type=module", "-e", harness, *[(STATIC / m).resolve().as_uri() for m in mods]],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


FRIENDLY = r"""
const {friendlyError: f, describeResponse} = await import(process.argv[1]);
const resp = (status, body, retry) => ({status, json: async () => body, headers: {get: (k) => k === 'Retry-After' ? retry : null}});
console.log(JSON.stringify({
  r429: f({status: 429, retryAfter: '12'}), r429min: f({status: 429, retryAfter: '3000'}), r429none: f({status: 429}),
  r503: f({status: 503, detail: 'The playground is at capacity right now. Please try again later.'}),
  r503tech: f({status: 503, detail: 'NEXCONN_MODE=live needs APPROVER_TOKEN'}),
  r502: f({status: 502, detail: 'Nexconn: /v4/x: HTTP 500'}), r500: f({status: 500, detail: 'Reference abc'}),
  r404s: f({status: 404, context: 'session'}), r401s: f({status: 401, context: 'session'}),
  r404join: f({status: 404, detail: 'This invite link is unknown or its sandbox expired.', context: 'join'}),
  r409: f({status: 409, detail: 'This sandbox already has 4 members.'}),
  r422: f({status: 422, detail: [{loc: ['body', 'name']}]}), html: f({status: 400, detail: '<b>Bad</b> things happened.'}),
  offline: f({offline: true}), zero: f({status: 0}),
  described: await describeResponse(resp(429, {detail: 'x'}, '5'), 'session'),
  describedNoBody: await describeResponse({status: 500, json: async () => { throw new Error('no'); }, headers: null}),
}));
"""


def test_friendly_errors():
    out = run(FRIENDLY, "friendly-errors.js")
    assert out["r429"] == "You're going fast — try again in 12 s."
    assert out["r429min"] == "You're going fast — try again in 50 min."
    assert out["r429none"] == "You're going fast — try again in a moment."
    assert out["r503"] == "The playground is at capacity right now. Please try again later."
    assert out["r503tech"].startswith("The playground is busy or at capacity")
    assert out["r502"] == "Nexconn or the network didn't answer — try again."
    assert out["r500"].startswith("Something went wrong on our side; nothing was bought or approved")
    assert out["r404s"].startswith("Your sandbox expired") and out["r401s"].startswith("This browser's sandbox")
    assert out["r404join"] == "This invite link is unknown or its sandbox expired."
    assert out["r409"] == "This sandbox already has 4 members."
    assert out["r422"] == "That didn't work (HTTP 422). Try again." and out["html"] == "That didn't work (HTTP 400). Try again."
    assert out["offline"] == out["zero"] == "You seem offline. Check your connection and try again."
    assert out["described"] == "You're going fast — try again in 5 s."
    assert out["describedNoBody"].startswith("Something went wrong on our side")


MODEL = r"""
const m = await import(process.argv[1]);
const code = 'a'.repeat(32);
console.log(JSON.stringify({
  invite: m.parseLocation(`/try/join/${code}`, ''), badInvite: m.parseLocation('/try/join/<x>', ''),
  plain: m.parseLocation('/try', ''), paid: m.parseLocation('/try', '?paid=ORD-1A&status=COMPLETED'),
  cancelled: m.parseLocation('/try', '?cancelled=ORD-2B'), evil: m.parseLocation('/try', '?paid=<script>&cancelled=a"b'),
  stepsNone: m.stepView(null), stepsSome: m.stepView({joined: true, auto_bought: true, invited: true}),
  stepsAll: m.stepView({joined: 1, auto_bought: 1, approval_requested: 1, decided: 1, paid: 1}),
  bar: [m.dailyProgress({spent_today: '75.00', daily_cap: '150.00'}), m.dailyProgress({spent_today: '354.98', daily_cap: '150'}),
        m.dailyProgress({}), m.dailyProgress({spent_today: '1', daily_cap: '0'})],
  money: [m.money('50.00'), m.money('7', 'EUR'), m.money('x')],
  pay: [m.safePayUrl('/try/s/0123456789ab/paypal/mock-checkout?token=MOCK-ABC'),
        m.safePayUrl('https://demo.example/try/s/0123456789ab/paypal/mock-checkout?token=MOCK-ABC'),
        m.safePayUrl('javascript:alert(1)'), m.safePayUrl('/paypal/mock-checkout?token=x'), m.safePayUrl(null)],
}));
"""


def test_try_model():
    out = run(MODEL, "try-model.js")
    assert out["invite"] == {"invite": True, "code": "a" * 32, "paid": None, "status": "", "cancelled": None}
    assert out["badInvite"]["invite"] is True and out["badInvite"]["code"] is None
    assert out["plain"]["invite"] is False
    assert out["paid"]["paid"] == "ORD-1A" and out["paid"]["status"] == "COMPLETED"
    assert out["cancelled"]["cancelled"] == "ORD-2B"
    assert out["evil"]["paid"] is None and out["evil"]["cancelled"] is None
    assert [s["next"] for s in out["stepsNone"]] == [True, False, False, False, False, False]
    some = {s["key"]: s for s in out["stepsSome"]}
    assert some["approval_requested"]["next"] and some["invited"]["done"] and not some["invited"]["next"]
    assert not any(s["next"] for s in out["stepsAll"])
    assert out["bar"] == [{"pct": 50, "over": False}, {"pct": 100, "over": True}, {"pct": 0, "over": False},
                          {"pct": 0, "over": False}]
    assert out["money"] == ["$50.00", "7.00 EUR", "—"]
    assert out["pay"] == ["/try/s/0123456789ab/paypal/mock-checkout?token=MOCK-ABC",
                          "/try/s/0123456789ab/paypal/mock-checkout?token=MOCK-ABC", None, None, None]


TRANSPORT = r"""
const mod = await import(process.argv[1]);
const calls = [];
const fetchImpl = async (url, opts = {}) => {
  calls.push(url);
  if (url.includes('/messages')) return calls.length > 2 ? {ok: false, status: 401} : {ok: true, json: async () => ({next: 0, messages: []})};
  return {ok: true, json: async () => ({message_id: 'M'})};
};
const t = mod.createMockTransport({token: 't'}, {fetchImpl, intervalMs: 5, base: '/try/mock'});
const statuses = [];
await t.start(() => {}, (s) => statuses.push(s));
await t.send('hi', null);
await new Promise((r) => setTimeout(r, 40));
t.stop();
const d = mod.createMockTransport({token: 't'}, {fetchImpl: async (u) => { calls.push(u); return {ok: true, json: async () => ({next: 0, messages: []})}; }});
await d.start(() => {}); d.stop();
console.log(JSON.stringify({calls, statuses}));
"""


def test_mock_transport_base_path_and_expired_room_session():
    out = run(TRANSPORT, "chat-transport.js")
    assert out["calls"][0] == "/try/mock/messages?after=0" and out["calls"][1] == "/try/mock/send"
    assert out["calls"][-1] == "/nexconn/mock/messages?after=0"            # default base unchanged
    assert out["statuses"][0] == "connected" and "session expired" in out["statuses"]


def test_try_page_wiring():
    """try.js / try.html: one room implementation (chat-room.js), text-only rendering, the checklist and
    chips the design asks for, creating and joining only by POST from a button."""
    js = (STATIC / "try.js").read_text()
    html = (STATIC / "try.html").read_text()
    room = (STATIC / "chat-room.js").read_text()
    assert 'from "/static/chat-room.js"' in js and "approvalCard(" not in js
    assert "innerHTML" not in js + room and "insertAdjacentHTML" not in js + room
    assert '{mockBase: "/try/mock"}' in js
    assert 'api("/try/start", {method: "POST"' in js and 'api("/try/join", {method: "POST"' in js
    for step in ("joined", "auto_bought", "approval_requested", "decided", "paid", "invited"):
        assert f'data-step="{step}"' in html
    for chip in ("under $50 · bought right away", "over $50 · asks you in chat", "gift card · always refused"):
        assert chip in html
    assert 'aria-live="polite"' in html and 'id="answer"' in html and 'for="name"' in html
    assert "Approve your AI buyer's purchases in a chat — try it in a minute" in html
    assert "After 24 h of inactivity the" in html and "stay with our chat provider, Nexconn" in html
    css = (STATIC / "try.css").read_text()
    assert "prefers-reduced-motion" in css and "grid-template-columns: 380px" in css
    assert "@import" not in css and "http" not in css.replace("http://www.w3.org/2000/svg", "")
