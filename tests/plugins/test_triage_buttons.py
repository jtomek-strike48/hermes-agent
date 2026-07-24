"""Tests for triage_buttons — the DM reply loop.

The load-bearing, highest-risk piece is ``maybe_capture_answer``: it runs on
EVERY inbound message via pre_gateway_dispatch, so it must fail OPEN (return
None → normal dispatch) in every case except a genuine pending-DM-answer match.
These tests pin that down, plus the store round-trip and the writeback body.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from plugins.triage_buttons import github_writeback as wb
from plugins.triage_buttons import handlers, store


# --- store -----------------------------------------------------------------

def test_store_put_peek_pop(tmp_path: Path):
    p = tmp_path / "t.json"
    key = store.key_for("slack", "D1", "U1")
    store.put(key, {"repo": "o/r", "number": 5, "ref": "o/r#5",
                    "questions": ["q1"], "asked_ts": "1.0"}, path=p)
    assert store.peek(key, path=p)["number"] == 5
    assert store.pop(key, path=p)["repo"] == "o/r"
    assert store.pop(key, path=p) is None  # one-shot


# --- writeback body --------------------------------------------------------

def test_answer_comment_pairs_questions_and_answer():
    body = wb.build_answer_comment(["repro?", "expected?"], "It crashes on empty input.")
    assert "repro?" in body and "It crashes on empty input." in body
    assert "Mercury" in body


def test_post_answer_empty_is_rejected():
    res = wb.post_answer("o/r", 1, ["q"], "   ", run=lambda *a, **k: None)
    assert res.ok is False and "empty" in res.detail


def test_post_answer_success():
    def run(args, capture_output=True, text=True):
        assert "comment" in args
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    res = wb.post_answer("o/r", 1, ["q"], "answer", run=run)
    assert res.ok is True


def test_post_answer_gh_failure_surfaces_detail():
    def run(args, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")
    res = wb.post_answer("o/r", 1, ["q"], "answer", run=run)
    assert res.ok is False and "not found" in res.detail


# --- maybe_capture_answer: FAIL OPEN in every non-match case ---------------

def _event(platform="slack", chat_type="im", user_id="U1", chat_id="D1", text="my answer"):
    src = SimpleNamespace(platform=platform, chat_type=chat_type,
                          user_id=user_id, chat_id=chat_id)
    return SimpleNamespace(source=src, text=text)


def _seed(tmp_path, **over):
    p = tmp_path / "t.json"
    key = store.key_for("slack", "D1", "U1")
    entry = {"repo": "o/r", "number": 5, "ref": "o/r#5", "questions": ["q1"], "asked_ts": ""}
    entry.update(over)
    store.put(key, entry, path=p)
    return p, key


def test_no_pending_entry_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "default_path", lambda: tmp_path / "empty.json")
    assert handlers.maybe_capture_answer(_event()) is None


def test_channel_message_never_intercepted(monkeypatch, tmp_path):
    p, _ = _seed(tmp_path)
    monkeypatch.setattr(store, "default_path", lambda: p)
    # A channel message (not a DM) must pass through even with a pending entry.
    assert handlers.maybe_capture_answer(_event(chat_type="channel")) is None


def test_non_slack_platform_ignored(monkeypatch, tmp_path):
    p, _ = _seed(tmp_path)
    monkeypatch.setattr(store, "default_path", lambda: p)
    assert handlers.maybe_capture_answer(_event(platform="discord")) is None


def test_command_in_dm_not_treated_as_answer(monkeypatch, tmp_path):
    p, _ = _seed(tmp_path)
    monkeypatch.setattr(store, "default_path", lambda: p)
    assert handlers.maybe_capture_answer(_event(text="/stop")) is None
    # entry must survive (not consumed) so the operator can still answer later
    assert store.peek(store.key_for("slack", "D1", "U1"), path=p) is not None


def test_pending_dm_answer_is_captured_and_posted(monkeypatch, tmp_path):
    p, key = _seed(tmp_path)
    monkeypatch.setattr(store, "default_path", lambda: p)
    posted = {}
    monkeypatch.setattr(wb, "post_answer",
                        lambda repo, num, qs, ans, **k: posted.update(repo=repo, num=num, ans=ans)
                        or wb.WritebackResult(ok=True, detail="ok"))
    monkeypatch.setattr(handlers, "_dm", lambda *a, **k: None)

    result = handlers.maybe_capture_answer(_event(text="Here are the repro steps."))
    assert result == {"action": "skip", "reason": "triage answer for o/r#5"}
    assert posted == {"repo": "o/r", "num": 5, "ans": "Here are the repro steps."}
    # consumed one-shot
    assert store.peek(key, path=p) is None


def test_failed_post_restages_for_retry(monkeypatch, tmp_path):
    p, key = _seed(tmp_path)
    monkeypatch.setattr(store, "default_path", lambda: p)
    monkeypatch.setattr(wb, "post_answer",
                        lambda *a, **k: wb.WritebackResult(ok=False, detail="gh boom"))
    monkeypatch.setattr(handlers, "_dm", lambda *a, **k: None)

    result = handlers.maybe_capture_answer(_event(text="answer"))
    assert result["action"] == "skip"
    # re-staged so the operator can retry
    assert store.peek(key, path=p) is not None


def test_missing_source_returns_none():
    assert handlers.maybe_capture_answer(SimpleNamespace(source=None, text="x")) is None
