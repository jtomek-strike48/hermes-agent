"""Slack button-click handling for the gated PM actions on the triage digest.

Four action ids, wired in ``__init__.register``:

  triage_set_priority  → add the proposed priority label
  triage_close         → close the issue (reason: completed)
  triage_mark_duplicate→ comment the canonical ref, close as not-planned
  triage_wont_fix      → close as not-planned with a reason

(The fifth digest button, ``triage_answer`` / "Answer in DM", is the reply loop
and is handled by ``handlers.handle_answer_click`` — not here.)

Every handler acks within Slack's 3s budget, authorizes the clicker against
``SLACK_ALLOWED_USERS`` (fail-closed — clicks trigger GitHub writes), and records
the executed action in the digest store so a double-click can't fire it twice.
"""

from __future__ import annotations

import logging
import os

from . import blocks, digest_store, github_actions

logger = logging.getLogger(__name__)

# Slack action_id → reviewer-contract action name (github_actions.execute vocab).
_ACTION_TO_CONTRACT = {
    blocks.ACTION_SET_PRIORITY: "set-priority",
    blocks.ACTION_CLOSE: "close",
    blocks.ACTION_DUPLICATE: "mark-duplicate",
    blocks.ACTION_WONT_FIX: "wont-fix",
}

# Past tense for the resolved message.
_DONE_VERB = {
    "set-priority": "Priority set",
    "close": "Closed",
    "mark-duplicate": "Marked duplicate",
    "wont-fix": "Closed (won't fix)",
}


def is_authorized(user_id: str) -> bool:
    """Authorize a clicker against ``SLACK_ALLOWED_USERS``. Fails CLOSED:
    unset/empty → deny; ``*`` → everyone; else must be in the allowlist.

    Clicking a gated button writes to GitHub (label/close/duplicate) — this
    handler is the only gate (the adapter's plugin wrapper does not run the
    gateway's own interactive-auth check), so an unset allowlist must never mean
    "allow all"."""
    raw = os.environ.get("SLACK_ALLOWED_USERS")
    if raw is None or not raw.strip():
        logger.warning(
            "[triage_buttons] SLACK_ALLOWED_USERS not set — denying click by %s "
            "(gated buttons write to GitHub; set the allowlist to enable them)", user_id,
        )
        return False
    allowed = {u.strip() for u in raw.split(",") if u.strip()}
    return "*" in allowed or user_id in allowed


def _ctx(body: dict) -> tuple[str, str, str, str]:
    user = body.get("user", {}) or {}
    channel = (body.get("channel", {}) or {}).get("id", "")
    msg_ts = (body.get("message", {}) or {}).get("ts", "")
    return user.get("id", ""), user.get("name", user.get("id", "unknown")), channel, msg_ts


def _find_gated_args(entry: dict, contract_action: str) -> dict:
    """The args the reviewer proposed for this gated action (from the staged
    entry's ``gated`` list). Empty dict if not found — the executor then decides
    whether it can proceed (e.g. mark-duplicate refuses without a target)."""
    for g in entry.get("gated") or []:
        if isinstance(g, dict) and g.get("action") == contract_action:
            return g.get("args", {}) if isinstance(g.get("args"), dict) else {}
    return {}


async def handle_action(ack, body, action, *, client, store_path=None) -> None:
    """Dispatch one gated button click. Always acks first."""
    await ack()

    action_id = action.get("action_id", "")
    key = action.get("value", "")
    user_id, user_name, channel, msg_ts = _ctx(body)

    contract_action = _ACTION_TO_CONTRACT.get(action_id)
    if contract_action is None:
        logger.warning("[triage_buttons] unknown gated action_id %r", action_id)
        return
    if not user_id:
        logger.warning("[triage_buttons] click with no user id on %s — ignoring", key)
        return
    if not is_authorized(user_id):
        logger.warning("[triage_buttons] unauthorized click by %s (%s) on %s", user_name, user_id, key)
        return

    entry = digest_store.get(key, path=store_path)
    if entry is None:
        logger.info("[triage_buttons] no staged issue for %s (already actioned?)", key)
        return

    # Double-click guard: claim the action atomically before writing. If it was
    # already done (or the entry vanished), do nothing.
    if not digest_store.mark_action_done(key, contract_action, path=store_path):
        logger.info("[triage_buttons] %s on %s already actioned", contract_action, key)
        return

    repo, number = entry.get("repo", ""), entry.get("number")
    args = _find_gated_args(entry, contract_action)
    result = github_actions.execute(contract_action, repo, number, args)

    item = dict(entry, _key=key)
    verb = _DONE_VERB.get(contract_action, contract_action)
    if result.ok:
        await _update(
            client, channel, msg_ts,
            text=f"{entry.get('ref', key)}: {verb.lower()} by {user_name}",
            blocks_payload=blocks.build_resolved_blocks(
                item, text=f"*{verb}* by {user_name}.",
                context=f"{entry.get('ref', key)} — {result.detail}"),
        )
        logger.info("[triage_buttons] %s %s by %s (%s)", contract_action, key, user_name, result.detail)
    else:
        # Un-claim so the operator can retry, and surface why it failed.
        _unmark(key, contract_action, store_path)
        await _post_thread(
            client, channel, msg_ts,
            f"*Couldn't {verb.lower()} {entry.get('ref', key)}* ({user_name}): {result.detail}\n"
            f"Fix that and click again.",
        )
        logger.warning("[triage_buttons] %s FAILED for %s: %s", contract_action, key, result.detail)


def _unmark(key: str, contract_action: str, store_path) -> None:
    """Best-effort rollback of the double-click claim so a failed write can be
    retried. Reloads, drops the action from ``done``, rewrites."""
    try:
        path = store_path or digest_store.default_path()
        data = digest_store.load(path)
        entry = data.get(key)
        if entry and contract_action in (entry.get("done") or []):
            entry["done"] = [a for a in entry["done"] if a != contract_action]
            digest_store._write(path, data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[triage_buttons] could not un-claim %s/%s: %s", key, contract_action, exc)


# --- Slack message helpers -------------------------------------------------

async def _update(client, channel, ts, *, text, blocks_payload) -> None:
    try:
        await client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks_payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[triage_buttons] chat_update failed for %s@%s: %s", channel, ts, exc)


async def _post_thread(client, channel, ts, text: str) -> None:
    try:
        await client.chat_postMessage(
            channel=channel, thread_ts=ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[triage_buttons] thread post failed for %s@%s: %s", channel, ts, exc)
