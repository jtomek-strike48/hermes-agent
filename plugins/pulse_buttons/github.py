"""``gh`` CLI wrappers for the pulse_buttons flow: the merge gate, the squash
merge, and the unblock analysis. Isolated from Slack wiring so the decisions are
pure and unit-testable with an injected ``run`` callable.

The merge gate is the safety-critical piece. The operator's rule is explicit:
"we have to ensure that CI is green since this is teamwork." So the gate
FAILS CLOSED — a pending, unknown, or unfetchable CI state is NOT green, and a
merge never proceeds on anything less than a live, freshly-fetched all-green.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Union

logger = logging.getLogger(__name__)

Number = Union[int, str]
Run = Callable[..., subprocess.CompletedProcess]

# Check-run conclusions that count as "not blocking a merge".
_OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# Rollup states that are still in flight — fail the gate closed (not green yet).
_PENDING_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "REQUESTED", "WAITING"}


def _run(run: Run, args: List[str]) -> subprocess.CompletedProcess:
    return run(["gh", *args], capture_output=True, text=True)


def _gh_json(run: Run, args: List[str]):
    proc = _run(run, args)
    if proc.returncode != 0:
        logger.warning("[pulse_buttons] gh %s failed (rc=%s): %s",
                       " ".join(args[:3]), proc.returncode, (proc.stderr or "")[:200])
        return None
    try:
        return json.loads(proc.stdout or "null")
    except (ValueError, TypeError):
        return None


# ``reviewThreads`` is NOT a ``gh pr view --json`` field — it exists only in the
# GraphQL API. This query returns the review threads (with their resolved flag
# and first comment) so the gate can prove "all comments addressed" precisely.
_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$num:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$num){"
    "reviewThreads(first:100){nodes{isResolved path "
    "comments(first:1){nodes{author{login} body}}}}}}}"
)


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def fetch_review_threads(repo: str, number: Number, *, run: Run = subprocess.run) -> Optional[list]:
    """Return the PR's review threads via GraphQL, or None on error (fail-soft).

    Each element: ``{"isResolved": bool, "path": str, "author": str, "body": str}``.
    A None return means "couldn't determine" — the gate treats that as
    fail-closed (unknown thread state is not "all resolved").
    """
    owner, name = _split_repo(repo)
    proc = _run(run, [
        "api", "graphql",
        "-f", f"query={_THREADS_QUERY}",
        "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"num={number}",
    ])
    if proc.returncode != 0:
        logger.warning("[pulse_buttons] graphql reviewThreads failed for %s#%s: %s",
                       repo, number, (proc.stderr or "")[:200])
        return None
    try:
        data = json.loads(proc.stdout or "null")
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (ValueError, TypeError, KeyError):
        return None
    out = []
    for n in nodes:
        comments = ((n.get("comments") or {}).get("nodes") or [])
        first = comments[0] if comments else {}
        out.append({
            "isResolved": bool(n.get("isResolved")),
            "path": n.get("path", ""),
            "author": (first.get("author") or {}).get("login", ""),
            "body": (first.get("body") or ""),
        })
    return out


@dataclass
class MergeGate:
    """Result of the pre-merge double-check. ``ok`` only when EVERY sub-check
    passes on freshly-fetched live data."""
    ok: bool
    head_sha: Optional[str] = None
    reasons: List[str] = field(default_factory=list)   # why it's blocked (empty when ok)
    passed: List[str] = field(default_factory=list)     # human-readable checks that passed


def _classify_ci(rollup: Optional[list]) -> tuple[str, List[str]]:
    """Return ('green'|'red'|'pending'|'none', [failing check names])."""
    if not rollup:
        return "none", []
    failing: List[str] = []
    saw_pending = False
    for c in rollup:
        concl = (c.get("conclusion") or "").upper()
        state = (c.get("status") or c.get("state") or "").upper()
        name = c.get("name") or c.get("context") or "check"
        if state in _PENDING_STATES or (not concl and state not in ("COMPLETED",)):
            saw_pending = True
            continue
        if concl and concl not in _OK_CONCLUSIONS:
            failing.append(f"{name} ({concl.lower()})")
    if failing:
        return "red", failing
    if saw_pending:
        return "pending", []
    return "green", []


def check_merge_gate(repo: str, number: Number, *, run: Run = subprocess.run) -> MergeGate:
    """Freshly re-fetch the PR and decide whether it is safe to merge.

    Gate (all must hold, evaluated on LIVE data, fail-closed):
      1. PR is OPEN and not a draft.
      2. mergeable == MERGEABLE and mergeStateStatus is not DIRTY/BLOCKED/BEHIND.
      3. Every review thread is resolved ("addressed all the comments").
      4. CI is GREEN — no failing checks AND nothing still pending. A "none"
         (no checks configured) is treated as blocked for teamwork safety:
         a repo with required CI that reports no runs is suspicious, so we
         surface it rather than merging blind.
    """
    # NOTE: reviewThreads is GraphQL-only — NOT a valid gh pr view --json field
    # (verified 2026-07-23; requesting it makes gh exit 1). Threads are fetched
    # separately via fetch_review_threads().
    data = _gh_json(run, [
        "pr", "view", str(number), "-R", repo, "--json",
        "state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
        "statusCheckRollup,headRefOid",
    ])
    if data is None:
        return MergeGate(ok=False, reasons=["could not fetch PR state from GitHub (gh error) — not merging"])

    head = data.get("headRefOid")
    reasons: List[str] = []
    passed: List[str] = []

    state = (data.get("state") or "").upper()
    if state != "OPEN":
        reasons.append(f"PR is {state or 'not open'}")
    elif data.get("isDraft"):
        reasons.append("PR is a draft")
    else:
        passed.append("PR is open and ready")

    mergeable = (data.get("mergeable") or "").upper()
    merge_state = (data.get("mergeStateStatus") or "").upper()
    review = (data.get("reviewDecision") or "").upper()
    if mergeable == "CONFLICTING":
        reasons.append("has merge conflicts (rebase/resolve first)")
    elif mergeable != "MERGEABLE":
        reasons.append(f"not mergeable (state={mergeable or 'unknown'})")
    else:
        passed.append("mergeable, no conflicts")

    # mergeStateStatus is GitHub's authoritative verdict — it already folds in
    # branch protection, required reviews, and required checks. BLOCKED/BEHIND/
    # DIRTY means something on GitHub's side is not satisfied. Surface it with a
    # hint from reviewDecision so the message is actionable ("teamwork safety").
    if merge_state in ("DIRTY", "BEHIND", "BLOCKED"):
        hint = ""
        if review == "CHANGES_REQUESTED":
            hint = " — changes requested by a reviewer"
        elif review == "REVIEW_REQUIRED":
            hint = " — a required review is missing"
        elif merge_state == "BEHIND":
            hint = " — branch is behind base (rebase/update)"
        reasons.append(f"GitHub reports merge {merge_state.lower()}{hint}")
    elif merge_state and merge_state not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        # UNKNOWN or an unrecognised state → fail closed, don't merge blind.
        reasons.append(f"merge state is {merge_state.lower()} — not confirmed mergeable")
    else:
        passed.append(f"GitHub merge state {merge_state.lower() or 'clean'}")
    if review == "CHANGES_REQUESTED" and "changes requested" not in " ".join(reasons):
        reasons.append("changes requested by a reviewer — address the comments first")

    # Unresolved review threads = comments not addressed (GraphQL, fail-closed).
    threads = fetch_review_threads(repo, number, run=run)
    if threads is None:
        reasons.append("could not verify review threads are resolved (gh error) — not merging")
    else:
        unresolved = [t for t in threads if not t.get("isResolved", False)]
        if unresolved:
            reasons.append(f"{len(unresolved)} unresolved review comment(s) — address them first")
        else:
            passed.append("all review comments resolved")

    ci, failing = _classify_ci(data.get("statusCheckRollup"))
    if ci == "red":
        reasons.append("CI failing: " + ", ".join(failing[:6]))
    elif ci == "pending":
        reasons.append("CI still running — not green yet (waiting to confirm before merge)")
    elif ci == "none":
        reasons.append("no CI checks reported — refusing to merge blind (teamwork safety)")
    else:
        passed.append("CI green")

    return MergeGate(ok=not reasons, head_sha=head, reasons=reasons, passed=passed)


@dataclass
class MergeResult:
    ok: bool
    detail: str = ""


def squash_merge(repo: str, number: Number, *, expected_head: Optional[str] = None,
                 run: Run = subprocess.run) -> MergeResult:
    """Squash-merge the PR. Re-verifies the gate immediately before merging so a
    click can't merge stale state, and (when given) that the head SHA still
    matches what the operator confirmed — a what-you-approved guard."""
    gate = check_merge_gate(repo, number, run=run)
    if not gate.ok:
        return MergeResult(ok=False, detail="gate re-check failed: " + "; ".join(gate.reasons))
    if expected_head and gate.head_sha and expected_head != gate.head_sha:
        return MergeResult(
            ok=False,
            detail=f"PR head moved since confirm ({expected_head[:8]} → {str(gate.head_sha)[:8]}) — re-run the check",
        )
    proc = _run(run, ["pr", "merge", str(number), "-R", repo, "--squash"])
    if proc.returncode != 0:
        return MergeResult(ok=False, detail=(proc.stderr or "gh pr merge failed").strip()[:300])
    return MergeResult(ok=True, detail="squash-merged")


def gather_unblock_context(repo: str, number: Number, *, run: Run = subprocess.run) -> dict:
    """Collect the raw signals a 'how do I unblock this?' answer needs:
    review threads (unresolved comment bodies), the reviewDecision, and failing
    CI check names + their detail URLs. Pure data — the caller formats advice."""
    data = _gh_json(run, [
        "pr", "view", str(number), "-R", repo, "--json",
        "reviewDecision,statusCheckRollup,mergeable,mergeStateStatus,title,url",
    ])
    if data is None:
        return {"error": "could not fetch PR from GitHub (gh error)"}

    # reviewThreads is GraphQL-only (see fetch_review_threads); None → skip.
    threads = fetch_review_threads(repo, number, run=run) or []
    unresolved = []
    for t in threads:
        if t.get("isResolved"):
            continue
        unresolved.append({
            "path": t.get("path", ""),
            "author": t.get("author", ""),
            "body": (t.get("body") or "")[:400],
        })

    _, failing = _classify_ci(data.get("statusCheckRollup"))
    failing_detail = []
    for c in (data.get("statusCheckRollup") or []):
        concl = (c.get("conclusion") or "").upper()
        if concl and concl not in _OK_CONCLUSIONS:
            failing_detail.append({
                "name": c.get("name") or c.get("context") or "check",
                "conclusion": concl.lower(),
                "url": c.get("detailsUrl") or c.get("targetUrl") or "",
            })

    return {
        "title": data.get("title", ""),
        "url": data.get("url", ""),
        "review_decision": data.get("reviewDecision") or "",
        "mergeable": (data.get("mergeable") or ""),
        "merge_state": (data.get("mergeStateStatus") or ""),
        "unresolved_threads": unresolved,
        "failing_checks": failing_detail,
    }
