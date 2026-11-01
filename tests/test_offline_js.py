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
