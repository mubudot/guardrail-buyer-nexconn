# Nexconn live end-to-end test (2026-10-30 UTC)

Deployment: https://guardrail-buyer-204-44-110-122.sslip.io (Nexconn edition, repository github.com/mubudot/guardrail-buyer-nexconn), with
`NEXCONN_MODE=live` (area SG, group `grbapprovals`, approvers `approver1`, `approver2`),
`PAYPAL_MODE=sandbox` (PayPal Sandbox, no real money), `AGENT_MODE=offline` (rule-based agent).
(After this test the deployment switched to a new group, `grbroom`, which is the room shown in the demo video; same code paths.)
The approver side was driven through the app's own `/chat` page in headless Chromium: it joins with the
approver token, gets a Nexconn access token from `POST /nexconn/session`, connects with the real
Nexconn Web Chat SDK 26.2.1, and the Approve / Reject buttons (or typed commands) post into the group.
Decisions reach the server only through Nexconn's signed `message:send` webhook (`NEXCONN_RELAY` off).
No secret values are recorded here.

| # | Scenario | What happened | Result |
|---|----------|---------------|--------|
| 1 | Over the per-order cap: "Buy 1 Mechanical Keyboard" (89.00 > 50.00) | `NEEDS_APPROVAL` → card posted to `grbapprovals` by `grbagent` (Platform Chat API) → approver1 pressed Approve in /chat → webhook delivered, signature checked, inbound recorded `source=webhook` → hard rules re-checked → PayPal Sandbox order `88563307UL154384U` created (`PAYER_ACTION_REQUIRED`, approve link on sandbox.paypal.com) → outcome posted back to the room, card closed as approved | PASS |
| 2 | Under the caps: "Buy 1 USB-C Charging Cable 2m" (12.99) | auto-allowed, PayPal Sandbox order `43H49222TJ5340725` created, nothing posted to the room | PASS |
| 3 | Policy denial: "Buy 1 Gift Card $100" | refused (blocked merchant, category not allowed); no order, no approval, nothing posted | PASS |
| 4 | Human rejection in chat: "Buy 1 Design Tool Annual License" (120.00, also over the daily cap) | card posted → approver2 pressed Reject → webhook → `REJECTED`, no order → "rejected — nothing was bought (decided by approver2 in chat)" | PASS |
| 5 | Late / typed decisions | approver1 typed `APPROVE apr-91275450` and `approve APR-91275450` after the rejection → both answered "approval is already REJECTED; nothing changed"; a plain chat line was ignored | PASS |
| 6 | Non-approver | a user outside `NEXCONN_APPROVER_IDS` (temporarily added to the group via the Platform API, removed afterwards) sent `approve APR-…` → `NOT_AN_APPROVER`, notice posted, nothing changed | PASS |
| 7 | Forged webhook | bad signature → 401; no signature headers → 401 | PASS |
| 8 | Restart | `systemctl restart guardrail` → bootstrap with the group already existing (join path) → `ready: true`, no errors | PASS |
| 9 | Dashboard decision mirrored | over-cap request approved through `POST /approvals/{id}/approve` → PayPal Sandbox order `3TL29399PP163314U`; card and outcome both posted to the room | PASS |

Observations
- Nexconn also delivers a `message:send` webhook for messages the server sends through the Platform
  API (the agent's own cards and outcomes); they are verified and ignored as "own message".
- `journalctl -u guardrail` showed no errors or warnings during the run; every authentic Nexconn
  delivery was answered 200 (the forged requests of scenario 7 got 401, as intended).

Fixed after the run (chat page only): the connection chip showed raw SDK status numbers
("Connection: 1") and its `/connected/` test would have shown "disconnected" as connected; it now
shows the SDK enum name and is green only for exactly "connected". Decided cards now drop their
Approve / Reject buttons instead of keeping them greyed out, on every copy of the card (an
at-least-once re-send can post the same request card twice; each copy is closed by the outcome).

# Live end-to-end test 2: the `/try` playground (2026-11-01 UTC)

Deployment: same host, code `532ebf9` (the stale-session fix below), `TRY_MODE=on`, Nexconn **live** (SG),
playground payments on the built-in simulated checkout (no PayPal account, no money). Two separate headless
Chromium browsers played two strangers: an **owner** who opened `/try` and a **teammate** who opened the owner's
invite link. Each became a fresh Nexconn user (registered by the server with the Platform Chat API) in the
sandbox's own private group; both connected with the real Web Chat SDK. Decisions reached the server only
through Nexconn's signed `message:send` webhook. Script: `live_try_e2e.py` (kept outside the repository);
no secret values were printed or recorded.

| # | Scenario | What happened | Result |
|---|----------|---------------|--------|
| 1 | Start a sandbox | one click → own sandbox, chat mode `live` ("Nexconn live · SG"), Connected, welcome post from the agent's bot user | PASS |
| 2 | Invite a teammate | the invite link joined a second Nexconn user to the same private group (roles owner + teammate) | PASS |
| 3 | Over the per-order cap: "Buy a 27-inch 4K monitor" ($329 > $50) | approval card delivered live to **both** browsers | PASS |
| 4 | Teammate approves in chat | Approve → Nexconn → signed webhook → `APPROVED`, simulated order created; outcome "approved … (decided by Live teammate in chat)" in the room; owner's guide ticked "decided" within 1.8 s | PASS |
| 5 | 36 concurrent `GET /try/state` with the owner's session (the path of the stale-session fix) | 36 × 200, session still valid | PASS |
| 6 | Hard rule: "Buy a $100 gift card" | refused (blocked merchant, category not allowed); no card, no order | PASS |
| 7 | Daily cap after the approval: "Buy 2 USB-C charging cables" ($25.98, under the per-order cap) | today's spend was already $329 of $150, so it became a new approval card ("daily spend would be 354.98 … over daily cap 150.00") instead of an automatic order — the daily cap holds even after a human approved a bigger purchase. The same request in a fresh sandbox was bought right away (order `PAYER_ACTION_REQUIRED`, no card) | PASS |
| 8 | Late typed decision | teammate typed `reject APR-…` after approving → "approval is already APPROVED; nothing changed" | PASS |
| 9 | Pay | simulated checkout in the same tab → back on `/try`, sandbox resumed, guide ticked "paid" | PASS |
| 10 | Reset | orders and spend cleared, room and both members kept; the teammate's session still valid | PASS |

Afterwards the three test sandboxes were expired with the operator CLI (`python -m app.playground.admin
expire-now --id …`): they are marked expired (their sessions and invite links stop working) and their databases on the
server are deleted. The Nexconn users and groups they created, and those groups' messages, stay on Nexconn's side
(the playground does not delete them; see `docs/PLAYGROUND.md`). Nothing in the main approval
room (`grbroom`) or its database changed. `journalctl -u guardrail` showed only 200s for Nexconn's webhook
deliveries during the run and no errors.

Six Nexconn users have been registered by the playground so far, all by these tests and one earlier smoke test (test accounts, not independent users; counter in the playground registry);
the playground caps live registrations at 500 (`TRY_MAX_LIVE_USERS`) and falls back to the offline stand-in
after that, well inside the free plan's 2,000 monthly active users.
