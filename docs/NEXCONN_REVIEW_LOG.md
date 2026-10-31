# Nexconn chat channel — review log

Independent reviews by OpenAI Codex (gpt-6.1-sol, high reasoning) of the Nexconn work (the diff from the PayPal version, commit `25627cc`) against the
PayPal submission (`25627cc`). Every material finding was fixed and covered by a regression test.

| Pass | Commit reviewed | Findings | Fixed in |
|---|---|---|---|
| 1 | 542ade7 | 1 high, 9 medium, 2 low | 1c82dc3 |
| 2 | 1c82dc3 | 1 high, 2 medium, 1 low | 49fe352 |
| 3 | 49fe352 | 1 medium | aedb2b3 |
| 4 | aedb2b3 | 2 medium | (this commit) |

**Pass 1.** Open `DEMO_MODE` could hand out live Nexconn approver tokens → live session/relay need a real
approver token. Webhook nonces were process-local → stored in SQLite with claim/done states. A failed handler
consumed its message → inbound claims (PROCESSING/DONE) with release on failure and takeover when stale.
Webhook ran sync work on the event loop → thread pool. Queued posts lost on restart and could be sent twice →
atomic claim per post, recovery on start, locked worker creation, drain on shutdown. Deliveries truncated at 50
items → all items. Chat page: typed decisions not relayed, relay errors ignored, failed connect hid the form →
fixed. Listener snapshot outside the exception boundary; group bootstrap could silently omit approvers → fixed.

**Pass 2.** The signature covers `nonce + timestamp` concatenated, so a used pair could be re-split
(`"abc0","T"` → `"abc","0T"`) → canonical timestamps and the signed string as the replay key. A message being
handled was acknowledged as a duplicate → 409 so Nexconn retries, and half-done decisions are finished from
their stored claim. Interrupted posts were only recovered on the next restart → the poster thread recovers
abandoned work every 30 s. Docs overstated the room as a complete audit trail → scoped to approvals.

**Pass 3.** A crash between a decision's commit and its listener left the card open forever → recovery posts
the committed outcome rebuilt from the stored approval / order.

**Pass 4.** Concurrent recovery could post an outcome twice → unique outcome per approval (`INSERT OR IGNORE`
on a partial unique index). Recovery could credit a refused or replayed chat decision → only a chat message
recorded as having made the decision is credited; otherwise the post says it was decided earlier and posted
after a restart.

## After the live end-to-end test (docs/NEXCONN_LIVE_E2E.md)
- Live review 1 (commit 7446886: SDK status names on the connection chip, decided cards drop their buttons): no substantive issues.
- Live review 2 (webhook / channel / chat page against the live facts): 2 findings, both fixed in 59065d7 — a re-sent request card (same approval, new message id) kept its buttons after the outcome (P2; every copy is now closed), and the E2E doc said every webhook got 200 although the forged ones got 401 (P3).
- Live review 3 (verifying 59065d7): no substantive issues.
