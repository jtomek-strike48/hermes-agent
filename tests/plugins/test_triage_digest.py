"""Tests for the buttoned triage digest surface (triage_buttons plugin).

Covers the four new modules that turn a Triage v2 verdict into an interactive
Slack digest: digest_store (staging + double-click guard), blocks (button rows
per verdict), github_actions (gated gh writes), and digest_actions (click
handling + fail-closed auth). The reply-loop pieces are covered separately in
test_triage_buttons.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.triage_buttons import blocks, digest_actions, digest_store, github_actions


# --- digest_store ----------------------------------------------------------

def _entry(**over):
    e = {
        "repo": "o/r", "number": 5, "ref": "o/r#5", "title": "t", "url": "u",
        "verdict": "needs-info", "summary": "s", "questions": ["q1"],
        "ask_operator": True, "gated": [{"action": "set-priority", "args": {"label": "P1: High"}}],
    }
    e.update(over)
    return e


def test_stage_get_roundtrip(tmp_path: Path):
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)
    assert key == "o/r#5"
    got = digest_store.get(key, path=p)
    assert got["verdict"] == "needs-info"
    assert got["gated"][0]["action"] == "set-priority"
    assert got["done"] == []  # fresh stage resets the guard


def test_stage_drops_unknown_fields(tmp_path: Path):
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(evil="DROP ME", message_ts="should-not-persist"), path=p)
    got = digest_store.get(key, path=p)
    assert "evil" not in got
    # message_ts is set by mark_published, not carried in on stage
    assert got.get("message_ts") is None


def test_stage_normalizes_none_lists(tmp_path: Path):
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(questions=None, gated=None, ask_operator=None), path=p)
    got = digest_store.get(key, path=p)
    assert got["questions"] == [] and got["gated"] == [] and got["ask_operator"] is False


def test_mark_published_sets_ts(tmp_path: Path):
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)
    digest_store.mark_published(key, message_ts="1700.1", path=p)
    assert digest_store.get(key, path=p)["message_ts"] == "1700.1"


def test_mark_action_done_is_one_shot(tmp_path: Path):
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)
    assert digest_store.mark_action_done(key, "set-priority", path=p) is True
    # second attempt on the same action → False (double-click guard)
    assert digest_store.mark_action_done(key, "set-priority", path=p) is False
    # a different action is still allowed
    assert digest_store.mark_action_done(key, "close", path=p) is True


def test_mark_action_done_missing_entry_returns_false(tmp_path: Path):
    p = tmp_path / "d.json"
    assert digest_store.mark_action_done("nope#1", "close", path=p) is False


def test_clear_unpublished_keeps_only_posted(tmp_path: Path):
    p = tmp_path / "d.json"
    posted = digest_store.stage(_entry(number=1), path=p)
    digest_store.mark_published(posted, message_ts="1.0", path=p)
    digest_store.stage(_entry(number=2), path=p)  # unpublished
    digest_store.clear_unpublished(path=p)
    data = digest_store.list_all(path=p)
    assert set(data) == {"o/r#1"}


def test_load_corrupt_file_returns_empty(tmp_path: Path):
    p = tmp_path / "d.json"
    p.write_text("{ not json")
    assert digest_store.load(p) == {}


# --- blocks ----------------------------------------------------------------

def _staged_item(**over):
    it = _entry(**over)
    it["_key"] = digest_store.key_for(it["repo"], it["number"])
    return it


def test_answer_button_carries_reply_loop_payload():
    item = _staged_item()
    row = blocks._action_row(item)
    btn = row["elements"][0]
    assert btn["action_id"] == blocks.ACTION_ANSWER
    payload = json.loads(btn["value"])
    assert payload["repo"] == "o/r" and payload["number"] == 5
    assert payload["questions"] == ["q1"]


def test_answer_button_value_bounded_under_slack_cap():
    # A verbose reviewer (many long questions) must NOT produce a >2000-char
    # button value — Slack would reject the whole digest post otherwise.
    item = _staged_item(questions=[
        f"This is a fairly long clarifying question number {i} the model might emit " * 3
        for i in range(20)
    ])
    row = blocks._action_row(item)
    btn = row["elements"][0]
    assert btn["action_id"] == blocks.ACTION_ANSWER
    assert len(btn["value"]) <= 2000
    # still valid JSON the reply loop can parse, with repo/number preserved
    payload = json.loads(btn["value"])
    assert payload["repo"] == "o/r" and payload["number"] == 5
    assert len(payload["questions"]) >= 1  # at least one question survives


def test_gated_button_carries_only_store_key():
    # An issue with a gated action but no operator questions → single gated button.
    item = _staged_item(ask_operator=False, questions=[])
    row = blocks._action_row(item)
    assert len(row["elements"]) == 1
    btn = row["elements"][0]
    assert btn["action_id"] == blocks.ACTION_SET_PRIORITY
    assert btn["value"] == "o/r#5"  # store key, not a JSON blob
    assert "P1: High" in btn["text"]["text"]  # proposed label surfaced


def test_no_actions_yields_no_row():
    item = _staged_item(ask_operator=False, questions=[], gated=[])
    assert blocks._action_row(item) is None


def test_unknown_gated_action_is_skipped():
    item = _staged_item(ask_operator=False, questions=[],
                        gated=[{"action": "launch-missiles", "args": {}}])
    assert blocks._action_row(item) is None


def test_build_digest_respects_block_ceiling():
    items = [_staged_item(number=i) for i in range(60)]
    repos = [{"repo": "o/r", "items": items}]
    bk = blocks.build_digest_blocks(repos, live=False)
    assert len(bk) <= blocks.MAX_BLOCKS
    assert any("truncated" in str(b) for b in bk)


def test_build_digest_header_reflects_live_flag():
    bk_dry = blocks.build_digest_blocks([], live=False)
    bk_live = blocks.build_digest_blocks([], live=True)
    assert any("dry-run" in str(b) for b in bk_dry)
    assert any("live" in str(b) for b in bk_live)


# --- github_actions (injected run, never hits network) ---------------------

def _ok_run(expect_sub):
    def run(args, capture_output=True, text=True):
        assert expect_sub in args
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_set_priority_adds_label():
    calls = {}

    def run(args, capture_output=True, text=True):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = github_actions.set_priority("o/r", 5, {"label": "P1: High"}, run=run)
    assert res.ok
    assert "--add-label" in calls["args"] and "P1: High" in calls["args"]


def test_set_priority_without_label_refuses():
    res = github_actions.set_priority("o/r", 5, {}, run=_ok_run("edit"))
    assert res.ok is False and "no priority" in res.detail


def test_close_uses_completed_reason():
    calls = {}

    def run(args, capture_output=True, text=True):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = github_actions.close_issue("o/r", 5, {"reason": "done"}, run=run)
    assert res.ok
    assert "close" in calls["args"] and "completed" in calls["args"]


def test_wont_fix_uses_not_planned():
    calls = {}

    def run(args, capture_output=True, text=True):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = github_actions.wont_fix("o/r", 5, {}, run=run)
    assert res.ok and "not planned" in calls["args"]


def test_mark_duplicate_requires_target():
    res = github_actions.mark_duplicate("o/r", 5, {}, run=_ok_run("comment"))
    assert res.ok is False and "duplicate target" in res.detail


def test_mark_duplicate_comments_then_closes():
    seen = []

    def run(args, capture_output=True, text=True):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = github_actions.mark_duplicate("o/r", 5, {"of": "o/r#1"}, run=run)
    assert res.ok
    assert any("comment" in a for a in seen) and any("close" in a for a in seen)


def test_execute_unknown_action_refuses():
    res = github_actions.execute("nuke", "o/r", 5, {}, run=_ok_run("x"))
    assert res.ok is False and "unknown" in res.detail


def test_gh_failure_surfaces_stderr():
    def run(args, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="label not found")

    res = github_actions.set_priority("o/r", 5, {"label": "P9"}, run=run)
    assert res.ok is False and "label not found" in res.detail


# --- digest_actions: auth + dispatch --------------------------------------

def test_is_authorized_fails_closed_when_unset(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    assert digest_actions.is_authorized("U1") is False


def test_is_authorized_allows_listed(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1,U2")
    assert digest_actions.is_authorized("U1") is True
    assert digest_actions.is_authorized("U9") is False


def test_is_authorized_wildcard(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "*")
    assert digest_actions.is_authorized("anyone") is True


class _FakeClient:
    def __init__(self):
        self.updates = []
        self.threads = []

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    async def chat_postMessage(self, **kwargs):
        self.threads.append(kwargs)


def _click_body(user="U1", key="o/r#5"):
    return {"user": {"id": user, "name": "op"}, "channel": {"id": "C1"},
            "message": {"ts": "1700.1"}}


async def _noack():
    return None


def _run_action(action_id, body, *, store_path, monkeypatch, gh_ok=True):
    """Drive digest_actions.handle_action with a stubbed github_actions.execute."""
    monkeypatch.setattr(github_actions, "execute",
                        lambda *a, **k: github_actions.ActionResult(ok=gh_ok, detail="detail"))
    client = _FakeClient()
    action = {"action_id": action_id, "value": body["_key"]}
    asyncio.run(digest_actions.handle_action(
        _noack, body, action, client=client, store_path=store_path))
    return client


def test_authorized_gated_click_executes_and_updates(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)
    digest_store.mark_published(key, message_ts="1700.1", path=p)

    body = _click_body(); body["_key"] = key
    client = _run_action(blocks.ACTION_SET_PRIORITY, body, store_path=p, monkeypatch=monkeypatch)

    assert client.updates, "expected the message to be rewritten on success"
    # action recorded (double-click guard) — a second identical click no-ops
    assert digest_store.get(key, path=p)["done"] == ["set-priority"]


def test_unauthorized_click_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)

    body = _click_body(); body["_key"] = key
    # execute must never be called; assert by making it explode if it is
    monkeypatch.setattr(github_actions, "execute",
                        lambda *a, **k: pytest.fail("gated write ran without auth"))
    client = _FakeClient()
    action = {"action_id": blocks.ACTION_SET_PRIORITY, "value": key}
    asyncio.run(digest_actions.handle_action(_noack, body, action, client=client, store_path=p))
    assert not client.updates and not client.threads
    assert digest_store.get(key, path=p)["done"] == []  # nothing claimed


def test_failed_write_unclaims_for_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)

    body = _click_body(); body["_key"] = key
    client = _run_action(blocks.ACTION_CLOSE, body, store_path=p, monkeypatch=monkeypatch, gh_ok=False)

    assert client.threads, "expected a failure explanation in-thread"
    # un-claimed so the operator can click again
    assert digest_store.get(key, path=p)["done"] == []


def test_double_click_is_guarded(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    p = tmp_path / "d.json"
    key = digest_store.stage(_entry(), path=p)
    body = _click_body(); body["_key"] = key

    calls = {"n": 0}

    def _exec(*a, **k):
        calls["n"] += 1
        return github_actions.ActionResult(ok=True, detail="ok")

    monkeypatch.setattr(github_actions, "execute", _exec)
    client = _FakeClient()
    action = {"action_id": blocks.ACTION_SET_PRIORITY, "value": key}
    asyncio.run(digest_actions.handle_action(_noack, body, action, client=client, store_path=p))
    asyncio.run(digest_actions.handle_action(_noack, body, action, client=client, store_path=p))
    assert calls["n"] == 1, "second click must be guarded, not re-executed"
