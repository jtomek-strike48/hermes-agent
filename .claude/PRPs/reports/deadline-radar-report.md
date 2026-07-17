# Implementation Report: Deadline Radar

## Summary
Added an opt-in, forward-looking proactive detector (`agent/deadline_radar.py`)
that nudges you BEFORE a commitment's due date lapses. It scans open kanban
cards whose `Due:` falls inside a `lead_time_hours` window ahead of now (due
soon, but NOT yet past due — past-due is `stalled_threads`' job), batches them
into ONE digest through the notification budget governor (category
`deadline_radar`), and dedups each card via the shared `stalled_nudges` ledger
under a `deadline:` candidate_id namespace. Exposed via
`hermes radar {scan,list,enable,disable}`. Pure composition of shipped
primitives — no new detection engine, no new state table.

## Rationale
Every shipped proactive detector is retrospective: `stalled_threads` fires on
past-due / quiet items; the morning brief flags cards ALREADY overdue. Nothing
warned before a deadline lapsed. Deadline radar fills that prospective gap on a
distinct detection axis (`now < due <= now + lead_time`) while reusing all the
existing plumbing (kanban DB, `_parse_due`, governor, `stalled_nudges`, CLI).

## Files Changed
| File | Action | Lines |
|---|---|---|
| `agent/deadline_radar.py` | CREATED | ~270 |
| `tests/agent/test_deadline_radar.py` | CREATED | ~290 (22 tests) |
| `hermes_cli/config.py` | UPDATED | +18 (`deadline_radar` block + `_KNOWN_ROOT_KEYS`) |
| `cli-config.yaml.example` | UPDATED | +18 (documented section) |
| `hermes_cli/notify_cmd.py` | UPDATED | +107 (`radar_command` + `_radar_*` + job guidance) |
| `hermes_cli/subcommands/notify.py` | UPDATED | +26 (`build_radar_parser`) |
| `hermes_cli/main.py` | UPDATED | +12 (import, `cmd_radar`, builder call, 2 allow-lists) |

## Design Decisions
1. **Per-item cooldown, not per-day idempotency.** Unlike the morning brief
   (one blanket message per day via `find_notification_by_candidate`), the radar
   nudges each approaching card once per `cooldown_hours` because different cards
   cross the lead-time boundary at different times. It reuses `stalled_threads`'
   `stalled_nudges` ledger with a distinct `deadline:card:<id>` id namespace, so
   it never collides with the stalled detector's `card:`/`thread:` rows.
2. **No aux LLM.** The source is a single local DB with a deterministic
   predicate; there is nothing to classify (contrast `stalled_threads`, which
   uses the LLM for the open-vs-resolved thread heuristic). Simpler + no model
   dependency.
3. **Urgency-scaled `value_hint`.** The soonest deadline drives value:
   ~1.0 for an imminent card, decaying to ~0.4 at the 48h mark, bounded to the
   governor's [0,1]. So an imminent deadline is judged more valuable than a
   distant one.

## Validation Results
| Level | Status | Notes |
|---|---|---|
| Static (ruff) | PASS | all 6 changed Python files |
| Unit tests | PASS | 22 new |
| Regression | PASS | 105 across stalled/omi/governor/brief/radar suites |
| Config smoke | PASS | `deadline_radar` loads; in `_KNOWN_ROOT_KEYS` |
| CLI wiring | PASS | `hermes radar` dispatches (usage banner like brief/threads) |
| **Live-data verification** | PASS | see below |

## Live-Data Verification (mandatory)
- Opt-in gate: `radar list` / `radar scan` on the real profile (default
  disabled) correctly refused with the consent message.
- `radar enable` + `radar list`: surfaced a REAL card — "Prepare TE demo
  environment", due in 10h — proving the kanban → `_parse_due` (prose `Due`
  body) → window → lead-render path works on live data.
- Governed send: `radar scan` #1 → `delivered=1` (real home-channel message);
  attention budget went 2 → 3 (exactly +1).
- **Idempotency:** `radar scan` #2 → `candidates=0 delivered=0` (card in
  cooldown); budget held at 3 — NO double-send (the class of bug live
  verification caught in morning-brief; clean here by design).
- Governor learned the category (`deadline_radar`: threshold 0.5, sent_count 1)
  → `notify keep/mute deadline_radar` feedback works with no extra wiring.
- Restored opt-in default (`radar disable`; `enabled = False`).

## Next Steps
- [ ] Code review (python-reviewer) — address CONFIRMED findings
- [ ] Commit + PR to fork (branch off, mirror prior features' #1–#4 flow)
