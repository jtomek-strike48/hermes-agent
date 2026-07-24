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


# --- PR mode ---------------------------------------------------------------

def _pr_verdict(**over):
    base = {
        "artifact": "pr", "ref": "o/r#1", "verdict": "lgtm",
        "summary": "s", "findings": [], "missing_info": [], "suggested_actions": [],
    }
    base.update(over)
    return base


def test_valid_pr_verdict_passes():
    v = rc.validate(_pr_verdict(verdict="blocker"), mode="pr")
    assert v["verdict"] == "blocker"


def test_issue_verdict_invalid_in_pr_mode():
    # "ready" is an issue verdict; it must not validate as a PR verdict.
    with pytest.raises(rc.ContractError):
        rc.validate(_pr_verdict(verdict="ready"), mode="pr")


def test_pr_review_submissions_are_gated():
    v = rc.validate(_pr_verdict(suggested_actions=[
        {"action": "approve", "args": {"body": "LGTM"}},
        {"action": "request-changes", "args": {"body": "fix X"}},
        {"action": "comment-review", "args": {"body": "nit"}},
        {"action": "merge", "args": {}},
    ]), mode="pr")
    gated = {a["action"] for a in rc.gated_actions(v, mode="pr")}
    assert gated == {"approve", "request-changes", "comment-review", "merge"}
    assert rc.auto_actions(v, mode="pr") == []


def test_pr_label_is_auto_but_ask_operator_too():
    v = rc.validate(_pr_verdict(suggested_actions=[
        {"action": "apply-label", "args": {"labels": ["area/frontend"]}},
        {"action": "ask-operator", "args": {"questions": ["intended?"]}},
    ]), mode="pr")
    autos = {a["action"] for a in rc.auto_actions(v, mode="pr")}
    assert autos == {"apply-label", "ask-operator"}
    assert rc.gated_actions(v, mode="pr") == []


def test_pr_approve_gated_flag_recomputed_not_trusted():
    """An LLM claiming approve is not-gated must be overridden — fail closed."""
    v = rc.validate(_pr_verdict(suggested_actions=[
        {"action": "approve", "gated": False, "args": {"body": "ok"}},
    ]), mode="pr")
    assert v["suggested_actions"][0]["gated"] is True


def test_issue_action_unknown_in_pr_mode_rejected():
    # "close"/"set-priority" are issue-mode actions; not part of PR vocab.
    with pytest.raises(rc.ContractError):
        rc.validate(_pr_verdict(suggested_actions=[{"action": "set-priority"}]), mode="pr")


def test_pr_payload_marks_self_authored_and_truncates_diff():
    import json
    pr = {"repo": "o/r", "number": 7, "title": "t", "body": "b",
          "labels": [{"name": "area/x"}], "author": {"login": "me"},
          "diff": "x" * 50000}
    payload = json.loads(rc.build_pr_review_payload(pr, "me"))
    assert payload["operator_is_author"] is True
    assert payload["ref"] == "o/r#7"
    assert len(payload["diff"]) == 40000  # truncated to the cap


# --- codebase mode ---------------------------------------------------------

def _codebase_verdict(**over):
    base = {
        "artifact": "codebase", "ref": "o/r", "verdict": "healthy",
        "summary": "s", "findings": [], "missing_info": [], "suggested_actions": [],
    }
    base.update(over)
    return base


def test_valid_codebase_verdict_passes():
    v = rc.validate(_codebase_verdict(verdict="at-risk"), mode="codebase")
    assert v["verdict"] == "at-risk"


def test_pr_verdict_invalid_in_codebase_mode():
    with pytest.raises(rc.ContractError):
        rc.validate(_codebase_verdict(verdict="lgtm"), mode="codebase")


def test_open_issue_is_gated_ask_operator_is_auto():
    v = rc.validate(_codebase_verdict(suggested_actions=[
        {"action": "open-issue", "args": {"title": "flaky tests", "body": "..."}},
        {"action": "ask-operator", "args": {"questions": ["intended coverage target?"]}},
    ]), mode="codebase")
    assert {a["action"] for a in rc.gated_actions(v, mode="codebase")} == {"open-issue"}
    assert {a["action"] for a in rc.auto_actions(v, mode="codebase")} == {"ask-operator"}


def test_open_issue_gated_flag_recomputed_not_trusted():
    v = rc.validate(_codebase_verdict(suggested_actions=[
        {"action": "open-issue", "gated": False, "args": {"title": "x"}},
    ]), mode="codebase")
    assert v["suggested_actions"][0]["gated"] is True


def test_cross_mode_action_rejected_in_codebase():
    # "merge"/"approve" are PR actions; not part of codebase vocab.
    with pytest.raises(rc.ContractError):
        rc.validate(_codebase_verdict(suggested_actions=[{"action": "merge"}]), mode="codebase")


def test_codebase_payload_passes_signals_through():
    import json
    signals = {"tests": "3 failing", "coverage": 0.71, "dep_audit": ["CVE-x"]}
    payload = json.loads(rc.build_codebase_review_payload("o/r", signals))
    assert payload["ref"] == "o/r"
    assert payload["signals"]["coverage"] == 0.71
    assert payload["signals"]["dep_audit"] == ["CVE-x"]


def test_codebase_payload_non_dict_signals_coerced():
    import json
    payload = json.loads(rc.build_codebase_review_payload("o/r", None))
    assert payload["signals"] == {}
