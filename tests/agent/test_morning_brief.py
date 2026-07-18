"""Unit tests for the morning brief.

The autouse hermetic fixture (tests/conftest.py) points HERMES_HOME at a
per-test tempdir and pins TZ=UTC. We stub the gather sources + synthesis +
governor so no model/network/live DB is needed, and assert on the composition,
suppression, dry-run, and fail-soft orchestration.
"""

import time
from unittest.mock import patch

import pytest

from agent import morning_brief as mb


def _cfg(**overrides):
    base = {
        "enabled": True,
        "scan_interval_hours": 24,
        "sections": ["stalled", "kanban", "omi"],
        "max_items": 10,
        "min_items_to_send": 1,
        "omi_lookback_conversations": 10,
        "board": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def cfg_patch():
    def _apply(**overrides):
        return patch.object(mb, "_cfg", return_value=_cfg(**overrides))

    return _apply


def _seed_card(*, title, body=None, status="running", age_hours=0.0):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, body=body, triage=True)
        if age_hours:
            old = int(time.time() - age_hours * 3600)
            conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (old, tid))
            conn.commit()
        return tid
    finally:
        conn.close()


# ── consent / dry-run ────────────────────────────────────────────────────────

def test_disabled_send_skips(cfg_patch):
    with cfg_patch(enabled=False), patch.object(mb, "_gather") as g:
        result = mb.run_morning_brief()
    assert result == {"skipped": "disabled"}
    g.assert_not_called()


def test_show_renders_even_when_disabled(cfg_patch):
    # render_brief ignores the enabled flag; with no sources it returns fallback.
    with cfg_patch(enabled=False), patch.object(mb, "_gather", return_value=[]):
        result = mb.render_brief()
    assert result["items"] == 0
    assert "morning" in result["text"].lower()


# ── gather composition ───────────────────────────────────────────────────────

def test_gather_composes_all_sources(cfg_patch):
    with cfg_patch(), patch.object(
        mb, "_gather_stalled", return_value=[{"source": "commitment", "text": "deck"}]
    ), patch.object(
        mb, "_gather_kanban", return_value=[{"source": "kanban", "text": "[overdue] x"}]
    ), patch.object(
        mb, "_gather_omi", return_value=[{"source": "omi", "text": "chat about TE"}]
    ):
        items = mb._gather(_cfg(), time.time())
    sources = {it["source"] for it in items}
    assert sources == {"commitment", "kanban", "omi"}


def test_gather_dedups(cfg_patch):
    dup = [{"source": "kanban", "text": "same"}, {"source": "kanban", "text": "same"}]
    with patch.object(mb, "_gather_stalled", return_value=dup), patch.object(
        mb, "_gather_kanban", return_value=[]
    ), patch.object(mb, "_gather_omi", return_value=[]):
        items = mb._gather(_cfg(), time.time())
    assert len(items) == 1


def test_gather_respects_max_items(cfg_patch):
    many = [{"source": "kanban", "text": f"t{i}"} for i in range(20)]
    with patch.object(mb, "_gather_stalled", return_value=many), patch.object(
        mb, "_gather_kanban", return_value=[]
    ), patch.object(mb, "_gather_omi", return_value=[]):
        items = mb._gather(_cfg(max_items=5), time.time())
    assert len(items) == 5


def test_source_error_is_fail_soft(cfg_patch):
    # stalled raises; kanban still contributes.
    with patch.object(mb, "_gather_stalled", side_effect=RuntimeError("boom")), patch.object(
        mb, "_gather_kanban", return_value=[{"source": "kanban", "text": "ok"}]
    ), patch.object(mb, "_gather_omi", return_value=[]):
        items = mb._gather(_cfg(), time.time())
    assert items == [{"source": "kanban", "text": "ok"}]


def test_omi_section_off_when_not_ready(cfg_patch):
    with patch("agent.omi_commitments._ensure_mcp_ready", return_value=False):
        items = mb._gather_omi(_cfg())
    assert items == []


def test_omi_none_result_graceful(cfg_patch):
    with patch("agent.omi_commitments._ensure_mcp_ready", return_value=True), patch(
        "agent.omi_commitments._call_mcp", return_value=None
    ):
        items = mb._gather_omi(_cfg())
    assert items == []


def test_omi_malformed_conversations_type(cfg_patch):
    # conversations wrapped but not a list -> no crash, no items
    with patch("agent.omi_commitments._ensure_mcp_ready", return_value=True), patch(
        "agent.omi_commitments._call_mcp", return_value={"conversations": 123}
    ):
        items = mb._gather_omi(_cfg())
    assert items == []


def test_omi_malformed_structured_dict(cfg_patch):
    # structured is not a dict; title/summary absent -> skipped, no AttributeError
    convs = {"conversations": [{"structured": "not-a-dict"}, {}]}
    with patch("agent.omi_commitments._ensure_mcp_ready", return_value=True), patch(
        "agent.omi_commitments._call_mcp", return_value=convs
    ):
        items = mb._gather_omi(_cfg())
    assert items == []


# ── kanban source (real tmp DB) ──────────────────────────────────────────────

def test_kanban_overdue_flagged(cfg_patch):
    _seed_card(title="Send deck", body="Due: 2000-01-01\n\n> deck")
    items = mb._gather_kanban(_cfg(), time.time())
    assert any("[overdue]" in it["text"] for it in items)


def test_kanban_open_not_overdue(cfg_patch):
    _seed_card(title="Future thing", body="Due: none")
    items = mb._gather_kanban(_cfg(), time.time())
    assert items and "[overdue]" not in items[0]["text"]


# ── synthesis + fallback ─────────────────────────────────────────────────────

def test_synthesis_uses_llm_when_available():
    items = [{"source": "commitment", "text": "deck"}]

    class _Resp:
        class _C:
            class _M:
                content = "Good morning. From your commitments: send the deck."
            message = _M()
        choices = [_C()]

    fake_client = type("C", (), {})()
    fake_client.chat = type("Ch", (), {})()
    fake_client.chat.completions = type("Co", (), {})()
    fake_client.chat.completions.create = lambda **kw: _Resp()
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(fake_client, "model-x"),
    ), patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value=None):
        text = mb._synthesize(items)
    assert "send the deck" in text.lower()


def test_synthesis_falls_back_when_no_client():
    items = [{"source": "commitment", "text": "deck"}]
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client", return_value=(None, None)
    ):
        text = mb._synthesize(items)
    assert "deck" in text  # deterministic fallback still lists the item


def test_synthesis_falls_back_on_call_error():
    items = [{"source": "commitment", "text": "deck"}]
    fake_client = type("C", (), {})()
    fake_client.chat = type("Ch", (), {})()
    fake_client.chat.completions = type("Co", (), {})()

    def _boom(**kw):
        raise RuntimeError("api down")

    fake_client.chat.completions.create = _boom
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(fake_client, "m"),
    ), patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value=None):
        text = mb._synthesize(items)
    assert "deck" in text


def test_fallback_empty_is_friendly():
    assert "nothing pressing" in mb._render_fallback([]).lower()


# ── suppression + delivery ───────────────────────────────────────────────────

def test_empty_suppressed_on_send(cfg_patch):
    with cfg_patch(min_items_to_send=1), patch.object(mb, "_gather", return_value=[]), patch.object(
        mb, "_deliver"
    ) as d:
        result = mb.run_morning_brief()
    assert result["skipped"] == "empty"
    d.assert_not_called()


def test_force_overrides_empty(cfg_patch):
    with cfg_patch(), patch.object(mb, "_gather", return_value=[]), patch.object(
        mb, "_deliver", return_value=True
    ) as d:
        result = mb.run_morning_brief(force=True)
    assert result["delivered"] == 1
    d.assert_called_once()


def test_governor_allow_sends(cfg_patch):
    items = [{"source": "commitment", "text": "deck"}]
    with cfg_patch(), patch.object(mb, "_gather", return_value=items), patch.object(
        mb, "_synthesize", return_value="brief text"
    ), patch("agent.notification_budget.should_deliver") as should, patch("agent.proactive_helpers.deliver_proactive", return_value=True) as send:
        from agent.notification_budget import BudgetDecision

        should.return_value = BudgetDecision(
            allow=True, reason="under-budget", score=0.9,
            threshold=0.5, category="morning_brief", ledger_id="x",
        )
        result = mb.run_morning_brief()
    assert result["delivered"] == 1
    send.assert_called_once()
    should.assert_called_once()


def test_governor_suppresses(cfg_patch):
    items = [{"source": "commitment", "text": "deck"}]
    with cfg_patch(), patch.object(mb, "_gather", return_value=items), patch.object(
        mb, "_synthesize", return_value="brief text"
    ), patch("agent.notification_budget.should_deliver") as should, patch("agent.proactive_helpers.deliver_proactive", return_value=True) as send:
        from agent.notification_budget import BudgetDecision

        should.return_value = BudgetDecision(
            allow=False, reason="below-threshold", score=0.1,
            threshold=0.5, category="morning_brief", ledger_id="x",
        )
        result = mb.run_morning_brief()
    assert result["delivered"] == 0
    send.assert_not_called()


def test_already_delivered_today_not_resent(cfg_patch):
    # A real send records an 'allowed' ledger row; a second send the same day
    # must NOT re-post (idempotency for an actual message, not just budget).
    items = [{"source": "commitment", "text": "deck"}]
    with cfg_patch(), patch.object(mb, "_gather", return_value=items), patch.object(
        mb, "_synthesize", return_value="brief text"
    ), patch("agent.proactive_helpers.deliver_proactive", return_value=True) as send:
        first = mb.run_morning_brief()
        second = mb.run_morning_brief()
    assert first["delivered"] == 1
    assert second["delivered"] == 0  # skipped as already-delivered
    assert send.call_count == 1  # sent exactly once


def test_top_level_fail_soft(cfg_patch):
    with cfg_patch(), patch.object(mb, "_gather", side_effect=RuntimeError("boom")):
        result = mb.run_morning_brief()
    assert "error" in result
    assert result["delivered"] == 0
