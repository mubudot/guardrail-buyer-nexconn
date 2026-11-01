# GuardRail Buyer

> An AI purchasing agent that pays with PayPal — but only inside the spend guardrails you set.

> **Nexconn Build Challenge 2026 edition — approve AI purchases in chat.** When the agent hits a spend cap,
> an approval card lands in a Nexconn group chat; an approver taps Approve / Reject in the
> [approval chat](#approve-in-chat-nexconn) (Nexconn Web Chat SDK) and the decision comes back through
> Nexconn's signed webhook into the same guarded code path.
> **Live demo:** https://guardrail-buyer-204-44-110-122.sslip.io/chat (approval room) ·
> https://guardrail-buyer-204-44-110-122.sslip.io/ (dashboard) — Nexconn live, PayPal Sandbox, offline agent.
> **Demo video:** https://youtu.be/XQdg7dW62xc (2:30) ·
> **What existed before vs. what was built for the challenge:** [below](#prior-work-and-challenge-work).
>
> **Try it yourself — no token needed:** https://guardrail-buyer-204-44-110-122.sslip.io/try — one click gives
> you a private sandbox: your own Nexconn user and approval room, your own limits and data, a rule-based
> agent and a simulated PayPal checkout. Ask for something under the limit (bought right away), over it
> (an approval card appears in your room: approve or reject it there), or a gift card (always refused),
> then pay on the simulated checkout and invite a teammate as a second approver. No sign-up, no e-mail,
> no real money; after 24 h of inactivity a sandbox closes and its orders and approvals are deleted (display names stay in the server's sandbox list until an operator purges them; chat messages stay on Nexconn). [How it works](#try-it-no-token-needed).

*The base app was built for the PayPal AI Hackathon (2026) (demo video for that version: https://youtu.be/6pp8X-yFJwk). Runs with no network at runtime (mock PayPal, rule-based offline agent, vendored AG Grid), or against the real PayPal Sandbox with Claude. Installing the Python dependencies needs a package index.*

![dashboard](docs/dashboard.png)

**For judges, in 3 minutes:** clone, then paste the [offline block](#no-keys-no-network-copy-paste-offline-block)
(no AI key, no PayPal account): it runs the test suite, starts the dashboard and sends one mixed request →
an order to pay, an approval to grant, a refusal. Open the printed URL, enter approver token `demo`, click
**Pay with PayPal** → **Pay now** and **Approve**. With your own keys the same flow runs on Claude and the
real PayPal Sandbox ([below](#against-the-real-paypal-sandbox)); a real Sandbox buyer approval → capture is
documented with ids and screenshots in [`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md).

**Approve in chat (Nexconn):** approval requests can go to a Nexconn group chat, where an approver taps
Approve / Reject and the guardrail state updates ([Approve in chat](#approve-in-chat-nexconn); off by default,
offline mock included).

**Bring your own agent:** the same guardrailed tools are an MCP server, so Claude Desktop, Codex or any
MCP client can shop only inside the policy ([Use it from any MCP agent](#use-it-from-any-mcp-agent-claude-desktop--codex)).

Letting an AI agent spend money is risky. GuardRail Buyer puts a deterministic
policy engine between the LLM and the wallet:

| Rule | Effect |
|---|---|
| Per-order cap (default $50) | above it → **NEEDS_APPROVAL** (a human must approve) |
| Daily cap (default $150, per policy timezone) | today's spend + this order above it → **NEEDS_APPROVAL** |
| Hard maximum per order (default $500) | above it → **DENY** |
| Merchant allowlist / blocklist | not allowed → **DENY** |
| Category allowlist | not allowed → **DENY** |
| Currency must match policy | otherwise → **DENY** |

The agent (Claude, via tool use) can search the catalog, dry-run the policy,
place an order, or ask for human approval. **It never calls PayPal directly** —
every order goes through `BuyerService`, which re-evaluates the policy on the
server. A DENY can never be escalated to approval, and approving a request
re-checks every deny rule (including the split-order hard maximum) in case the policy or the
request's other purchases changed; a human approval waives only the soft caps. What exactly is enforced, and what is not,
is listed under [Known limitations](#known-limitations).

## Architecture

```
natural-language request                     any MCP client (Claude Desktop, Codex, …)
        │                                              │ stdio JSON-RPC
        ▼                                              ▼
 LLM agent (tool use)                         MCP server (no credentials)       app/mcp_server.py
   app/agent.py + app/llm/ (Anthropic |                │ POST /tools/{name}
     OpenAI-compatible provider)                       │
   tools: search_catalog · check_policy ·              ▼
          create_order · request_approval      GuardedTools: strict schemas      app/tool_gateway.py
        │                                              │
        ▼                                              ▼
 BuyerService ── guardrail engine (pure) ── SpendLedger (SQLite)   app/service.py, app/guardrails.py,
                                                                   app/store.py, app/catalog.py,
                                                                   app/reporting.py (audit/spend views),
                                                                   app/policy_admin.py (owner policy edits)
        │
        ▼
 PayPalClient: MockPayPal | SandboxPayPal (Orders v2)              app/paypal_client.py
        │   create order → buyer approves on PayPal → /paypal/return → capture
        ▼
 FastAPI: REST API + AG Grid dashboard at /                        app/main.py, app/static/
```

### How AG Grid is used (Community 32.3.3, vendored, no CDN)

The dashboard (`app/static/app.js`, `app/static/policy.js`) is AG Grid grids that a finance lead works in, not just tables:

| Grid | What it does with AG Grid |
|---|---|
| Pending approvals | `cellRenderer` **Approve / Reject** buttons calling the approver-token API; the agent's justification and the rules hit, with full text in tooltips (`tooltipField`, `tooltipValueGetter`); `getRowId` |
| Orders | **Pay with PayPal** link or capture id per order (`cellRenderer`), PayPal order id, status badges; item names wrap (`wrapText` + `autoHeight`); `getRowId` so polling updates rows in place instead of redrawing |
| Spend vs caps | one row per guardrail with its limit and what happens above it; today's use and headroom on the daily-cap row (`valueFormatter` for money, `autoSizeStrategy: fitGridWidth`) |
| Audit | every guardrail decision: `floatingFilter` on every column, a quick-search box (`quickFilterText`), risk colouring (`cellClassRules` / `rowClassRules`: deny red, approval amber, unconfirmed payment orange), **Export CSV** (`exportDataAsCsv` with a `processCellCallback` that neutralises spreadsheet formulas), row click → detail panel with the quote, every rule evaluated and the payment timeline, keyset paging for older rows |
| Policy editor (**Policy & spend** tab) | three editable grids: caps with `agTextCellEditor` + `valueSetter` validation, merchants (allowed / blocked / not listed) and categories (allowed / not allowed) with `agSelectCellEditor`; unsaved cells highlighted (`cellClassRules`), **Save policy** sends only the changed fields to `PUT /policy` with the owner token and the server validates again; a change-history grid (`wrapText` + `autoHeight`) |
| Spend over time (**Policy & spend** tab) | **AG Charts Community 10.3.3** (vendored, loaded only when the tab is opened): captured vs reserved per day, stacked bars, with the daily cap as a dashed line; 7/14/30/90 days from `GET /spend/history` |

Everything is the free **Community** edition (MIT); no Enterprise module or licence key is used.

### Payment flow and spend accounting

1. **Check + reserve atomically.** The policy check and the budget reservation run in one SQLite write
   transaction (`BEGIN IMMEDIATE`) *before* PayPal is called, so concurrent requests (threads, or several
   processes sharing the DB file) cannot all pass against the same spend.
2. A policy-ALLOWed (or human-approved) order is then created with PayPal Orders v2 (`intent: CAPTURE`,
   `PayPal-Request-Id` = our order id, so a retry returns the same PayPal order). PayPal returns
   `PAYER_ACTION_REQUIRED` and a `payer-action` link, shown as **Pay with PayPal** in the dashboard.
3. The buyer approves on PayPal and is redirected to `GET /paypal/return?token=<paypal order id>`. The order
   is atomically moved to `CAPTURING`, captured, and redirected to the dashboard (a refresh or a parallel
   return never captures twice).
4. `GET /paypal/cancel?token=…&state=…` cancels **only** an order still awaiting the buyer
   (`PAYER_ACTION_REQUIRED`), atomically. `state` is an HMAC of our order id with a server secret (stored
   in the DB, or `STATE_SECRET`), generated when the order is created and embedded in that order's PayPal
   `cancel_url`; a request without it, or with a forged/other order's state, gets `403`. Orders being
   captured, captured, or of unknown outcome are left untouched.
5. If PayPal's answer is ambiguous (HTTP 5xx, timeout/transport error, capture status `PENDING` or missing),
   the order becomes `NEEDS_RECONCILIATION` and keeps its reservation. After a capture call that failed
   ambiguously (5xx, timeout, or `ORDER_ALREADY_CAPTURED` that could not be read back) the app asks PayPal
   for the order status once right away; an unknown create outcome or a capture reported `PENDING` waits
   for `POST /orders/{id}/reconcile`. Only a confirmed `COMPLETED` capture is recorded as `COMPLETED`. A
   reconcile that replays an unknown create and is refused for a reason that says nothing about the first
   attempt (401/403/404/409/429 …, or a failed OAuth token request) keeps the order reserved; only a
   `400`/`422` from the Orders API releases it.
6. **Recovery is monotonic and exclusive.** Every status change bumps a persisted per-order generation
   (`op_gen`). A reconcile/capture result is applied only if the generation it started from is unchanged;
   a delayed, superseded provider read is rejected (`result: SUPERSEDED`). The one result accepted
   regardless is a confirmed `COMPLETED` capture (it is final). Once a capture outcome was ambiguous
   (`capture_unknown=1`), the order never returns to `PAYER_ACTION_REQUIRED` and cannot be cancelled or
   expired; it stays reserved until PayPal reports it `COMPLETED`, declined or `VOIDED`. A create replay
   that returns an already `APPROVED`/`COMPLETED` PayPal order is captured/verified, never shown as
   awaiting the buyer.

| Order status | Counts toward the daily cap? |
|---|---|
| `CREATING` (budget reserved, PayPal create in flight) | **yes, reserved** |
| `PAYER_ACTION_REQUIRED` (created, awaiting buyer) | **yes, reserved**, so an agent cannot open many unpaid orders to dodge the cap |
| `CAPTURING` (capture in flight; cannot be cancelled or expired) | **yes, reserved** |
| `NEEDS_RECONCILIATION` (payment outcome unknown) | **yes, reserved** until PayPal confirms either way |
| `COMPLETED` (capture confirmed `COMPLETED`) | yes, captured |
| `CANCELLED` / `EXPIRED` / `CAPTURE_FAILED` / `FAILED` / `MOCK_RESET` | no, reservation released |

Approvals: a human approval is **claimed once** (`PENDING → EXECUTING → APPROVED`) in the same transaction
that creates its single order; repeating the approve call returns the existing order. An approval is
**bound to its quote** (product, quantity, amount, currency, merchant, category): if the catalog changed in
between, approving returns `409 QUOTE_CHANGED`, nothing is bought, and a fresh approval is needed.
Executing an approval re-checks the hard rules inside the same transaction, including the split-order
hard maximum over what the same agent request has already ordered (so two queued $329 monitor approvals
from one request cannot both execute against the $500 hard maximum); the per-order and daily caps are
the only rules a human approval waives.

Split orders: within one agent request (`request_id`, one per `/agent/request` run), orders for the
**same SKU from the same merchant** count together toward the per-order cap and the hard maximum. Three
separate one-mouse orders ($24.50 each) in one request → the third needs approval, exactly like one
three-mouse order ($73.50).

Unapproved orders expire after `PAYPAL_APPROVAL_TTL_MIN` (default 180 min); expiry is checked
lazily on reads and on return. A reservation stays on the day it was made (policy timezone).
`GET /spend` reports `captured`, `reserved`, `total`, `remaining` and `needs_reconciliation` against the caps.

## Quick start

Requires Python 3.11+ (tested with 3.12) and git. `requirements.txt` pins the exact tested versions.

### No keys, no network: copy-paste offline block

Runs the tests, the server and the CLI with the rule-based offline agent and the mock PayPal. Only the
`pip install` line needs network (a package index). Paste into a **fresh** bash shell at the repository root
(the exports below override `.env` for the rest of that shell; see *Switching modes*):

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                         # the only step that needs network

# --- everything below: no API key, no PayPal account, no network ---
export AGENT_MODE=offline          # rule-based parser instead of Claude (applies to server AND CLI)
export PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1          # mock PayPal with a local mock checkout page
export APPROVER_TOKEN=demo         # token for approve / reject / cancel
export DEMO_MODE=1                 # explicit local-demo flag (only an empty APPROVER_TOKEN depends on it)
export GRB_NO_DOTENV=1             # ignore any .env file, so nothing switches back to Claude/Sandbox
export DB_PATH="$(mktemp -d)/guardrail-demo.db"         # throwaway database for this shell
export PORT=8000                   # any free port; if it is busy, pick another one ...
export PUBLIC_BASE_URL="http://127.0.0.1:$PORT"         # ... and keep this in sync (mock checkout + PayPal links)
export GRB_INSTANCE="demo-$$-$RANDOM"                   # lets the ready check recognise THIS server

python -m pytest -q                                     # -> 586 passed, 3 skipped (two jsdom UI tests, optional MCP SDK interop; without node: 575 passed, 14 skipped)

uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "${DB_PATH%/*}/server.log" 2>&1 &
SERVER_PID=$!
READY=0
for i in $(seq 1 40); do                                # wait up to ~20 s for /health of THIS instance
  kill -0 "$SERVER_PID" 2>/dev/null || break            # server exited: port busy, or mock DB locked
  curl -sf "$PUBLIC_BASE_URL/health" | grep -q "\"instance\":\"$GRB_INSTANCE\"" && { READY=1; break; }
  sleep 0.5
done
if [ "$READY" = 1 ]; then echo "server ready: $PUBLIC_BASE_URL/ (pid $SERVER_PID)"; else
  echo "SERVER NOT READY - see ${DB_PATH%/*}/server.log (port $PORT busy? export another PORT and the"
  echo "matching PUBLIC_BASE_URL, then rerun from the uvicorn line)"; kill "$SERVER_PID" 2>/dev/null; fi

if [ "$READY" = 1 ]; then
  curl -s -X POST "$PUBLIC_BASE_URL/agent/request" -H 'content-type: application/json' \
    -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}' \
    | python -c 'import json,sys; print(json.load(sys.stdin)["final_text"])'
  curl -s "$PUBLIC_BASE_URL/spend"; echo               # cables reserved: total 25.98
fi
python -m app.cli "3 bags of coffee beans and 2 snack boxes"   # CLI: own in-memory DB each run
```

Then open the printed URL (`$PUBLIC_BASE_URL/`), enter `demo` as approver token, click **Pay with PayPal**
on the cables order (local mock checkout → **Pay now**), then **Approve** the keyboard and confirm in the dialog
(**Approve & create PayPal order**).

**Reset / rerun:** `kill $SERVER_PID` (stop the server) and `rm -f "$DB_PATH"*` (or start a new shell and paste
the `export` lines again, which picks a new temp DB). The ready check fails, instead of silently talking to
another program, when the port is already taken: it only accepts a `/health` answer carrying this shell's
`GRB_INSTANCE`.

**Mock mode is single-process.** The mock PayPal keeps its orders **in memory**, so only one server process
may use a database in mock mode: on startup it takes an exclusive lock file next to the **resolved** DB file
(`realpath($DB_PATH).mock.lock`, so a symlink, relative or `..` alias of the same file contends for the same
lock; a DB file with several hard links is refused in mock mode) and exits with a clear error if another
process holds it (do not use `uvicorn --workers N` with the mock). Only after taking the lock does it treat open mock orders from a
previous run as lost: they are marked `MOCK_RESET` and their reservations released (completed orders stay
as history). The CLI takes the same lock when `CLI_DB_PATH` points at a file (it refuses, exit code 3, while
a mock server holds it). Use a fresh `DB_PATH` for a clean demo. Sandbox mode has no such restriction.

**Database upgrades.** The schema is versioned (`PRAGMA user_version`, currently 4). On startup every pending
migration runs in its own `BEGIN IMMEDIATE … COMMIT` transaction that holds its DDL, its data backfill and the
version bump, so an interrupted upgrade leaves the DB at the previous version and is redone on the next start.
Upgrading a DB from an earlier release marks every order then open in `NEEDS_RECONCILIATION` / `CAPTURING`
as "capture may have happened" (it stays reserved and can only be resolved from PayPal, never cancelled). A DB
written by a newer version is refused.

**Switching modes.** Exported variables win over `.env`, and the offline block sets `AGENT_MODE`,
`PAYPAL_MODE` and `GRB_NO_DOTENV=1` (which disables `.env` entirely). Before following the Claude or
Sandbox instructions below, stop the server and open a **new shell**, or run:
`unset AGENT_MODE PAYPAL_MODE PAYPAL_MOCK_APPROVAL APPROVER_TOKEN DEMO_MODE GRB_NO_DOTENV DB_PATH PORT PUBLIC_BASE_URL GRB_INSTANCE`.

### With Claude (and optionally the real PayPal Sandbox)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then put your Anthropic key in ANTHROPIC_API_KEY
```

`.env` is loaded automatically (python-dotenv); real environment variables win.
The defaults run with a **mock PayPal** (no PayPal account, no network) and the Claude agent.

### Bring your own model

The agent works with either API format; pick one with `LLM_PROVIDER` (unset: `anthropic` if
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` is set, else `openai` if `OPENAI_API_KEY` is set). Both
providers (`app/llm/`) offer the model the same four tools, and every tool call still goes through
`BuyerService`, so the spend policy decides about money whichever model you plug in. Without any model,
use `AGENT_MODE=offline` (above).

```bash
# .env: one VAR=value per line; pick ONE of (a) / (b). Replace the 'your-…' values.
# (a) Anthropic Messages API, or any Messages-compatible endpoint
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-key
MODEL=claude-sonnet-5-5
#   optional: ANTHROPIC_BASE_URL=https://your-messages-compatible-host
#   optional: ANTHROPIC_AUTH_TOKEN=your-token        (sent as Bearer instead of x-api-key)

# (b) OpenAI-compatible Chat Completions with tool calling
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key                       # not needed for a local Ollama
OPENAI_MODEL=your-model-id                           # e.g. provider/model on OpenRouter
OPENAI_BASE_URL=https://api.openai.com/v1            # OpenRouter: https://openrouter.ai/api/v1 · Ollama: http://localhost:11434/v1
#   DeepSeek, vLLM, LM Studio or a gateway work the same way: set OPENAI_BASE_URL to the server's /v1 base.
#   Newer OpenAI models want max_completion_tokens: OPENAI_MAX_TOKENS_PARAM=max_completion_tokens (or none)
```

Put the lines in `.env`, or pass them as a one-shot command prefix, e.g.
`LLM_PROVIDER=openai OPENAI_API_KEY=your-openai-key OPENAI_MODEL=your-model-id python -m app.cli "2 USB-C cables"`. The model must
support tool (function) calling. Upstream errors are reduced to a known error type and a short message;
any configured secret or key-like token is withheld or masked before it can reach a client.

> **About the offline agent.** `AGENT_MODE=offline` swaps Claude for a small deterministic, rule-based
> parser (`app/offline_agent.py`, clearly **not** AI) with a **positive-only grammar**:
> `[buy|get|order|purchase|need] <quantity> <catalog item> [for/as <short purpose>]`, clauses joined by
> commas, "and", "plus" or ";"; `<quantity>` is a non-negative whole number or a/an/one…ten/pair/dozen, read only from the slot
> right before the item ("two three cables" is refused as ambiguous), and item words must come from the
> catalog vocabulary. Anything else ("Avoid buying …", "… only when
> approved", "Buy -1 …", "1.5", unknown verbs or words) is refused as a whole with the supported grammar in
> the reply, and quantity 0 is never ordered. It calls the *same* tools through the *same* `BuyerService`, so every guardrail, PayPal order,
> approval and refusal behaves exactly as with Claude; the dashboard header shows
> **OFFLINE rule-based agent (no AI)**. Claude remains the default and handles free-form requests.

Run all commands from the repository root with the venv active (paths in `.env` are relative).
The commands below use bash syntax (`VAR=value cmd`); on Windows set the variables in `.env` instead.
If you serve on a port other than 8000, also set `PUBLIC_BASE_URL` to the same port (used for the PayPal
return / cancel links and the mock checkout).

### Repro steps ("do X → see Y")

1. **Run the tests** — `python -m pytest -q` → see `586 passed, 3 skipped` (589 tests, 168 of them for the Nexconn chat channel and the `/try` playground; no network, no API key needed; the skips are two headless UI tests that need jsdom and one interop test that needs the official `mcp` SDK; without `node` eleven more node-only tests are skipped: `575 passed, 14 skipped`).
2. **Start the app** — `APPROVER_TOKEN=demo PAYPAL_MOCK_APPROVAL=1 uvicorn app.main:app --port 8000` (add `AGENT_MODE=offline`, or use the exports from the offline block, if you have no Anthropic key) →
   open <http://localhost:8000/> for the dashboard (or `/docs` for Swagger). Enter `demo` in the
   *Approver token* box. `PAYPAL_MOCK_APPROVAL=1` makes the offline mock behave like the real
   Sandbox: every order waits for a buyer approval on a local mock checkout page.
3. **Check the policy** — `curl localhost:8000/policy` → see the caps, allowlists and `"spent_today": "0.00"`.
4. **Ask the agent to buy something mixed** —
   ```bash
   curl -X POST localhost:8000/agent/request -H 'content-type: application/json' \
     -d '{"request":"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."}'
   ```
   (or type it into *Ask the agent* on the dashboard) → the cables ($25.98) get a PayPal order
   (**Pay with PayPal**, $25.98 reserved), the keyboard ($89) gets an **approval request**, and the
   gift card is **refused** (blocked merchant + category). See `docs/sample_transcript.json` for a real run.
   Click **Pay with PayPal** → approve → you land back on the dashboard with the order `COMPLETED`.
5. **See what is waiting for you** — `curl 'localhost:8000/approvals?status=PENDING'` → see the keyboard approval with the agent's justification and the policy reasons.
   Keep its id for the next step:
   `APR_ID=$(curl -s 'localhost:8000/approvals?status=PENDING' | python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')`
6. **Approve it** — click **Approve** in *Pending approvals*, review the confirmation dialog and click **Approve & create PayPal order**, or
   `curl -X POST -H 'X-Approver-Token: demo' "localhost:8000/approvals/$APR_ID/approve"` → a new order
   linked to the approval, awaiting the buyer; `GET /spend` shows `total: 114.98`. Without the token → `401`.
7. **Or reject it** (instead of step 6) — **Reject**, then **Reject request** in the dialog / `curl -X POST -H 'X-Approver-Token: demo' "localhost:8000/approvals/$APR_ID/reject"` → status `REJECTED`, no money moves.
8. **One-shot from the CLI** — `python -m app.cli "3 bags of coffee beans and 2 snack boxes"` → see each tool call and its verdict (see `docs/sample_cli_run.txt`; without a key: `AGENT_MODE=offline python -m app.cli "…"`, sample in `docs/sample_offline_run.txt`). The CLI starts from an empty in-memory ledger each run. `CLI_DB_PATH=<file>` makes it use a database file; in mock mode the CLI must then own that file exclusively (same lock as the server) and exits with code 3 while a mock server uses it, so do **not** point the CLI at a running mock server's database. Sharing a database between CLI and server works only in Sandbox mode.
9. **Audit every decision** — open the **Audit** tab (or `curl localhost:8000/decisions`) → one row per guardrail decision: the gift card is a red `deny` (rule hit `merchant_blocklist`/`category_allowlist`), the keyboard an amber `approval` (`per_order_cap`), an order whose payment is unconfirmed is highlighted orange. Filter any column, search with the box, **Export CSV**, and click a row → quote, every rule evaluated and the payment timeline (`CREATING → PAYER_ACTION_REQUIRED → CAPTURING → COMPLETED`). The Audit tab says how much of the history it shows ("Showing newest 1000 of N"); **Load older** pages back with the API cursor; it polls only while the tab is visible, and the open detail refreshes with each poll (labelled with its snapshot time). CSV cells that start with `= + - @` (or tab / CR) are prefixed with `'` so spreadsheets do not evaluate them. Headless check without a browser: `node scripts/ui_smoke_jsdom.cjs http://127.0.0.1:8000` (needs `npm i jsdom@24`); `GRB_JSDOM_NODE_PATH=<that node_modules> pytest tests/test_ui_jsdom.py` runs the Audit-tab regressions (skipped without jsdom).
10. **Edit the policy** — open the **Policy & spend** tab → the spend-over-time chart and the policy grids. Double-click
    *Per-order cap*, type `100`, Enter, then *Daily cap*, type `250`, Enter (after steps 4 and 6 today's total is
    $114.98, so a second $89 keyboard would exceed the old $150 daily cap) → the cells turn yellow, **Save policy**
    (owner token: blank = the approver token `demo`) → saved, listed in *Change history*, and another keyboard is now within policy
    (`curl -X POST localhost:8000/tools/check_policy -H 'content-type: application/json' -d '{"arguments":{"product_id":"P-1003","quantity":1}}'`
    → `ALLOW`). Try a per-order cap above the daily cap → refused by the server, nothing changes. Without the
    token: `curl -X PUT localhost:8000/policy -H 'content-type: application/json' -d '{"daily_cap":"1000"}'` → `401`.
11. **Shop from an MCP agent** — see [Use it from any MCP agent](#use-it-from-any-mcp-agent-claude-desktop--codex)
    (set `GRB_API_URL` to this server, and orders/approvals land in this dashboard).

### Against the real PayPal Sandbox

Create a free REST app at <https://developer.paypal.com/> (Apps & Credentials → Sandbox), copy its
client id and secret into `.env`, then:

Use a fresh shell (see *Switching modes*), then:

```bash
# in .env: PAYPAL_MODE=sandbox  PAYPAL_CLIENT_ID=…  PAYPAL_CLIENT_SECRET=…  (PUBLIC_BASE_URL if not :8000)
APPROVER_TOKEN=demo uvicorn app.main:app --port 8000
python scripts/sandbox_smoke.py          # no LLM: guardrails -> real Sandbox orders -> status -> capture-before-approval -> cancel
python scripts/sandbox_smoke.py --app http://localhost:8000 --agent --token demo   # same, through the running app + Claude agent
```

The smoke test creates real Sandbox orders (status `PAYER_ACTION_REQUIRED`, approve link on
`www.sandbox.paypal.com/checkoutnow`), reads their live status, shows that a capture before the buyer
approved is refused by PayPal (`422 ORDER_NOT_APPROVED` → the app answers `409 NOT_APPROVED_YET`, order
and budget reservation unchanged), and cancels them so the reservation is released. Sample output:
`docs/sandbox_smoke_run.txt`.

**Verified end to end (2026-10-30):** real Claude agent → real Sandbox order → a Sandbox personal test buyer logs in
and pays on `www.sandbox.paypal.com` → `/paypal/return` → capture `COMPLETED` (PayPal order `34M919590A962473E`,
capture `8VA9873790040992M`). Timeline, API checks and screenshots: [`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md).

To finish a payment, click **Pay with PayPal**, log in with a Sandbox *personal* test account (Testing
Tools → Sandbox Accounts in the developer dashboard) and pay → PayPal redirects to `/paypal/return`, the
order is captured and shows `COMPLETED` with its capture id. If that redirect is lost (e.g. the app
ran on another host), `POST /orders/{id}/capture` captures it afterwards.

`PAYPAL_AUTH=proxy` is for deployments where an egress proxy injects `Authorization: Basic` for
`api-m.sandbox.paypal.com`; the app then holds no PayPal credentials at all (PayPal REST accepts the
client credentials as Basic auth directly on the Orders API).

### Changing the policy

Edit `policy.yaml` (or point `POLICY_PATH` at a `.json` file) and restart. Example: set
`per_order_cap: 100` → the keyboard in step 4 is bought directly instead of needing approval.

At runtime the owner can change the caps and allowlists in the **Policy & spend** tab (or `PUT /policy` with
`X-Owner-Token`) without a restart (repro step 10). The edit is stored in the database and overrides
`policy.yaml` for that database (a fresh `DB_PATH` starts from `policy.yaml` again); every process sharing
the database uses it from its next decision.

## Approve in chat (Nexconn)

*Added for the Nexconn Build Challenge 2026 (see [prior work](#prior-work-and-challenge-work)). Off unless
`NEXCONN_MODE` is set; with it unset the app behaves exactly as above.*

Approvals should reach the people who decide, where they already talk. With the Nexconn channel on, every
**NEEDS_APPROVAL** the agent raises is posted as an approval card into a Nexconn **group channel**; an approver
taps **Approve** / **Reject** (or types `approve APR-1A2B3C4D`) in the chat, and the guardrail state updates:

```
agent → BuyerService.request_approval ──► card in the Nexconn group (Platform Chat API, as the bot user)
approver taps Approve in /chat (Nexconn Web Chat SDK) ──► message:send webhook (signed) ──►
    room? approver? message seen before? ──► BuyerService.approve / reject  (the same guarded path as the
    dashboard: hard rules re-checked, quote bound, claimed once) ──► outcome posted back into the room
```

| Piece | Where |
|---|---|
| Settings (`NEXCONN_*`), validated at startup | `app/nexconn/config.py` |
| Platform Chat API client (request signing, SG/NA/SA hosts with backup-host failover) + in-memory mock | `app/nexconn/client.py` |
| Card / outcome text, decision parsing (`approve APR-…`, or the structured `extra` the buttons send) | `app/nexconn/protocol.py` |
| Webhook signature, time window, single-use nonce, `message:send` parsing | `app/nexconn/webhook.py` |
| The channel: listens to approval events, posts (outbox + retry), handles decisions | `app/nexconn/channel.py` |
| Routes: `/chat`, `/nexconn/session`, `/nexconn/webhook`, … | `app/nexconn/routes.py` |
| Approver chat page on the Nexconn Web Chat SDK (`@nexconn/chat` 26.2.1); loads the latest 50 messages; on join it checks the server's approval list, lists any still-pending approval whose card is older above the room (same buttons) and closes a card in view that was already decided | `app/static/chat.html`, `chat.js`, `chat-room.js` (room rendering, shared with `/try`), `chat-transport.js`, `chat-reconcile.js` |
| `/try` playground: per-visitor sandbox (own Nexconn user, group and database), invites, limits, expiry | `app/playground/`, `app/static/try.*`, [`docs/PLAYGROUND.md`](docs/PLAYGROUND.md) |

**Rules the chat cannot bend.** Only user ids in `NEXCONN_APPROVER_IDS` can decide (anyone else gets a
"not an approver" note and nothing changes); a decision must name the approval id (`ok`, `yes`, `approve` alone
do nothing); the agent's own bot user cannot decide; messages from any other channel are ignored; every message id
is acted on once (webhook retries, the relay and a replayed delivery cannot approve twice); a second decision
on a closed approval is refused. A chat approval waives only the soft caps, exactly like the dashboard. Decisions
made on the dashboard are posted to the room too, so the room mirrors every approval request and its outcome
(it is not the system of record: automatic in-policy purchases, policy denials and later payment events stay in
the dashboard's audit log). Posting is best effort and at least once: if Nexconn is down the purchase flow is
unaffected, the post is kept as `FAILED`, retried automatically (up to 5 times) and by `POST /nexconn/retry`; a
decision whose handler crashed is finished on the next start or recovery pass.

### If Nexconn is unreachable

In `NEXCONN_MODE=live`, `NEXCONN_FALLBACK=auto` (default) switches `/try` and `/chat` to a labelled
offline room when credentials/plan access are declined, or after two consecutive transport/server
failures. Only chat transport is replaced: guardrails, approval claims and hard-rule rechecks, spending
limits, audit logs, and simulated payments still run for real. `/chat` keeps its approver-token checks.
`NEXCONN_FALLBACK=off` preserves the old failure behavior; `force` always uses the stand-in without
calling Nexconn. `NEXCONN_PROBE_EVERY_S=60` bounds lazy recovery probes triggered by state/config/health
requests. SDK connection failures can request a globally limited server probe; `/try` members can
also downgrade their own sandbox without changing the site's upstream state.

After recovery, new sandboxes and the main room use live Nexconn. Fallen-back sandboxes stay on the
stand-in and offer **Start a new live sandbox**; `/chat` offers **Reconnect**. `/health` includes
`nexconn.upstream` (`state`, safe `reason`, `since`, `last_probe_at`, `fallback`), `nexconn.effective_mode`,
and `playground.new_sandboxes`. `/health/ready` stays 200 while serving the working stand-in, reporting
`checks.nexconn=true` and `nexconn_mode="offline-fallback"`. See [the playground notes](docs/PLAYGROUND.md#if-nexconn-is-unreachable)
for persistence and recovery details.

### Try it (no token needed)

`/try` is a self-serve playground for visitors (`TRY_MODE=on`; off by default, needs `NEXCONN_MODE=mock`
or `live`). One click creates a **sandbox**: a Nexconn user registered for the visitor (live mode; the
browser connects with the Web Chat SDK), a private Nexconn group with the agent bot, and a separate SQLite
database with its own copy of the policy. The page guides through the loop with a progress checklist
(join the room → under-cap request → over-cap request → approve / reject in the chat → pay on the
simulated checkout → optionally invite a teammate, who gets their own Nexconn user and joins the room as
a second approver). Payments are always simulated and the agent is always the rule-based offline agent;
the playground never calls PayPal or an LLM, and never touches the main database or the judges' room
(decisions in a sandbox room arrive through the same signed webhook and are routed to that sandbox by
group id). Limits: per-IP rate limits for creating, joining and agent requests, a cap on active
sandboxes and on the Nexconn users the playground may register (beyond it new sandboxes run on an
offline stand-in, labelled in the page), 4 members per sandbox, deletion after 24 h idle. Settings:
[`TRY_*`](#configuration); isolation model, data lifecycle and threat model:
[`docs/PLAYGROUND.md`](docs/PLAYGROUND.md).

### Try it offline (no Nexconn account)

In the offline block's shell, before starting the server:

```bash
export NEXCONN_MODE=mock NEXCONN_APPROVER_IDS=alice,bob
```

Start the server as in the block, open `$PUBLIC_BASE_URL/chat`, enter approver token `demo`, **Join room**
as `alice`, then send the agent request from the block: the keyboard's approval card appears in the room;
tap **Approve** and the outcome (order id, "decided by alice in chat") is posted back. The mock is an
in-memory stand-in for Nexconn (same calls, no network); the dashboard's **Approval chat** chip links to it.

### With a real Nexconn app (free plan)

1. Create a free Nexconn account and app at <https://console.nexconn.ai> (Free Chat Plan, no card). On
   **Key Management** copy the **App Key**, **App Secret** and the data center.
2. Set `NEXCONN_MODE=live NEXCONN_APP_KEY=… NEXCONN_APP_SECRET=… NEXCONN_AREA=SG` (or `NA` / `SA`) and
   `NEXCONN_APPROVER_IDS=…` in `.env`. The App Secret stays on the server (it signs server API calls and
   checks webhooks); the browser only gets the App Key and a per-approver access token.
3. Decisions come back through a webhook: in the console, **Chat → Chat settings → Webhooks**, set
   `https://<your host>/nexconn/webhook` and select the `message:send` event. For a local run without a public
   URL, set `NEXCONN_RELAY=1` instead: the `/chat` page then forwards each decision it sent (authorised by the
   approver token; the webhook and the relay de-duplicate on the Nexconn message id).
4. Start the server: it registers the bot and approver users and creates the group (`NEXCONN_GROUP_ID`) with
   all of them in it. Open `/chat`, join as an approver: the page loads the pinned Web Chat SDK through an
   import map (`/nexconn/sdk/*.mjs` → jsDelivr; `NEXCONN_SDK_BASE` to self-host) and connects with the token.

`GET /nexconn/status` (approver token) lists what was posted and received, with errors.

**Limits.** Nexconn's webhook signature covers the nonce and timestamp, not the body; we add a time window and
single-use nonces kept in the database (so they hold across worker processes and restarts), and the approver
allowlist bounds what a forged decision could do, but use an `https://` webhook URL. The approver token is one
shared secret: whoever holds it can join the room as any listed approver (as on the dashboard, where it allows
every approval). In live mode an open `DEMO_MODE` without `APPROVER_TOKEN` cannot issue Nexconn approver tokens
or relay decisions (503). Only text messages are used (the structured part rides in the message's `extra`), so the cards
read well in any Nexconn client. The mock does not emulate Nexconn beyond what the flow needs.

### Prior work and challenge work

| | What | Where |
|---|---|---|
| **Existed before** (built Oct 29–30, 2026 for the PayPal AI Hackathon; public at [mubudot/guardrail-buyer](https://github.com/mubudot/guardrail-buyer)) | guardrail policy engine, PayPal Orders v2 Sandbox integration, Claude and offline agents, MCP server, AG Grid dashboard | the rest of the code base |
| **Built for the Nexconn challenge** (from Oct 30, 2026) | Nexconn Platform Chat API client (signing, data-center failover), signed `message:send` webhook receiver (time window, single-use nonces), approval channel (cards, decisions, approver allowlist, outbox + retry, recovery), approver chat page on the Nexconn Web Chat SDK, offline mock of the room, live deployment, the `/try` playground, automatic offline fallback, 168 new tests | `app/nexconn/`, `app/playground/`, `app/static/chat.html`, `chat.js`, `chat-room.js`, `chat-transport.js`, `chat-reconcile.js`, `chat.css`, `try.*`, `tests/test_nexconn_*.py`, `tests/test_playground*.py`, `tests/test_try_js.py`, `tests/test_offline*.py`; small edits to existing files: an observer hook on approval events (`app/service.py`), wiring of the channel and routes (`app/main.py`), an "Approval chat" link and style on the dashboard, `NEXCONN_*` in `.env.example`, `NEXCONN_APP_SECRET` added to the MCP credential scrub, test fixtures, docs and counts |

The git history shows the split: commits up to `25627cc` are the PayPal version (identical to the public
PayPal repo); the Nexconn work starts at the commit "Nexconn chat approval channel". Later fixes from the
PayPal repo were merged in (merge commit "Merge main … into nexconn").

## Use it from any MCP agent (Claude Desktop / Codex)

GuardRail Buyer is also an **MCP server** (`app/mcp_server.py`, stdio, no extra dependency), so any
MCP client (Claude Desktop, Codex, Cursor, your own agent) can shop, but only through the guardrails.
The agent gets seven tools and nothing else:

| Tool | What it does | Can it move money? |
|---|---|---|
| `search_catalog` | products with server-set id, merchant, category, price | no |
| `check_policy` | dry-run: ALLOW / NEEDS_APPROVAL / DENY with reasons | no |
| `create_order` | `product_id` + `quantity` only → policy gate → reservation → PayPal Orders v2 order (buyer approves at `approve_url`) | only if the policy says ALLOW |
| `request_approval` | files a human approval for an over-cap order (DENY can't be escalated) | no, a person decides in the dashboard |
| `get_order_status` | live order status (`AWAITING_PAYER_APPROVAL` / `ORDER_PLACED` / …) | no |
| `get_approval_status` | `PENDING` / `APPROVED` (+ its order) / `REJECTED` / `DENIED_BY_POLICY` | no |
| `get_spend_summary` | captured, reserved, remaining budget, caps | no |

**Why the MCP agent can't bypass anything:**

- Every tool call goes through `GuardedTools` (`app/tool_gateway.py`) into the same `BuyerService` the
  built-in agent and the dashboard use: per-order cap, daily cap, hard maximum, merchant/category rules,
  reservation in one SQLite transaction before PayPal is called, approvals bound to their quote.
- Arguments are validated strictly against the published schemas (`additionalProperties: false`, no type
  coercion): an extra `price`, `merchant`, `amount` or `payee` is refused (`INVALID_ARGUMENTS`), not ignored.
- There is **no approve, reject, cancel or capture tool**. Approvals need the approver token in the
  dashboard; the buyer still confirms every payment on PayPal.
- The MCP process drops the app's credentials. In **remote mode** (recommended) it only forwards calls to the
  running GuardRail server (`POST /tools/{name}`), which holds the PayPal credentials. At start it removes
  `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ACCESS_TOKEN`, `APPROVER_TOKEN`, `POLICY_OWNER_TOKEN` and `STATE_SECRET`
  from its own environment, and results are reduced to public order fields (no signed cancel state).
- Splitting doesn't help: every order an external agent places is grouped server-side per policy day, so
  same-SKU orders at the same merchant count as one purchase for the per-order cap and the hard maximum,
  across MCP sessions and REST callers (the client can't choose or rotate the group).

**Two modes.** *Remote* (`GRB_API_URL=http://127.0.0.1:8000`): the full product (orders appear in the
dashboard, approvals by a human, payment on PayPal Sandbox or the mock checkout). *Embedded* (no
`GRB_API_URL`): a self-contained offline sandbox in the MCP process (mock PayPal that captures at once,
in-memory ledger, no keys, no network); real PayPal is refused in this mode on purpose.

**Try it without any client** (offline, from the repository root with the venv active):

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"create_order","arguments":{"product_id":"P-1001","quantity":2}}}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_order","arguments":{"product_id":"P-5001","quantity":1}}}' \
 '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"create_order","arguments":{"product_id":"P-1003","quantity":1,"price":"0.01"}}}' \
 | python -m app.mcp_server
# -> id 2: ORDER_PLACED 25.98 · id 3: DENIED (blocked merchant) · id 4: isError, INVALID_ARGUMENTS ("price" is set by the server)
```

**Claude Desktop** (`claude_desktop_config.json`; start the GuardRail server first, e.g. with the offline block):

```json
{
  "mcpServers": {
    "guardrail-buyer": {
      "command": "/ABSOLUTE/PATH/paypal-guardrail-buyer/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "/ABSOLUTE/PATH/paypal-guardrail-buyer",
        "GRB_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

On Windows use `C:\\path\\to\\paypal-guardrail-buyer\\.venv\\Scripts\\python.exe` as `command`. Leave
out `GRB_API_URL` for the embedded offline sandbox. Do **not** put PayPal credentials or the approver
token here: they belong to the GuardRail server only.

**Codex CLI:**

```bash
codex mcp add guardrail-buyer \
  --env PYTHONPATH=/ABSOLUTE/PATH/paypal-guardrail-buyer --env GRB_API_URL=http://127.0.0.1:8000 \
  -- /ABSOLUTE/PATH/paypal-guardrail-buyer/.venv/bin/python -m app.mcp_server
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.guardrail-buyer]
command = "/ABSOLUTE/PATH/paypal-guardrail-buyer/.venv/bin/python"
args = ["-m", "app.mcp_server"]
env = { PYTHONPATH = "/ABSOLUTE/PATH/paypal-guardrail-buyer", GRB_API_URL = "http://127.0.0.1:8000" }
```

Then ask, for example: *"Buy 2 USB-C cables and a mechanical keyboard for our new hire."* → cables:
order awaiting the buyer on PayPal; keyboard: approval request in the dashboard. A live run in which a real
model (via Codex) was told to try a prompt injection (gift card, $640 chair, `price=0.01`, self-approval)
and every attempt was refused by the server is recorded in [`docs/MCP_LIVE_SMOKE.md`](docs/MCP_LIVE_SMOKE.md).

**Protocol:** newline-delimited JSON-RPC 2.0 on stdin/stdout (logs on stderr): `initialize`, `ping`,
`tools/list`, `tools/call`, revisions 2025-06-18 / 2025-03-26 / 2024-11-05; tools carry MCP annotations
(`readOnlyHint` etc.) and results carry `structuredContent`. Policy outcomes (DENIED, NEEDS_APPROVAL) are
normal results; invalid arguments and unknown ids are `isError: true`. Tested with a stdio client in the
test suite (embedded and remote, prompt-injection attempts included) and, when it is installed, with the
official MCP Python SDK (`tests/test_mcp.py::test_interop_with_official_mcp_sdk`, checked with `mcp` 2.3.0).

**Why we wrap Orders v2 ourselves instead of PayPal's Agent Toolkit / MCP server.** We evaluated
[PayPal's Agent Toolkit](https://github.com/paypal/agent-toolkit) and its
[MCP server](https://developer.paypal.com/tools/mcp-server/) (`@paypal/mcp` locally, or
`mcp.paypal.com` / `mcp.sandbox.paypal.com` remotely). They are a good fit for a merchant who wants an
assistant to run PayPal tasks (invoices, orders, refunds, disputes, subscriptions). For a guarded *buyer*
they do not fit: the client is configured with a PayPal access token (or OAuth consent) so the agent's
process holds PayPal authority, and `create_order` takes the amount and items from the model. Putting our
gate in front would mean re-implementing every money-moving tool anyway (server-side catalog prices,
reservation before the provider call, `PayPal-Request-Id` idempotency, signed cancel URLs,
ambiguous-outcome reconciliation), and the toolkit's own money-moving tools (`pay_order`, refunds) would
have to be disabled. So the gateway exposes only intent-level tools (`product_id`, `quantity`) and calls
PayPal Orders v2 itself from the server; the Agent Toolkit stays a candidate for read-only or merchant-side
tasks behind the same gateway (see roadmap).

## Demo video pipeline

`scripts/video/make_demo.sh` rebuilds the demo-video draft reproducibly: Piper TTS narration per scene
(`scripts/video/scenes.json`, script in `docs/VIDEO_SCRIPT.md`) → app in Sandbox mode → headless
Chromium (Playwright) drives the dashboard and records the screen → ffmpeg cuts the model's thinking
time and lays the narration on top → `docs/demo_draft.mp4`. No music. The Sandbox buyer login (scene 7) is
recorded only when a Sandbox *personal* test account is given in `SANDBOX_BUYER_EMAIL` /
`SANDBOX_BUYER_PASSWORD` (environment only); without them scene 7 is a labelled placeholder card
(see `docs/VIDEO_SCRIPT.md`). The prepared 2:18 cut is a `--skip-checkout` take of the current dashboard
(real Sandbox + Claude) followed by the real buyer-approval clip from an earlier take, labelled on screen.

## REST API

| Method | Path | Description |
|---|---|---|
| POST | `/agent/request` | `{"request": "..."}` — run the agent; returns final text, transcript, token usage. Rate-limited per client IP (`AGENT_RATE_LIMIT`, default 20/60 s) → `429` |
| GET | `/orders` | all orders (`?status=COMPLETED`) with the policy decision attached |
| GET | `/orders/{id}` | one order; `?refresh=true` (default) adds `paypal_live_status` from PayPal |
| POST | `/orders/{id}/capture` | capture after buyer approval (fallback for a lost redirect); not approved yet → `409 NOT_APPROVED_YET` |
| POST | `/orders/{id}/reconcile` | resolve a `NEEDS_RECONCILIATION` order from PayPal's order status (never charges twice) |
| POST | `/orders/{id}/cancel` | cancel an order still awaiting the buyer, release its reservation (approver token); otherwise `409`; `503` when no `APPROVER_TOKEN` is configured (unless `DEMO_MODE=1`) |
| GET | `/approvals` | approval requests (`?status=PENDING`) |
| POST | `/approvals/{id}/approve` | approve once → quote unchanged? deny rules (incl. split-order hard max) re-checked → PayPal order; a repeat returns the same order; changed quote → `409`. Needs `X-Approver-Token`; with no `APPROVER_TOKEN` configured → `503` unless `DEMO_MODE=1` |
| POST | `/approvals/{id}/reject` | reject, nothing is paid (same token) |
| GET | `/paypal/return?token=…` | PayPal redirect after buyer approval → capture |
| GET | `/paypal/cancel?token=…&state=…` | PayPal redirect on cancel → requires the signed `state` from the order's cancel_url (`403` otherwise); cancels only if still awaiting the buyer |
| GET | `/decisions` | audit log: every guardrail decision (policy check, create order, approval request, approval execution) with verdict, rule hit, amount, merchant, request id, timestamp and the current status of the order/approval it led to newest first, compact rows (rules load in the detail). Paged: `{items, total, limit, has_more, next_before_id}`; `?limit=` (1–5000, default 1000), `?before_id=<decision id>` = keyset cursor for older decisions |
| GET | `/decisions/{id}` | one decision: quote, every rule evaluated (PASS / DENY / NEEDS_APPROVAL / WAIVED / SKIPPED), approval, and the order's payment timeline (status transitions) |
| GET | `/spend` | today's captured / reserved / remaining vs caps |
| GET | `/policy` | active policy + today's spend + `revision` (changes with every owner edit) |
| GET | `/` | AG Grid dashboard |
| GET | `/catalog` | demo products (`?q=keyboard`) |
| GET | `/health` | PayPal mode, model, approver mode, policy owner mode, rate limits, `instance` (= `GRB_INSTANCE`) |
| GET | `/tools` | the guarded tool set (same JSON schemas the MCP server publishes) |
| POST | `/tools/{name}` | `{"arguments": {...}, "session_id": "mcp-<16 hex>"}` (session id optional: validated, never used for policy decisions) — run one guarded tool through the policy gate (used by the MCP server in remote mode). Orders are grouped per policy day for split-order aggregation. Policy outcomes are `200 {"ok": true, "result": …}`; invalid arguments / unknown ids are `{"ok": false, "code": …}`; unknown tool → `404`. Rate-limited per client IP (`TOOLS_RATE_LIMIT`, default 120/60 s) |
| PUT | `/policy` | owner edit of `per_order_cap`, `daily_cap`, `hard_max_per_order`, `allowed_categories`, `allowed_merchants`, `blocked_merchants` (only the fields to change). Needs `X-Owner-Token`; validated server-side (amounts with ≤ 2 decimals, per-order ≤ daily ≤ …, hard max ≥ per-order, non-empty allowlists, no merchant both allowed and blocked) → `422` with the reason, nothing changes. Send `GET /policy`'s `revision` as `If-Match` (the dashboard does): if the policy changed meanwhile → `409`, nothing changes |
| GET | `/policy/history` | owner edits, newest first (who, when, each field from → to) |
| GET | `/spend/history?days=14` | captured / reserved / total per day (1–90 days, policy timezone) for the spend chart |

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | auto | `anthropic` or `openai` (see *Bring your own model*); unset = from the credential that is set |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | any Messages-API-compatible endpoint |
| `ANTHROPIC_API_KEY` | – | sent as `x-api-key` if set; leave unset behind a gateway that injects auth on the wire |
| `ANTHROPIC_AUTH_TOKEN` | – | alternative to the key, sent as `Authorization: Bearer` |
| `MODEL` | `claude-sonnet-5-5` | model for the Anthropic provider |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible Chat Completions base (OpenRouter, DeepSeek, vLLM, Ollama, a gateway) |
| `OPENAI_API_KEY` | – | sent as `Authorization: Bearer`; optional for local servers |
| `OPENAI_MODEL` | – (required for `openai`) | model id the endpoint serves |
| `OPENAI_MAX_TOKENS_PARAM` | `max_tokens` | `max_completion_tokens` for newer OpenAI models, `none` to omit |
| `AGENT_MODE` | `claude` | `offline` = deterministic rule-based parser, no AI and no key (for judges / CI) |
| `PAYPAL_MODE` | `mock` | `sandbox` uses the real PayPal Sandbox |
| `PAYPAL_MOCK_APPROVAL` | `0` | mock only: `1` = orders wait for a simulated buyer approval |
| `PAYPAL_AUTH` | `oauth` | `oauth`: client id + secret → OAuth2 token → Bearer. `proxy`: no credentials in the app, an egress proxy injects `Authorization: Basic` |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | – | Sandbox REST app credentials (`oauth` mode) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | base of the PayPal return / cancel URLs |
| `PAYPAL_APPROVAL_TTL_MIN` | `180` | unapproved orders expire and release their reservation |
| `APPROVER_TOKEN` | – | required for approve / reject / cancel. Empty = those endpoints answer `503` (fail closed) |
| `DEMO_MODE` | `0` | `1` = explicit local demo: an empty `APPROVER_TOKEN` leaves approve / reject / cancel open (logs a warning) |
| `AGENT_RATE_LIMIT` | `20/60` | max `/agent/request` calls per client IP per window (`N/SECONDS`, both positive whole numbers); `off` disables. Anything else (e.g. `0/60`) stops the server at startup with a clear error. In-memory, per process |
| `STATE_SECRET` | random, stored in the DB | HMAC key for the signed cancel `state` |
| `POLICY_OWNER_TOKEN` | – (falls back to `APPROVER_TOKEN`) | required by `PUT /policy` (`X-Owner-Token`); with neither set, policy editing answers `503` unless `DEMO_MODE=1` |
| `TOOLS_RATE_LIMIT` | `120/60` | max `POST /tools/{name}` calls per client IP per window (same format as `AGENT_RATE_LIMIT`) |
| `GRB_API_URL` | – | MCP server only: GuardRail server to forward tool calls to (remote mode); unset = embedded offline sandbox |
| `MCP_DB_PATH` | `:memory:` | MCP server, embedded mode only: database file for the offline sandbox |
| `PORT`, `GRB_INSTANCE` | – | used by the README launch/ready check only (`GRB_INSTANCE` is echoed by `/health`) |
| `POLICY_PATH`, `CATALOG_PATH`, `DB_PATH` | `policy.yaml`, `data/catalog.json`, `guardrail.db` | |

**`/try` playground** (off unless `TRY_MODE=on`; invalid values stop the server at startup):

| Env var | Default | Notes |
|---|---|---|
| `TRY_MODE` | `off` | `on` serves `/try` (needs `NEXCONN_MODE=mock` or `live`); `off`: the routes answer `404` |
| `TRY_DIR` | `var/try` | registry database + one database per sandbox |
| `TRY_TTL_HOURS` | `24` | idle sandboxes expire: database deleted, invite link dead |
| `TRY_MAX_ACTIVE` | `200` | more active sandboxes → `503` |
| `TRY_MAX_LIVE_USERS` | `500` | lifetime cap on Nexconn users the playground registers; beyond it sandboxes use the offline stand-in (`0` = always) |
| `TRY_MAX_MEMBERS` | `4` | members per sandbox, owner included → `409` |
| `TRY_CREATE_LIMIT` / `TRY_JOIN_LIMIT` | `5/3600` / `10/3600` | per client IP (`N/SECONDS` or `off`) → `429` with `Retry-After` |
| `TRY_AGENT_LIMIT` / `TRY_AGENT_LIMIT_IP` | `20/600` / `30/600` | agent requests per sandbox / per client IP |
| `TRY_CACHE_SIZE` | `32` | sandboxes kept open in memory; the rest reload from disk |

**Health and operations.** `GET /health/live` answers `200 {"ok": true}` whenever the process does;
`GET /health/ready` answers `200` when the database, the Nexconn channel (if on) and the playground
registry (if on) answer, else `503` naming the failed check. Unexpected errors return a JSON `500` with a
reference id (logged with the traceback). `deploy/` has an example systemd unit (`Restart=always`) and a
watchdog timer that restarts the service after three failed liveness checks;
`python -m app.playground.admin stats | expire-now | purge-expired` manages the playground's data.

## Status / roadmap

- [x] Guardrail engine + SQLite daily spend ledger, unit-tested
- [x] FastAPI backend with approvals workflow, MockPayPal
- [x] Claude tool-use agent wired to the guardrails
- [x] Buyer-approval round trip (create → approve → return → capture), reservations, cancel / expiry — tested with the mock and a wire-format test of the Sandbox client
- [x] Live run against the PayPal Sandbox: orders created, live status, capture-before-approval handled, cancel (`scripts/sandbox_smoke.py`)
- [x] Full buyer payment with a Sandbox personal test account, verified by hand ([`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md))
- [x] A second real Sandbox buyer payment recorded as footage for the demo video ("Second run" in [`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md))
- [x] AG Grid dashboard for orders, approvals and spend, approver token
- [x] AG Grid **Audit** tab: every guardrail decision with column filters, quick search, risk highlighting, CSV export and a detail panel (quote, rules evaluated, payment timeline)
- [x] Offline rule-based agent mode for running without an AI key (`AGENT_MODE=offline`)
- [x] Bring your own model: Anthropic Messages or any OpenAI-compatible endpoint (`LLM_PROVIDER`)
- [x] Demo video (2:18): live run of the current dashboard against the real PayPal Sandbox with Claude, plus the real Sandbox buyer approval clip recorded earlier the same day (labelled on screen), placed before the closing card (YouTube upload and the Devpost video link are pending)
- [x] MCP server: the guarded tools for any MCP agent (Claude Desktop, Codex …), remote + embedded offline modes, prompt-injection tests, live Codex run ([`docs/MCP_LIVE_SMOKE.md`](docs/MCP_LIVE_SMOKE.md)) — not shown in the video
- [x] AG Grid **Policy & spend** tab: owner-token protected policy editor with server validation and change history, spend-over-time chart (AG Charts Community) — present in the current build, not demonstrated in the video
- [ ] PayPal webhooks (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`) with signature verification
- [ ] PayPal Agent Toolkit for read-only / merchant-side tasks behind the same gateway

## Known limitations

This is a hackathon demo meant to run locally. Be precise about what it does and does not protect:

- **Authentication is local-demo scope (review B6, partly addressed).** There is still no requester identity
  or ownership: anyone who can reach the server can call `/agent/request`, list all orders and approvals,
  and trigger capture/reconcile of an order (capture only succeeds if the buyer approved on PayPal). What
  exists: `/agent/request` has a simple **in-memory per-IP rate limit** (`AGENT_RATE_LIMIT`, per process,
  reset on restart, keyed on the socket address, so it is not a defence behind a proxy or against many
  IPs); approve / reject / cancel need the single shared `APPROVER_TOKEN` (stored in the browser's
  `localStorage`), and an empty token **fails closed** (`503`) unless `DEMO_MODE=1` is set explicitly.
  The public `GET /paypal/cancel` callback is **not** covered by the approver token; it requires the
  per-order HMAC `state` from the order's cancel_url (review B3), and even then only cancels an order
  still awaiting buyer approval. Anyone who obtains that cancel URL (it is in the buyer's PayPal redirect)
  can cancel that one unpaid order. Do not expose the app on a public network.
- **The MCP / `/tools` surface has the same local-demo scope.** `POST /tools/{name}` is open to anyone who
  can reach the server (like `/agent/request`), rate-limited per IP; the MCP server forwards calls without
  its own authentication. Split-order aggregation for external agents covers the same SKU at the same
  merchant within one policy day (different SKUs are judged per order; the daily cap is the backstop), and
  it is deliberately stricter than for `/agent/request`: two separate same-SKU purchases on one day count
  together toward the per-order cap.
- **Policy edits are global and owner-token based.** One owner token (or the approver token when
  `POLICY_OWNER_TOKEN` is unset, so in that setup approvers can also change the policy); the change history
  keeps the last 200 edits in the database and records "owner", not a person. Currency and timezone are not
  editable. An edit applies to the next decision in every process sharing the database; orders already
  created are not revoked (see below).
- **Merchants are catalog labels (review B7).** Every order is paid to the one Sandbox merchant behind the
  configured PayPal REST app; the catalog merchant is not mapped to a payee and item details are not sent
  to PayPal. Merchant allow/block rules therefore enforce trusted catalog metadata, not real multi-merchant
  payment routing.
- **Reservation-day semantics.** Spend counts on the day the order was *reserved* (policy timezone). An order
  reserved yesterday and captured today counts toward yesterday, so the daily cap is not a cap on cash
  captured per calendar day.
- **Policy changes grandfather existing orders.** Orders already created (awaiting the buyer) are still
  captured after a policy change; only pending *approvals* are re-checked against the current deny rules
  when approved. Cancel open orders by hand (`POST /orders/{id}/cancel`) after tightening the policy.
- **Split-order aggregation is narrow.** Only orders for the same SKU from the same merchant are aggregated:
  within one `/agent/request` run for the built-in agent, and within one policy day for external agents
  (MCP / `/tools`). Different SKUs, or separate `/agent/request` runs, are judged per order; the daily cap
  is the backstop.
- **Reconciliation is on demand.** An ambiguous capture is checked once right away; every ambiguous
  outcome can be re-checked via `POST /orders/{id}/reconcile` (or a capture/return retry); there is no webhook or background job yet. An
  order whose capture outcome was ambiguous stays reserved (never cancellable or expirable) until PayPal
  reports it `COMPLETED`, declined or `VOIDED`; if PayPal no longer returns the order at all, resolve it by
  hand.
- **Mock PayPal state is in memory and mock mode is single-process** (reviews B9/N3): an exclusive lock
  file next to the resolved DB file enforces one mock server per database (aliases included; hard-linked DB
  files are refused). See the offline block above.
- **Audit history in the dashboard is paged**: the grid loads the newest 1000 decisions and older pages on
  request; search, filters and CSV export cover the loaded rows (the label says how many of the total).
  Older loaded rows are not re-polled; there is no retention/pruning of the audit log yet.
- **Upgrading a database from an older release** (any DB without `PRAGMA user_version` 4, including DBs
  already opened by rounds 5/6) is conservative: every order that was `NEEDS_RECONCILIATION` or
  `CAPTURING` at upgrade time is treated as "a capture may have happened", so it
  stays reserved and can no longer be cancelled or expired, only resolved forward with
  `POST /orders/{id}/reconcile` once PayPal reports it `COMPLETED`, declined or `VOIDED`. Stop all servers
  before upgrading, and back up the `.db` file first. Older orders get no audit timeline (the detail panel
  shows only their creation and current status), and decisions made before the upgrade are not in the audit log.
- **Concurrency guarantees assume one SQLite file on local disk** shared by all server processes (SQLite
  locking is unreliable on network file systems).
- **The offline agent is a restricted grammar**, not a language model: positive shopping lists only, item
  words from the catalog vocabulary. Each clause is parsed once into verb / quantity / item / purpose; only
  the quantity slot directly before the item and the item phrase decide what is ordered (so "Buy a USB-C
  cable for two interns" orders one cable). The optional `for …`/`as …` purpose tail is not used for
  quantity or item selection; it is only screened against a short list of condition/negation words (a
  denylist, so it is not a complete guarantee that unusual wording there is meaningless to a human
  reader). Verb, quantity and item are validated positively.
- **The real Sandbox buyer approval → capture loop was run for real on 2026-10-30, not in CI**: a first run by
  hand (redirect through a tunnel) and the recorded demo-video run, both with one Sandbox personal test buyer
  ([`docs/SANDBOX_E2E.md`](docs/SANDBOX_E2E.md)). The
  automated tests still exercise this loop only against the mock and a wire-format fake of the Sandbox API.

## AI tools used

- **Claude (Anthropic)** — `claude-sonnet-5-5` is the runtime purchasing agent (tool use via the Messages API).
- The optional `AGENT_MODE=offline` parser is plain hand-written rules, not a model.
- **Piper TTS** (offline, open source; voice trained on LibriTTS-R, CC BY 4.0) generated the demo-video narration.
- **Hark** (an AI agent, built on Claude) was used as a coding assistant to scaffold the backend, guardrail engine, tests and this README. All code was reviewed and tested by running the test suite and live agent calls.
- **OpenAI Codex (CLI)** was used as an independent code reviewer; findings and fixes are logged in `docs/CODEX_REVIEW_*.md` and [`docs/REVIEW_LOG.md`](docs/REVIEW_LOG.md).

## Other projects by the author

Disclosed for transparency: **near-agent-spend-guard** (a Rust NEAR smart contract that enforces spending
limits for AI agents), entered in NEAR HACKATHON, not in this hackathon. It is an independent project and
shares no code with this repository.

## License

MIT — see [LICENSE](LICENSE).
