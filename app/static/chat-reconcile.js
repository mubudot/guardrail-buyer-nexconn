// Reconcile the chat page with the server's approval list (GET /approvals) when an approver joins.
// The page only loads the latest 50 room messages, so a card can be in view while its outcome message is
// further back (or the card can be older than the history). The server's status is authoritative:
//  - a rendered card whose approval is no longer PENDING (and whose outcome is not in view) is closed;
//  - a PENDING approval without a rendered card is listed above the room so it stays reachable;
//  - every decided approval is returned in `decided`, so a card copy delivered later (a retried delivery)
//    can be rendered closed even if no copy of it was in view at join time.
// Pure functions, no DOM: tested in plain node (tests/test_nexconn_js.py).
const APR = /^APR-[0-9A-F]{8}$/;

export function planReconcile(approvals, renderedIds, resolvedIds) {
  const older = [], close = [], decided = [];
  if (!Array.isArray(approvals)) return {older, close, decided};
  const rendered = new Set(renderedIds), resolved = new Set(resolvedIds);
  for (const a of approvals) {
    const id = a && typeof a.id === "string" ? a.id : "";
    const status = a && typeof a.status === "string" ? a.status : "";
    if (!APR.test(id) || !status || resolved.has(id)) continue;
    if (status === "PENDING") { if (!rendered.has(id)) older.push(a); }
    else { decided.push({id, status}); if (rendered.has(id)) close.push({id, status}); }
  }
  return {older, close, decided};
}

const LABELS = {
  APPROVED: ["Closed: approved", "tone-ok"],
  EXECUTING: ["Approved: placing the order", "tone-ok"],
  REJECTED: ["Closed: rejected", "tone-bad"],
  DENIED_BY_POLICY: ["Closed: refused by a hard rule", "tone-bad"],
  QUOTE_CHANGED: ["Closed: price changed", "tone-bad"],
};

export function closedLabel(status) {
  const [text, tone] = LABELS[status] || [`Closed: ${String(status).toLowerCase().replace(/_/g, " ")}`, "tone-bad"];
  return {text, tone};
}
