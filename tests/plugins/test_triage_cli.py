"""Tests for the ``hermes triage`` CLI (triage_buttons.cli).

Focus on the pure decision logic that turns Triage v2 outcomes into digest-store
items: the outcome→item mapping, the button-worthiness filter, and the publish
``[SILENT]`` short-circuit. The LLM/gh layer (agent.issue_triage) is stubbed —
these tests never touch the network.
"""

from __future__ import annotations

from argparse import Namespace

from agent.issue_triage import PlannedAction, TriageOutcome
from plugins.triage_buttons import cli


def _issue(number=5, title="t"):
    return {"repo": "o/r", "number": number, "title": title}


def test_outcome_to_item_maps_answer_in_dm():
    out = TriageOutcome(
        ref="o/r#5", verdict="needs-info", summary="under-specified",
        missing_info=["repro?"], ask_target="operator",
    )
    item = cli._outcome_to_item(_issue(), out, live=False)
    assert item["ask_operator"] is True
    assert item["questions"] == ["repro?"]
    assert item["url"].endswith("/o/r/issues/5")


def test_ready_verdict_never_gets_answer_in_dm():
    # A "ready" verdict that still carries operator questions is a contract
    # self-contradiction (clarifying questions belong to needs-info). The surface
    # must not raise a spurious Answer-in-DM button for it.
    out = TriageOutcome(ref="o/r#5", verdict="ready", summary="actionable",
                        missing_info=["stray question the model emitted"],
                        ask_target="operator")
    item = cli._outcome_to_item(_issue(), out, live=False)
    assert item["ask_operator"] is False
    assert item["questions"] == []


def test_outcome_to_item_reporter_questions_not_operator_facing():
    # ask_target=reporter → questions went to GitHub, NOT an Answer-in-DM button.
    out = TriageOutcome(ref="o/r#5", verdict="needs-info", summary="s",
                        missing_info=["q"], ask_target="reporter")
    item = cli._outcome_to_item(_issue(), out, live=True)
    assert item["ask_operator"] is False
    assert item["questions"] == []


def test_outcome_to_item_carries_gated():
    out = TriageOutcome(
        ref="o/r#5", verdict="ready", summary="s",
        gated_planned=[{"action": "set-priority", "args": {"label": "P1: High"}}],
    )
    item = cli._outcome_to_item(_issue(), out, live=False)
    assert item["gated"][0]["action"] == "set-priority"


def test_button_worthy_requires_answer_or_decision():
    ready_no_actions = {"ask_operator": False, "questions": [], "gated": []}
    assert cli._is_button_worthy(ready_no_actions) is False

    answerable = {"ask_operator": True, "questions": ["q"], "gated": []}
    assert cli._is_button_worthy(answerable) is True

    for decision in ("close", "mark-duplicate", "wont-fix"):
        gated = {"ask_operator": False, "questions": [], "gated": [{"action": decision}]}
        assert cli._is_button_worthy(gated) is True, f"{decision} should surface the issue"


def test_lone_set_priority_is_not_button_worthy():
    # set-priority alone must NOT surface an issue — the reviewer proposes it on
    # nearly every unprioritized issue, so it would list the whole backlog.
    only_priority = {"ask_operator": False, "questions": [],
                     "gated": [{"action": "set-priority", "args": {"label": "P1: High"}}]}
    assert cli._is_button_worthy(only_priority) is False


def test_set_priority_rides_along_when_issue_already_surfaced():
    # A needs-info issue that ALSO has a set-priority proposal is still worthy
    # (via the answer), and set-priority rides along as a button (blocks handles
    # the rendering). The filter must not reject it just because set-priority
    # isn't a decision action.
    answerable_with_priority = {
        "ask_operator": True, "questions": ["repro?"],
        "gated": [{"action": "set-priority", "args": {"label": "P2: Medium"}}],
    }
    assert cli._is_button_worthy(answerable_with_priority) is True


def test_auto_summary_dry_vs_live():
    out = TriageOutcome(
        ref="o/r#5", verdict="ready", summary="s",
        auto_planned=[PlannedAction("apply-label", {"labels": ["bug"]}, gated=False, executed=False)],
    )
    assert "would label" in cli._auto_summary(out, live=False)
    out.auto_planned[0].executed = True
    assert "labeled bug" in cli._auto_summary(out, live=True)


def test_publish_silent_when_no_actionable_issues(monkeypatch, capsys):
    # Sweep returns issues, but none are button-worthy → [SILENT], no Slack post.
    monkeypatch.setattr(cli, "_run_sweep", lambda *, live: [])
    rc = cli.triage_command(Namespace(triage_action="publish", channel="", live=False, dry_run=False))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "[SILENT]"


def test_run_sweep_filters_non_actionable(monkeypatch):
    # One ready-no-action issue (dropped) + one answerable (kept).
    import agent.issue_triage as t

    monkeypatch.setattr(cli, "_self_login", lambda: "me")
    monkeypatch.setattr(t, "repo_labels", lambda repo, **k: {"bug"})
    monkeypatch.setattr(t, "list_open_issues",
                        lambda repo, label, **k: [_issue(1), _issue(2)])

    def _process(issue, me, existing, **k):
        if issue["number"] == 1:
            return TriageOutcome(ref="o/r#1", verdict="ready", summary="fine")
        return TriageOutcome(ref="o/r#2", verdict="needs-info", summary="s",
                             missing_info=["q"], ask_target="operator")

    monkeypatch.setattr(t, "process_issue", _process)
    repos = cli._run_sweep(live=False)
    assert len(repos) == 1
    nums = [it["number"] for it in repos[0]["items"]]
    assert nums == [2]  # only the answerable one


def test_run_sweep_skips_repo_on_fetch_error(monkeypatch):
    import agent.issue_triage as t

    monkeypatch.setattr(cli, "_self_login", lambda: "me")
    monkeypatch.setattr(t, "repo_labels", lambda repo, **k: set())
    monkeypatch.setattr(t, "list_open_issues", lambda repo, label, **k: None)  # gh error
    assert cli._run_sweep(live=False) == []
