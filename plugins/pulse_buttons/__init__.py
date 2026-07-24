"""pulse_buttons — next-step action buttons for the Project Pulse digest.

The pulse cron (``hermes pulse publish``) scans the operator's key repos, stages
each surfaced item, and posts ONE Block Kit digest to the Slack home channel
with a bucket-appropriate button per item:

* Review with Claude   — spawn a headless ``/review-pr`` on this laptop; the
  verbatim review lands in the digest thread (own PRs → Slack only, not GitHub).
* Double-check & merge — run the live merge gate (resolved threads + green CI +
  mergeable, fail-closed); on pass, present a two-step "Confirm merge" (squash).
* How do I unblock?     — analyze changes-requested comments / failing CI and
  post concrete next steps to the thread.

This module only wires the surfaces (CLI + Slack action handlers); the logic
lives in the sibling modules (store / blocks / github / actions / local_review /
cli). Mirrors the tested pr_review_buttons plugin architecture.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ACTION_IDS = (
    "pulse_review",
    "pulse_merge_check",
    "pulse_merge_confirm",
    "pulse_unblock",
)


def register(ctx) -> None:
    from plugins.pulse_buttons.cli import pulse_command, register_cli

    ctx.register_cli_command(
        name="pulse",
        help="Publish the buttoned Project Pulse digest to Slack",
        setup_fn=register_cli,
        handler_fn=lambda args: _exit(pulse_command(args)),
        description=(
            "Operator/agent CLI for the Project Pulse buttons flow. 'publish' "
            "scans the key repos, stages each item, and posts the buttoned "
            "digest to Slack; buttons run a local review / gated merge / unblock "
            "analysis on click."
        ),
    )

    async def _on_click(ack, body, action):
        from plugins.pr_review_buttons import slackio
        from plugins.pulse_buttons import actions

        try:
            client = slackio.make_client()
        except Exception as exc:
            logger.error("[pulse_buttons] cannot build Slack client: %s", exc)
            await ack()
            return
        await actions.handle_action(ack, body, action, client=client)

    for action_id in _ACTION_IDS:
        ctx.register_slack_action_handler(action_id, _on_click)

    logger.debug("pulse_buttons registered CLI + %d action handlers", len(_ACTION_IDS))


def _exit(code):
    """Normalise a CLI handler return into an exit code (0 when None)."""
    import sys

    sys.exit(int(code or 0))
