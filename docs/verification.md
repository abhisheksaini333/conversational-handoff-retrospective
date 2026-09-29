# Verification ledger

Verification date: 2026-09-29.

| Check | Status |
|---|---|
| Core state and persistence tests | 18 passed |
| HTTP authentication/validation/retry tests | 7 passed |
| Rasa resume-event shape tests | 2 passed |
| Historical Rasa 2.8.14 runtime | Version command executed; Python 3.8.10, SDK 2.8.2 |
| NLU/Core training | Completed using original synthetic training data |
| Real Rasa integration demo | 12 checks: interruption, acknowledgement, durable pause after process restart, guarded user restart, resume, completion, replay, failure and retry |
| Held-out natural-language evaluation | 8 development + 8 held-out utterances; baseline/calibrated routing accuracy 0.625, deployed threshold routing accuracy 0.5, intent macro F1 0.435; calibration did not improve held-out routing |
| GitHub CI | Not yet published/run |

`evidence/tdd-red.txt` and `evidence/http-red.txt` record initial failures before implementation. The first training attempt used an incompatible classifier/featurizer combination; the pipeline was corrected to DIETClassifier with sparse count features and trained successfully. `evidence/tests.log` contains the current passing test run.

A real Rasa integration regression was reproduced: restoring the form with a followup action skipped validation of the next user message. Rasa 2 requires the last action to be `action_listen` for this validation path. The resume event sequence now restores listening state; the complete form journey passes. The regression test and real-service demo cover this behavior.

Review also reproduced lost pause state after a Rasa process restart and stale context on a second handoff. Persistent SQL trackers and explicit transcript refresh address both. The integration demo restarts Rasa, sends two consecutive `/restart` messages during human ownership, verifies continued silence, completes the ticket, then verifies a normal restart works. The guarded restart schedules `action_listen` to stop further policy prediction. Session expiry is disabled. Core tests distinguish omitted context from an explicit empty transcript in replay fingerprints.
