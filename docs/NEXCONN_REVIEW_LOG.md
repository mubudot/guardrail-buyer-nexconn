# Nexconn chat channel — review log

Independent reviews by OpenAI Codex (gpt-6.1-sol, high reasoning) of the `nexconn` branch diff against the
PayPal submission (`25627cc`). Every material finding was fixed and covered by a regression test.

| Pass | Commit reviewed | Findings | Fixed in |
|---|---|---|---|
| 1 | c0b49b6 | 1 high, 9 medium, 2 low | 4aabf53 |
| 2 | 4aabf53 | 1 high, 2 medium, 1 low | 10e03f4 |
| 3 | 10e03f4 | 1 medium | b0fcdd8 |
| 4 | b0fcdd8 | 2 medium | (this commit) |

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
