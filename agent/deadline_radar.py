"""Deadline radar — a forward-looking nudge for commitments due SOON.

The prospective complement to the stalled-thread detector. Where
``stalled_threads`` fires on commitments that are ALREADY past due or have gone
quiet, and the morning brief surfaces cards that are ALREADY overdue, the radar
warns BEFORE a deadline lapses: open kanban cards whose ``Due:`` falls inside a
configurable lead-time window ahead of now (e.g. "due in the next 24h, still
open"). It never fires on items that are already past due — those are the
stalled detector's job — so the two never double-nudge the same card for the
same reason.

Design invariants (mirror agent/stalled_threads.py):
  - OPT-IN. Does nothing unless ``deadline_radar.enabled`` is true (dry-run
    ``list`` also respects the flag, matching ``list_stalled_candidates``).
  - FAIL-SOFT. Any source that raises contributes zero; the scan still returns
    a summary rather than propagating.
  - GOVERNED. The digest goes through ``should_deliver`` (category
    ``deadline_radar``) so it is capped and learns from keep/mute feedback.
  - DEDUP. Each approaching card is nudged at most once per cooldown window, via
    the shared ``stalled_nudges`` ledger under a distinct ``deadline:`` id
    namespace so it never collides with the stalled detector's own nudges.
  - NO MCP. The only source is the local kanban DB, so there is no discovery
    bootstrap and no best-effort external call.

NOTE ON COUPLING: this module reuses ``stalled_threads._parse_due`` (the
``Due:``-line / prose-``due <date>`` parser). It is a stable internal helper we
own; reusing it avoids re-implementing due-date parsing. Tests cover the
integration so a signature change is caught.
"""

import logging
import time
from typing import Any, Dict, List

from hermes_cli.config import load_config
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

logger = logging.getLogger(__name__)

# Open card statuses worth a deadline nudge (mirrors stalled_threads).
_OPEN_STATUSES = {"triage", "todo", "ready", "scheduled", "blocked"}


def _cfg() -> Dict[str, Any]:
    cfg = load_config()
    section = cfg.get("deadline_radar", {}) if isinstance(cfg, dict) else {}
    return section if isinstance(section, dict) else {}


def _humanize_lead(seconds: float) -> str:
    """Render a positive time-until into a short 'in 3h' / 'in 2d' phrase."""
    if seconds < 0:
        seconds = 0
    hours = seconds / 3600.0
    if hours < 1:
        minutes = max(1, int(seconds // 60))
        return f"in {minutes}m"
    if hours < 48:
        return f"in {int(round(hours))}h"
    days = int(round(hours / 24.0))
    return f"in {days}d"


def _gather_upcoming(
    conn: Any, cfg: Dict[str, Any], now: float
) -> List[Dict[str, Any]]:
    """Open kanban cards with a Due date inside the lead-time window ahead.

    A card qualifies when ``now < due <= now + lead_time`` — i.e. it is due
    soon but NOT yet past due (past-due cards belong to the stalled detector).
    """
    from agent.stalled_threads import _parse_due
    from hermes_cli import kanban_db as kb

    lead_s = float(cfg.get("lead_time_hours", 24)) * 3600
    horizon = now + lead_s
    out: List[Dict[str, Any]] = []
    for card in kb.list_tasks(conn, include_archived=True):
        if card.status not in _OPEN_STATUSES:
            continue
        due = _parse_due(card.body)
        # Due-soon window: strictly future, within the lead time. Excludes
        # already-past-due cards (due <= now) — those are stalled_threads'.
        if due is None or due <= now or due > horizon:
            continue
        title = (card.title or "").strip()
        if not title:
            continue
        lead = _humanize_lead(due - now)
        out.append({
            "candidate_id": f"deadline:card:{card.id}",
            "kind": "deadline",
            "task_id": card.id,
            "due_epoch": int(due),
            "lead": lead,
            "text": f"[due {lead}] {title}",
        })
    # Soonest deadline first — the most urgent nudge leads the digest.
    out.sort(key=lambda it: it["due_epoch"])
    return out


def _deliver_digest(items: List[Dict[str, Any]]) -> bool:
    """Route ONE batched digest through the governor. Returns True if sent.

    ``value_hint`` scales with urgency: the closer the soonest deadline, the
    higher the value (a card due in an hour matters more than one due in a day).
    """
    if not items:
        # Callers only invoke this with a non-empty digest, but make the
        # invariant explicit so the urgency min() below can never see [].
        return False
    try:
        from agent.notification_budget import should_deliver

        # Urgency from the soonest item: 1.0 at/under an hour out, decaying to
        # ~0.4 at the far edge of the window. Bounded to the governor's [0,1].
        now = time.time()
        soonest_lead_h = min(
            max(0.0, (it["due_epoch"] - now) / 3600.0) for it in items
        )
        value_hint = max(0.4, min(1.0, 1.0 - (soonest_lead_h / 48.0)))
        day_key = time.strftime("%Y-%m-%d")
        decision = should_deliver(
            category="deadline_radar",
            value_hint=value_hint,
            candidate_id=f"deadline:{day_key}",
        )
        if not decision.allow:
            logger.info(
                "deadline_radar: digest suppressed by notification budget (%s)",
                decision.reason,
            )
            return False

        lines = [
            f"{i}. {it.get('text', '(upcoming deadline)')}"
            for i, it in enumerate(items, 1)
        ]
        n = len(items)
        message = f"Heads up — {n} deadline(s) coming up:\n" + "\n".join(lines)
        from agent.proactive_helpers import deliver_proactive

        return deliver_proactive(message)
    except Exception as exc:
        logger.warning(
            "deadline_radar: digest delivery failed: %s", exc, exc_info=True
        )
        return False


def run_deadline_radar() -> Dict[str, Any]:
    """Detect commitments due soon and nudge once per cooldown window.

    Returns ``{"scanned", "candidates", "nudged", "delivered"}``,
    ``{"skipped": <reason>}``, or ``{"error": <msg>}``. FAIL-SOFT: any uncaught
    error (DB open, dedup/record write) yields an error summary instead of
    propagating to the CLI/cron caller.
    """
    try:
        return _run_deadline_radar_impl()
    except Exception as exc:
        logger.error("deadline_radar: run failed: %s", exc, exc_info=True)
        return {
            "error": str(exc),
            "scanned": 0,
            "candidates": 0,
            "nudged": 0,
            "delivered": 0,
        }


def _run_deadline_radar_impl() -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return {"skipped": "disabled"}

    cooldown_s = float(cfg.get("cooldown_hours", 12)) * 3600
    max_items = int(cfg.get("max_items_per_digest", 5))
    board = cfg.get("board") or None

    from hermes_cli import kanban_db as kb

    db = SessionDB(get_hermes_home() / "state.db")
    now = time.time()

    candidates: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = kb.connect(board=board)
        candidates.extend(_gather_upcoming(conn, cfg, now))
    except Exception as exc:
        logger.warning("deadline_radar: upcoming scan failed: %s", exc)
    finally:
        if conn is not None:
            conn.close()

    scanned = len(candidates)
    if not candidates:
        return {"scanned": 0, "candidates": 0, "nudged": 0, "delivered": 0}

    # Dedup against the cooldown window so a card due soon isn't re-nudged on
    # every scan. Shares the stalled_nudges ledger but under a 'deadline:' id
    # namespace, so it never collides with the stalled detector's rows.
    fresh = [
        c
        for c in candidates
        if not db.stall_nudged_recently(c["candidate_id"], cooldown_s)
    ]
    if not fresh:
        return {"scanned": scanned, "candidates": 0, "nudged": 0, "delivered": 0}

    digest_items = fresh[:max_items]
    delivered = _deliver_digest(digest_items)
    if delivered:
        for it in digest_items:
            db.record_stall_nudge(it["candidate_id"], it["kind"], it["text"])

    logger.info(
        "deadline_radar scan: scanned=%d candidates=%d nudged=%d delivered=%s",
        scanned,
        len(fresh),
        len(digest_items) if delivered else 0,
        delivered,
    )
    return {
        "scanned": scanned,
        "candidates": len(fresh),
        "nudged": len(digest_items) if delivered else 0,
        "delivered": 1 if delivered else 0,
    }


def list_upcoming_deadlines() -> Dict[str, Any]:
    """Dry-run: return commitments due within the lead-time window WITHOUT
    nudging (for ``hermes radar list``). Skips the cooldown/governor entirely.
    FAIL-SOFT: an uncaught error yields ``{"error": ...}`` instead of raising.
    """
    try:
        return _list_upcoming_deadlines_impl()
    except Exception as exc:
        logger.error("deadline_radar: list failed: %s", exc, exc_info=True)
        return {"error": str(exc), "candidates": []}


def _list_upcoming_deadlines_impl() -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return {"skipped": "disabled"}

    board = cfg.get("board") or None
    from hermes_cli import kanban_db as kb

    now = time.time()
    candidates: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = kb.connect(board=board)
        candidates.extend(_gather_upcoming(conn, cfg, now))
    except Exception as exc:
        logger.warning("deadline_radar: upcoming scan failed: %s", exc)
    finally:
        if conn is not None:
            conn.close()
    return {"candidates": candidates}
