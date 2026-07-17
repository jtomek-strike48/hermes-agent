# Code Review: Deadline Radar

**Reviewed**: 2026-07-16
**Branch**: `feat/deadline-radar` (local)
**Mode**: python-reviewer on the new module + tests, with live-data verification
**Decision**: APPROVE (one informational fix applied)

## Summary
New opt-in, forward-looking proactive detector — the prospective complement to
`stalled_threads`. The reviewer traced all 5 load-bearing correctness claims
against the actual code and confirmed each; no CRITICAL/HIGH/MEDIUM issues. One
informational hardening (explicit empty-items guard in `_deliver_digest`) was
applied even though the invariant already held at both call sites.

## Validation Results
| Check | Result |
|---|---|
| Lint (ruff) | Pass (6 Python files) |
| Unit tests | Pass (23, +1 for the applied guard) |
| Regression | Pass (105 across proactive suites) |
| Live-data verification | Pass — real card surfaced, governed send once, second scan deduped (no double-send) |

## Findings

### VERIFIED CORRECT (reviewer traced each in code)
- **Due-soon window** (`now < due <= now + lead_time`): past-due and
  beyond-horizon cards correctly excluded; both boundaries checked. 8 boundary
  tests.
- **Fail-soft**: both public entry points catch all exceptions; each source
  gather is individually guarded with DB close in `finally`; `_deliver_digest`
  returns False on any error. No exception reaches the CLI/cron caller.
- **Idempotency / namespace**: `deadline:card:<id>` cannot collide with
  `stalled_threads`' `card:`/`thread:` rows in the shared `stalled_nudges`
  ledger; cooldown dedup verified live (second scan → 0 nudged, budget held).
- **`value_hint` bounded [0.4, 1.0]**: `max(0.4, min(1.0, ...))` cannot escape;
  urgency scales with the soonest deadline.
- **No injection/secret leak**: card titles (user data) are embedded as
  plain text into a governed notification — no HTML/eval/shell; downstream
  channels handle their own rendering.

### Applied (informational, not a blocker)
- **Empty-items guard in `_deliver_digest`.** The urgency `min(... for it in
  items)` would raise `ValueError` on `[]`. Already safe (two call-site guards
  guarantee non-empty + the exception handler catches it), but added an explicit
  `if not items: return False` so the invariant is self-documenting and
  future callers can't trip it. Covered by
  `test_deliver_digest_empty_items_returns_false`.

### Not changed (accepted)
- Negative `lead_time_hours` would just yield empty results (fail-soft); config
  defaults are sane. Not worth a validation branch.

## Decision
APPROVE. Clean, mirrors the shipped stalled/brief patterns, validation green,
live-verified end-to-end including the double-send class of bug (clean here by
per-item cooldown design).
