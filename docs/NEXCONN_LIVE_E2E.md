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
