"""Tests for Triage v2 (agent/issue_triage.py).

Focus on the deterministic, safety-relevant logic with an injected fake LLM and
a fake ``gh`` runner — never hitting the network:
  - label intersection drops labels not already in the repo (high-confidence)
  - dry_run writes NOTHING; live executes auto actions
  - smart-route: reporter != you → GitHub comment; reporter == you → ask in Slack
  - gated actions are collected, never auto-run
"""

from __future__ import annotations

from types import SimpleNamespace

from agent import issue_triage as t


class _FakeLLM:
    """Minimal OpenAI-client stand-in returning a canned JSON verdict."""

    def __init__(self, verdict_json: str):
        self._json = verdict_json
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        msg = SimpleNamespace(content=self._json)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _issue(**over):
    base = {"repo": "Strike48/matrix", "number": 42, "title": "t", "body": "b",
            "author": {"login": "someone-else"}, "labels": []}
    base.update(over)
    return base


def _writes_recorder():
    calls = []

    def run(args, capture_output=True, text=True):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run, calls


# --- label intersection ----------------------------------------------------

def test_label_plan_keeps_only_existing():
    action = {"action": "apply-label", "args": {"labels": ["type/bug", "made-up"]}}
    planned = t.plan_label_action(action, existing={"type/bug", "area/x"})
    assert planned.args["labels"] == ["type/bug"]
    assert "made-up" in planned.note


def test_label_plan_all_dropped_when_none_exist():
    action = {"action": "apply-label", "args": {"labels": ["new1", "new2"]}}
    planned = t.plan_label_action(action, existing=set())
    assert planned.args["labels"] == []


# --- dry-run vs live -------------------------------------------------------

_LABEL_VERDICT = (
    '{"artifact":"issue","verdict":"ready","summary":"clear bug",'
    '"suggested_actions":[{"action":"apply-label","args":{"labels":["type/bug"]}}]}'
)


def test_dry_run_writes_nothing():
    run, calls = _writes_recorder()
    out = t.process_issue(_issue(), "me", {"type/bug"}, dry_run=True,
                          client=_FakeLLM(_LABEL_VERDICT), model="m", run=run)
    assert out.verdict == "ready"
    assert calls == []  # no gh writes in dry-run
    assert out.auto_planned[0].args["labels"] == ["type/bug"]
    assert out.auto_planned[0].executed is False


def test_live_applies_existing_label():
    run, calls = _writes_recorder()
    out = t.process_issue(_issue(), "me", {"type/bug"}, dry_run=False,
                          client=_FakeLLM(_LABEL_VERDICT), model="m", run=run)
    assert any("edit" in c and "--add-label" in c for c in calls)
    assert out.auto_planned[0].executed is True


# --- smart-route missing info ----------------------------------------------

_NEEDS_INFO = (
    '{"artifact":"issue","verdict":"needs-info","summary":"vague",'
    '"missing_info":["repro steps?"],'
    '"suggested_actions":[{"action":"ask-reporter","args":{"questions":["repro steps?"]}}]}'
)


def test_reporter_other_posts_github_comment():
    run, calls = _writes_recorder()
    out = t.process_issue(_issue(author={"login": "other"}), "me", set(), dry_run=False,
                          client=_FakeLLM(_NEEDS_INFO), model="m", run=run)
    assert out.ask_target == "reporter"
    assert any("comment" in c for c in calls)


def test_reporter_is_operator_routes_to_slack_not_github():
    run, calls = _writes_recorder()
    out = t.process_issue(_issue(author={"login": "me"}), "me", set(), dry_run=False,
                          client=_FakeLLM(_NEEDS_INFO), model="m", run=run)
    assert out.ask_target == "operator"
    assert out.missing_info == ["repro steps?"]
    # Nothing posted to GitHub — it's routed to Slack instead.
    assert not any("comment" in c for c in calls)


# --- gated actions never auto-run ------------------------------------------

_CLOSE_VERDICT = (
    '{"artifact":"issue","verdict":"wont-fix","summary":"out of scope",'
    '"suggested_actions":[{"action":"close","args":{"reason":"oos"}}]}'
)


def test_gated_close_is_not_executed_even_live():
    run, calls = _writes_recorder()
    out = t.process_issue(_issue(), "me", set(), dry_run=False,
                          client=_FakeLLM(_CLOSE_VERDICT), model="m", run=run)
    assert any(g["action"] == "close" for g in out.gated_planned)
    assert not any("close" in c for c in calls)  # never auto-closed


# --- bad LLM output fails soft ---------------------------------------------

def test_non_json_verdict_yields_error_outcome():
    out = t.process_issue(_issue(), "me", set(), dry_run=True,
                          client=_FakeLLM("sorry, I cannot"), model="m")
    assert out.error and out.verdict == ""


def test_json_object_parser_tolerates_code_fence():
    obj = t._parse_json_object('```json\n{"verdict":"ready"}\n```')
    assert obj == {"verdict": "ready"}
