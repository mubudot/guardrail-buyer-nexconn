// Plain-language error messages for the /try playground (and /chat). Pure: no DOM, no fetch; tested in
// node (tests/test_try_js.py). The server's own `detail` is used only when it is already a sentence meant
// for people (the playground's refusals); anything technical is replaced by a plain sentence.
const PLAIN = /^[A-Z][^{}<>\n]{8,300}[.!?]$/;

// err: {status?, detail?, retryAfter?, offline?, context?} -> one sentence.
// context: "session" for calls that need an existing sandbox (a 404 there means it expired).
export function friendlyError({status, detail, retryAfter, offline, context} = {}) {
  if (offline || status === 0 || status == null) return "You seem offline. Check your connection and try again.";
  const text = typeof detail === "string" ? detail.trim() : "";
  if (status === 429) {
    const s = Number.parseInt(retryAfter, 10);
    return Number.isFinite(s) && s > 0 ? `You're going fast — try again in ${s >= 120 ? `${Math.ceil(s / 60)} min` : `${s} s`}.` : "You're going fast — try again in a moment.";
  }
  if (status === 404 && context === "session") return "Your sandbox expired. Start a new one — it takes one click.";
  if (status === 401 && context === "session") return "This browser's sandbox is no longer known. Start a new one.";
  if (status === 503) return PLAIN.test(text) ? text : "The playground is busy or at capacity right now. Please try again in a few minutes.";
  if (status === 502 || status === 504) return "Nexconn or the network didn't answer — try again.";
  if (status >= 500) return "Something went wrong on our side; nothing was bought or approved. Try again.";
  if (PLAIN.test(text)) return text;
  return `That didn't work (HTTP ${status}). Try again.`;
}

// Read a failed fetch Response into friendlyError's input (async: reads the JSON body if any).
export async function describeResponse(r, context) {
  let detail = "";
  try { const body = await r.json(); detail = typeof body?.detail === "string" ? body.detail : ""; } catch { /* no body */ }
  return friendlyError({status: r.status, detail, retryAfter: r.headers?.get?.("Retry-After"), context});
}
