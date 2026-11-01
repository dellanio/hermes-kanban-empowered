# Live acceptance, v0.1

Observed at the factory (Windows, 17 profiles) on 2026-10-31 and 2026-11-01.

| Item | Result | Evidence / notes |
|---|---|---|
| Reminder on schedule | PASS | Test board `ke-aceite`, card `t_1fca668b` blocked at 23:38; first reminder (stage `reminder1`) sent 30 min later (Telegram msg 6823). |
| Telegram reply "feito" (idle session) | PASS | Card unblocked; comment "unblocked via Telegram by Dell (6802889854): feito"; audit action `human_done`. |
| Pinned live panel | PASS | Published and edited in place (msg 6813). Recreation after manual deletion: NOT TESTED. |
| `explain` on the live system | PASS | Blocked card: dependencies, guard, live, fresh and wait rows. Child-of-blocked root case: NOT TESTED live (unit tests cover it). |
| Resilient Telegram transport | PASS | The live network to `api.telegram.org` fails intermittently (WinError 10054, TLS handshake timeouts; Hermes' native adapter sees them too). Since the 2026-11-01 03:32 install the plugin sends via the fallback IPv4 `149.154.166.110` with no send failures; `doctor` shows `telegram_path`. |
| Message format v2 and brand line on live Telegram | PASS | Msg 6840 (03:40) and panel edit (03:41). Owner visual validation pending. |
| Veto hooks | PASS (inert) | Patch applied to the installed Hermes and active after the 03:40 gateway restart (`doctor`: `veto_hooks` present). No consumer in v0.1, by design. |

## Not tested live (unit-tested; follow-ups)

- Structured-ask refusal by a real agent (`kanban_block` without Need / Why / How to reply).
- Reply path while the owner's agent session is busy.
- Replies from a stranger, and replayed or expired replies.
- Warning shown when the core patch is removed.
- Panel recreation after the pinned message is deleted by hand.
- `explain` for a child of a blocked root, on the live board.
