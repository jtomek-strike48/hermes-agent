# Implementation Report: Morning Brief

## Summary
Added an opt-in daily digest (`agent/morning_brief.py`) that composes open loops from the three shipped subsystems — stalled candidates, open/overdue kanban cards, best-effort recent Omi — synthesizes them into one source-attributed message via the aux LLM (with deterministic fallback), and delivers through the notification budget governor once per day. Exposed via `hermes brief {show,send,enable,disable}`. Pure composition — no new detection, no new state table.

## Assessment vs Reality
| Metric | Predicted (Plan) | Actual |
|---|---|---|
| Complexity | Medium (high reuse) | Medium — matched |
| Confidence | 8/10 | Accurate; live verification caught one real idempotency bug (as anticipated) |
| Files Changed | 7 | 7 (2 created, 5 modified) |

## Tasks Completed
| # | Task | Status | Notes |
|---|---|---|---|
| 1 | Config section | ✅ Complete | `morning_brief` in DEFAULT_CONFIG + `_KNOWN_ROOT_KEYS` + example |
| 2 | Brief module | ✅ Complete | Deviated — added an explicit already-delivered guard (live-bug fix, below) |
| 3 | CLI (`brief`) | ✅ Complete | show/send/enable/disable; mirrors `threads`/`omi` |
| 4 | Opt-in cron guidance | ✅ Complete | prints real `hermes cron create '0 7 * * *'` |
| 5 | Tests | ✅ Complete | 20 tests |

## Validation Results
| Level | Status | Notes |
|---|---|---|
| Static Analysis (ruff) | ✅ Pass | all Python files (the `.yaml.example` isn't ruff-lintable — excluded) |
| Unit Tests | ✅ Pass | 20 new |
| Regression | ✅ Pass | 109 across proactive features, zero regressions |
| Build/import | ✅ Pass | CLI wired; enable/disable round-trips |
| **Live-data verification** | ✅ Pass | Real profile: 8 items rendered w/ source attribution; caught + fixed a re-send bug |

## Files Changed
| File | Action | Lines |
|---|---|---|
| `agent/morning_brief.py` | CREATED | ~300 |
| `tests/agent/test_morning_brief.py` | CREATED | ~290 |
| `hermes_cli/config.py` | UPDATED | +~16 |
| `cli-config.yaml.example` | UPDATED | +~18 |
| `hermes_cli/subcommands/notify.py` | UPDATED | +~28 |
| `hermes_cli/notify_cmd.py` | UPDATED | +~110 |
| `hermes_cli/main.py` | UPDATED | +~12 |

## Deviations from Plan
1. **Explicit already-delivered guard in `_deliver` (live-bug fix).** The plan assumed the governor's per-day `candidate_id` idempotency prevented double-*sending*. Live verification disproved this: the governor's "idempotent-replay" returns the SAME decision (`allow=True`) for a repeated candidate — which re-sends. A second `brief send` the same day double-posted (budget went 1→2). Fixed: `_deliver` now checks `find_notification_by_candidate` for an already-`allowed` brief today and skips the resend before calling the governor. Re-verified live: second send returns `delivered=0`, budget holds. Added `test_already_delivered_today_not_resent` (uses the real governor, not a stub, so it actually exercises the ledger). **This corrects a latent assumption that also affects how future once-per-day producers should use the governor.**

## Issues Encountered
1. **Governor idempotency ≠ no-resend** — see deviation #1. Root cause: `should_deliver`'s replay is about not double-counting budget, not about suppressing a repeat action. Any producer that performs a real side-effect (send/post) on `allow` must guard resends itself.
2. **ruff on non-Python file** — including `cli-config.yaml.example` in the ruff args produced 75 spurious "errors"; ruff passes on the actual Python files. No code issue.

## Tests Written
| Test File | Tests | Coverage |
|---|---|---|
| `tests/agent/test_morning_brief.py` | 20 | consent/dry-run, gather composition + dedup + max-items + fail-soft, omi off/None, kanban overdue (real tmp DB), synthesis (LLM + 2 fallback paths), empty-suppression, force, governor allow/suppress, **already-delivered idempotency**, top-level fail-soft |

## Live-Data Verification
- `hermes brief show` on the real profile rendered 8 items with correct source attribution ("From your board", "From Omi"), pulling the 3 real kanban cards (correctly `blocked`, not falsely overdue) + 5 real Omi conversation titles (live `get_conversations` + double-decode worked), synthesized by the real aux LLM.
- Standalone (no gateway) execution worked — no silent-zero.
- Caught the double-send bug; after fix, second same-day `send` correctly skips (`delivered=0`, budget unchanged).
- Real profile left DISABLED (opt-in default restored).

## Next Steps
- [ ] Code review via `/code-review`
- [ ] Commit + PR to fork (rebase onto fork/main first, per prior features)
- [ ] Future: calendar section (shell out to google-workspace skill when configured) — documented v2 extension
