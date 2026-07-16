# Plan: Morning Brief (actionable daily digest)

## Summary
A once-a-day, opt-in digest that aggregates the user's open loops into ONE concise, source-attributed message to the home channel. It does NOT re-implement detection — it composes the shipped subsystems: stalled/open-loop candidates (`stalled_threads.list_stalled_candidates`), open + overdue kanban cards, and best-effort recent Omi context, then synthesizes them into short grouped prose via the aux LLM and delivers through the notification budget governor (category `morning_brief`, one per day). `hermes brief {show,send,enable,disable}`.

## User Story
As a busy operator, I want one short "here's your day" message each morning that names where each item came from, so I start the day oriented without opening five tools — and without being spammed on empty days.

## Problem → Solution
**Current state:** Mercury has three proactive detectors but each pings independently; there's no single daily orientation. → **Desired state:** one governed, source-attributed digest per day that composes what already exists, suppressing itself when there's nothing worth saying.

## Metadata
- **Complexity**: Medium (high reuse; no new state table, no new detection). ~7 files.
- **Source PRD**: N/A (research idea #7)
- **Estimated Files**: 7 (1 new module, `hermes_cli/config.py`, `cli-config.yaml.example`, `subcommands/notify.py`, `notify_cmd.py`, `main.py`, 1 test file)

---

## UX Design

### After
```
┌──────────────────────────────────────────────────────────────┐
│ daily cron (opt-in) OR `hermes brief send`                     │
│   GATHER (each source fail-soft, contributes 0 on error):      │
│     • stalled_threads.list_stalled_candidates() -> open loops  │
│     • kanban open + overdue cards (reuse _parse_due)           │
│     • Omi recent conversations (best-effort, only if omi up)   │
│     • calendar -> NOT in v1 (no native source; see NOT Building)│
│        ↓ (skip if total items < min_items_to_send, unless force)│
│   SYNTHESIZE via aux LLM -> greeting + grouped attributed lines │
│     (fallback: deterministic plain-text render if LLM down)    │
│        ↓                                                        │
│   should_deliver("morning_brief", candidate_id=f"brief:{day}") │
│        ↓ ALLOW                                                 │
│   send_message_tool -> home channel                            │
│                                                                │
│   "Good morning. 3 things today:                               │
│    From your commitments: Send the deck to Sarah (due today)   │
│    Awaiting your reply: @josh in #proj (3d)                    │
│    From Omi: you mentioned prepping the TE demo"               │
└──────────────────────────────────────────────────────────────┘
```
`hermes brief show` = dry-run: renders the same digest to stdout, always (ignores min-items + governor + send).

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| Daily orientation | none | one governed digest | opt-in cron |
| Preview | none | `hermes brief show` (dry-run, no send) | |
| Manual trigger | none | `hermes brief send` | governed + idempotent per day |
| Control | none | `hermes brief enable/disable`; `hermes notify mute morning_brief` | reuses governor feedback |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `agent/stalled_threads.py` | 1-100, 392-440 | The module to mirror (structure, fail-soft `_impl` wrapper, `_parse_due`, `list_stalled_candidates` return shape) |
| P0 | `agent/omi_commitments.py` | 60-121, 129-160, 348-393 | `_maybe_json`/`_ensure_mcp_ready`/`_call_mcp`, aux-LLM call block, `_notify` (governor + send) |
| P0 | `agent/notification_budget.py` | `should_deliver` | Deliver the brief through this; category `morning_brief` |
| P0 | `hermes_cli/kanban_db.py` | 839-852, 2725-2774, 8709-8752 | `Task` (created_at epoch INT, no due col), `list_tasks`, `_to_epoch` |
| P1 | `hermes_cli/notify_cmd.py` | 183-286 | `threads_command`/`_threads_*`/`_register_threads_job` trio to mirror |
| P1 | `hermes_cli/subcommands/notify.py` | 61-83 | `build_threads_parser` to mirror |
| P1 | `hermes_cli/config.py` | 2292-2303, 5461 | `stalled_threads` config block + `_KNOWN_ROOT_KEYS` line |
| P1 | `hermes_cli/main.py` | 300, 4296-4300, 13350-13351, 11366, 12580 | import, `cmd_threads` wrapper, builder call, 2 allow-lists |
| P2 | `tests/agent/test_stalled_threads.py` | all | Test structure to mirror |

## External Documentation
None — internal patterns only. No external research needed.

---

## Patterns to Mirror

### FAIL_SOFT_WRAPPER (public fn delegates to _impl; top-level guard)
```python
// SOURCE: agent/stalled_threads.py:275-297 (run_stalled_thread_scan pattern)
def run_morning_brief(force: bool = False) -> Dict[str, Any]:
    try:
        return _run_morning_brief_impl(force=force)
    except Exception as exc:
        logger.error("morning_brief: run failed: %s", exc, exc_info=True)
        return {"error": str(exc), "delivered": 0}
```

### DYNAMIC_DB_PATH (resolve at call time — profile/test safe)
```python
// SOURCE: agent/stalled_threads.py (module-level import + call-time use)
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
# ... db = SessionDB(get_hermes_home() / "state.db")
```

### STALLED_SOURCE (reuse the dry-run — do NOT re-detect)
```python
// SOURCE: agent/stalled_threads.py:392 — returns {"candidates":[{candidate_id,kind,text[,task_id]}]}
from agent.stalled_threads import list_stalled_candidates
res = list_stalled_candidates()
open_loops = res.get("candidates", []) if "error" not in res else []
```

### KANBAN_OPEN + DUE (reuse _parse_due; NO due column; epoch INT)
```python
// SOURCE: hermes_cli/kanban_db.py:2725 + stalled_threads._parse_due
from hermes_cli import kanban_db as kb
from agent.stalled_threads import _parse_due
conn = kb.connect(board=board or None)
try:
    cards = [t for t in kb.list_tasks(conn, include_archived=True)
             if t.status in {"triage","todo","ready","scheduled","blocked"}]
finally:
    conn.close()
# overdue: due = _parse_due(t.body); due and due < now  (now = time.time())
```

### OMI_BEST_EFFORT (only get_conversations is verified; others defensive)
```python
// SOURCE: agent/omi_commitments.py:79-121 — reuse verbatim via import, don't re-dispatch raw
from agent.omi_commitments import _ensure_mcp_ready, _call_mcp
if _ensure_mcp_ready():
    convs = _call_mcp("get_conversations", {"limit": 10})  # -> list | dict | None
    # _call_mcp already double-decodes + returns None on error/absent tool
```

### AUX_LLM_SYNTHESIS (mirror; label "morning_brief"; fallback if unavailable)
```python
// SOURCE: agent/omi_commitments.py:129-160
from agent.auxiliary_client import get_auxiliary_extra_body, get_text_auxiliary_client
client, model = get_text_auxiliary_client("morning_brief")
if client is None or not model:
    return _render_fallback(items)   # deterministic plain-text — never fail with items present
resp = client.chat.completions.create(
    model=model,
    messages=[{"role":"system","content":_BRIEF_SYSTEM_PROMPT},
              {"role":"user","content":json.dumps(items, ensure_ascii=False)[:12000]}],
    temperature=0, max_tokens=1024, extra_body=get_auxiliary_extra_body() or None)
text = resp.choices[0].message.content or ""
```

### GOVERNED_SEND (one message, per-day candidate_id)
```python
// SOURCE: agent/omi_commitments.py:348-393 / stalled_threads.py:242-276
from agent.notification_budget import should_deliver
day_key = time.strftime("%Y-%m-%d")
decision = should_deliver(category="morning_brief", value_hint=<0..1>,
                          candidate_id=f"brief:{day_key}")
if decision.allow:
    from tools.send_message_tool import send_message_tool
    send_message_tool({"message": brief_text})
```

### CLI_TRIO (mirror threads exactly)
```python
// SOURCE: hermes_cli/subcommands/notify.py:61 + notify_cmd.py:183 + main.py:4296
def build_brief_parser(subparsers, *, cmd_brief):
    p = subparsers.add_parser("brief", help="...")
    sub = p.add_subparsers(dest="brief_action")
    sub.add_parser("show"); sub.add_parser("send"); sub.add_parser("enable"); sub.add_parser("disable")
    p.set_defaults(func=cmd_brief)
```

### CONFIG_ATOMIC_ENABLE (reuse the atomic write from _threads_set_enabled)
```python
// SOURCE: hermes_cli/notify_cmd.py:241 (_threads_set_enabled)
from hermes_cli.config import atomic_config_write, get_config_path, read_raw_config, load_config
on_disk = read_raw_config() or {}
section = dict(on_disk.get("morning_brief", {})); section["enabled"] = enabled
on_disk["morning_brief"] = section
atomic_config_write(get_config_path(), on_disk, sort_keys=False)
```

### TEST_STRUCTURE
```python
// SOURCE: tests/agent/test_stalled_threads.py:14-46 (cfg_patch + autouse HERMES_HOME)
@pytest.fixture
def cfg_patch():
    def _apply(**o): return patch.object(mb, "_cfg", return_value=_cfg(**o))
    return _apply
```

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `agent/morning_brief.py` | CREATE | `run_morning_brief(force)`, `render_brief()` (dry-run), gather/synthesize/deliver helpers |
| `hermes_cli/config.py` | UPDATE | `morning_brief` section in DEFAULT_CONFIG + `_KNOWN_ROOT_KEYS` |
| `cli-config.yaml.example` | UPDATE | document `morning_brief` |
| `hermes_cli/subcommands/notify.py` | UPDATE | `build_brief_parser` |
| `hermes_cli/notify_cmd.py` | UPDATE | `brief_command` + `_brief_{show,send,set_enabled}` + `_register_brief_job` |
| `hermes_cli/main.py` | UPDATE | `cmd_brief` + builder call + `"brief"` in 2 allow-lists |
| `tests/agent/test_morning_brief.py` | CREATE | unit tests |

## NOT Building
- **Calendar in v1.** Verified: the ONLY calendar reader is the Google-Workspace *skill CLI* (`google_api.py calendar list`), OAuth-gated, invoked by shelling out — not a tool/MCP/local store. A subprocess+OAuth dependency is too heavy and fragile for v1. Documented as a clean future extension: add a `calendar` section that shells out to the skill when the user has it configured. The `sections` config list leaves room for it.
- **New Omi tools.** `get_daily_summaries`/`get_action_items` are NOT verified in this repo — only `mcp__omi__get_conversations` exists. v1 uses `get_conversations` (best-effort) + the already-filed Omi kanban cards (reliable, via the kanban source). Any other Omi tool is called defensively via `_call_mcp` (returns None if absent) — never assumed.
- **New state table.** The governor's per-day `candidate_id=f"brief:{day_key}"` already guarantees at-most-once-per-day + idempotency on cron double-fire (verified: `should_deliver` returns the prior decision for a repeated candidate). No `morning_brief` table needed.
- **Re-implementing detection.** The brief READS `list_stalled_candidates()`; it does not re-scan threads or re-parse commitments beyond the kanban overdue check.
- **Per-item action buttons / Slack Block Kit.** v1 is plain attributed text (matches the existing `send_message_tool` path). Interactive affordances are a later enhancement.

---

## Gather / Synthesize / Deliver Semantics
1. Opt-in gate: `not cfg.enabled` (and not `force`/dry-run) → for `send`, `{"skipped":"disabled"}`. `show` always renders.
2. **Gather** (config `sections` toggles each; each wrapped so an error contributes `[]`):
   - `stalled` → `list_stalled_candidates()` candidates (already tagged `kind` commitment/thread).
   - `kanban` → open cards; mark overdue via `_parse_due(body) < now`; include a bounded count of today/overdue.
   - `omi` → if `_ensure_mcp_ready()`, `_call_mcp("get_conversations", {"limit": N})`, summarize titles only (best-effort; skip silently if None).
   - `calendar` → skipped (NOT building); if present in `sections`, log "calendar source not configured".
3. Cap total gathered to `max_items`; count items.
4. **Suppress**: for `send`, if `total_items < min_items_to_send` and not `force` → `{"skipped":"empty", "items":0}` (no send). `show` renders regardless.
5. **Synthesize**: aux LLM (`morning_brief` label) → greeting + grouped attributed lines. Untrusted-content note in the system prompt (no instruction-following). If client unavailable or call fails → `_render_fallback(items)` deterministic plain-text (grouped by source). Never return empty text when items exist.
6. **Deliver** (send path only): `should_deliver("morning_brief", candidate_id=f"brief:{day_key}", value_hint=…)`; if allow, `send_message_tool({"message": text})`.
7. Return `{"items", "delivered", "skipped"?}`. Fail-soft top-level.

---

## Step-by-Step Tasks

### Task 1: Config section (`hermes_cli/config.py` + example)
- **ACTION**: Add `morning_brief` to DEFAULT_CONFIG (after `stalled_threads`) + `_KNOWN_ROOT_KEYS`; document in `cli-config.yaml.example`.
- **IMPLEMENT**:
  ```python
  "morning_brief": {
      "enabled": False,
      "scan_interval_hours": 24,          # cron hint (guidance text)
      "sections": ["stalled", "kanban", "omi"],   # calendar omitted in v1
      "max_items": 10,
      "min_items_to_send": 1,             # skip empty days
      "omi_lookback_conversations": 10,
      "board": "",
  }
  ```
- **MIRROR**: CONFIG (the `stalled_threads` block).
- **GOTCHA**: `_deep_merge` ignores None-over-dict; keys auto-load.
- **VALIDATE**: `python -c "from hermes_cli.config import load_config as l; print(l()['morning_brief']['sections'])"`.

### Task 2: Brief module (`agent/morning_brief.py`)
- **ACTION**: Create `run_morning_brief(force=False) -> dict`, `render_brief() -> dict` (dry-run text), and helpers `_gather(cfg) -> list`, `_synthesize(items) -> str`, `_render_fallback(items) -> str`, `_deliver(text, items) -> bool`.
- **IMPLEMENT**: per Gather/Synthesize/Deliver. `_gather` calls the three sources, each in its own try. Kanban overdue reuses `_parse_due` from `stalled_threads`.
- **MIRROR**: FAIL_SOFT_WRAPPER, DYNAMIC_DB_PATH, STALLED_SOURCE, KANBAN_OPEN+DUE, OMI_BEST_EFFORT, AUX_LLM_SYNTHESIS, GOVERNED_SEND.
- **IMPORTS**: `json, logging, time`; `from hermes_cli.config import load_config`; `from hermes_cli import kanban_db as kb`; `from agent.stalled_threads import list_stalled_candidates, _parse_due`; `from agent.omi_commitments import _ensure_mcp_ready, _call_mcp`; `from agent.auxiliary_client import get_text_auxiliary_client, get_auxiliary_extra_body`; `from agent.notification_budget import should_deliver` (lazy, in `_deliver`); `from hermes_constants import get_hermes_home`.
- **GOTCHA**: NO MCP dependency for stalled/kanban — only the omi section needs `_ensure_mcp_ready`. Omi tools beyond `get_conversations` are unverified → call defensively, treat None as "no omi data". kanban `created_at` is epoch **INT**, `time.time()` is float — compare with a float `now`, fine for `<`; don't mix with any per-message float math (there is none here). `render_brief`/`show` must NOT call the governor or send. Importing `_parse_due`/`_ensure_mcp_ready` (underscore-prefixed) across modules is acceptable here — they're stable internal helpers we own; note the coupling in a comment.
- **VALIDATE**: unit tests (Task 5) + live check (validation section).

### Task 3: CLI (`subcommands/notify.py` + `notify_cmd.py` + `main.py`)
- **ACTION**: `build_brief_parser` (show/send/enable/disable); `brief_command` dispatch + `_brief_show` (prints `render_brief()` text), `_brief_send` (runs `run_morning_brief(force=False)`, prints summary), `_brief_set_enabled` (atomic config), `_register_brief_job`; `cmd_brief` wrapper + builder call + `"brief"` in both allow-lists.
- **MIRROR**: CLI_TRIO, CONFIG_ATOMIC_ENABLE, and `_register_threads_job` (real `hermes cron create '0 7 * * *' --name morning-brief --no-agent --script brief_scan.py` guidance).
- **GOTCHA**: `show` is the dry-run (always renders, no governor/send); `send` is governed. Reuse `atomic_config_write`.
- **VALIDATE**: `hermes brief show` runs on a fresh profile; `hermes brief enable` writes `morning_brief.enabled: true`.

### Task 4: Register opt-in cron (guidance)
- **ACTION**: `_register_brief_job` prints the daily-cron guidance (`0 7 * * *`).
- **MIRROR**: `_register_threads_job`.
- **VALIDATE**: guidance uses real positional-schedule cron syntax.

### Task 5: Tests (`tests/agent/test_morning_brief.py`)
- **ACTION**: Unit-test gather composition, empty-suppression, force/dry-run, governor routing, fallback render, fail-soft.
- **IMPLEMENT**: stub `list_stalled_candidates` (return canned candidates), stub kanban via a seeded tmp card, stub `_ensure_mcp_ready`/`_call_mcp` (omi off + on), stub `get_text_auxiliary_client` (present → prose; None → fallback), stub `should_deliver`.
- **MIRROR**: TEST_STRUCTURE.
- **VALIDATE**: `python -m pytest tests/agent/test_morning_brief.py -q`.

---

## Testing Strategy

### Unit Tests
| Test | Input | Expected | Edge? |
|---|---|---|---|
| disabled send skips | `enabled=False`, send | `{"skipped":"disabled"}`, no gather | ✓ |
| show renders even when disabled | `enabled=False`, show | non-empty text | ✓ |
| gather composes 3 sources | stalled+kanban+omi stubbed | items from all present | |
| empty suppressed on send | 0 items, `min_items_to_send=1` | `{"skipped":"empty"}`, no send | ✓ |
| force overrides empty | 0 items, force=True | attempts deliver | ✓ |
| overdue kanban included | card body past `Due:` | appears in items | |
| omi off (mcp not ready) | `_ensure_mcp_ready`→False | omi contributes 0, brief still built | ✓ |
| omi error | `_call_mcp`→None | graceful, 0 omi items | ✓ |
| synthesis prose | aux client present | uses LLM text | |
| synthesis fallback | aux client None | deterministic render, non-empty | ✓ |
| governor allow → send | should_deliver allow | delivered=1, one send | |
| governor suppress | should_deliver deny | delivered=0, no send | ✓ |
| per-day idempotency | same day candidate_id | second send replays prior decision | ✓ |
| source throws | `list_stalled_candidates` raises | that source 0, others still gather | ✓ |
| top-level fail-soft | DB open raises | `{"error":...}`, no crash | ✓ |

### Edge Cases Checklist
- [x] Empty (no items → suppressed unless force)
- [x] Max (`max_items` cap)
- [x] Invalid types (omi None / malformed; LLM down → fallback)
- [x] Permission/disabled (consent gate; show bypasses)
- [x] Timestamp (kanban epoch INT vs float now — no cross-source math)
- [x] Idempotency (per-day candidate_id)

---

## Validation Commands

### Static Analysis
```bash
cd /home/jtomek/.hermes/hermes-agent
python -m ruff check agent/morning_brief.py hermes_cli/config.py hermes_cli/notify_cmd.py \
  hermes_cli/subcommands/notify.py hermes_cli/main.py tests/agent/test_morning_brief.py
```
EXPECT: All checks passed. (Note: hook now only ruff-formats opt-in repos; this repo won't be reflowed — keep edits surgical.)

### Unit Tests
```bash
python -m pytest tests/agent/test_morning_brief.py -q -p no:cacheprovider
```
EXPECT: all pass. (Use nix `python -m pytest`, NOT `scripts/run_tests.sh`.)

### Config smoke
```bash
python -c "from hermes_cli.config import load_config as l; print(l()['morning_brief'])"
```

### Regression
```bash
python -m pytest tests/agent/test_stalled_threads.py tests/agent/test_omi_commitments.py tests/agent/test_notification_budget.py tests/gateway/test_delivery.py -q -p no:cacheprovider
```
EXPECT: no regressions.

### LIVE DATA VERIFICATION (mandatory — the recurring lesson)
```bash
# 1. Dry-run render against the REAL profile — confirm real items appear with
#    correct SOURCE attribution and sane text (this is where mocked bugs hide):
hermes brief show
# 2. Confirm the kanban overdue parse works on the real Omi cards
#    (prose 'Due …' bodies) — items should reflect their real due state:
hermes brief show | sed -n '1,40p'
# 3. Governed send end-to-end (standalone process, no gateway):
hermes brief enable && hermes brief send
hermes notify status     # confirm a morning_brief ledger entry
# 4. Idempotency: run send twice — second must NOT double-post:
hermes brief send        # expect suppressed/idempotent-replay
# 5. Restore opt-in default:
hermes brief disable
```
EXPECT: `show` lists real open loops + overdue cards with "From your commitments / Awaiting your reply / From Omi" attribution; `send` posts once to the home channel and the second `send` does not double-post; ledger shows the entry.

### Manual Validation
- [ ] `hermes brief show` renders a real, attributed digest from your live data
- [ ] Empty-day: with nothing open, `send` suppresses (no spam); `show` still renders a friendly "nothing pressing"
- [ ] `hermes notify mute morning_brief` then re-send → suppressed
- [ ] Kanban overdue cards actually surface (verify against a known past-due card)

---

## Acceptance Criteria
- [ ] All tasks complete; validation green; no regressions
- [ ] Opt-in (default disabled); `show` dry-run bypasses governor/send; `send` governed
- [ ] Composes stalled + kanban + best-effort omi; calendar explicitly deferred
- [ ] One governed message per day (per-day candidate_id); idempotent on double-fire
- [ ] Empty digest suppressed (unless force/show); LLM-down falls back to plain text
- [ ] **Live-data verification executed**
- [ ] No new state table (governor idempotency justified); no invented sources

## Completion Checklist
- [ ] Mirrors stalled/omi module structure (fail-soft `_impl`, dynamic DB path)
- [ ] Reuses `list_stalled_candidates` + `_parse_due` + omi `_call_mcp` (no re-detection, no raw dispatch)
- [ ] Fallback render guarantees non-empty output when items exist
- [ ] Tests + live verification both done
- [ ] `cli-config.yaml.example` documents the section
- [ ] Edits surgical (hook no longer reflows this repo)

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Omi tool assumptions (only get_conversations verified) | High | Low | Best-effort via `_call_mcp` → None-safe; reliable Omi signal comes from kanban cards |
| Empty/noisy brief erodes trust | Med | Med | min_items_to_send suppression + governor cap + mute feedback |
| Cross-module `_parse_due`/`_ensure_mcp_ready` coupling | Med | Low | Stable owned helpers; documented; covered by tests. If they change, brief tests catch it |
| LLM synthesis unavailable | Med | Low | Deterministic fallback render; never fails with items present |
| Double-send on cron double-fire | Low | Med | Per-day candidate_id + governor idempotency (verified) |
| Standalone silent-zero (Omi-style) | Med | Med | Mandatory live `brief show`; omi is best-effort so its absence is expected, not a bug |

## Notes
- **Composition, not detection** — this is the lightest feature of the four because it orchestrates shipped subsystems. Net-new is one module + config + CLI.
- **Calendar is the obvious v2** — the plan leaves a `sections` slot and a documented extension point (shell out to the google-workspace skill when configured). Deliberately not v1 to avoid an OAuth/subprocess dependency.
- **No new table** — decided after verifying `should_deliver`'s per-candidate idempotency already covers once-per-day + double-fire. Adding a table would duplicate that.
- Follows the same hard-won lessons as the prior three: dynamic DB path, top-level fail-soft, reuse the double-decoding omi helpers, and a mandatory live-data step.
```
