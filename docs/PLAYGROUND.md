# The /try playground — design notes

`/try` lets any visitor run the whole approval loop in about a minute, as a real Nexconn user, without
an approver token and without touching the judges' room or the main data. It is off by default
(`TRY_MODE=off`); with `TRY_MODE=on` it needs the chat channel (`NEXCONN_MODE=mock` or `live`).

Code: `app/playground/` — `config.py` (TRY_* settings), `registry.py` (who exists), `sandbox.py`
(one sandbox), `manager.py` (create / join / reset / expire / LRU / message routing), `routes.py`
(HTTP), `admin.py` (operator CLI). Page: `app/static/try.html`, `try.js`, `try-model.js`, `try.css`,
with the room rendering shared with `/chat` in `chat-room.js` and plain-language errors in
`friendly-errors.js`.

## What a visitor gets

One POST from the "Start my sandbox" button creates a **sandbox**:

| Part | Live (`NEXCONN_MODE=live`) | Offline stand-in |
|---|---|---|
| Chat user | a Nexconn user `t<15 random>` registered with the visitor's display name (Platform API `issue_token`), connected by the Web Chat SDK in the browser | an in-process stand-in (same contract as the `/chat` mock) |
| Approval room | a Nexconn group `grbtry<sandbox id>`: the agent bot + the visitor + invited teammates | the same group id on the stand-in |
| Data | its own SQLite file `TRY_DIR/<sandbox id>.db`: own policy copy (from `policy.yaml`), own daily cap, own orders / approvals / audit log | same |
| Payments | always simulated (a mock PayPal with a checkout page under `/try/s/<id>/paypal/…`); the playground never calls the PayPal API | same |
| Agent | always the rule-based offline agent bound to the sandbox's own service; never an LLM | same |

The visitor is the approver of their own room. "Invite a teammate" gives a link (`/try/join/<code>`);
the friend gets their own Nexconn user and joins the same room as a second approver.

## Isolation model

* **Data.** A sandbox's `BuyerService` opens only its own database file. The main database, `/approvals`,
  `/orders`, `/nexconn/*` and the dashboard are unchanged and never read or written by the playground.
* **Rooms.** Each sandbox has its own group id. The signed webhook (`POST /nexconn/webhook`) stays the
  only live entry for decisions: signature, timestamp window and nonce checks run first, exactly as
  before; then each message item is dispatched by `channelId` — the main group to the main channel
  (unchanged), `grbtry…` to that sandbox's channel, anything else is ignored.
* **Deciders.** A sandbox room's approvers are exactly its members. A message from anyone else (including
  the judges' approvers) gets the existing `NOT_AN_APPROVER` notice; an approval id from another sandbox
  or from the main room is unknown in this room's database.
* **Sessions.** The page holds a session secret (32 random bytes, `X-Try-Session`); the registry stores
  only its SHA-256. Each member has their own secret. Invite codes are derived with an HMAC key kept in
  the registry and are looked up by their hash; a code is valid only while its sandbox is active.
* **Rendering.** Every message, name and agent answer is rendered as text (no `innerHTML`). Pay buttons
  accept only same-origin `/try/s/<id>/paypal/mock-checkout` links.

## Limits (all configurable; invalid values stop the server at startup)

| Setting | Default | Effect |
|---|---|---|
| `TRY_TTL_HOURS` | 24 | a sandbox idle this long expires: database deleted, room forgotten, invite dead |
| `TRY_MAX_ACTIVE` | 200 | more active sandboxes → 503 "at capacity" |
| `TRY_MAX_LIVE_USERS` | 500 | lifetime cap on Nexconn users the playground registers (persisted); beyond it new sandboxes run on the offline stand-in, labelled in the page |
| `TRY_MAX_MEMBERS` | 4 | members per sandbox, owner included → 409 |
| `TRY_CREATE_LIMIT` | 5/3600 per IP | new sandboxes → 429 with `Retry-After` |
| `TRY_JOIN_LIMIT` | 10/3600 per IP | joins |
| `TRY_AGENT_LIMIT` | 20/600 per sandbox | agent requests |
| `TRY_AGENT_LIMIT_IP` | 30/600 per IP | agent requests across sandboxes |
| `TRY_CACHE_SIZE` | 32 | sandboxes kept open in memory (the rest reload from disk on their next request) |

Creating and joining are POST-only, so link previews and crawlers never register users. A Nexconn
refusal that looks like a quota or rate limit falls back to the offline stand-in for that sandbox; any
other Nexconn failure answers 502 and leaves nothing behind (the seat reservation is released).

## Data lifecycle

* Everything needed to resume lives on disk: `TRY_DIR/registry.db` + one database per sandbox. After a
  restart the visitor's next request reloads the sandbox and re-sends room posts a previous run left
  queued or failed.
* Simulated PayPal orders are in memory (like the main app's mock mode): after a restart, open ones
  are marked `MOCK_RESET` and their reservation released; the agent can be asked again.
* Rooms on the offline stand-in lose their message history on restart; pending approvals stay
  reachable (the page lists them above the room, as `/chat` does).
* **Reset** (owner only) deletes the sandbox database and starts a fresh one (orders, approvals, today's
  spend), keeps the room and members, and posts "Sandbox was reset" — earlier cards in the room close.
* **Expiry** runs at startup and at most every 10 minutes on requests. The registry row is kept (marked
  `EXPIRED`) for the counters; `python -m app.playground.admin purge-expired` drops those rows and any
  orphan files. Nexconn users and groups created by the playground are not deleted on Nexconn's side;
  they hold no data beyond the room's messages, and the lifetime cap bounds how many exist.

Operator CLI (no network, safe while the server runs):

```bash
python -m app.playground.admin stats            # active / expired / members / live users registered
python -m app.playground.admin expire-now       # TTL sweep now (--all: every sandbox)
python -m app.playground.admin purge-expired    # drop expired rows and orphan database files
```

## Threat model: what a malicious visitor can and cannot do

**Cannot**

* see, decide or spend in another sandbox, the main data or the judges' room (separate databases,
  rooms routed by group id, deciders checked against the room's members);
* move money: payments are always simulated, the playground never calls PayPal or an LLM;
* forge a decision: decisions arrive only through Nexconn's signed webhook (live) or the room transport
  authenticated by a per-user token (stand-in), and go through the same guarded `BuyerService` path
  (hard rules re-checked, quote bound, claimed once);
* inject markup into another member's page (text-only rendering, cleaned `extra` fields);
* register users from a GET, a link preview or a crawler.

**Can (bounded)**

* create sandboxes and Nexconn users up to the per-IP rate limits and the global caps
  (`TRY_MAX_ACTIVE`, `TRY_MAX_LIVE_USERS`); beyond the live cap the playground degrades to the stand-in;
* post arbitrary text in their own room, visible to the people they invited;
* share their invite link; anyone holding it can join until the member cap or expiry.

Rate limits are per process and in memory (the deployment runs one worker); per-IP limits rely on
the reverse proxy passing the visitor's address (`uvicorn --proxy-headers`, see `deploy/`).

## Stability

* `GET /health/live` answers whenever the process does; `GET /health/ready` checks the main database,
  the Nexconn channel and the playground registry and answers 503 naming the failed check. `/health`
  carries a `playground` block (mode, active sandboxes, live seats left, whether the stand-in is in use).
* Unexpected exceptions become a JSON 500 with a reference id that is logged with the traceback; no
  internals are returned.
* `deploy/` has an example systemd unit (`Restart=always`) and a watchdog timer that restarts the
  service after three failed liveness checks.
