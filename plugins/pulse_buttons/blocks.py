"""Slack Block Kit builders for the Project Pulse buttoned digest.

One message groups the surfaced items by repo; each item renders a title line
(linked to the PR/issue) plus a bucket-appropriate action row:

  ready     → [Double-check & merge]
  awaiting  → [Review with Claude]  [Double-check & merge]
  blocked   → [Review with Claude]  [How do I unblock?]
  new_prs   → [Review with Claude]   (PRs only; issues get no action)
  rotting   → (no button — informational)

The button ``value`` is only the store key; the item body lives in the store,
well under Slack's 2000-char value cap. Pure dict builders — no client, no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, List

MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000

Block = Dict[str, Any]

# action_id → button spec. Kept here so actions.py and blocks.py agree on ids.
ACTION_REVIEW = "pulse_review"
ACTION_MERGE_CHECK = "pulse_merge_check"
ACTION_MERGE_CONFIRM = "pulse_merge_confirm"
ACTION_UNBLOCK = "pulse_unblock"

_BUCKET_HEADINGS = [
    ("ready", "Ready to merge"),
    ("awaiting", "Green, awaiting your review"),
    ("blocked", "Blocked on you"),
    ("new_prs", "New since last pulse"),
    ("rotting", "Rotting P0/P1"),
]


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _title_line(item: Dict[str, Any]) -> str:
    repo = item.get("repo", "")
    num = item.get("number", "")
    url = item.get("url", "")
    title = (item.get("title") or "")[:110]
    detail = item.get("detail") or ""
    label = f"{repo.split('/')[-1]}#{num}"
    linked = f"<{url}|{label}>" if url else label
    parts = [f"*{linked}*"]
    if title:
        parts.append(title)
    line = " — ".join(parts)
    return f"{line}\n_{detail}_" if detail else line


def _btn(text: str, action_id: str, key: str, style: str | None = None) -> Block:
    b = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": key,
    }
    if style:
        b["style"] = style
    return b


def _action_row(bucket: str, item: Dict[str, Any]) -> Block | None:
    """Bucket-appropriate button row, or None for informational items."""
    key = item["_key"]
    is_pr = item.get("kind", "pr") == "pr"
    elements: List[Block] = []

    if bucket == "ready" and is_pr:
        elements.append(_btn("Double-check & merge", ACTION_MERGE_CHECK, key, "primary"))
    elif bucket == "awaiting" and is_pr:
        elements.append(_btn("Review with Claude", ACTION_REVIEW, key, "primary"))
        elements.append(_btn("Double-check & merge", ACTION_MERGE_CHECK, key))
    elif bucket == "blocked" and is_pr:
        elements.append(_btn("Review with Claude", ACTION_REVIEW, key))
        elements.append(_btn("How do I unblock?", ACTION_UNBLOCK, key, "primary"))
    elif bucket == "new_prs" and is_pr:
        elements.append(_btn("Review with Claude", ACTION_REVIEW, key))
    # rotting, and any issue item, get no action row.

    if not elements:
        return None
    return {"type": "actions", "block_id": f"pulse::{key}", "elements": elements}


def build_digest_blocks(repos: List[Dict[str, Any]]) -> List[Block]:
    """Build the buttoned digest.

    ``repos`` is a list of ``{"repo": str, "buckets": {bucket: [item,...]}}``
    where each item carries ``_key`` (its store key). Guarantees the result
    stays within Slack's 50-block ceiling by capping items per bucket and
    noting the overflow.
    """
    blocks: List[Block] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Project Pulse", "emoji": False}},
    ]
    for entry in repos:
        repo = entry["repo"]
        buckets = entry["buckets"]
        if not any(buckets.get(k) for k, _ in _BUCKET_HEADINGS):
            continue
        blocks.append({"type": "divider"})
        blocks.append(_section(f"*{repo.split('/')[-1]}*"))
        for bucket, heading in _BUCKET_HEADINGS:
            items = buckets.get(bucket) or []
            if not items:
                continue
            blocks.append(_context(heading))
            for item in items[:6]:
                if len(blocks) >= MAX_BLOCKS - 2:
                    blocks.append(_context("…more items truncated to fit Slack's block limit"))
                    return blocks
                blocks.append(_section(_title_line(item)))
                row = _action_row(bucket, item)
                if row is not None:
                    blocks.append(row)
            if len(items) > 6:
                blocks.append(_context(f"…and {len(items) - 6} more"))
    return blocks


def build_confirm_merge_blocks(item: Dict[str, Any], gate_passed: List[str]) -> List[Block]:
    """The two-step merge confirmation: show what passed + a Confirm button."""
    key = item["_key"] if "_key" in item else f"{item.get('repo')}#{item.get('number')}"
    passed = "\n".join(f"• {p}" for p in gate_passed) or "• (all checks passed)"
    return [
        _section(_title_line(item)),
        _section(f"*Double-check passed:*\n{passed}\n\nMerge (squash)?"),
        {
            "type": "actions",
            "block_id": f"pulse-confirm::{key}",
            "elements": [
                _btn("Confirm merge (squash)", ACTION_MERGE_CONFIRM, key, "primary"),
            ],
        },
    ]


def build_resolved_blocks(item: Dict[str, Any], *, text: str, context: str) -> List[Block]:
    """A terminal state (merged / blocked / done) — no buttons, records outcome."""
    return [_section(_title_line(item)), _section(text), _context(context)]
