"""``hermes triage`` — operator/agent CLI for the buttoned issue-triage digest.

Subcommands:

* ``publish`` — run Triage v2 over the configured repos (agent.issue_triage),
  execute the reversible auto-actions (apply existing labels / comment
  clarifying questions to non-operator reporters) ONLY when ``--live``, stage
  every button-worthy issue, and post ONE buttoned Block Kit digest to the Slack
  home channel. This is what the triage cron runs. Prints ``[SILENT]`` when
  nothing is actionable so a ``--no-agent`` cron delivers nothing.
* ``list`` — show staged (pending) triaged issues.

Two independent flags:
  --live      perform the AUTONOMOUS GitHub auto-writes (default: dry — no auto
              GitHub writes; the digest still posts, since posting is not a write).
  --dry-run   scan + print what would be posted; do NOT post to Slack.

Gated buttons (set-priority / close / mark-duplicate / won't-fix) and the
Answer-in-DM reply loop ALWAYS execute on click — the operator's explicit click
IS the authorization. ``--live`` governs only the sweep's autonomous actions.

Reuses ``pr_review_buttons.slackio`` (the stdlib Slack client) so publish works
from the installed ``hermes`` interpreter, which lacks slack_sdk.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from typing import Any, Dict, List

from plugins.pr_review_buttons import slackio

from . import blocks, digest_store

# Repos/labels to triage (matches the existing StrikeKit triage scope). A label
# of "" means all open issues in the repo.
TRIAGE_TARGETS = [
    ("Strike48/matrix", "strikekit"),
]


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="triage_action")
    pub = subs.add_parser("publish", help="Triage the configured repos and post the buttoned digest")
    pub.add_argument("--channel", default="", help="Slack channel id (default: SLACK_HOME_CHANNEL)")
    pub.add_argument("--live", action="store_true",
                     help="Perform the autonomous GitHub auto-actions (default: dry, no auto writes)")
    pub.add_argument("--dry-run", action="store_true",
                     help="Scan + print what would be posted; do not post to Slack")
    subs.add_parser("list", help="List staged (pending) triaged issues")


def triage_command(args: argparse.Namespace) -> int:
    action = getattr(args, "triage_action", None)
    if action == "publish":
        return asyncio.run(_cmd_publish(args))
    if action == "list":
        return _cmd_list()
    print("usage: hermes triage {publish|list}", file=sys.stderr)
    return 2


def _self_login() -> str:
    """The operator's own gh login (so triage can route ask-reporter vs
    ask-operator). Empty on failure — the engine then treats no one as the
    operator, which only means questions go to GitHub, never the wrong way."""
    try:
        out = subprocess.run(["gh", "api", "user", "-q", ".login"],
                             capture_output=True, text=True, timeout=30)
        return (out.stdout or "").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _issue_url(repo: str, number: Any) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _auto_summary(outcome, *, live: bool) -> str:
    """One-line context describing the reversible auto-actions the sweep took
    (live) or would take (dry). Empty when there were none."""
    bits: List[str] = []
    for a in outcome.auto_planned:
        if a.action == "apply-label":
            labels = ", ".join(a.args.get("labels", []))
            if labels:
                verb = "labeled" if (live and a.executed) else "would label"
                bits.append(f"{verb} {labels}")
        elif a.action == "ask-reporter":
            qs = a.args.get("questions", [])
            if qs:
                verb = "asked reporter" if (live and a.executed) else "would ask reporter"
                bits.append(f"{verb} {len(qs)} question(s)")
        # ask-operator is surfaced as the Answer-in-DM button, not an auto note.
    return "auto: " + "; ".join(bits) if bits else ""


def _outcome_to_item(issue: Dict[str, Any], outcome, *, live: bool) -> Dict[str, Any]:
    """Map a TriageOutcome + its source issue into a digest-store entry."""
    repo = issue.get("repo", "")
    number = issue.get("number")
    # Answer-in-DM only makes sense for a needs-info verdict: the contract says
    # clarifying questions accompany needs-info, so a "ready" (or any non-needs-
    # info) verdict that still carries operator questions is a self-contradiction
    # — don't surface a spurious Answer button for it. This keeps the digest to
    # genuinely under-specified issues instead of one button per open issue.
    ask_operator = outcome.ask_target == "operator" and outcome.verdict == "needs-info"
    return {
        "repo": repo,
        "number": number,
        "ref": outcome.ref or digest_store.key_for(repo, number),
        "title": issue.get("title", ""),
        "url": _issue_url(repo, number),
        "verdict": outcome.verdict,
        "summary": outcome.summary,
        "questions": list(outcome.missing_info) if ask_operator else [],
        "ask_operator": ask_operator,
        "gated": [dict(g) for g in outcome.gated_planned],
        "auto_summary": _auto_summary(outcome, live=live),
    }


# Gated actions that represent a real DECISION about the issue's fate, and so
# earn it a spot in the digest on their own. set-priority is deliberately NOT
# here: the reviewer proposes it on almost every unprioritized issue, so letting
# it surface an issue would list the whole backlog twice a day. set-priority
# still renders as a button when the issue is surfaced for one of these reasons
# (it rides along — see blocks._action_row).
_DECISION_ACTIONS = frozenset({"close", "mark-duplicate", "wont-fix"})


def _is_button_worthy(item: Dict[str, Any]) -> bool:
    """An issue gets a digest row only when it needs a call from the operator:
    it's under-specified and they reported it (Answer-in-DM), or the reviewer
    proposes a decision (close / mark-duplicate / won't-fix). A lone set-priority
    proposal is not enough — otherwise the digest is the entire open backlog."""
    has_answer = bool(item.get("ask_operator") and item.get("questions"))
    has_decision = any(
        isinstance(g, dict) and g.get("action") in _DECISION_ACTIONS
        for g in (item.get("gated") or [])
    )
    return has_answer or has_decision


def _run_sweep(*, live: bool) -> List[Dict[str, Any]]:
    """Triage every configured repo; return a list of
    ``{"repo": str, "items": [button-worthy item, ...]}`` (repos with no
    actionable issue are dropped)."""
    from agent import issue_triage as t

    me = _self_login()
    repos_out: List[Dict[str, Any]] = []
    for repo, label in TRIAGE_TARGETS:
        existing = t.repo_labels(repo)
        issues = t.list_open_issues(repo, label)
        if issues is None:
            print(f"triage publish: could not fetch issues for {repo} (gh error) — skipped",
                  file=sys.stderr)
            continue
        items: List[Dict[str, Any]] = []
        for issue in issues:
            outcome = t.process_issue(issue, me, existing, dry_run=not live)
            if outcome.error:
                print(f"triage publish: {outcome.ref}: {outcome.error}", file=sys.stderr)
                continue
            item = _outcome_to_item(issue, outcome, live=live)
            if _is_button_worthy(item):
                items.append(item)
        if items:
            repos_out.append({"repo": repo, "items": items})
    return repos_out


async def _cmd_publish(args: argparse.Namespace) -> int:
    live = bool(getattr(args, "live", False))
    repos = _run_sweep(live=live)

    if not any(e["items"] for e in repos):
        print("[SILENT]")
        return 0

    # Fresh publish cycle: drop stale unpublished items from a prior failed run,
    # then stage the current picture and attach each item's store key.
    digest_store.clear_unpublished()
    staged_repos: List[Dict[str, Any]] = []
    for entry in repos:
        staged_items = []
        for item in entry["items"]:
            key = digest_store.stage(item)
            staged_items.append(dict(item, _key=key))
        staged_repos.append({"repo": entry["repo"], "items": staged_items})

    bk = blocks.build_digest_blocks(staged_repos, live=live)

    if getattr(args, "dry_run", False):
        print(f"[dry-run] would post {len(bk)} blocks; staged issues:")
        return _cmd_list()

    channel = args.channel or slackio.home_channel()
    if not channel:
        print("triage publish: no channel (set --channel or SLACK_HOME_CHANNEL)", file=sys.stderr)
        return 1
    try:
        client = slackio.make_client()
    except Exception as exc:
        print(f"triage publish: {exc}", file=sys.stderr)
        return 1

    text = "Issue Triage — issues awaiting your call"
    try:
        ts = await slackio.post_message(client, channel, text, bk)
    except Exception as exc:
        print(f"triage publish: Slack post failed: {exc}", file=sys.stderr)
        return 1

    for entry in staged_repos:
        for item in entry["items"]:
            if digest_store.get(item["_key"]) is not None:
                digest_store.mark_published(item["_key"], message_ts=ts)
    print(f"published Triage digest to {channel} (ts={ts}); {'LIVE' if live else 'dry-run (no auto writes)'}")
    return 0


def _cmd_list() -> int:
    data = digest_store.list_all()
    if not data:
        print("no staged triage items")
        return 0
    for key, entry in data.items():
        state = "posted" if entry.get("message_ts") else "PENDING"
        flags = []
        if entry.get("ask_operator") and entry.get("questions"):
            flags.append("answer-in-dm")
        for g in entry.get("gated") or []:
            if isinstance(g, dict) and g.get("action"):
                flags.append(g["action"])
        print(f"{state:8} {key}  [{entry.get('verdict','?')}]  {' '.join(flags)}")
    return 0
