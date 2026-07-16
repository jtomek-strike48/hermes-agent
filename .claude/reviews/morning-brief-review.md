# Code Review: Morning Brief

**Reviewed**: 2026-07-16
**Branch**: `feat/morning-brief` (local, uncommitted)
**Mode**: Local review (python-reviewer on the new module + self-verification)
**Decision**: APPROVE (fixes applied)

## Summary
New opt-in daily digest composing the three shipped subsystems. The specialist review + live-data verification each surfaced real issues; every load-bearing finding was verified against the code (several the reviewer self-corrected), the genuine ones fixed.

## Validation Results
| Check | Result |
|---|---|
| Lint (ruff) | Pass (Python files) |
| Unit tests | Pass (22, +3 review/live-driven) |
| Regression | Pass (109 across proactive features) |
| Live-data verification | Pass — caught + fixed a double-send bug |

## Findings

### CRITICAL / correctness (fixed)
- **Double-send on repeat (found by LIVE verification, not the reviewer).** The plan assumed the governor's per-day `candidate_id` was idempotent-against-sending; it is not — `should_deliver` replays `allow=True`, which re-sends. A second same-day `brief send` double-posted (budget 1->2). FIXED: `_deliver` now checks `find_notification_by_candidate` for an already-`allowed` brief today and skips before sending. Re-verified live: second send `delivered=0`, budget held. Regression test `test_already_delivered_today_not_resent` (uses the real governor).
- **Idempotency-guard TOCTOU race (reviewer CRITICAL).** Verified real but low-impact: check->send isn't atomic, so two near-simultaneous runs could both send (at-most-twice). For a single-process daily cron this never triggers; a hard fix (DB unique constraint) isn't worth it for a daily digest. FIXED by honesty: corrected the docstring's "idempotent per day" overclaim to state the real best-effort guarantee + the residual concurrent-fire caveat.

### HIGH (fixed)
- **`temperature=0.3` inconsistency.** The other two aux modules use `temperature=0`. Changed to `0` for determinism/consistency.

### MEDIUM (fixed / accepted)
- **`str(title)` before truncation** — pathological-size guard. FIXED -> `str(title or "")[:200].strip()`.
- **Calendar section log level** — a user who explicitly adds `calendar` should see it. FIXED -> `logger.warning`.
- **Payload truncation at 12000 can corrupt JSON.** Accepted — falls back cleanly to deterministic render, and `max_items` already bounds the list. Fail-soft as designed.
- **Idempotency-check `except` at debug.** Accepted — a broken state.db degrading the guard is preferable to a hard failure blocking the daily brief; the fail-soft posture is deliberate.

### Rejected (verified false alarms — several self-corrected by the reviewer)
- **"exception escapes `render_brief` on SessionDB failure"** — reviewer self-corrected: the `SessionDB(...)` call IS inside the try. Confirmed.
- **"`_gather_omi` KeyError/AttributeError on malformed dicts"** — self-corrected + I stress-tested 8 malformed shapes (title as nested dict, structured as str/list, empty, non-dict item): all safe via `.get()` + `isinstance` guards. Added 2 regression tests anyway.
- **LOW code-smells** (redundant `_cfg` isinstance, dedup source-priority, fallback group order, redundant `or` safety) — style/future-config notes, no behavior impact. Skipped to keep the diff focused.

## Verified-correct (no change)
- All top-level functions fail-soft (run/render/_gather/_synthesize/_deliver).
- max_items applied AFTER dedup; min_items_to_send suppression; force overrides both.
- `render_brief` (dry-run) never calls governor/send.
- Fallback guaranteed non-empty when items exist.
- value_hint scales with item count, capped at 1.0.

## Files Reviewed
| File | Change |
|---|---|
| `agent/morning_brief.py` | Added — idempotency guard + temp + str + log + docstring fixes applied |
| `tests/agent/test_morning_brief.py` | Added (22 tests) |
| `hermes_cli/config.py`, `cli-config.yaml.example` | config section |
| `hermes_cli/subcommands/notify.py`, `notify_cmd.py`, `main.py` | `brief` CLI (mirrors reviewed threads/omi) |

## Decision
APPROVE. The real double-send bug (caught live) is fixed and tested; the residual TOCTOU race is documented and acceptable for a daily cron; validation green.
