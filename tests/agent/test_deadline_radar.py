"""Unit tests for the deadline radar.

The autouse hermetic fixture (tests/conftest.py) points HERMES_HOME at a
per-test tempdir and pins TZ=UTC. We seed real kanban cards in the tmp DB and
stub the governor/send so no model/network is needed, asserting on the
due-soon window, cooldown dedup, urgency-scaled delivery, and fail-soft
orchestration.
"""

import time
from unittest.mock import patch

import pytest

from agent import deadline_radar as dr


def _cfg(**overrides):
    base = {
        "enabled": True,
        "scan_interval_hours": 4,
        "lead_time_hours": 24,
        "cooldown_hours": 12,
        "max_items_per_digest": 5,
        "board": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def cfg_patch():
    def _apply(**overrides):
        return patch.object(dr, "_cfg", return_value=_cfg(**overrides))

    return _apply


def _seed_card(*, title, due_epoch=None, status_triage=True):
    """Create a kanban card whose body carries a 'Due: <epoch>' line.

    _to_epoch accepts a bare integer epoch string, so an epoch keeps the test's
    due date exact and TZ-independent.
    """
    from hermes_cli import kanban_db as kb

    body = f"Due: {int(due_epoch)}" if due_epoch is not None else None
    conn = kb.connect()
    try:
        return kb.create_task(conn, title=title, body=body, triage=status_triage)
    finally:
        conn.close()


# ── consent / dry-run ────────────────────────────────────────────────────────

def test_disabled_scan_skips(cfg_patch):
    with cfg_patch(enabled=False):
        result = dr.run_deadline_radar()
    assert result == {"skipped": "disabled"}


def test_disabled_list_skips(cfg_patch):
    with cfg_patch(enabled=False):
        result = dr.list_upcoming_deadlines()
    assert result == {"skipped": "disabled"}


# ── due-soon window ──────────────────────────────────────────────────────────

def test_due_soon_within_window_included(cfg_patch):
    now = time.time()
    _seed_card(title="Ship deck", due_epoch=now + 3 * 3600)  # in 3h
    with cfg_patch():
        result = dr.list_upcoming_deadlines()
    cands = result["candidates"]
    assert len(cands) == 1
    assert cands[0]["kind"] == "deadline"
    assert cands[0]["candidate_id"].startswith("deadline:card:")
    assert "[due in 3h]" in cands[0]["text"]


def test_past_due_excluded(cfg_patch):
    # Already past due is the stalled detector's job, not the radar's.
    now = time.time()
    _seed_card(title="Overdue thing", due_epoch=now - 3600)  # 1h ago
    with cfg_patch():
        result = dr.list_upcoming_deadlines()
    assert result["candidates"] == []


def test_beyond_lead_time_excluded(cfg_patch):
    now = time.time()
    _seed_card(title="Far off", due_epoch=now + 100 * 3600)  # ~4d out
    with cfg_patch(lead_time_hours=24):
        result = dr.list_upcoming_deadlines()
    assert result["candidates"] == []


def test_no_due_date_excluded(cfg_patch):
    _seed_card(title="No due date", due_epoch=None)
    with cfg_patch():
        result = dr.list_upcoming_deadlines()
    assert result["candidates"] == []


def test_lead_time_boundary_inclusive(cfg_patch):
    # A card due exactly at the horizon edge is still inside the window.
    now = 1_000_000.0
    _seed_card(title="Edge", due_epoch=now + 24 * 3600)
    with cfg_patch(lead_time_hours=24), patch("time.time", return_value=now):
        result = dr.list_upcoming_deadlines()
    assert len(result["candidates"]) == 1


def test_sorted_soonest_first(cfg_patch):
    now = time.time()
    _seed_card(title="Later", due_epoch=now + 20 * 3600)
    _seed_card(title="Sooner", due_epoch=now + 2 * 3600)
    with cfg_patch():
        result = dr.list_upcoming_deadlines()
    titles = [c["text"] for c in result["candidates"]]
    assert "Sooner" in titles[0] and "Later" in titles[1]


# ── humanize helper ──────────────────────────────────────────────────────────

def test_humanize_lead_minutes():
    assert dr._humanize_lead(30 * 60) == "in 30m"


def test_humanize_lead_hours():
    assert dr._humanize_lead(3 * 3600) == "in 3h"


def test_humanize_lead_days():
    assert dr._humanize_lead(72 * 3600) == "in 3d"


def test_humanize_lead_negative_floored():
    assert dr._humanize_lead(-100) == "in 1m"


# ── scan / deliver / dedup ───────────────────────────────────────────────────

def test_scan_no_candidates_no_send(cfg_patch):
    with cfg_patch(), patch.object(dr, "_deliver_digest") as deliver:
        result = dr.run_deadline_radar()
    assert result == {"scanned": 0, "candidates": 0, "nudged": 0, "delivered": 0}
    deliver.assert_not_called()


def test_scan_delivers_and_records(cfg_patch):
    now = time.time()
    _seed_card(title="Due soon", due_epoch=now + 2 * 3600)
    with cfg_patch(), patch.object(dr, "_deliver_digest", return_value=True) as deliver:
        result = dr.run_deadline_radar()
    assert result["delivered"] == 1
    assert result["nudged"] == 1
    deliver.assert_called_once()
    # The item was recorded to the cooldown ledger, so an immediate re-scan
    # finds it already-nudged and sends nothing.
    with cfg_patch(), patch.object(dr, "_deliver_digest", return_value=True) as deliver2:
        result2 = dr.run_deadline_radar()
    assert result2 == {"scanned": 1, "candidates": 0, "nudged": 0, "delivered": 0}
    deliver2.assert_not_called()


def test_not_recorded_when_delivery_suppressed(cfg_patch):
    now = time.time()
    _seed_card(title="Due soon", due_epoch=now + 2 * 3600)
    with cfg_patch(), patch.object(dr, "_deliver_digest", return_value=False):
        result = dr.run_deadline_radar()
    assert result["delivered"] == 0
    assert result["nudged"] == 0
    # Not recorded -> still a fresh candidate on the next scan.
    with cfg_patch(), patch.object(dr, "_deliver_digest", return_value=False):
        result2 = dr.run_deadline_radar()
    assert result2["candidates"] == 1


def test_max_items_caps_digest(cfg_patch):
    now = time.time()
    for i in range(8):
        _seed_card(title=f"card {i}", due_epoch=now + (i + 1) * 900)  # staggered
    captured = {}

    def _fake_deliver(items):
        captured["n"] = len(items)
        return True

    with cfg_patch(max_items_per_digest=3), patch.object(
        dr, "_deliver_digest", side_effect=_fake_deliver
    ):
        result = dr.run_deadline_radar()
    assert captured["n"] == 3
    assert result["nudged"] == 3


# ── governor routing (real should_deliver via _deliver_digest) ───────────────

def test_deliver_digest_routes_through_governor():
    now = time.time()
    items = [{
        "candidate_id": "deadline:card:1",
        "kind": "deadline",
        "due_epoch": int(now + 3600),
        "text": "[due in 1h] X",
    }]
    with patch("agent.notification_budget.should_deliver") as sd, patch("agent.proactive_helpers.deliver_proactive", return_value=True) as send:
        from agent.notification_budget import BudgetDecision

        sd.return_value = BudgetDecision(
            allow=True, reason="under-budget", score=0.9,
            threshold=0.5, category="deadline_radar",
        )
        sent = dr._deliver_digest(items)
    assert sent is True
    send.assert_called_once()
    # value_hint is urgency-scaled: a card due in ~1h is near the top of [0,1].
    _, kwargs = sd.call_args
    assert kwargs["category"] == "deadline_radar"
    assert kwargs["value_hint"] >= 0.9


def test_deliver_digest_empty_items_returns_false():
    # Defensive invariant: never crash the urgency min() on an empty digest.
    with patch("agent.notification_budget.should_deliver") as sd:
        assert dr._deliver_digest([]) is False
    sd.assert_not_called()


def test_deliver_digest_suppressed_no_send():
    now = time.time()
    items = [{
        "candidate_id": "deadline:card:1",
        "kind": "deadline",
        "due_epoch": int(now + 3600),
        "text": "[due in 1h] X",
    }]
    with patch("agent.notification_budget.should_deliver") as sd, patch("agent.proactive_helpers.deliver_proactive", return_value=True) as send:
        from agent.notification_budget import BudgetDecision

        sd.return_value = BudgetDecision(
            allow=False, reason="below-threshold", score=0.1,
            threshold=0.5, category="deadline_radar",
        )
        sent = dr._deliver_digest(items)
    assert sent is False
    send.assert_not_called()


def test_deliver_digest_urgency_lower_for_far_deadline():
    now = time.time()
    items = [{
        "candidate_id": "deadline:card:1",
        "kind": "deadline",
        "due_epoch": int(now + 24 * 3600),  # a full day out
        "text": "[due in 24h] X",
    }]
    with patch("agent.notification_budget.should_deliver") as sd, patch("agent.proactive_helpers.deliver_proactive", return_value=True):
        from agent.notification_budget import BudgetDecision

        sd.return_value = BudgetDecision(
            allow=True, reason="under-budget", score=0.5,
            threshold=0.5, category="deadline_radar",
        )
        dr._deliver_digest(items)
    _, kwargs = sd.call_args
    # 24h out -> 1 - 24/48 = 0.5, clearly below the ~1.0 of an imminent one.
    assert 0.4 <= kwargs["value_hint"] <= 0.6


# ── fail-soft ────────────────────────────────────────────────────────────────

def test_run_fail_soft_on_db_error(cfg_patch):
    with cfg_patch(), patch.object(
        dr, "_gather_upcoming", side_effect=RuntimeError("boom")
    ):
        # Gather error is swallowed inside the impl (source fail-soft) -> 0 items.
        result = dr.run_deadline_radar()
    assert result["scanned"] == 0
    assert result["delivered"] == 0


def test_run_fail_soft_top_level(cfg_patch):
    # An error escaping the impl (e.g. cfg access) yields an error summary.
    with patch.object(dr, "_run_deadline_radar_impl", side_effect=RuntimeError("x")):
        result = dr.run_deadline_radar()
    assert "error" in result
    assert result["delivered"] == 0


def test_list_fail_soft_top_level():
    with patch.object(
        dr, "_list_upcoming_deadlines_impl", side_effect=RuntimeError("x")
    ):
        result = dr.list_upcoming_deadlines()
    assert "error" in result
    assert result["candidates"] == []
