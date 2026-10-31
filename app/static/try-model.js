// Pure helpers for the /try page (no DOM, no fetch): tested in node (tests/test_try_js.py).

export const STEPS = ["joined", "auto_bought", "approval_requested", "decided", "paid"];   // + optional "invited"
const CODE = /^[0-9a-f]{32}$/;
const ORDER = /^[A-Za-z0-9-]{1,64}$/;

// Where the page was opened: an invite link (/try/join/<code>) and/or a return from the simulated checkout
// (?paid=<order>&status=… or ?cancelled=<order>).
export function parseLocation(pathname, search) {
  const m = /^\/try\/join\/([^/?#]+)\/?$/.exec(pathname || "");
  const code = m && CODE.test(m[1]) ? m[1] : null;
  const q = new URLSearchParams(search || "");
  const paid = ORDER.test(q.get("paid") || "") ? q.get("paid") : null;
  const cancelled = ORDER.test(q.get("cancelled") || "") ? q.get("cancelled") : null;
  return {invite: Boolean(m), code, paid, status: (q.get("status") || "").slice(0, 40), cancelled};
}

// The checklist: which steps are done, and the one to do next (the first open one of the five).
export function stepView(steps) {
  const s = steps && typeof steps === "object" ? steps : {};
  const next = STEPS.find((k) => !s[k]) || null;
  return [...STEPS, "invited"].map((key) => ({key, done: Boolean(s[key]), next: key === next}));
}

// Spent-today bar: percentage of the daily cap (0..100) and whether the cap is exceeded.
export function dailyProgress(limits) {
  const spent = Number.parseFloat(limits?.spent_today), cap = Number.parseFloat(limits?.daily_cap);
  if (!Number.isFinite(spent) || !Number.isFinite(cap) || cap <= 0) return {pct: 0, over: false};
  return {pct: Math.min(100, Math.round((spent / cap) * 100)), over: spent > cap};
}

export function money(amount, currency = "USD") {
  const n = Number.parseFloat(amount);
  if (!Number.isFinite(n)) return "—";
  return currency === "USD" ? `$${n.toFixed(2)}` : `${n.toFixed(2)} ${currency}`;
}

// Same-origin checkout links only: the agent's answer is shown as text, its pay links as buttons.
export function safePayUrl(url) {
  return typeof url === "string" && /^\/try\/s\/[0-9a-f]{12}\/paypal\/mock-checkout\?token=[A-Za-z0-9-]{1,64}$/.test(
    url.replace(/^https?:\/\/[^/]+/, "")) ? url.replace(/^https?:\/\/[^/]+/, "") : null;
}
