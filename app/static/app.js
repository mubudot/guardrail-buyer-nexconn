/* GuardRail Buyer dashboard: AG Grid Community, plain JS, talks to the FastAPI REST API.
   Shared helpers ($, el, api, toast, usd, statusCell, renderKpis, confirmDialog…) live in ui.js. */
const tokenInput = $("token");
tokenInput.value = localStorage.getItem("approverToken") || "";
tokenInput.addEventListener("change", () => localStorage.setItem("approverToken", tokenInput.value));

const time = (p) => p.value ? new Date(p.value).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "";

/* ---------- shared grid options ---------- */
const MONEY_COL = {cellClass: "num ag-right-aligned-cell", headerClass: "num ag-right-aligned-header"};
const empty = (title, hint) =>
  `<div class="empty"><div class="empty-t">${escapeHtml(title)}</div><div class="empty-h">${escapeHtml(hint)}</div></div>`;
const GRID_BASE = {loading: true, tooltipShowDelay: 200, suppressCellFocus: false,
                   overlayLoadingTemplate: '<div class="empty"><span class="spinner" aria-hidden="true"></span>Loading…</div>'};
// Phone-width layout: below 700px the technical columns are hidden (ids, justification and rule
// text, which the confirmation dialog and the audit detail still show), the action column is pinned
// to the right edge so Approve / Pay stay on screen, and a "scroll sideways" cue appears whenever
// the remaining columns are still wider than the grid.
const NARROW = 700;
const narrowLayout = (gridId, cols, action) => (e) => {
  const narrow = e.clientWidth < NARROW;
  e.api.setColumnsVisible(cols, !narrow);
  e.api.applyColumnState({state: [{colId: action, pinned: narrow ? "right" : null}]});
  requestAnimationFrame(() => {
    const vp = $(gridId).querySelector(".ag-center-cols-viewport");
    $(`${gridId}-cue`).hidden = !(narrow && vp && vp.scrollWidth > vp.clientWidth + 2);
  });
};
const LOAD_FAILED = empty("Could not load", "The server did not answer; retrying every 5 seconds.");
// Error overlays are tracked apart from "has data ever loaded": a failed first load shows
// "Could not load" until the next successful load of that same resource restores the normal empty
// state, so an empty table is never shown as a failure, nor a failure as an empty result.
const overlays = {coreError: false, auditError: false, auditLoaded: false};
// AG Grid does not re-render an overlay that is already mounted when its template option changes:
// set the template, then re-show the no-rows overlay so the text on screen actually changes.
function setEmptyOverlay(g, tpl) {
  g.setGridOption("overlayNoRowsTemplate", tpl);
  g.setGridOption("loading", false);
  if (g.getDisplayedRowCount() === 0) { g.hideOverlay(); g.showNoRowsOverlay(); }
}
const AUDIT_EMPTY = empty("No guardrail decisions yet", "Every policy check and order attempt is logged here.");
const APPROVALS_EMPTY = empty("Nothing waiting for you", "Requests over a cap appear here for a human decision.");
const ORDERS_EMPTY = empty("No orders yet", "Ask the agent above: orders within your caps appear here.");
// small grids grow with their rows; past `max` rows they keep a fixed height and scroll
function fitRows(api, elId, n, max) {
  const auto = n <= max;
  api.setGridOption("domLayout", auto ? "autoHeight" : "normal");
  $(elId).classList.toggle("auto", auto);
  $(elId).classList.toggle("has-rows", n > 0);
}

// Tab / Shift+Tab move through the buttons or links inside a cell before the grid moves on
// (AG Grid otherwise jumps straight to the next cell and skips them).
function tabThroughControls(params) {
  const e = params.event;
  if (e.key !== "Tab" || e.type !== "keydown") return false;
  const cell = e.target.closest && e.target.closest(".ag-cell");
  const ctl = cell ? [...cell.querySelectorAll("button:not([disabled]), a[href]")] : [];
  if (!ctl.length) return false;
  const i = ctl.indexOf(document.activeElement);
  // i === -1: the cell itself has focus (entered by Tab or Shift+Tab): go to the first control on
  // Tab, the last one on Shift+Tab; leave the cell after the last (Tab) or first (Shift+Tab) one
  const j = e.shiftKey ? (i === -1 ? ctl.length - 1 : i > 0 ? i - 1 : -2) : (i < ctl.length - 1 ? i + 1 : -2);
  if (j < 0) return false;
  e.preventDefault(); ctl[j].focus(); return true;
}

/* ---------- approvals ---------- */
// Class renderer with refresh(): a 5-second poll keeps the same buttons (and keyboard focus).
class ApprovalButtons {
  init(p) {
    this.p = p; this.gui = document.createElement("span");
    for (const [label, cls, action] of [["Approve", "ok", "approve"], ["Reject", "bad", "reject"]]) {
      const b = document.createElement("button");
      b.type = "button"; b.className = `btn grid-btn ${cls}`; b.textContent = label;
      b.dataset.id = p.data.id; b.dataset.action = action;
      b.onclick = () => reviewApproval(this.p.data, action, b);
      this.gui.appendChild(b);
    }
    this.refresh(p);
  }
  getGui() { return this.gui; }
  refresh(p) {
    this.p = p;
    const d = p.data;
    for (const b of this.gui.children) {
      b.hidden = d.status !== "PENDING";
      b.setAttribute("aria-label", `${b.textContent} ${d.id}: ${d.quantity} × ${d.product_name}, ${usd(d.amount)}`);
    }
    return true;
  }
}
// Approval flow: a confirmation dialog shows what is being approved, which rule it hit and what
// happens next, and asks for the approver token when the server needs one and none is entered.
async function reviewApproval(a, action, btn) {
  const approve = action === "approve";
  // ask for a token when the server requires one, or when /health has not answered yet (fail safe)
  const needsToken = (!health || !!health.approver_auth) && !tokenInput.value;
  const ok = await confirmDialog({
    title: approve ? `Approve ${a.id}?` : `Reject ${a.id}?`,
    intro: approve
      ? "The agent asked for this because it is over an automatic cap."
      : "The agent's request is closed and nothing is ordered.",
    rows: [["Item", `${a.quantity} × ${a.product_name}`], ["Amount", `${usd(a.amount)} ${a.currency || ""}`.trim()],
           ["Merchant", a.merchant], ["Rule hit", (a.reasons || []).join("; ")],
           ["Agent's justification", a.justification]],
    notes: approve ? ["The server re-checks the deny rules and the hard maximum first.",
                      "Then it creates a PayPal order; nothing is charged until the buyer approves it on PayPal."] : [],
    confirmLabel: approve ? "Approve & create PayPal order" : "Reject request",
    tone: approve ? "ok" : "danger", needsToken,
    // the poll may have re-rendered the row: find the same action again, else the approvals heading
    returnFocus: () => [...document.querySelectorAll("#approvals button[data-action]")]
      .find((b) => b.dataset.id === a.id && b.dataset.action === action && !b.hidden) || $("approvals-title")});
  if (ok) await decide(a.id, action, btn);
}
async function decide(id, action, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/approvals/${id}/${action}`, {method: "POST",
      headers: {"X-Approver-Token": tokenInput.value}});
    const o = r.order;
    // the PayPal checkout opens only while the buyer still has to approve, and only for a vetted URL
    const url = o && o.status === "PAYER_ACTION_REQUIRED" ? safeUrl(o.approve_url) : null;
    if (url) toast(`${id} approved → order ${o.id} awaits payer approval on PayPal`, "ok");
    else toast(`${id}: ${r.result}${o ? " · " + o.id : ""}`);
    if (url) window.open(url, "_blank", "noopener");
  } catch (e) { toast(`${action} failed: ${e.message}`, "bad"); btn.disabled = false; }
  refresh();
}
const approvalsGrid = agGrid.createGrid($("approvals"), {
  columnDefs: [
    {field: "id", headerName: "Approval", width: 150, suppressSizeToFit: true, tooltipField: "id"},
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 160},
    {field: "quantity", headerName: "Qty", minWidth: 60, maxWidth: 65},
    {field: "amount", headerName: "Amount", minWidth: 95, maxWidth: 105, valueFormatter: (p) => usd(p.value), ...MONEY_COL},
    {field: "justification", headerName: "Agent's justification", flex: 1.4, minWidth: 180, tooltipField: "justification"},
    {field: "reasons", headerName: "Why it needs you", flex: 1.4, minWidth: 180,
      valueFormatter: (p) => (p.value || []).join("; "), tooltipValueGetter: (p) => (p.value || []).join("\n")},
    {field: "status", minWidth: 112, maxWidth: 118, cellRenderer: statusCell},
    {colId: "decision", headerName: "Decision", minWidth: 200, maxWidth: 205, cellRenderer: ApprovalButtons, sortable: false,
     suppressKeyboardEvent: tabThroughControls},
  ],
  ...GRID_BASE, rowData: [], domLayout: "autoHeight",
  overlayNoRowsTemplate: APPROVALS_EMPTY,
  getRowId: (p) => p.data.id, rowClass: "row-pending", suppressColumnVirtualisation: true,
  onGridSizeChanged: narrowLayout("approvals", ["id", "quantity", "justification", "reasons", "status"], "decision"),
  rowHeight: 48, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true, sortable: true},
});

/* ---------- orders ---------- */
// Pay button while the buyer still has to approve on PayPal, else the capture id (always as text).
class PayCell {
  init(p) { this.gui = document.createElement("span"); this.refresh(p); }
  getGui() { return this.gui; }
  refresh(p) {
    const d = p.data, url = d.status === "PAYER_ACTION_REQUIRED" ? safeUrl(d.approve_url) : null;
    const cur = this.gui.firstElementChild;
    if (url) {
      const a = cur && cur.tagName === "A" ? cur : document.createElement("a");
      a.className = "btn grid-btn paypal"; a.textContent = "Pay with PayPal";
      a.href = url; a.target = "_blank"; a.rel = "noopener";
      a.setAttribute("aria-label", `Pay ${d.id} (${usd(d.amount)}) with PayPal, opens in a new tab`);
      if (a !== cur) this.gui.replaceChildren(a);
    } else {
      this.gui.replaceChildren(document.createTextNode(d.capture_id || ""));
    }
    return true;
  }
}
const ordersGrid = agGrid.createGrid($("orders"), {
  columnDefs: [
    {field: "created_at", headerName: "Time", width: 92, suppressSizeToFit: true, valueFormatter: time, sort: "desc"},
    {field: "id", headerName: "Order", width: 150, suppressSizeToFit: true, tooltipField: "id"},
    // Item wraps instead of truncating (row grows); the full name is also the tooltip
    {field: "product_name", headerName: "Item", flex: 1, minWidth: 120, wrapText: true, autoHeight: true,
      tooltipField: "product_name", cellClass: "wrap-cell"},
    {field: "quantity", headerName: "Qty", minWidth: 60, maxWidth: 65, ...MONEY_COL},
    {field: "amount", headerName: "Amount", minWidth: 95, maxWidth: 105, valueFormatter: (p) => usd(p.value), ...MONEY_COL},
    {colId: "policy", headerName: "Policy", minWidth: 118, maxWidth: 122, valueGetter: (p) => p.data.approval_id ? "APPROVED" : (p.data.decision || {}).verdict,
      cellRenderer: statusCell},
    {field: "status", headerName: "Status", minWidth: 160, maxWidth: 165, cellRenderer: statusCell},
    {field: "paypal_order_id", headerName: "PayPal order", width: 178, suppressSizeToFit: true,
      tooltipField: "paypal_order_id"},
    // wide enough for a 17-character capture id (e.g. 8VA9873790040992M) and the Pay button
    {colId: "payment", headerName: "Payment / capture", width: 182, suppressSizeToFit: true, cellRenderer: PayCell, sortable: false, suppressKeyboardEvent: tabThroughControls,
      tooltipValueGetter: (p) => p.data && p.data.capture_id ? `Capture ${p.data.capture_id}` : null},
  ],
  ...GRID_BASE, rowData: [], domLayout: "autoHeight",
  overlayNoRowsTemplate: ORDERS_EMPTY,
  getRowId: (p) => p.data.id,
  onGridSizeChanged: narrowLayout("orders", ["id", "paypal_order_id", "created_at", "quantity", "policy"], "payment"),
  rowClassRules: {"row-await": (p) => p.data && p.data.status === "PAYER_ACTION_REQUIRED",
                  "row-done": (p) => p.data && p.data.status === "COMPLETED",
                  "row-bad": (p) => p.data && ["FAILED", "CAPTURE_FAILED", "NEEDS_RECONCILIATION"].includes(p.data.status)},
  rowHeight: 48, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true, sortable: true},
});

/* ---------- spend vs caps ---------- */
const spendGrid = agGrid.createGrid($("spend"), {
  columnDefs: [
    {field: "rule", headerName: "Guardrail", flex: 1.6, minWidth: 210, tooltipField: "rule"},
    {field: "limit", headerName: "Limit", flex: 0.7, minWidth: 110, valueFormatter: (p) => p.value == null ? "–" : usd(p.value), ...MONEY_COL},
    {field: "used", headerName: "Used today", flex: 0.7, minWidth: 110, valueFormatter: (p) => p.value == null ? "–" : usd(p.value), ...MONEY_COL},
    {field: "remaining", headerName: "Headroom", flex: 0.7, minWidth: 110, valueFormatter: (p) => p.value == null ? "–" : usd(p.value), ...MONEY_COL},
    {field: "effect", headerName: "Above the limit", flex: 1.2, minWidth: 160, cellRenderer: statusCell},
  ],
  ...GRID_BASE, rowData: [], domLayout: "autoHeight", rowHeight: 42, autoSizeStrategy: {type: "fitGridWidth"},
  defaultColDef: {resizable: true},
});


/* ---------- audit: every guardrail decision (AG Grid Community features only) ---------- */
const STAGE = {check: "policy check", order: "create order", approval_request: "approval request",
               approval_execute: "approval executed"};
const RISK_RULES = {
  "risk-DENY": (p) => p.data && p.data.risk === "DENY",
  "risk-NEEDS_APPROVAL": (p) => p.data && p.data.risk === "NEEDS_APPROVAL",
  "risk-NEEDS_RECONCILIATION": (p) => p.data && p.data.risk === "NEEDS_RECONCILIATION",
};
// Columns flex to the grid's width (UI E2E note: the fixed widths overflowed the card and pushed
// Outcome / Payment status off-screen). The two id columns are hidden by default (both are in the
// detail panel); "show ids" brings them back. Search and CSV always include them.
const ID_COLS = ["request_id", "id"];
const shortStamp = (v) => v ? new Date(v).toLocaleString([], {month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}) : "";
const auditGrid = agGrid.createGrid($("audit"), {
  columnDefs: [
    {field: "created_at", headerName: "Timestamp", flex: 1, minWidth: 135, sort: "desc", filter: "agDateColumnFilter",
      valueFormatter: (p) => shortStamp(p.value), tooltipValueGetter: (p) => p.value ? new Date(p.value).toLocaleString() : null,
      filterParams: {comparator: (day, v) => { const d = new Date(v); d.setHours(0, 0, 0, 0);
        return d < day ? -1 : d > day ? 1 : 0; }}},
    {field: "verdict", headerName: "Decision", flex: 0.8, minWidth: 88, filter: "agTextColumnFilter",
      valueFormatter: (p) => ({ALLOW: "allow", NEEDS_APPROVAL: "approval", DENY: "deny"})[p.value] || p.value,
      cellClassRules: {"risk-DENY": (p) => p.value === "DENY", "risk-NEEDS_APPROVAL": (p) => p.value === "NEEDS_APPROVAL"}},
    {field: "rule_hit", headerName: "Rule hit", flex: 1.2, minWidth: 145, filter: "agTextColumnFilter",
      wrapText: true, autoHeight: true, cellClass: "wrap-cell", tooltipValueGetter:
      (p) => [p.value, ...(p.data.reasons || [])].filter(Boolean).join("\n")},
    {field: "product_name", headerName: "Item", flex: 2, minWidth: 150, filter: "agTextColumnFilter", wrapText: true, autoHeight: true, cellClass: "wrap-cell", 
      valueGetter: (p) => p.data ? `${p.data.quantity} × ${p.data.product_name}` : "", tooltipValueGetter: (p) => p.value},
    {field: "amount", headerName: "Amount", flex: 0.7, minWidth: 88, filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data ? Number(p.data.amount) : null, valueFormatter: (p) => usd(p.value), ...MONEY_COL},
    {field: "merchant", headerName: "Merchant", flex: 1.3, minWidth: 110, filter: "agTextColumnFilter", tooltipField: "merchant",
      wrapText: true, autoHeight: true, cellClass: "wrap-cell"},
    {field: "stage", headerName: "Stage", flex: 1.1, minWidth: 120, filter: "agTextColumnFilter", wrapText: true, autoHeight: true, cellClass: "wrap-cell", 
      valueFormatter: (p) => STAGE[p.value] || p.value, tooltipValueGetter: (p) => STAGE[p.value] || p.value},
    {field: "outcome", headerName: "Outcome", flex: 1.2, minWidth: 200, filter: "agTextColumnFilter", cellRenderer: statusCell,
      tooltipField: "outcome"},
    {field: "order_status", headerName: "Payment status", flex: 1.1, minWidth: 158, filter: "agTextColumnFilter",
      cellRenderer: statusCell, tooltipField: "order_status",
      cellClassRules: {"risk-NEEDS_RECONCILIATION": (p) => p.value === "NEEDS_RECONCILIATION"}},
    {field: "request_id", headerName: "Request id", flex: 1, minWidth: 135, filter: "agTextColumnFilter", hide: true},
    {field: "id", headerName: "Decision id", flex: 1, minWidth: 135, filter: "agTextColumnFilter", hide: true},
  ],
  includeHiddenColumnsInQuickFilter: true,
  rowData: [], getRowId: (p) => p.data.id, rowClassRules: RISK_RULES,
  defaultColDef: {resizable: true, sortable: true, floatingFilter: true},
  rowSelection: {mode: "singleRow", checkboxes: false, enableClickSelection: true},
  ...GRID_BASE, rowHeight: 40,
  overlayNoRowsTemplate: AUDIT_EMPTY,
  isExternalFilterPresent: () => $("audit-hide-checks").checked,
  doesExternalFilterPass: (node) => node.data.stage !== "check",
  onRowClicked: (e) => showDecision(e.data.id),
  onCellKeyDown: (e) => { if (e.event && e.event.key === "Enter" && e.data) showDecision(e.data.id); },
});
$("audit-search").addEventListener("input", (e) => auditGrid.setGridOption("quickFilterText", e.target.value));
$("audit-hide-checks").addEventListener("change", () => auditGrid.onFilterChanged());
$("audit-show-ids").addEventListener("change", (e) => auditGrid.setColumnsVisible(ID_COLS, e.target.checked));
// CSV injection (CODEX_REVIEW_4 N4): quoting does not stop a spreadsheet from evaluating a
// cell that starts with = + - @ TAB or CR. Prefix such text with an apostrophe so it is literal.
const FORMULA_START = /^[=+\-@\t\r]/;
function csvSafe(v) {
  if (v == null) return v;
  const s = String(v);
  return FORMULA_START.test(s) ? "'" + s : s;
}
function csvCell(p) {
  const v = p.column.getColId() === "created_at" ? p.value : (p.formatValue ? p.formatValue(p.value) : p.value);
  return typeof v === "number" ? v : csvSafe(v);
}
$("audit-csv").addEventListener("click", () => auditGrid.exportDataAsCsv({
  fileName: `guardrail-audit-${new Date().toISOString().slice(0, 10)}.csv`,
  allColumns: true,  // the id columns are exported even while hidden
  processCellCallback: csvCell,
  processHeaderCallback: (p) => csvSafe(p.column.getColDef().headerName || p.column.getColId()),
}));
$("detail-close").addEventListener("click", () => {
  detail.gen++; detail.id = null;            // drop any response still in flight
  $("audit-detail").hidden = true; auditGrid.deselectAll();
});

const kv = (rows) => el("table", {}, ...rows.filter((r) => r[1] != null && r[1] !== "")
  .map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, v))));

// Detail identity + freshness (CODEX_REVIEW_4 N3): every request carries the selection
// generation it was made for; a response for an older selection (or after Close) is dropped.
// The open detail is re-fetched on every Audit poll and labelled with its snapshot time.
const detail = {gen: 0, id: null};
async function showDecision(id, {quiet = false} = {}) {
  const box = $("audit-detail"), body = $("detail-body");
  const gen = ++detail.gen; detail.id = id;
  const wasHidden = box.hidden;
  box.hidden = false; $("detail-title").textContent = id;
  // on narrower screens the panel sits below the grid: bring it into view when it opens
  if (wasHidden && !quiet && box.scrollIntoView && getComputedStyle($("audit-wrap")).flexDirection === "column")
    box.scrollIntoView({block: "nearest", behavior: "smooth"});
  if (!quiet) { body.replaceChildren(el("div", {class: "empty"}, el("span", {class: "spinner", "aria-hidden": "true"}), "Loading…")); $("detail-asof").textContent = ""; }
  let d;
  try { d = await api(`/decisions/${encodeURIComponent(id)}`); }
  catch (e) {
    if (gen !== detail.gen) return;
    if (quiet) { $("detail-asof").textContent = `Refresh failed (${e.message}); showing the earlier snapshot`; return; }
    body.replaceChildren(el("div", {class: "empty err"}, "Could not load this decision: " + e.message)); return;
  }
  if (gen !== detail.gen || detail.id !== id) return;  // a newer selection / refresh owns the panel
  renderDecision(d);
}
function renderDecision(d) {
  const body = $("detail-body");
  const dec = d.decision, q = d.quote;
  $("detail-asof").textContent = `Snapshot ${new Date(d.as_of || Date.now()).toLocaleTimeString()} · refreshes while the Audit tab is open`;
  body.textContent = "";
  body.append(el("h4", {}, "Decision"), kv([
    ["Verdict", dec.verdict], ["Stage", STAGE[dec.stage] || dec.stage], ["Outcome", dec.outcome],
    ["Rule hit", dec.rule_hit], ["When", new Date(dec.created_at).toLocaleString()],
    ["Request id", dec.request_id], ["Spent before", usd(dec.spent_before)],
    ["Reasons", (dec.reasons || []).join("; ")]]));
  body.append(el("h4", {}, "Quote"), kv([
    ["Product", `${q.product_id} · ${dec.product_name}`], ["Quantity", q.quantity],
    ["Amount", `${usd(q.amount)} ${q.currency}`], ["Merchant", q.merchant], ["Category", q.category]]));
  body.append(el("h4", {}, "Rules evaluated"), el("table", {}, ...(d.rules || []).map((r) =>
    el("tr", {}, el("td", {}, r.rule), el("td", {}, el("span", {class: `rule-${r.outcome}`}, r.outcome), " ", el("span", {class: "note"}, r.detail))))));
  if (d.approval) body.append(el("h4", {}, "Approval"), kv([
    ["Approval", d.approval.id], ["Status", d.approval.status], ["Justification", d.approval.justification],
    ["Decided", d.approval.decided_at ? new Date(d.approval.decided_at).toLocaleString() : "pending"]]));
  if ((d.related_total || 0) > 1) body.append(el("p", {class: "note"},
    `${d.related_total} decisions for this request/product` +
    (d.related_total > (d.related_decisions || []).length ? ` (first ${d.related_decisions.length} returned)` : "")));
  body.append(el("h4", {}, "Payment timeline"));
  if (!d.order) body.append(el("p", {class: "note"}, "No order was created by this decision."));
  else {
    body.append(kv([["Order", d.order.id], ["Current status", d.order.status],
                    ["PayPal order", d.order.paypal_order_id], ["Capture", d.order.capture_id]]));
    body.append(el("ol", {}, ...d.timeline.map((t) => el("li", {},
      el("span", {class: `st ${t.to_status} tone-${toneOf(t.to_status)}`}, (t.from_status ? `${t.from_status} → ` : "") + t.to_status),
      el("div", {class: "note"}, `${t.at ? new Date(t.at).toLocaleString() : ""}${t.note ? " · " + t.note : ""}`)))));
  }
}

/* ---------- audit paging (CODEX_REVIEW_4 N5) ---------- */
// The newest page is re-polled; "Load older" follows the API's keyset cursor (before_id).
// Rows already loaded are kept and merged by id, so filters/search/CSV cover everything
// loaded, and the label always says how much of the stored history that is.
const audit = {rows: new Map(), total: 0, oldestCursor: null, hasMore: false};
const AUDIT_PAGE = 1000;
function auditApply(page, older) {
  for (const r of page.items) audit.rows.set(r.id, r);
  audit.total = page.total;
  if (older || audit.oldestCursor === null) { audit.hasMore = page.has_more; audit.oldestCursor = page.next_before_id; }
  const rows = [...audit.rows.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0));
  auditGrid.setGridOption("rowData", rows);  // getRowId -> delta update, selection kept
  auditGrid.setGridOption("loading", false);
  const n = rows.length;
  $("audit-count").textContent = `(${audit.total})`;
  $("audit-window").textContent = n >= audit.total
    ? `Showing all ${audit.total} decisions`
    : `Showing newest ${n} of ${audit.total} decisions · search, filters and CSV cover loaded rows only`;
  $("audit-older").hidden = !(audit.hasMore && n < audit.total);
}
async function loadOlderDecisions() {
  if (!audit.oldestCursor) return;
  const btn = $("audit-older"); btn.disabled = true;
  try {
    const page = await api(`/decisions?limit=${AUDIT_PAGE}&before_id=${encodeURIComponent(audit.oldestCursor)}`);
    auditApply(page, true);
  } catch (e) { toast("load older failed: " + e.message); }
  btn.disabled = false;
}
$("audit-older").addEventListener("click", loadOlderDecisions);
const auditVisible = () => !$("view-audit").hidden && !document.hidden;

/* ---------- tabs ---------- */
const TABS = [...document.querySelectorAll(".tab")];
for (const tab of TABS) {
  tab.addEventListener("click", () => {
    for (const t of TABS) {
      const on = t === tab; t.classList.toggle("active", on); t.setAttribute("aria-selected", on);
      t.tabIndex = on ? 0 : -1;
      $(t.dataset.view).hidden = !on;
    }
    if (tab.id === "tab-audit") refreshAudit();
  });
  tab.addEventListener("keydown", (e) => {  // arrow keys move between tabs (WAI-ARIA tabs pattern)
    const i = TABS.indexOf(tab), n = TABS.length;
    const j = {ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1}[e.key];
    if (j === undefined) return;
    e.preventDefault(); TABS[j].focus(); TABS[j].click();
  });
}

/* ---------- header mode pill ---------- */
// "approver token on" next to an empty token box read as a contradiction (UI E2E note). Say what
// the server requires and whether this browser has a token entered.
let health = null;
function approverText(h) {
  if (h.approver_auth) return tokenInput.value
    ? ["token entered · approvals enabled", "The server requires an approver token to approve, reject or cancel. A token is entered in this browser; the server checks it on each action."]
    : ["enter the approver token to approve", "The server requires an approver token to approve, reject or cancel. Type it in the Approver token box to act on approvals."];
  if (h.approver_mode === "disabled")
    return ["approvals disabled (no APPROVER_TOKEN)", "The server has no APPROVER_TOKEN set, so nobody can approve, reject or cancel."];
  return ["approvals OPEN (DEMO_MODE)", "DEMO_MODE with an empty APPROVER_TOKEN: anyone can approve, reject or cancel."];
}
const PAYPAL_ENV = {sandbox: "PayPal Sandbox", mock: "PayPal mock (local)", live: "PayPal LIVE"};
function renderMode() {
  if (!health) return;
  const [txt, tip] = approverText(health);
  const m = $("mode");
  m.textContent = PAYPAL_ENV[health.paypal] || `PayPal: ${health.paypal}`;
  m.className = `chip env-${health.paypal}`;
  m.title = health.paypal === "sandbox" ? "Real PayPal Sandbox API (api-m.sandbox.paypal.com): test money only"
    : health.paypal === "mock" ? "Local mock PayPal: no PayPal API calls" : "PayPal environment";
  const ag = $("agent-mode");
  ag.textContent = health.agent_mode === "offline" ? "OFFLINE rule-based agent (no AI)" : `Agent: ${health.model}`;
  ag.className = `chip${health.agent_mode === "offline" ? " offline" : ""}`; ag.hidden = false;
  ag.title = health.agent_mode === "offline" ? "AGENT_MODE=offline: a deterministic parser stands in for the LLM" : "LLM tool-use agent";
  const ap = $("approver-mode");
  ap.textContent = txt; ap.title = tip; ap.hidden = false;
  ap.classList.toggle("warn", health.approver_mode !== "token");
  const chat = $("chat-link");
  if (chat) chat.hidden = !health.nexconn || health.nexconn.mode === "off";
}
tokenInput.addEventListener("input", renderMode);

let auditRefreshing = null;
function refreshAudit() {  // newest audit page + the open detail; never overlapping
  if (auditRefreshing) return auditRefreshing;
  auditRefreshing = (async () => {
    try {
      const page = await api(`/decisions?limit=${AUDIT_PAGE}`);
      overlays.auditLoaded = true;
      auditApply(page, false);
      if (overlays.auditError) { overlays.auditError = false; setEmptyOverlay(auditGrid, AUDIT_EMPTY); }
      if (detail.id && !$("audit-detail").hidden) await showDecision(detail.id, {quiet: true});
      conn.ok("audit");
    } catch (e) {
      conn.fail("audit", e);
      if (!overlays.auditLoaded) {  // never loaded: say so instead of "No guardrail decisions yet"
        overlays.auditError = true;
        setEmptyOverlay(auditGrid, LOAD_FAILED);
      }
      auditGrid.setGridOption("loading", false);
    }
  })().finally(() => { auditRefreshing = null; });
  return auditRefreshing;
}

let refreshing = null, refreshAgain = false;
function refresh() {  // never overlapping: a request made while one runs schedules exactly one more
  if (refreshing) { refreshAgain = true; return refreshing; }
  refreshing = doRefresh().finally(() => {
    refreshing = null;
    if (refreshAgain) { refreshAgain = false; refresh(); }
  });
  return refreshing;
}
async function loadHealth() {  // header chips; retried by every poll until it answers once
  const h = await api("/health").catch((e) => { conn.fail("health", e); return null; });
  if (h) { health = h; renderMode(); conn.ok("health"); }
  return h;
}
async function doRefresh() {
  try {
    if (!health) loadHealth();
    if (auditVisible()) refreshAudit();  // the audit log is polled only while its tab is shown
    const [orders, approvals, spend] = await Promise.all([api("/orders"), api("/approvals"), api("/spend")]);
    conn.ok("core");
    const pendingRows = approvals.filter((a) => a.status === "PENDING");
    ordersGrid.setGridOption("rowData", orders);
    approvalsGrid.setGridOption("rowData", pendingRows);
    fitRows(ordersGrid, "orders", orders.length, 8);
    fitRows(approvalsGrid, "approvals", pendingRows.length, 6);
    for (const g of [ordersGrid, approvalsGrid, spendGrid]) g.setGridOption("loading", false);
    if (overlays.coreError) {  // recovered from a failed first load: back to the normal empty states
      overlays.coreError = false;
      setEmptyOverlay(ordersGrid, ORDERS_EMPTY);
      setEmptyOverlay(approvalsGrid, APPROVALS_EMPTY);
    }
    const badge = $("approvals-count");
    badge.textContent = pendingRows.length; badge.hidden = !pendingRows.length;
    spendGrid.setGridOption("rowData", [
      {rule: "Daily cap (captured + reserved)", limit: spend.daily_cap, used: spend.total, remaining: spend.remaining, effect: "NEEDS_APPROVAL"},
      {rule: "Per-order cap", limit: spend.per_order_cap, used: null, remaining: null, effect: "NEEDS_APPROVAL"},
      {rule: "Hard max per order", limit: spend.hard_max_per_order, used: null, remaining: null, effect: "DENY"},
      {rule: "Merchant / category allowlist", limit: null, used: null, remaining: null, effect: "DENY"},
    ]);
    $("spend-day").textContent = `Day ${spend.day} (policy timezone) · ${spend.currency}`;
    renderKpis(spend, pendingRows.length);
  } catch (e) {
    conn.fail("core", e);
    if (!conn.loadedOnce) {  // nothing loaded yet: show an error instead of an endless "Loading…"
      overlays.coreError = true;
      for (const g of [ordersGrid, approvalsGrid, spendGrid]) setEmptyOverlay(g, LOAD_FAILED);
    }
  }
}

let asking = false;  // one agent request at a time (Ctrl+Enter could otherwise submit twice)
$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("ask").value.trim(); if (!text || asking) return;
  asking = true;
  const btn = $("ask-btn"); btn.disabled = true; btn.textContent = "Working…";
  const ans = $("answer"); ans.hidden = false; ans.className = "answer busy";
  ans.replaceChildren(el("span", {class: "spinner", "aria-hidden": "true"}), "The agent is searching the catalog, checking your policy and placing orders…");
  try {
    const r = await api("/agent/request", {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({request: text})});
    ans.className = "answer";
    ans.replaceChildren(...renderAgentAnswer(r));
  } catch (e) { ans.className = "answer err"; ans.replaceChildren(el("strong", {}, "Agent error: "), e.message); }
  finally { asking = false; btn.disabled = false; btn.textContent = "Send"; }
  refresh();
});
for (const b of document.querySelectorAll("#examples [data-ask]"))
  b.addEventListener("click", () => { $("ask").value = b.dataset.ask; $("ask").focus(); });
$("ask").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!asking) $("ask-form").requestSubmit(); }
});

(async () => {
  await loadHealth();
  const q = new URLSearchParams(location.search);
  if (q.get("paid")) toast(q.get("status") === "PAYER_ACTION_REQUIRED"
    ? `${q.get("paid")}: PayPal says the buyer has not approved yet, nothing captured`
    : `Payment returned from PayPal: ${q.get("paid")} → ${q.get("status")}`, q.get("status") === "COMPLETED" ? "ok" : "");
  if (q.get("cancelled")) toast(`Buyer cancelled ${q.get("cancelled")}: reserved budget released`);
  if (q.get("paid") || q.get("cancelled")) history.replaceState(null, "", "/");
  refresh();
  setInterval(refresh, 5000);
  document.addEventListener("visibilitychange", () => { if (auditVisible()) refreshAudit(); });
})();
