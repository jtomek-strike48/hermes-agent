"""Slack button-click handling for the Project Pulse digest.

Four action ids, wired in ``__init__.register``:

  pulse_review        → spawn a detached local ``/review-pr`` (result to thread)
  pulse_merge_check   → run the live merge gate; if it passes, rewrite the item
                        with a two-step "Confirm merge" button; else show why not
  pulse_merge_confirm → re-run the gate + squash-merge (the irreversible step)
  pulse_unblock       → gather changes-requested / failing-CI context, post
                        concrete next steps to the thread

Every handler acks within Slack's 3s budget, authorizes the clicker against
``SLACK_ALLOWED_USERS`` (fail-closed — clicks trigger writes), and updates the
message so an action can't be silently double-fired.
"""

from __future__ import annotations

import logging
import os

from . import blocks, github, store

logger = logging.getLogger(__name__)


def is_authorized(user_id: str) -> bool:
    """Authorize a clicker against ``SLACK_ALLOWED_USERS``. Fails CLOSED:
    unset/empty → deny; ``*`` → everyone; else must be in the allowlist.

    Clicking triggers merges and spawns local processes — this handler is the
    only gate (the adapter's plugin wrapper does not run the gateway's own
    interactive-auth check), so an unset allowlist must never mean "allow all".
    """
    raw = os.environ.get("SLACK_ALLOWED_USERS")
    if raw is None or not raw.strip():
        logger.warning(
            "[pulse_buttons] SLACK_ALLOWED_USERS not set — denying click by %s "
            "(buttons trigger writes; set the allowlist to enable them)", user_id,
        )
        return False
    allowed = {u.strip() for u in raw.split(",") if u.strip()}
    return "*" in allowed or user_id in allowed


def _ctx(body: dict) -> tuple[str, str, str, str]:
    user = body.get("user", {}) or {}
    channel = (body.get("channel", {}) or {}).get("id", "")
    msg_ts = (body.get("message", {}) or {}).get("ts", "")
    return user.get("id", ""), user.get("name", user.get("id", "unknown")), channel, msg_ts


async def handle_action(ack, body, action, *, client, store_path=None) -> None:
    """Dispatch one button click. Always acks first."""
    await ack()

    action_id = action.get("action_id", "")
    key = action.get("value", "")
    user_id, user_name, channel, msg_ts = _ctx(body)

    if not user_id:
        logger.warning("[pulse_buttons] click with no user id on %s — ignoring", key)
        return
    if not is_authorized(user_id):
        logger.warning("[pulse_buttons] unauthorized click by %s (%s) on %s", user_name, user_id, key)
        return

    entry = store.get(key, path=store_path)
    if entry is None:
        logger.info("[pulse_buttons] no staged item for %s (already actioned?)", key)
        return
    item = dict(entry, _key=key)

    if action_id == blocks.ACTION_REVIEW:
        await _do_review(client, channel, msg_ts, item, user_name)
    elif action_id == blocks.ACTION_MERGE_CHECK:
        await _do_merge_check(client, channel, msg_ts, item, user_name)
    elif action_id == blocks.ACTION_MERGE_CONFIRM:
        await _do_merge_confirm(client, channel, msg_ts, item, user_name, store_path)
    elif action_id == blocks.ACTION_UNBLOCK:
        await _do_unblock(client, channel, msg_ts, item, user_name)
    else:
        logger.warning("[pulse_buttons] unknown action_id %r", action_id)


async def _do_review(client, channel, msg_ts, item, user_name) -> None:
    """Spawn a detached local /review-pr; the child posts the result to the thread."""
    from . import local_review

    repo, number, url = item.get("repo", ""), item.get("number", ""), item.get("url", "")
    if local_review.clone_dir(repo) is None:
        await _post_thread(client, channel, msg_ts,
                           f"No local clone for {repo} — can't review here. (Expected under ~/Code.)")
        return
    ok = local_review.spawn_review(repo, number, url, channel, msg_ts)
    if ok:
        await _post_thread(
            client, channel, msg_ts,
            f"Started a local Claude review of {repo}#{number} on your laptop "
            f"(clicked by {user_name}). The full review will land in this thread when it finishes "
            f"(these are your own PRs, so nothing is posted to GitHub).",
        )
        logger.info("[pulse_buttons] spawned local review for %s#%s by %s", repo, number, user_name)
    else:
        await _post_thread(client, channel, msg_ts,
                           f"Could not start the local review for {repo}#{number} — see gateway logs.")


async def _do_merge_check(client, channel, msg_ts, item, user_name) -> None:
    """Run the live gate. Pass → rewrite the item with a Confirm-merge button.
    Fail → post exactly why (CI must be green — teamwork), buttons kept for retry."""
    repo, number = item.get("repo", ""), item.get("number", "")
    gate = github.check_merge_gate(repo, number)
    if gate.ok:
        # Persist the confirmed head so the confirm step can detect a moved head.
        item["head_sha"] = gate.head_sha or item.get("head_sha", "")
        try:
            store.stage(item, path=None)  # overwrite in place with the fresh head
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[pulse_buttons] could not persist confirmed head for %s: %s", item["_key"], exc)
        payload = blocks.build_confirm_merge_blocks(item, gate.passed)
        await _update(client, channel, msg_ts,
                     text=f"{repo}#{number}: double-check passed — confirm to merge",
                     blocks_payload=payload)
        logger.info("[pulse_buttons] merge gate PASSED for %s#%s (clicked by %s)", repo, number, user_name)
    else:
        reasons = "\n".join(f"• {r}" for r in gate.reasons)
        await _post_thread(
            client, channel, msg_ts,
            f"*Not ready to merge {repo}#{number}* (checked by {user_name}):\n{reasons}\n\n"
            f"Fix these and click *Double-check & merge* again.",
        )
        logger.info("[pulse_buttons] merge gate BLOCKED for %s#%s: %s", repo, number, gate.reasons)


async def _do_merge_confirm(client, channel, msg_ts, item, user_name, store_path) -> None:
    """The irreversible step. Atomically pop (double-click guard), re-run the
    gate inside squash_merge, and merge only if still green at the confirmed head."""
    repo, number = item.get("repo", ""), item.get("number", "")
    popped = store.pop(item["_key"], path=store_path)
    if popped is None:
        logger.info("[pulse_buttons] confirm-merge for %s already actioned", item["_key"])
        return

    result = github.squash_merge(repo, number, expected_head=popped.get("head_sha"))
    if result.ok:
        await _update(client, channel, msg_ts,
                     text=f"{repo}#{number} merged by {user_name}",
                     blocks_payload=blocks.build_resolved_blocks(
                         item, text=f"*Merged (squash)* by {user_name}.",
                         context=f"{repo}#{number} — {result.detail}"))
        logger.info("[pulse_buttons] MERGED %s#%s by %s", repo, number, user_name)
    else:
        # Re-stage so the operator can retry, and surface why it didn't merge.
        store.stage(popped, path=store_path)
        await _post_thread(
            client, channel, msg_ts,
            f"*Merge of {repo}#{number} did not proceed* ({user_name}): {result.detail}\n"
            f"Re-run *Double-check & merge* once resolved.",
        )
        logger.warning("[pulse_buttons] merge FAILED for %s#%s: %s", repo, number, result.detail)


async def _do_unblock(client, channel, msg_ts, item, user_name) -> None:
    """Gather changes-requested / failing-CI signals and post concrete steps."""
    repo, number = item.get("repo", ""), item.get("number", "")
    ctx = github.gather_unblock_context(repo, number)
    await _post_thread(client, channel, msg_ts, _format_unblock(repo, number, ctx))
    logger.info("[pulse_buttons] posted unblock guidance for %s#%s (clicked by %s)", repo, number, user_name)


def _format_unblock(repo: str, number, ctx: dict) -> str:
    """Turn the raw unblock context into a short, actionable message."""
    if "error" in ctx:
        return f"Couldn't analyze {repo}#{number}: {ctx['error']}"
    lines = [f"*How to unblock {repo}#{number}:*"]
    threads = ctx.get("unresolved_threads") or []
    checks = ctx.get("failing_checks") or []

    if checks:
        lines.append("\n*Failing CI — fix these first:*")
        for c in checks[:6]:
            u = f" <{c['url']}|logs>" if c.get("url") else ""
            lines.append(f"• {c['name']} ({c['conclusion']}){u}")
    if threads:
        lines.append("\n*Unresolved review comments to address:*")
        for t in threads[:6]:
            where = f"`{t['path']}` " if t.get("path") else ""
            who = f"{t['author']}: " if t.get("author") else ""
            body_first = (t.get("body") or "").splitlines()
            snippet = body_first[0][:160] if body_first else ""
            lines.append(f"• {where}{who}{snippet}")
    if not checks and not threads:
        ms = ctx.get("merge_state") or ""
        mg = ctx.get("mergeable") or ""
        if mg.upper() == "CONFLICTING":
            lines.append("\n• Merge conflicts — rebase onto the base branch and resolve.")
        elif ms:
            lines.append(f"\n• No failing CI or open comments, but merge state is `{ms}`. "
                        f"Likely needs a rebase or a required review/approval.")
        else:
            lines.append("\n• Nothing obviously blocking from GitHub — it may just need a rebase or an approval.")
    return "\n".join(lines)


# --- Slack message helpers -------------------------------------------------

async def _update(client, channel, ts, *, text, blocks_payload) -> None:
    try:
        await client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks_payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[pulse_buttons] chat_update failed for %s@%s: %s", channel, ts, exc)


async def _post_thread(client, channel, ts, text: str) -> None:
    """Post a threaded reply under the digest message (keeps the digest intact)."""
    try:
        await client.chat_postMessage(
            channel=channel, thread_ts=ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[pulse_buttons] thread post failed for %s@%s: %s", channel, ts, exc)
