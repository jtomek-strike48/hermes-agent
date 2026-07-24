"""Slack Block Kit builders for the buttoned triage digest.

One message groups the triaged issues by repo; each issue renders a verdict +
summary line (linked to the issue) plus an action row generated from its
verdict and the reviewer's gated suggested-actions:

  needs-info + you're the reporter → [Answer in DM]  (feeds the reply loop)
  <any> with gated actions         → [Set priority] [Close] [Mark duplicate] [Won't-fix]

The reversible auto-actions (apply-label, ask-reporter comment) are executed by
the sweep itself (subject to --live) and are NOT buttons — buttons are only for
things that need a human: answering your own under-specified issue, or the
consequential/gated PM actions.

Button ``value`` is only the store key for gated actions (the entry lives in the
digest store); the Answer-in-DM button carries a small JSON payload so the reply
loop's existing handler (handlers.handle_answer_click) can open the DM without a
second store lookup. Pure dict builders — no client, no I/O.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

MAX_BLOCKS = 50

Block = Dict[str, Any]

# action_id → shared with __init__/digest_actions. "triage_answer" is the reply
# loop's existing action (handled by handlers.handle_answer_click); the rest are
# the gated PM actions handled by digest_actions.handle_action.
ACTION_ANSWER = "triage_answer"
ACTION_SET_PRIORITY = "triage_set_priority"
ACTION_CLOSE = "triage_close"
ACTION_DUPLICATE = "triage_mark_duplicate"
ACTION_WONT_FIX = "triage_wont_fix"

# Reviewer-contract action name → (button label, action_id, style).
_GATED_BUTTONS = {
    "set-priority": ("Set priority", ACTION_SET_PRIORITY, None),
    "close": ("Close", ACTION_CLOSE, "danger"),
    "mark-duplicate": ("Mark duplicate", ACTION_DUPLICATE, "danger"),
    "wont-fix": ("Won't fix", ACTION_WONT_FIX, "danger"),
}

_VERDICT_EMOJI = {
    "ready": "🟢",
    "needs-info": "❓",
    "duplicate": "🔁",
    "wont-fix": "🚫",
    "stale": "🕸️",
}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _issue_line(item: Dict[str, Any]) -> str:
    repo = item.get("repo", "")
    num = item.get("number", "")
    url = item.get("url", "")
    verdict = item.get("verdict", "")
    summary = (item.get("summary") or item.get("title") or "")[:200]
    label = f"{repo.split('/')[-1]}#{num}"
    linked = f"<{url}|{label}>" if url else label
    emoji = _VERDICT_EMOJI.get(verdict, "•")
    head = f"{emoji} *{linked}* — `{verdict}`" if verdict else f"• *{linked}*"
    return f"{head}\n{summary}" if summary else head


def _btn(text: str, action_id: str, value: str, style: Optional[str] = None) -> Block:
    b = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        b["style"] = style
    return b


# Slack rejects the WHOLE message post if any button value exceeds 2000 chars,
# so a verbose LLM missing_info must never be embedded raw — bound it well under
# the cap (headroom for the JSON envelope + the repo/ref fields).
_VALUE_BUDGET = 1800
_MAX_QUESTIONS = 8
_MAX_Q_LEN = 300


def _bounded_answer_payload(item: Dict[str, Any]) -> str:
    """Serialize the reply-loop payload, guaranteed under Slack's value cap.

    Caps the number of questions and each question's length, then — as a hard
    backstop — drops trailing questions until the JSON fits ``_VALUE_BUDGET``.
    A verbose reviewer can shorten what the operator sees in the DM, but it can
    never break the whole digest post."""
    ref = item.get("ref", f"{item.get('repo','')}#{item.get('number','')}")
    questions = [str(q)[:_MAX_Q_LEN] for q in (item.get("questions") or [])][:_MAX_QUESTIONS]

    def _dump(qs: List[str]) -> str:
        return json.dumps({
            "repo": item.get("repo", ""),
            "number": item.get("number"),
            "ref": ref,
            "questions": qs,
            "asked_ts": item.get("message_ts", ""),
        }, ensure_ascii=False)

    payload = _dump(questions)
    while len(payload) > _VALUE_BUDGET and questions:
        questions.pop()
        payload = _dump(questions)
    return payload


def _answer_button(item: Dict[str, Any]) -> Block:
    """The "Answer in DM" button. Its value is the JSON payload the reply loop's
    ``handlers.handle_answer_click`` expects: {repo, number, ref, questions,
    asked_ts}, bounded to stay under Slack's ~2000-char value cap."""
    return _btn("Answer in DM", ACTION_ANSWER, _bounded_answer_payload(item), "primary")


def _action_row(item: Dict[str, Any]) -> Optional[Block]:
    """Build the button row for one triaged issue, or None if it has none.

    Answer-in-DM appears when the reviewer needs YOUR input (ask_operator). The
    gated PM buttons appear for each gated action the reviewer suggested."""
    key = item["_key"]
    elements: List[Block] = []

    if item.get("ask_operator") and item.get("questions"):
        elements.append(_answer_button(item))

    seen: set = set()
    for g in item.get("gated") or []:
        name = g.get("action", "") if isinstance(g, dict) else ""
        spec = _GATED_BUTTONS.get(name)
        if spec is None or name in seen:
            continue  # unknown / duplicate gated action → no button
        seen.add(name)
        label, action_id, style = spec
        # For set-priority, surface the proposed label in the button text so the
        # operator sees WHAT they're about to set before clicking.
        if name == "set-priority":
            prio = (g.get("args", {}) or {}).get("label") or (g.get("args", {}) or {}).get("priority")
            if prio:
                label = f"Set {prio}"
        elements.append(_btn(label, action_id, key, style))

    if not elements:
        return None
    return {"type": "actions", "block_id": f"triage::{key}", "elements": elements}


def build_digest_blocks(repos: List[Dict[str, Any]], *, live: bool) -> List[Block]:
    """Build the buttoned triage digest.

    ``repos`` is a list of ``{"repo": str, "items": [item, ...]}`` where each
    item carries ``_key`` (its store key), the verdict fields, and the auto
    actions already taken (``auto_summary``) so the digest is a faithful record
    of what the sweep did. ``live`` toggles the header note (dry-run vs live).

    Guarantees the result stays within Slack's 50-block ceiling by capping items
    per repo and noting the overflow.
    """
    mode = "live — auto-actions applied" if live else "dry-run — nothing written yet"
    blocks: List[Block] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Issue Triage", "emoji": False}},
        _context(f"Contract-based triage · {mode}"),
    ]
    for entry in repos:
        repo = entry["repo"]
        items = entry.get("items") or []
        if not items:
            continue
        blocks.append({"type": "divider"})
        blocks.append(_section(f"*{repo.split('/')[-1]}*"))
        for item in items:
            if len(blocks) >= MAX_BLOCKS - 2:
                blocks.append(_context("…more issues truncated to fit Slack's block limit"))
                return blocks
            blocks.append(_section(_issue_line(item)))
            auto = item.get("auto_summary") or ""
            if auto:
                blocks.append(_context(auto))
            row = _action_row(item)
            if row is not None:
                blocks.append(row)
    return blocks


def build_resolved_blocks(item: Dict[str, Any], *, text: str, context: str) -> List[Block]:
    """Replace an issue's action row after a gated click resolves it."""
    return [_section(_issue_line(item)), _section(text), _context(context)]
