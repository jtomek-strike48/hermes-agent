"""triage_buttons — the buttoned issue-triage digest + its DM reply loop.

Triage v2 (agent.issue_triage) emits the shared reviewer contract per issue.
This plugin turns that into an interactive Slack surface and wires four things:

1. The ``hermes triage`` CLI (publish/list): runs the sweep, executes the
   reversible auto-actions (only with ``--live``), stages every button-worthy
   issue, and posts ONE buttoned digest. This is what the triage cron runs, and
   it's what makes the reply loop below reachable.

2. Gated PM action handlers (``triage_set_priority`` / ``triage_close`` /
   ``triage_mark_duplicate`` / ``triage_wont_fix``): on click, perform the
   consequential GitHub write the reviewer proposed but never auto-ran.
   (digest_actions.handle_action)

3. A Slack action handler (``triage_answer`` / "Answer in DM"): on click,
   records a pending-answer session keyed to the operator's DM and DMs them the
   reviewer's clarifying questions. (handlers.handle_answer_click)

4. A ``pre_gateway_dispatch`` hook: fired once per inbound message BEFORE agent
   dispatch (gateway/run.py). When the next DM from that user matches a pending
   entry, the hook posts their answer to the GitHub issue and returns
   ``{"action": "skip"}`` so the message is consumed (not also handled by the
   agent). This is the ONLY robust way to correlate a free-text reply with an
   issue without core-gateway edits — the plugin action surface is buttons-only,
   and thread/session routing is in-memory + restart-fragile.

The hook callback MUST be synchronous: ``PluginManager.invoke_hook`` calls
callbacks with plain ``cb(**kwargs)`` and does not await coroutines. All the
work there (a ``gh`` subprocess + a Slack post via urllib) is synchronous.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ACTION_ANSWER = "triage_answer"
# Gated PM actions handled by digest_actions.handle_action.
_GATED_ACTION_IDS = (
    "triage_set_priority",
    "triage_close",
    "triage_mark_duplicate",
    "triage_wont_fix",
)


def register(ctx) -> None:
    # --- CLI: hermes triage {publish|list} --------------------------------
    from plugins.triage_buttons.cli import register_cli, triage_command

    ctx.register_cli_command(
        name="triage",
        help="Publish the buttoned Issue Triage digest to Slack",
        setup_fn=register_cli,
        handler_fn=lambda args: _exit(triage_command(args)),
        description=(
            "Operator/agent CLI for contract-based issue triage. 'publish' runs "
            "Triage v2 over the configured repos, applies reversible auto-actions "
            "(only with --live), stages each button-worthy issue, and posts the "
            "buttoned digest to Slack; buttons run the gated PM actions / DM reply "
            "loop on click."
        ),
    )

    # --- Slack action handler: "Answer in DM" (reply loop) ----------------
    async def _on_answer_click(ack, body, action):
        from plugins.pr_review_buttons import slackio
        from plugins.triage_buttons import handlers

        await ack()
        try:
            client = slackio.make_client()
        except Exception as exc:
            logger.error("[triage_buttons] cannot build Slack client: %s", exc)
            return
        await handlers.handle_answer_click(body, action, client=client)

    ctx.register_slack_action_handler(ACTION_ANSWER, _on_answer_click)

    # --- Slack action handlers: gated PM actions --------------------------
    async def _on_gated_click(ack, body, action):
        from plugins.pr_review_buttons import slackio
        from plugins.triage_buttons import digest_actions

        try:
            client = slackio.make_client()
        except Exception as exc:
            logger.error("[triage_buttons] cannot build Slack client: %s", exc)
            await ack()
            return
        await digest_actions.handle_action(ack, body, action, client=client)

    for action_id in _GATED_ACTION_IDS:
        ctx.register_slack_action_handler(action_id, _on_gated_click)

    # --- pre_gateway_dispatch hook: capture the DM answer ------------------
    # SYNC callback (invoke_hook does not await). Returns {"action":"skip"} to
    # consume the message when it's an answer to a pending triage question.
    def _on_inbound(event=None, gateway=None, session_store=None, **_ignored):
        from plugins.triage_buttons import handlers

        try:
            return handlers.maybe_capture_answer(event)
        except Exception as exc:  # never break the gateway dispatch path
            logger.warning("[triage_buttons] inbound capture failed: %s", exc)
            return None

    ctx.register_hook("pre_gateway_dispatch", _on_inbound)

    logger.debug(
        "triage_buttons registered CLI + answer action + %d gated actions + pre_gateway_dispatch hook",
        len(_GATED_ACTION_IDS),
    )


def _exit(code):
    """Normalise a CLI handler return into an exit code (0 when None)."""
    import sys

    sys.exit(int(code or 0))
