"""Run a headless Claude ``/review-pr`` on THIS laptop and post the result to
the Pulse Slack thread.

The gateway runs on 127.0.0.1 — the operator's laptop — so "review with Claude
locally" means shelling out to the installed ``claude`` CLI here. ``/review-pr``
checks out the PR head in a worktree and runs empirical gates, so it must run
INSIDE a local clone of the target repo (matching the crons' ``~/Code`` layout).

A review takes minutes — far past Slack's 3-second ack budget — so the click
handler spawns this DETACHED (new session) and returns immediately; when the
review finishes, this process posts the verbatim output back into the digest
thread. These are the operator's OWN PRs (GitHub forbids a formal self-review),
so the output goes to Slack only — nothing is written to GitHub.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# owner/repo → local clone directory (mirrors the crons' ~/Code checkouts).
# Verified 2026-07-23: red_team (underscore) is the red-team-docs clone.
_CLONE_MAP: Dict[str, str] = {
    "Strike48-public/pick": "~/Code/pick",
    "Strike48/4n6_nexus": "~/Code/4n6_nexus",
    "Strike48/red-team-docs": "~/Code/red_team",
}

# Cap the headless review so a wedged run can't hold a subprocess forever.
_REVIEW_TIMEOUT_S = int(os.environ.get("PULSE_REVIEW_TIMEOUT_S", "1500"))  # 25 min
# Model for the local review. Haiku is too weak for a real review; default to a
# capable model, overridable.
_REVIEW_MODEL = os.environ.get("PULSE_REVIEW_MODEL", "claude-sonnet-5")
# Second choice when the primary is overloaded/unavailable. A review is a ~25-min
# headless run triggered by a Slack click; losing it to a transient capacity blip
# means the operator clicks and silently gets nothing back.
_REVIEW_FALLBACK_MODEL = os.environ.get(
    "PULSE_REVIEW_FALLBACK_MODEL", "us.anthropic.claude-opus-4-8[1m]"
)


def clone_dir(repo: str) -> Optional[Path]:
    """Local clone path for ``repo`` if it exists on disk, else None."""
    raw = _CLONE_MAP.get(repo)
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    return path if (path / ".git").is_dir() else None


def build_review_command(url: str, cwd: Path) -> list[str]:
    """The headless claude invocation. Kept pure for unit testing.

    ``-p`` runs non-interactively; the prompt is the slash command plus the PR
    URL. ``--permission-mode acceptEdits`` is deliberately NOT used — review is
    read-only; the command may create a scratch worktree but must not touch the
    working tree, so we leave the default (no auto-approved writes).

    ``--fallback-model`` covers an overloaded/unavailable primary — the flag
    applies to ``--print``/``-p`` runs, which is exactly this one. Verified
    end-to-end: with the primary forced to fail, the run reported "Opus 5 not
    available — using Opus 4.8 for this session" and billed opus-4-8, exit 0.
    """
    return [
        "claude", "-p", f"/review-pr {url}",
        "--model", _REVIEW_MODEL,
        "--fallback-model", _REVIEW_FALLBACK_MODEL,
    ]


def _post_to_thread(channel: str, thread_ts: str, text: str) -> None:
    import asyncio

    from plugins.pr_review_buttons import slackio  # reuse the stdlib Slack client

    async def _go():
        client = slackio.make_client()
        # Slack section text caps ~3000 chars; a review is longer. Post as a
        # threaded reply with the body in a code block, chunked.
        chunks = _chunk(text, 2800)
        for i, chunk in enumerate(chunks):
            prefix = "" if i else "Claude review\n"
            await slackio.post_message(
                client, channel, f"{prefix}{chunk[:2900]}",
                [{"type": "section", "text": {"type": "mrkdwn", "text": f"{prefix}```{chunk}```"}}],
                thread_ts=thread_ts,
            )

    try:
        asyncio.run(_go())
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[pulse_buttons] failed posting review to Slack thread: %s", exc)


def _chunk(text: str, limit: int) -> list[str]:
    text = text or "(empty review output)"
    if len(text) <= limit:
        return [text]
    out, remaining = [], text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


def run_review_blocking(repo: str, number, url: str, channel: str, thread_ts: str) -> None:
    """Run the review to completion and post the result. Called in the detached
    child (see :func:`spawn_review`). Never raises — always tries to post an
    outcome so a failure is visible in Slack, not silent."""
    cwd = clone_dir(repo)
    if cwd is None:
        _post_to_thread(
            channel, thread_ts,
            f"Could not run a local review for {repo}#{number}: no local clone found "
            f"(expected {_CLONE_MAP.get(repo, '?')}). Clone it, then click again.",
        )
        return

    cmd = build_review_command(url, cwd)
    logger.info("[pulse_buttons] running local review: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=_REVIEW_TIMEOUT_S,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            body = f"Local review of {repo}#{number} exited {proc.returncode}.\n\n{err[:1500]}"
        else:
            body = out or f"Review produced no output (exit {proc.returncode})."
    except subprocess.TimeoutExpired:
        body = f"Local review of {repo}#{number} timed out after {_REVIEW_TIMEOUT_S}s."
    except (OSError, subprocess.SubprocessError) as exc:
        body = f"Local review of {repo}#{number} failed to run: {exc}"

    header = f"Review of {repo}#{number} ({url}):\n\n"
    _post_to_thread(channel, thread_ts, header + body)


def spawn_review(repo: str, number, url: str, channel: str, thread_ts: str) -> bool:
    """Fork a fully-detached child that runs the review and posts the result.

    ``start_new_session=True`` reparents the child so it outlives the gateway's
    click handler (and the gateway itself). Returns True if the spawn was
    initiated. The child re-enters this module as a script with marker env vars
    so it does not depend on the parent staying alive.
    """
    try:
        env = dict(os.environ)
        env["_PULSE_REVIEW_WORKER"] = "1"
        env["_PULSE_REPO"] = str(repo)
        env["_PULSE_NUMBER"] = str(number)
        env["_PULSE_URL"] = str(url)
        env["_PULSE_CHANNEL"] = str(channel)
        env["_PULSE_THREAD_TS"] = str(thread_ts)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from the gateway's process group
            cwd=str(Path(os.path.expanduser("~"))),
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("[pulse_buttons] could not spawn local review: %s", exc)
        return False


def _worker_main() -> None:
    """Entry point for the detached child (env-driven)."""
    run_review_blocking(
        os.environ.get("_PULSE_REPO", ""),
        os.environ.get("_PULSE_NUMBER", ""),
        os.environ.get("_PULSE_URL", ""),
        os.environ.get("_PULSE_CHANNEL", ""),
        os.environ.get("_PULSE_THREAD_TS", ""),
    )


if __name__ == "__main__" and os.environ.get("_PULSE_REVIEW_WORKER") == "1":
    # Ensure the Hermes repo root is importable (for plugins.pr_review_buttons).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    _worker_main()
