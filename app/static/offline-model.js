// Shared pure view rules for /try and /chat. SDK reports are hints; only the server probes Nexconn.
export const OFFLINE_TOOLTIP = "Nexconn is unreachable, so this room runs on an in-process stand-in. Guardrails and approvals run for real.";
const REASONS = {
  auth: "Why: Nexconn declined this app's credentials (the free chat plan may have ended).",
  timeout: "Why: Nexconn didn't answer in time.",
  server: "Why: Nexconn returned server errors.",
  unknown: "Why: the Nexconn connection failed.",
  manual: "Why: offline mode was switched on for this server.",
};

export function reasonLine(reason, since, localTime = (date) => date.toLocaleString([], {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
})) {
  const line = REASONS[reason] || REASONS.unknown;
  const date = since ? new Date(since) : null;
  return line + (date && Number.isFinite(date.getTime()) ? ` Since ${localTime(date)}.` : "");
}

export function offlineView(data) {
  const f = data?.fallback;
  const visible = (data?.chat_mode || data?.mode) === "mock" && f?.active === true &&
    ["upstream", "auth", "timeout", "server", "unknown", "manual"].includes(f.reason);
  return {visible, reason: reasonLine(f?.detail || f?.reason, f?.since),
          recovered: visible && (f?.upstream || data?.upstream?.state) === "live"};
}

export function sdkFailure(status, elapsedMs = 0) {
  if (status === "connected") return false;
  return elapsedMs >= 15000 || /auth|plan|expired|token|failed|error|not enabled/i.test(status || "");
}

// Injectable timers keep the connection watchdog deterministic in node tests. A disconnected SDK
// gets another 15 seconds; explicit authentication/plan failures are reported immediately. After the first
// successful connection the watchdog stops: a later drop is not treated as Nexconn being down.
export function watchSdk(onFailure, {setTimer = setTimeout, clearTimer = clearTimeout} = {}) {
  let timer = null, stopped = false;
  function fail() {
    if (stopped) return;
    stopped = true; clearTimer(timer); timer = null;
    onFailure();
  }
  function arm() { if (timer === null && !stopped) timer = setTimer(fail, 15000); }
  arm();
  return {
    status(s) {
      if (stopped) return;
      // Once connected, later drops (sleep, flaky Wi-Fi, a second tab) are not upstream failures.
      if (s === "connected") { stopped = true; clearTimer(timer); timer = null; }
      else if (sdkFailure(s)) fail();
      else arm();
    },
    fail,
    stop() { stopped = true; clearTimer(timer); timer = null; },
  };
}
