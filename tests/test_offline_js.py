"""Banner wording/view rules and SDK connection recovery helpers, using plain node only."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def test_offline_banner_wording_links_and_accessibility():
    for page in ("try", "chat"):
        html = (STATIC / f"{page}.html").read_text()
        js = (STATIC / f"{page}.js").read_text()
        assert "Offline mode — Nexconn isn't reachable right now" in html
        assert "Nothing here is a recording." in html
        assert "The guardrails, approvals, spending limits and simulated payments are the real code," in html
        assert "running for real — only the chat transport is replaced." in html
        assert 'https://youtu.be/XQdg7dW62xc" target="_blank" rel="noopener noreferrer"' in html
        assert 'https://github.com/mubudot/guardrail-buyer-nexconn/blob/main/docs/NEXCONN_LIVE_E2E.md" target="_blank" rel="noopener noreferrer"' in html
        assert 'id="offline-banner" class="offline-banner" role="status" aria-live="polite" hidden' in html
        assert 'from "/static/offline-model.js"' in js and "updateOfflineNotice" in js
        assert "watchSdk(" in js
    assert "Offline stand-in (Nexconn unreachable)" in (STATIC / "chat.js").read_text()
    assert "Start a new live sandbox" in (STATIC / "try.html").read_text()
    assert '$("start-form").requestSubmit()' in (STATIC / "try.js").read_text()
    assert 'window.confirm("Leave this sandbox' in (STATIC / "try.js").read_text()
    css = (STATIC / "chat.css").read_text()
    assert "overflow-wrap: anywhere" in css and "border-left: 4px solid var(--warn)" in css


HARNESS = r"""
const m = await import(process.argv[1]);
const reason = ['auth', 'timeout', 'server', 'unknown', 'untrusted'].map((r) => m.reasonLine(r, null));
const since = m.reasonLine('auth', '2026-11-01T12:30:00+00:00', () => 'Nov 1, 12:30');
const invalidSince = m.reasonLine('server', '<bad date>');
const view = [
  m.offlineView({mode: 'live', fallback: {active: true, reason: 'auth'}}),
  m.offlineView({mode: 'mock'}),
  m.offlineView({chat_mode: 'mock', fallback: {active: true, reason: 'capacity'}}),
  m.offlineView({chat_mode: 'mock', fallback: {active: true, reason: 'upstream', detail: 'timeout', upstream: 'offline'}}),
  m.offlineView({chat_mode: 'mock', fallback: {active: true, reason: 'upstream', detail: 'auth', upstream: 'live'}}),
  m.offlineView({mode: 'mock', fallback: {active: true, reason: 'server'}, upstream: {state: 'offline'}}),
];
const failures = [m.sdkFailure('connected', 15000), m.sdkFailure('connecting', 14999),
  m.sdkFailure('connecting', 15000), m.sdkFailure('auth error'), m.sdkFailure('kicked offline by other client'),
  m.sdkFailure('plan expired'), m.sdkFailure('disconnected')];
let callback = null, timerCount = 0, failCount = 0;
const timers = {setTimer: (fn, ms) => { if (ms !== 15000) throw Error('wrong timeout'); callback = fn; return ++timerCount; },
                clearTimer: () => { callback = null; }};
const watch = m.watchSdk(() => failCount++, timers);
watch.status('connecting');
const initialTimers = timerCount;
watch.status('connected');
const cleared = callback === null;
// after the first connection the watchdog is done: drops, kicks and later errors change nothing
watch.status('disconnected'); watch.status('kicked offline by other client'); watch.status('auth failed'); watch.fail();
const afterConnected = failCount;
const never = m.watchSdk(() => failCount++, timers);
never.status('connecting');
callback(); never.fail(); never.status('auth failed');
const once = failCount;
const stopped = m.watchSdk(() => failCount++, timers);
stopped.stop(); stopped.fail();
const kicked = m.watchSdk(() => failCount++, timers);
kicked.status('kicked offline by other client');   // a second tab of the same member is not an outage
const afterKick = failCount;
kicked.stop();
const immediate = m.watchSdk(() => failCount++, timers);
immediate.status('auth failed');
const manual = m.reasonLine('manual');
console.log(JSON.stringify({reason, since, invalidSince, view, failures, initialTimers, cleared, once, failCount,
  afterConnected, afterKick, manual,
  tooltip: m.OFFLINE_TOOLTIP}));
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_offline_model_reason_lines_visibility_and_sdk_watchdog():
    proc = subprocess.run([NODE, "--input-type=module", "-e", HARNESS, (STATIC / "offline-model.js").as_uri()],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    reasons = ["Why: Nexconn declined this app's credentials (the free chat plan may have ended).",
               "Why: Nexconn didn't answer in time.", "Why: Nexconn returned server errors.",
               "Why: the Nexconn connection failed."]
    assert out["reason"] == [*reasons, reasons[-1]]
    assert out["since"] == reasons[0] + " Since Nov 1, 12:30."
    assert out["invalidSince"] == reasons[2]
    assert [view["visible"] for view in out["view"]] == [False, False, False, True, True, True]
    assert [view["recovered"] for view in out["view"]] == [False, False, False, False, True, False]
    assert out["view"][3]["reason"] == reasons[1]
    assert out["failures"] == [False, False, True, True, False, True, False]   # kicked: not an outage
    assert out["initialTimers"] == 1 and out["cleared"] and out["afterConnected"] == 0
    assert out["once"] == 1 and out["afterKick"] == 1 and out["failCount"] == 2
    assert out["manual"] == "Why: offline mode was switched on for this server."
    assert out["tooltip"] == "Nexconn is unreachable, so this room runs on an in-process stand-in. Guardrails and approvals run for real."


PENDING_SEND = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const relayStatus = Number(process.argv[2]);
const elements = new Map(), polls = [], transports = [], relays = [], rendered = [], alerts = [];
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: false, handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; }, append() {}, focus() {}});
  return elements.get(id);
};
let cfg = {mode: 'live', relay: true, approver_ids: ['alice', 'bob']};
let resolveSend;
const fetch = async (url, opts = {}) => {
  if (url === '/nexconn/config') return {ok: true, json: async () => ({...cfg})};
  if (url.startsWith('/nexconn/session?')) return {ok: true, json: async () => ({
    mode: cfg.mode, user_id: new URL(url, 'http://local').searchParams.get('as_user'),
    group_id: 'approvals', approver_ids: cfg.approver_ids,
  })};
  if (url === '/nexconn/relay') {
    relays.push({headers: opts.headers, body: JSON.parse(opts.body)});
    return {ok: relayStatus === 200, status: relayStatus, json: async () => ({detail: 'relay unavailable'})};
  }
  if (url === '/health') return {ok: true, json: async () => ({})};
  throw Error(`unexpected fetch: ${url}`);
};
const context = vm.createContext({document: {getElementById: $, hidden: false}, fetch,
  localStorage: {getItem: () => 'original-token', setItem() {}},
  setInterval: (fn) => polls.push(fn), window: {alert: (msg) => alerts.push(msg)}});
const stubs = {
  '/static/chat-transport.js': {createTransport: async (session) => {
    const transport = {mode: session.mode, stopped: false, sends: [],
      async start(onMessages, onStatus) { onStatus('connected'); },
      send(text, extra) { this.sends.push({text, extra}); return new Promise((r) => { resolveSend = r; }); },
      stop() { this.stopped = true; }};
    transports.push(transport);
    return transport;
  }},
  '/static/chat-room.js': {el: () => ({}),
    isDecision: (text, extra) => extra?.grb === 'decision' || /^approve APR-/.test(text),
    createRoom: () => ({renderMessage: (m) => rendered.push(m), onMessages() {},
      async syncApprovals() {}, clear() {}})},
  '/static/offline-model.js': {OFFLINE_TOOLTIP: '', offlineView: () => ({visible: false}),
    watchSdk: () => ({status() {}, stop() {}, fail() {}})},
  '/static/offline-notice.js': {updateOfflineNotice() {}},
};
const page = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'), {context});
await page.link((name) => {
  const exports = stubs[name];
  if (!exports) throw Error(`unexpected import: ${name}`);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, {context});
});
await page.evaluate();
const tick = () => new Promise((r) => setImmediate(r));
await tick();
$('as-user').value = 'alice';
await $('join-form').handlers.submit({preventDefault() {}});
$('text').value = 'approve APR-12345678';
const pending = $('composer').handlers.submit({preventDefault() {}});
// The real pollConfig -> join path replaces transport, identity, credentials and relay setting.
cfg = {...cfg, mode: 'mock', relay: false};
$('as-user').value = 'bob'; $('token').value = 'replacement-token';
polls[0]();
await tick();
const beforeCompletion = {modes: transports.map((t) => t.mode), stopped: transports[0].stopped,
  sends: transports.map((t) => t.sends), relays: relays.length};
resolveSend({messageId: 'LIVE-DECISION'});
await pending;
console.log(JSON.stringify({beforeCompletion, relays, rendered, alerts, text: $('text').value}));
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("relay_status", [200, 502])
def test_chat_pending_live_decision_relays_original_session_after_automatic_rejoin(relay_status):
    proc = subprocess.run([NODE, "--experimental-vm-modules", "--input-type=module", "-e", PENDING_SEND,
                           str(STATIC / "chat.js"), str(relay_status)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["beforeCompletion"] == {
        "modes": ["live", "mock"], "stopped": True,
        "sends": [[{"text": "approve APR-12345678", "extra": None}], []], "relays": 0,
    }
    assert out["relays"] == [{
        "headers": {"Content-Type": "application/json", "X-Approver-Token": "original-token"},
        "body": {"message_id": "LIVE-DECISION", "user_id": "alice", "text": "approve APR-12345678", "extra": None},
    }]
    assert len(out["rendered"]) == 1
    assert out["rendered"][0]["id"] == "LIVE-DECISION" and out["rendered"][0]["from"] == "alice"
    if relay_status == 200:
        assert out["alerts"] == [] and out["text"] == ""
    else:
        assert len(out["alerts"]) == 1 and "HTTP 502: relay unavailable" in out["alerts"][0]
        assert out["text"] == "approve APR-12345678"


RETRY = r"""
const m = await import(process.argv[1]);
console.log(JSON.stringify([
  m.sdkRetryDelay(409, null, 0), m.sdkRetryDelay(409, null, 1), m.sdkRetryDelay(0, null, 2),
  m.sdkRetryDelay(429, '31', 0), m.sdkRetryDelay(429, 'soon', 0), m.sdkRetryDelay(429, '600', 0),
  m.sdkRetryDelay(409, null, 4), m.sdkRetryDelay(409, null, 5), m.sdkRetryDelay(401, null, 0),
  m.sdkRetryDelay(500, '5', 0),
]));
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_sdk_failed_retry_policy_backs_off_and_honours_retry_after():
    proc = subprocess.run([NODE, "--input-type=module", "-e", RETRY, (STATIC / "offline-model.js").as_uri()],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [2000, 4000, 8000, 31000, 2000, 60000, 32000, None, None, None]
    js = (STATIC / "try.js").read_text()
    body = js[js.index("async function fallbackForSdk"):js.index("async function send(")]
    assert "sdkRetryDelay(status, r?.headers.get(\"Retry-After\"), attempt)" in body
    assert body.count("if (!current()) return;") >= 3     # a restarted room or new session stops retrying


TRY_ROOM_RETRY = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const elements = new Map(), polls = [], requests = [], transports = [], toasts = [];
const elem = (id) => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: false, style: {}, handlers: {}, textContent: '',
    addEventListener(type, fn) { this.handlers[type] = fn; }, focus() {}, append() {}, replaceChildren() {},
    setAttribute() {}, querySelector() { return null; }, classList: {add() {}, remove() {}, toggle() {}}});
  return elements.get(id);
};
let mode = 'live', now = 1_000_000, sessionAnswers = [409, 409, 200];
const sandbox = () => ({sandbox_id: 'abc', name: 'Owner', chat_mode: mode,
  fallback: {active: mode === 'mock', reason: 'upstream', detail: 'auth', upstream: 'offline'},
  you: {role: 'owner', name: 'Owner', user_id: 'owner'}, members: [{name: 'Owner', user_id: 'owner', role: 'owner'}],
  expires_at: 'Mon 2026-11-02 8:00 AM CST (UTC+08:00)', steps: {},
  limits: {currency: 'USD', per_order_cap: '50', hard_max_per_order: '500', daily_cap: '250', spent_today: '0'},
  invite_url: '/try/join/code', orders: [], approvals: []});
class FakeDate extends Date { static now() { return now; } }
const context = vm.createContext({Date: FakeDate,
  document: {getElementById: elem, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, hidden: false},
  location: {pathname: '/try', search: '', origin: 'http://local'},
  localStorage: {getItem: () => 'owner-secret', setItem() {}, removeItem() {}},
  history: {replaceState() {}}, window: {confirm: () => true},
  setTimeout: () => 1, clearTimeout() {}, setInterval: (fn) => { polls.push(fn); return polls.length; }, clearInterval() {},
  fetch: async (url) => {
    requests.push(url);
    if (url === '/try/state') return {ok: true, json: async () => sandbox()};
    if (url === '/try/session') {
      const status = sessionAnswers.shift();
      if (status !== 200) return {ok: false, status, json: async () => ({detail: 'busy'})};
      return {ok: true, json: async () => ({mode, user_id: 'owner', group_id: 'g'})};
    }
    throw Error(`unexpected URL ${url}`);
  }});
const stubs = {
  '/static/chat-transport.js': {createTransport: async (s) => { transports.push(s.mode); return {async start() {}, stop() {}}; }},
  '/static/chat-room.js': {el: () => ({}), createRoom: () => ({clear() {}, onMessages() {}, async syncApprovals() {}})},
  '/static/friendly-errors.js': {describeResponse: async (r) => `HTTP ${r.status}`, friendlyError: () => 'failure'},
  '/static/try-model.js': {dailyProgress: () => ({pct: 0, over: false}), money: String,
    parseLocation: () => ({invite: false}), safePayUrl: () => false, stepView: () => []},
  '/static/offline-model.js': {OFFLINE_TOOLTIP: '', offlineView: () => ({visible: mode === 'mock'}),
    sdkRetryDelay: () => null, watchSdk: () => ({status() {}, fail() {}, stop() {}})},
  '/static/offline-notice.js': {updateOfflineNotice() {}},
};
const source = fs.readFileSync(process.argv[1], 'utf8') + '\nexport {state};';
const page = new vm.SourceTextModule(source, {context});
await page.link((name) => new vm.SyntheticModule(Object.keys(stubs[name]), function () {
  for (const [k, v] of Object.entries(stubs[name])) this.setExport(k, v);
}, {context}));
await page.evaluate();
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
await tick();
const sessions = () => requests.filter((u) => u === '/try/session').length;
const out = {initial: sessions()};
mode = 'mock';
now += 1000; await polls[0](); await tick();
out.beforeBackoff = sessions();             // 4 s backoff not over: no new session request
now += 4000; await polls[0](); await tick();
out.secondTry = sessions();                 // retried once, 409 again: next wait 8 s
now += 4000; await polls[0](); await tick();
out.stillWaiting = sessions();
now += 4500; await polls[0](); await tick();
out.thirdTry = sessions();
now += 60000; await polls[0](); await tick();
console.log(JSON.stringify({...out, after: sessions(), transports, session: page.namespace.state.session?.mode ?? null,
  toasts: elements.get('toast')?.textContent ?? null}));
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_try_failed_initial_room_start_is_retried_from_the_state_poll_with_backoff():
    proc = subprocess.run([NODE, "--experimental-vm-modules", "--input-type=module", "-e", TRY_ROOM_RETRY,
                           str(STATIC / "try.js")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert (out["initial"], out["beforeBackoff"], out["secondTry"], out["stillWaiting"], out["thirdTry"]) == (1, 1, 2, 2, 3)
    assert out["after"] == 3 and out["transports"] == ["mock"] and out["session"] == "mock"


# Review R5: the session is issued but the transport fails to start (e.g. a 503 loading the mock room
# history). The half-started room must be dropped so the state poll retries with backoff.
TRY_TRANSPORT_RETRY = TRY_ROOM_RETRY.replace(
    "let mode = 'live', now = 1_000_000, sessionAnswers = [409, 409, 200];",
    "let mode = 'mock', now = 1_000_000, sessionAnswers = [200, 200, 200, 200], startFailures = 1, stops = 0;",
).replace(
    "{createTransport: async (s) => { transports.push(s.mode); return {async start() {}, stop() {}}; }}",
    "{createTransport: async (s) => { transports.push(s.mode); return {async start() {"
    " if (startFailures-- > 0) { const e = Error('history 503'); e.status = 503; throw e; } }, stop() { stops++; }}; }}",
).replace(
    "const out = {initial: sessions()};\nmode = 'mock';",
    "const out = {initial: sessions(), clearedAfterFailure: page.namespace.state.session === null, stops};",
)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_try_failed_transport_start_drops_the_room_and_is_retried():
    proc = subprocess.run([NODE, "--experimental-vm-modules", "--input-type=module", "-e", TRY_TRANSPORT_RETRY,
                           str(STATIC / "try.js")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["initial"] == 1 and out["clearedAfterFailure"] is True and out["stops"] == 1   # failed transport stopped
    # 4 s backoff: no retry at +1 s, retried at +5 s and the room comes up; no further session requests
    assert (out["beforeBackoff"], out["secondTry"], out["stillWaiting"], out["thirdTry"], out["after"]) == (1, 2, 2, 2, 2)
    assert out["transports"] == ["mock", "mock"] and out["session"] == "mock"
