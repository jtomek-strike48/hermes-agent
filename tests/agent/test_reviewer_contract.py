"""Tests for the shared reviewer contract.

The gating logic is safety-critical: it decides which reviewer-suggested actions
may auto-execute vs. must wait for a human click. These tests pin that down,
especially the fail-closed defaults.
"""

from __future__ import annotations

import pytest

from agent import reviewer_contract as rc


def _issue_verdict(**over):
    base = {
        "artifact": "issue", "ref": "o/r#1", "verdict": "ready",
        "summary": "s", "findings": [], "missing_info": [], "suggested_actions": [],
    }
    base.update(over)
    return base


# --- verdict vocab ---------------------------------------------------------

def test_valid_issue_verdict_passes():
    v = rc.validate(_issue_verdict(verdict="needs-info"), mode="issue")
    assert v["verdict"] == "needs-info"


def test_bad_verdict_rejected():
    with pytest.raises(rc.ContractError):
        rc.validate(_issue_verdict(verdict="looks-good"), mode="issue")


def test_unknown_mode_rejected():
    with pytest.raises(rc.ContractError):
        rc.validate(_issue_verdict(), mode="nonsense")


def test_missing_optional_lists_coerced():
    v = rc.validate({"verdict": "ready"}, mode="issue")
    assert v["findings"] == [] and v["missing_info"] == [] and v["suggested_actions"] == []


# --- action gating (the safety-critical part) ------------------------------

def test_reversible_actions_are_not_gated():
    v = rc.validate(_issue_verdict(suggested_actions=[
        {"action": "apply-label", "args": {"labels": ["type/bug"]}},
        {"action": "ask-reporter", "args": {"questions": ["repro?"]}},
    ]), mode="issue")
    autos = rc.auto_actions(v, mode="issue")
    assert {a["action"] for a in autos} == {"apply-label", "ask-reporter"}
    assert rc.gated_actions(v, mode="issue") == []


def test_consequential_actions_are_gated():
    v = rc.validate(_issue_verdict(suggested_actions=[
        {"action": "close", "args": {"reason": "done"}},
        {"action": "set-priority", "args": {"priority": "P1"}},
    ]), mode="issue")
    gated = {a["action"] for a in rc.gated_actions(v, mode="issue")}
    assert gated == {"close", "set-priority"}
    assert rc.auto_actions(v, mode="issue") == []


def test_gated_flag_is_recomputed_not_trusted():
    """An LLM claiming close is not-gated must be overridden — fail closed."""
    v = rc.validate(_issue_verdict(suggested_actions=[
        {"action": "close", "gated": False, "args": {}},
    ]), mode="issue")
    assert v["suggested_actions"][0]["gated"] is True


def test_unknown_action_rejected():
    with pytest.raises(rc.ContractError):
        rc.validate(_issue_verdict(suggested_actions=[{"action": "delete-repo"}]), mode="issue")


def test_action_without_name_rejected():
    with pytest.raises(rc.ContractError):
        rc.validate(_issue_verdict(suggested_actions=[{"args": {}}]), mode="issue")


def test_is_gated_unknown_action_defaults_closed():
    assert rc.is_gated("issue", "some-new-action") is True


# --- payload builder -------------------------------------------------------

def test_payload_marks_operator_as_reporter():
    import json
    issue = {"repo": "o/r", "number": 5, "title": "t", "body": "b",
             "labels": [{"name": "bug"}], "author": {"login": "me"}}
    payload = json.loads(rc.build_triage_user_payload(issue, "me"))
    assert payload["operator_is_reporter"] is True
    assert payload["ref"] == "o/r#5"


def test_payload_detects_other_reporter():
    import json
    issue = {"repo": "o/r", "number": 5, "author": {"login": "someone-else"}}
    payload = json.loads(rc.build_triage_user_payload(issue, "me"))
    assert payload["operator_is_reporter"] is False
