"""``hermes pulse`` — operator/agent CLI for the buttoned Project Pulse digest.

Subcommands:

* ``publish`` — run the pulse scan (via the ``project_pulse`` script's
  ``scan_all``), stage every surfaced item, and post ONE buttoned Block Kit
  digest to the Slack home channel. This is what the pulse cron runs. Prints
  ``[SILENT]`` when there is nothing to surface, so a ``--no-agent`` cron
  delivers nothing.
* ``list`` — show staged (pending) items.

Deliberately reuses ``pr_review_buttons.slackio`` (the stdlib Slack client) so
publish works from the installed ``hermes`` interpreter, which lacks slack_sdk.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from plugins.pr_review_buttons import slackio

from . import blocks, store


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="pulse_action")
    pub = subs.add_parser("publish", help="Scan, stage, and post the buttoned Pulse digest to Slack")
    pub.add_argument("--channel", default="", help="Slack channel id (default: SLACK_HOME_CHANNEL)")
    pub.add_argument("--dry-run", action="store_true",
                     help="Scan + print what would be posted; no Slack, no watermark advance")
    subs.add_parser("list", help="List staged (pending) pulse items")


def pulse_command(args: argparse.Namespace) -> int:
    action = getattr(args, "pulse_action", None)
    if action == "publish":
        return asyncio.run(_cmd_publish(args))
    if action == "list":
        return _cmd_list()
    print("usage: hermes pulse {publish|list}", file=sys.stderr)
    return 2


def _load_pulse_scanner():
    """Import ``scan_all`` from the standalone ~/.hermes/scripts/project_pulse.py.

    It's a script, not an installed module, so load it by path. Returns the
    module or None if it can't be found (fail-soft: publish then no-ops).
    """
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    path = Path(home) / "scripts" / "project_pulse.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("project_pulse", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stage_all(repos: list[dict]) -> list[dict]:
    """Stage every actionable item and return the repos list with ``_key`` on
    each staged item so the digest builder can attach buttons.

    Only PR items get staged (issues have no actions); rotting/issue items are
    still rendered but without buttons, so they don't need a store entry.
    """
    staged_repos = []
    for entry in repos:
        repo = entry["repo"]
        out_buckets = {}
        for bucket, items in entry["buckets"].items():
            new_items = []
            for item in items:
                if item.get("kind", "pr") == "pr" and item.get("number") is not None:
                    key = store.stage(dict(item, bucket=bucket))
                    new_items.append(dict(item, _key=key))
                else:
                    # Issue / non-actionable — render without a store key/button.
                    new_items.append(dict(item, _key=f"{repo}#{item.get('number')}"))
            out_buckets[bucket] = new_items
        staged_repos.append({"repo": repo, "buckets": out_buckets})
    return staged_repos


async def _cmd_publish(args: argparse.Namespace) -> int:
    mod = _load_pulse_scanner()
    if mod is None:
        print("pulse publish: could not load project_pulse.py scanner", file=sys.stderr)
        return 1

    # Dry run must not consume the new-items watermark.
    result = mod.scan_all(advance_watermark=not args.dry_run)
    repos = result.get("repos", [])
    warnings = result.get("warnings", [])

    # Anything actionable to show?
    has_items = any(any(b.get(k) for k in b) for e in repos for b in [e["buckets"]])
    if not has_items and not warnings:
        print("[SILENT]")
        return 0

    # Fresh publish cycle: drop stale unpublished items from a prior failed run.
    store.clear_unpublished()
    staged = _stage_all(repos)
    bk = blocks.build_digest_blocks(staged)

    if args.dry_run:
        print(f"[dry-run] would post {len(bk)} blocks; staged items:")
        return _cmd_list()

    channel = args.channel or slackio.home_channel()
    if not channel:
        print("pulse publish: no channel (set --channel or SLACK_HOME_CHANNEL)", file=sys.stderr)
        return 1
    try:
        client = slackio.make_client()
    except Exception as exc:
        print(f"pulse publish: {exc}", file=sys.stderr)
        return 1

    text = "Project Pulse — items awaiting your next step"
    try:
        ts = await slackio.post_message(client, channel, text, bk)
    except Exception as exc:
        print(f"pulse publish: Slack post failed: {exc}", file=sys.stderr)
        return 1

    # Mark every staged item published against this message ts (buttons rewrite
    # this same message / reply in its thread).
    for entry in staged:
        for items in entry["buckets"].values():
            for item in items:
                if "_key" in item and store.get(item["_key"]) is not None:
                    store.mark_published(item["_key"], message_ts=ts)
    print(f"published Pulse digest to {channel} (ts={ts})")
    return 0


def _cmd_list() -> int:
    data = store.load(store.default_path())
    if not data:
        print("no staged pulse items")
        return 0
    for key, entry in data.items():
        state = "posted" if entry.get("message_ts") else "PENDING"
        print(f"{state:8} {key}  [{entry.get('bucket','?')}]")
    return 0
