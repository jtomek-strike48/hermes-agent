"""Handlers for the triage DM reply loop.

Two entry points, called from ``__init__.register``:

* ``handle_answer_click`` (async) — the "Answer in DM" button. Records a
  pending-answer session and DMs the operator the reviewer's questions.
* ``maybe_capture_answer`` (SYNC) — the ``pre_gateway_dispatch`` hook body. If
  the inbound message is a DM from a user with a pending triage answer, post the
  answer to the GitHub issue, clear the state, and return ``{"action":"skip"}``
  to consume it. Otherwise return None (normal dispatch) — this hook fires for
  EVERY inbound message, so it must be conservative and fail OPEN.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

from . import github_writeback as wb
from . import store

logger = logging.getLogger(__name__)


# --- button click: record pending + DM the questions ----------------------

async def handle_answer_click(body: dict, action: dict, *, client, store_path=None) -> None:
    """The operator clicked "Answer in DM". ``action['value']`` carries the
    store key of the triaged issue (repo#number); the digest-side staging
    recorded the questions. Open a DM, post the questions, and record a
    pending-answer session keyed to that DM + user."""
    user = body.get("user", {}) or {}
    user_id = user.get("id", "")
    if not user_id:
        return

    # The button value is a JSON blob {repo, number, ref, questions, asked_ts}
    # (small; well under Slack's 2000-char value cap for a handful of questions).
    try:
        payload = json.loads(action.get("value", "{}"))
    except (ValueError, TypeError):
        logger.warning("[triage_buttons] bad answer-button value")
        return
    repo, number = payload.get("repo", ""), payload.get("number")
    questions = payload.get("questions", []) or []
    if not repo or number is None:
        return

    dm_id = _open_dm(user_id)
    if not dm_id:
        logger.error("[triage_buttons] could not open DM with %s", user_id)
        return

    store.put(
        store.key_for("slack", dm_id, user_id),
        {"repo": repo, "number": number, "ref": payload.get("ref", f"{repo}#{number}"),
         "questions": questions, "asked_ts": payload.get("asked_ts", "")},
        path=store_path,
    )

    qlines = "\n".join(f"• {q}" for q in questions) or "(clarify the issue)"
    text = (f"You're clarifying *{repo}#{number}*. Reply here with the details and "
            f"I'll add them to the issue:\n{qlines}")
    try:
        await client.chat_postMessage(
            channel=dm_id, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception as exc:
        logger.error("[triage_buttons] DM post failed: %s", exc)


# --- pre_gateway_dispatch hook: capture the answer ------------------------

def maybe_capture_answer(event) -> Optional[Dict[str, Any]]:
    """SYNC. Return {"action":"skip"} iff this inbound message is a DM answer to
    a pending triage question (and it was posted to the issue). Else None.

    Fail OPEN: any uncertainty → None (let the message dispatch normally). This
    runs for every inbound message, so it must never eat an unrelated one."""
    src = getattr(event, "source", None)
    if src is None:
        return None
    platform = _platform_name(getattr(src, "platform", None))
    if platform != "slack":
        return None
    # Only DMs — never intercept a channel message.
    if getattr(src, "chat_type", "") not in ("dm", "im"):
        return None
    user_id = getattr(src, "user_id", "") or ""
    dm_id = getattr(src, "chat_id", "") or ""
    if not user_id or not dm_id:
        return None

    key = store.key_for("slack", dm_id, user_id)
    entry = store.peek(key)
    if entry is None:
        return None  # no pending answer → normal dispatch

    text = (getattr(event, "text", "") or "").strip()
    if not text or text.startswith("/") or text.startswith("!"):
        # A command in the DM is not an answer — leave the pending entry and let
        # it dispatch (the operator can still run /stop etc.).
        return None

    # Consume the pending entry (one-shot) and post the answer to the issue.
    entry = store.pop(key)
    if entry is None:
        return None
    result = wb.post_answer(entry.get("repo", ""), entry.get("number"),
                            entry.get("questions", []) or [], text)
    ref = entry.get("ref", f"{entry.get('repo','')}#{entry.get('number','')}")
    if result.ok:
        _dm(dm_id, f"Added your clarification to {ref}. Thanks — that unblocks it.")
    else:
        # Re-stage so the operator can retry, and tell them why.
        store.put(key, entry)
        _dm(dm_id, f"Couldn't post to {ref}: {result.detail}. Reply again to retry.")
    # Consume the message either way — it was an answer, not a new agent turn.
    return {"action": "skip", "reason": f"triage answer for {ref}"}


# --- minimal Slack helpers (stdlib, sync — usable from the sync hook) ------

def _bot_token() -> str:
    raw = os.environ.get("SLACK_BOT_TOKEN", "")
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    return toks[0] if toks else ""


def _slack_post(method: str, payload: dict) -> dict:
    tok = _bot_token()
    if not tok:
        return {"ok": False, "error": "no_token"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data, method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network
        logger.warning("[triage_buttons] slack %s failed: %s", method, exc)
        return {"ok": False, "error": str(exc)}


def _open_dm(user_id: str) -> str:
    resp = _slack_post("conversations.open", {"users": user_id})
    if resp.get("ok"):
        return (resp.get("channel") or {}).get("id", "")
    return ""


def _dm(channel: str, text: str) -> None:
    _slack_post("chat.postMessage", {"channel": channel, "text": text})


def _platform_name(platform) -> str:
    """Normalize a Platform enum / string to its lowercase name."""
    if platform is None:
        return ""
    val = getattr(platform, "value", None)
    return str(val if val is not None else platform).lower()
