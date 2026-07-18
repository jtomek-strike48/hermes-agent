"""Unit tests for the notification / attention budget governor.

The autouse hermetic fixture (tests/conftest.py) points HERMES_HOME at a
per-test tempdir and pins TZ=UTC, so should_deliver()/record_feedback() run
against an isolated state.db and a deterministic day bucket.
"""

import time
from unittest.mock import patch

import pytest

from agent import notification_budget as nb


def _cfg(**overrides):
    """A full notifications config dict with optional per-key overrides."""
    base = {
        "enabled": True,
        "daily_cap": 3,
        "daily_ceiling": 5,
        "base_threshold": 0.5,
        "escalation_threshold": 0.8,
        "dismiss_step": 0.1,
        "act_step": 0.05,
        "threshold_min": 0.1,
        "threshold_max": 0.95,
        "p_act_ewma_alpha": 0.3,
        "default_value_hint": 0.5,
        "categories": {},
    }
    base.update(overrides)
    return base


@pytest.fixture
def cfg_patch():
    """Patch _config() to return a controllable notifications config."""

    def _apply(**overrides):
        return patch.object(nb, "_config", return_value=_cfg(**overrides))

    return _apply


def test_disabled_passes_through(cfg_patch):
    with cfg_patch(enabled=False):
        d = nb.should_deliver("test", value_hint=1.0)
    assert d.allow is True
    assert d.reason == "governor-disabled"


def test_high_value_first_send_allowed(cfg_patch):
    # p_act seed = base_threshold 0.5, value 1.0, cost 0 -> score 0.5 >= thr 0.5
    with cfg_patch():
        d = nb.should_deliver("test", value_hint=1.0)
    assert d.allow is True
    assert d.reason == "under-budget"


def test_low_value_below_threshold_deferred(cfg_patch):
    # Cold-start p_act=1.0 so score == value_hint: 0.4 < threshold 0.5.
    with cfg_patch():
        d = nb.should_deliver("test", value_hint=0.4)
    assert d.allow is False
    assert d.reason == "below-threshold"


def test_hard_ceiling_blocks_even_high_score(cfg_patch):
    # Cold-start p_act=1.0 → score == value_hint, so value 1.0 clears escalation.
    with cfg_patch():
        for i in range(5):  # fill to ceiling with escalation-clearing sends
            r = nb.should_deliver("cat", value_hint=1.0, candidate_id=f"c{i}")
            assert r.allow is True
        d = nb.should_deliver("cat", value_hint=1.0, candidate_id="over")
    assert d.allow is False
    assert d.reason == "hard-ceiling-reached"


def test_soft_cap_low_score_deferred_high_score_allowed(cfg_patch):
    with cfg_patch():
        # Below the soft cap (3): a mid score (>= threshold) is allowed.
        for i in range(3):
            r = nb.should_deliver("cat", value_hint=0.6, candidate_id=f"cap{i}")
            assert r.allow is True
        # At the soft cap: a mid score (0.6 >= thr 0.5 but < escalation 0.8)
        # is deferred.
        mid = nb.should_deliver("cat", value_hint=0.6, candidate_id="mid")
        assert mid.allow is False
        assert mid.reason == "over-soft-cap-low-score"
        # ...but a high score (>= escalation) still spends from the budget.
        hi = nb.should_deliver("cat", value_hint=0.9, candidate_id="hi")
        assert hi.allow is True


def test_idempotent_candidate_not_double_counted(cfg_patch):
    with cfg_patch():
        first = nb.should_deliver("cat", value_hint=1.0, candidate_id="dup")
        second = nb.should_deliver("cat", value_hint=1.0, candidate_id="dup")
    assert first.allow is True
    assert second.reason == "idempotent-replay"
    assert second.allow == first.allow


def test_dismiss_raises_threshold(cfg_patch):
    with cfg_patch():
        nb.record_feedback("cat", "dismiss")
        db = nb._open_db()
        stats = db.get_category_stats("cat")
    # base 0.5 + dismiss_step 0.1 = 0.6
    assert stats["threshold"] == pytest.approx(0.6)
    assert stats["dismiss_count"] == 1


def test_act_lowers_threshold(cfg_patch):
    with cfg_patch():
        nb.record_feedback("cat", "act")
        db = nb._open_db()
        stats = db.get_category_stats("cat")
    # base 0.5 - act_step 0.05 = 0.45
    assert stats["threshold"] == pytest.approx(0.45)
    assert stats["act_count"] == 1


def test_threshold_clamped_to_max(cfg_patch):
    with cfg_patch(dismiss_step=0.5):
        for _ in range(5):
            nb.record_feedback("cat", "dismiss")
        db = nb._open_db()
        stats = db.get_category_stats("cat")
    assert stats["threshold"] <= 0.95


def test_per_category_override(cfg_patch):
    # omi_commitment gets a tiny ceiling; generic keeps the default.
    overrides = {"categories": {"omi_commitment": {"daily_ceiling": 1}}}
    with cfg_patch(**overrides):
        a = nb.should_deliver("omi_commitment", value_hint=1.0, candidate_id="o1")
        b = nb.should_deliver("omi_commitment", value_hint=1.0, candidate_id="o2")
    assert a.allow is True
    assert b.allow is False
    assert b.reason == "hard-ceiling-reached"


def test_zero_ceiling_means_unbounded_not_gagged(cfg_patch):
    # daily_ceiling=0 (and cap=0) must mean "no limit", not "suppress all".
    with cfg_patch(daily_cap=0, daily_ceiling=0):
        results = [
            nb.should_deliver("cat", value_hint=1.0, candidate_id=f"z{i}")
            for i in range(10)
        ]
    assert all(r.allow for r in results)
    assert {r.reason for r in results} == {"under-budget"}


def test_fail_open_on_internal_error(cfg_patch):
    # Force an error deep in the impl; should_deliver must still ALLOW.
    with patch.object(nb, "_config", side_effect=RuntimeError("boom")):
        d = nb.should_deliver("cat", value_hint=0.1)
    assert d.allow is True
    assert d.reason == "governor-error-fail-open"


def test_unknown_feedback_signal_ignored(cfg_patch):
    with cfg_patch():
        nb.record_feedback("cat", "bogus")  # must not raise or write
        db = nb._open_db()
        assert db.get_category_stats("cat") is None


def test_budget_status_reports_usage(cfg_patch):
    with cfg_patch():
        nb.should_deliver("cat", value_hint=1.0, candidate_id="s1")  # allowed
        nb.should_deliver("cat", value_hint=0.1, candidate_id="s2")  # deferred
        status = nb.budget_status()
    assert status["allowed"] == 1
    assert status["cap"] == 3
    assert status["ceiling"] == 5
    assert len(status["deferred"]) == 1


# ── Implicit act-detection ──────────────────────────────────────────────────
#
# Closes the governor's learning loop without the user ever running
# `notify keep`/`mute`: an allowed proactive notification whose engagement
# window has fully elapsed is reconciled — if the user sent an inbound message
# inside the window it counts as an implicit ACT (lower the bar); silence is
# recorded as `settled` and NEVER raises the bar (a useful FYI needs no reply).

_WINDOW_MIN = 60
_LOOKBACK_H = 48


def _il_cfg(cfg_patch, *, enabled=True, **il_overrides):
    """cfg_patch with an implicit_learning sub-block merged in."""
    il = {
        "enabled": enabled,
        "engagement_window_minutes": _WINDOW_MIN,
        "max_lookback_hours": _LOOKBACK_H,
        "exclude_sources": ["tool", "tui", "cron"],
    }
    il.update(il_overrides)
    return cfg_patch(implicit_learning=il)


def _seed_allowed(db, *, ledger_id, category, created_at,
                  platform=None, chat_id=None):
    """Insert one allowed, not-yet-reconciled ledger row."""
    db.record_notification({
        "id": ledger_id,
        "candidate_id": f"cand:{ledger_id}",
        "category": category,
        "score": 0.6,
        "p_act": 0.6,
        "value_hint": 0.6,
        "attention_cost": 0.0,
        "threshold_used": 0.5,
        "decision": "allowed",
        "platform": platform,
        "chat_id": chat_id,
        "day_key": "2026-07-17",
        "created_at": created_at,
    })


def _seed_user_message(db, *, source, ts, chat_id=None, content="thanks!"):
    """Create a session with one inbound user message at *ts*."""
    sid = f"sess-{source}-{int(ts)}-{chat_id or 'home'}"
    db.create_session(sid, source, chat_id=chat_id, session_key=sid)
    db.append_message(sid, role="user", content=content, timestamp=ts)
    return sid


def test_implicit_act_detected_lowers_threshold(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60  # window fully elapsed
        _seed_allowed(db, ledger_id="L1", category="deadline_radar",
                      created_at=created, platform="telegram", chat_id="123")
        # Reply landed inside the window, same channel.
        _seed_user_message(db, source="telegram", chat_id="123",
                           ts=created + _WINDOW_MIN * 30)
        result = nb.reconcile_implicit_feedback()
        stats = db.get_category_stats("deadline_radar")
    assert result["acted"] == 1
    assert result["settled"] == 0
    # base 0.5 - act_step 0.05 = 0.45
    assert stats["threshold"] == pytest.approx(0.45)
    assert stats["act_count"] == 1


def test_implicit_silence_marks_settled_without_raising_bar(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        _seed_allowed(db, ledger_id="L1", category="morning_brief",
                      created_at=created)
        # No inbound user message at all.
        result = nb.reconcile_implicit_feedback()
        stats = db.get_category_stats("morning_brief")
    assert result["acted"] == 0
    assert result["settled"] == 1
    # Silence must NOT raise the bar (that is explicit `mute`'s job only).
    assert stats is None or stats.get("dismiss_count", 0) == 0
    # Rescan must find nothing (row is stamped `settled`).
    with _il_cfg(cfg_patch):
        again = nb.reconcile_implicit_feedback()
    assert again["reconciled"] == 0


def test_implicit_window_not_elapsed_left_untouched(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        # Created only 5 min ago — the 60-min window has not elapsed yet.
        _seed_allowed(db, ledger_id="L1", category="stalled_thread",
                      created_at=now - 5 * 60)
        result = nb.reconcile_implicit_feedback()
    assert result["reconciled"] == 0


def test_implicit_disabled_skips(cfg_patch):
    with _il_cfg(cfg_patch, enabled=False):
        db = nb._open_db()
        now = time.time()
        _seed_allowed(db, ledger_id="L1", category="cat",
                      created_at=now - 2 * _WINDOW_MIN * 60)
        result = nb.reconcile_implicit_feedback()
    assert result.get("skipped") == "disabled"


def test_implicit_max_lookback_excludes_ancient(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        # Older than the 48h lookback horizon.
        created = now - (_LOOKBACK_H + 1) * 3600
        _seed_allowed(db, ledger_id="L1", category="cat", created_at=created)
        _seed_user_message(db, source="telegram", ts=created + 60)
        result = nb.reconcile_implicit_feedback()
    assert result["reconciled"] == 0


def test_implicit_deferred_rows_never_reconciled(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        db.record_notification({
            "id": "D1", "candidate_id": "c", "category": "cat",
            "score": 0.1, "p_act": 0.5, "value_hint": 0.1,
            "attention_cost": 0.0, "threshold_used": 0.5,
            "decision": "deferred", "platform": None, "chat_id": None,
            "day_key": "2026-07-17", "created_at": created,
        })
        _seed_user_message(db, source="telegram", ts=created + 60)
        result = nb.reconcile_implicit_feedback()
    assert result["reconciled"] == 0


def test_implicit_channel_scoped_ignores_other_chats(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        _seed_allowed(db, ledger_id="L1", category="omi_commitment",
                      created_at=created, platform="telegram", chat_id="123")
        # Reply in a DIFFERENT chat — must not count when a channel is known.
        _seed_user_message(db, source="telegram", chat_id="999",
                           ts=created + _WINDOW_MIN * 30)
        result = nb.reconcile_implicit_feedback()
    assert result["acted"] == 0
    assert result["settled"] == 1


def test_implicit_time_only_fallback_when_channel_unknown(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        # Digest producers send with no recorded channel (chat_id NULL).
        _seed_allowed(db, ledger_id="L1", category="morning_brief",
                      created_at=created)
        _seed_user_message(db, source="telegram", chat_id="anything",
                           ts=created + _WINDOW_MIN * 30)
        result = nb.reconcile_implicit_feedback()
    assert result["acted"] == 1


def test_implicit_excluded_source_not_engagement(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        _seed_allowed(db, ledger_id="L1", category="morning_brief",
                      created_at=created)
        # A `tool`/`tui` message is not a human reply.
        _seed_user_message(db, source="tool", ts=created + _WINDOW_MIN * 30)
        result = nb.reconcile_implicit_feedback()
    assert result["acted"] == 0
    assert result["settled"] == 1


def test_implicit_reconcile_fail_soft(cfg_patch):
    with _il_cfg(cfg_patch), patch.object(
        nb, "_open_db", side_effect=RuntimeError("db down")
    ):
        result = nb.reconcile_implicit_feedback()
    assert "error" in result
    assert result["reconciled"] == 0


def test_budget_status_auto_reconciles_when_enabled(cfg_patch):
    with _il_cfg(cfg_patch):
        db = nb._open_db()
        now = time.time()
        created = now - 2 * _WINDOW_MIN * 60
        _seed_allowed(db, ledger_id="L1", category="deadline_radar",
                      created_at=created, platform="telegram", chat_id="123")
        _seed_user_message(db, source="telegram", chat_id="123",
                           ts=created + _WINDOW_MIN * 30)
        # Reading status should settle the pending act as a side effect.
        nb.budget_status()
        stats = db.get_category_stats("deadline_radar")
    assert stats is not None
    assert stats["act_count"] == 1
