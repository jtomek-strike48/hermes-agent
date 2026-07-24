"""The gated GitHub PM writes behind the triage digest's buttons.

These are the consequential/irreversible actions the reviewer contract marks
``gated=True`` — they never run in the sweep; they run only when the operator
clicks the corresponding button. Each is a thin, injectable ``gh`` wrapper
(``run`` is injected so tests never hit the network) returning an
:class:`ActionResult`.

Priority is set via a label (``set-priority`` args carry the proposed label, e.g.
"P1: High"); the reviewer only proposes labels that already exist in the repo,
so we add it directly. Close / won't-fix / mark-duplicate leave a short comment
explaining the action for the issue record, then close — mirroring how a human
maintainer would.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class ActionResult:
    ok: bool
    detail: str = ""


def _run(run: Run, args: List[str]) -> subprocess.CompletedProcess:
    return run(["gh", *args], capture_output=True, text=True)


def _result(proc: subprocess.CompletedProcess, ok_detail: str) -> ActionResult:
    if proc.returncode != 0:
        return ActionResult(ok=False, detail=(proc.stderr or "gh command failed").strip()[:300])
    return ActionResult(ok=True, detail=ok_detail)


def _priority_label(args: Dict[str, Any]) -> str:
    """The proposed priority label from a set-priority action's args. Accepts
    either ``label`` (full label text) or ``priority`` (a shorthand like "P1")."""
    args = args or {}
    return str(args.get("label") or args.get("priority") or "").strip()


def set_priority(repo: str, number, args: Dict[str, Any], *, run: Run = subprocess.run) -> ActionResult:
    """Apply the proposed priority label. Reversible in the GitHub UI, but gated
    because triage state is something teammates rely on."""
    label = _priority_label(args)
    if not label:
        return ActionResult(ok=False, detail="no priority label proposed")
    try:
        proc = _run(run, ["issue", "edit", str(number), "-R", repo, "--add-label", label])
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(ok=False, detail=f"gh invocation failed: {exc}")
    return _result(proc, f"labeled `{label}`")


def close_issue(repo: str, number, args: Dict[str, Any], *, run: Run = subprocess.run) -> ActionResult:
    """Close the issue with an optional reason comment (reason=completed)."""
    reason = str((args or {}).get("reason") or "").strip()
    cmd = ["issue", "close", str(number), "-R", repo, "--reason", "completed"]
    if reason:
        cmd += ["--comment", f"Closing after triage: {reason}"]
    try:
        proc = _run(run, cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(ok=False, detail=f"gh invocation failed: {exc}")
    return _result(proc, "issue closed")


def wont_fix(repo: str, number, args: Dict[str, Any], *, run: Run = subprocess.run) -> ActionResult:
    """Close as not-planned with a reason comment."""
    reason = str((args or {}).get("reason") or "out of scope for the project").strip()
    try:
        proc = _run(run, ["issue", "close", str(number), "-R", repo, "--reason", "not planned",
                          "--comment", f"Won't fix: {reason}"])
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(ok=False, detail=f"gh invocation failed: {exc}")
    return _result(proc, "closed as not planned")


def mark_duplicate(repo: str, number, args: Dict[str, Any], *, run: Run = subprocess.run) -> ActionResult:
    """Comment linking the canonical issue, then close as not-planned. The ``of``
    arg is the duplicate target (owner/repo#N or #N)."""
    of = str((args or {}).get("of") or "").strip()
    if not of:
        return ActionResult(ok=False, detail="no duplicate target (of) provided")
    try:
        c = _run(run, ["issue", "comment", str(number), "-R", repo,
                       "--body", f"Duplicate of {of}. Closing in favor of that issue."])
        if c.returncode != 0:
            return _result(c, "")
        proc = _run(run, ["issue", "close", str(number), "-R", repo, "--reason", "not planned"])
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(ok=False, detail=f"gh invocation failed: {exc}")
    return _result(proc, f"marked duplicate of {of}")


# action name (reviewer-contract vocab) → executor. digest_actions maps a Slack
# action_id to the contract name, then dispatches through here.
EXECUTORS: Dict[str, Callable[..., ActionResult]] = {
    "set-priority": set_priority,
    "close": close_issue,
    "wont-fix": wont_fix,
    "mark-duplicate": mark_duplicate,
}


def execute(action: str, repo: str, number, args: Dict[str, Any],
            *, run: Run = subprocess.run) -> ActionResult:
    """Dispatch a gated action by its contract name. Unknown → refused (never
    guess at a consequential write)."""
    fn: Optional[Callable[..., ActionResult]] = EXECUTORS.get(action)
    if fn is None:
        return ActionResult(ok=False, detail=f"unknown gated action {action!r}")
    return fn(repo, number, args or {}, run=run)
