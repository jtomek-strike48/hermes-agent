"""Triage v2 — the issue-mode reviewer that emits the shared contract and acts
within guardrails.

Flow per issue:
  1. Fetch open issues (gh) for the configured repos/labels.
  2. Ask the auxiliary LLM for a contract verdict (agent.reviewer_contract).
  3. Validate it (fail-closed gating recomputed in the contract).
  4. Execute the AUTO actions (gated=False) — currently: apply-label (only
     labels that ALREADY EXIST in the repo AND were suggested) and post
     clarifying questions (ask-reporter → GitHub comment when the reporter is
     someone else). GATED actions are collected for a button surface, not run.
  5. Missing-info where the operator is the reporter → routed to Slack (handled
     by the caller / reply-loop, not here).

Everything honours ``dry_run``: in dry-run NOTHING is written to GitHub — the
planned actions are returned so the operator can inspect them first. Same
no-surprises pattern the Pulse build used.

Deterministic pieces (label intersection, action planning) are pure and unit-
tested; the LLM call and gh writes are injected so tests never hit the network.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent import reviewer_contract as rc

logger = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]


# --- gh helpers (injectable for tests) -------------------------------------

def _run(run: Run, args: List[str]) -> subprocess.CompletedProcess:
    return run(["gh", *args], capture_output=True, text=True)


def _gh_json(run: Run, args: List[str]):
    proc = _run(run, args)
    if proc.returncode != 0:
        logger.warning("[triage] gh %s failed: %s", " ".join(args[:3]), (proc.stderr or "")[:200])
        return None
    try:
        return json.loads(proc.stdout or "null")
    except (ValueError, TypeError):
        return None


def list_open_issues(repo: str, label: str = "", *, run: Run = subprocess.run) -> Optional[list]:
    args = ["issue", "list", "-R", repo, "--state", "open", "--limit", "60",
            "--json", "number,title,body,author,labels,updatedAt"]
    if label:
        args += ["--label", label]
    data = _gh_json(run, args)
    if data is None:
        return None
    for it in data:
        it["repo"] = repo
    return data


def repo_labels(repo: str, *, run: Run = subprocess.run) -> set:
    """The set of label names that ALREADY EXIST in the repo (for the
    high-confidence intersection — we never auto-create labels)."""
    data = _gh_json(run, ["label", "list", "-R", repo, "--limit", "200", "--json", "name"])
    if not isinstance(data, list):
        return set()
    return {d.get("name", "") for d in data if isinstance(d, dict)}


# --- LLM call --------------------------------------------------------------

def triage_issue(issue: Dict[str, Any], operator_login: str,
                 *, client=None, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get a validated contract verdict for one issue, or None on failure.

    ``client``/``model`` are injectable; when absent, resolves the text
    auxiliary client (same path morning_brief uses), so this survives a
    Bedrock-SSO gap the same way (returns None → the caller skips the issue).
    """
    if client is None:
        try:
            from agent.auxiliary_client import get_text_auxiliary_client
            client, model = get_text_auxiliary_client("issue_triage")
        except Exception as exc:
            logger.debug("[triage] aux client unavailable: %s", exc)
            return None
    if client is None or not model:
        return None

    payload = rc.build_triage_user_payload(issue, operator_login)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": rc.ISSUE_TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=0,
            max_tokens=1500,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("[triage] LLM call failed for %s: %s", issue.get("number"), exc)
        return None

    verdict = _parse_json_object(text)
    if verdict is None:
        logger.warning("[triage] non-JSON verdict for %s: %s", issue.get("number"), text[:160])
        return None
    verdict.setdefault("ref", f"{issue.get('repo','')}#{issue.get('number','')}")
    try:
        return rc.validate(verdict, mode="issue")
    except rc.ContractError as exc:
        logger.warning("[triage] contract violation for %s: %s", issue.get("number"), exc)
        return None


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON object from an LLM reply (tolerates a stray code fence)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


# --- action planning + execution -------------------------------------------

@dataclass
class PlannedAction:
    action: str
    args: Dict[str, Any]
    gated: bool
    note: str = ""          # why it will / won't run (e.g. dropped labels)
    executed: bool = False   # set True after a real (non-dry-run) write


def plan_label_action(action: Dict[str, Any], existing: set) -> PlannedAction:
    """Intersect suggested labels with the repo's EXISTING label set. Labels not
    already in the repo are dropped (high-confidence policy: never auto-create).
    """
    suggested = [str(l) for l in (action.get("args", {}).get("labels") or [])]
    keep = [l for l in suggested if l in existing]
    dropped = [l for l in suggested if l not in existing]
    note = ""
    if dropped:
        note = f"dropped (not in repo): {', '.join(dropped)}"
    return PlannedAction(action="apply-label", args={"labels": keep}, gated=False, note=note)


@dataclass
class TriageOutcome:
    ref: str
    verdict: str
    summary: str
    auto_planned: List[PlannedAction] = field(default_factory=list)   # gated=False
    gated_planned: List[Dict[str, Any]] = field(default_factory=list)  # need a click
    missing_info: List[str] = field(default_factory=list)
    ask_target: str = ""    # "reporter" | "operator" | "" — where questions go
    error: str = ""


def process_issue(issue: Dict[str, Any], operator_login: str, existing_labels: set,
                  *, dry_run: bool = True, client=None, model=None,
                  run: Run = subprocess.run) -> TriageOutcome:
    """Triage one issue and (unless dry_run) execute its AUTO actions."""
    ref = f"{issue.get('repo','')}#{issue.get('number','')}"
    verdict = triage_issue(issue, operator_login, client=client, model=model)
    if verdict is None:
        return TriageOutcome(ref=ref, verdict="", summary="", error="triage failed (LLM/contract)")

    out = TriageOutcome(
        ref=ref, verdict=verdict["verdict"], summary=verdict.get("summary", ""),
        missing_info=list(verdict.get("missing_info", [])),
    )

    reporter = (issue.get("author") or {}).get("login", "")
    operator_is_reporter = reporter == operator_login

    for a in rc.gated_actions(verdict, mode="issue"):
        out.gated_planned.append(a)

    for a in rc.auto_actions(verdict, mode="issue"):
        name = a["action"]
        if name == "apply-label":
            planned = plan_label_action(a, existing_labels)
            if planned.args["labels"] and not dry_run:
                planned.executed = _apply_labels(issue, planned.args["labels"], run=run)
            out.auto_planned.append(planned)
        elif name == "ask-reporter":
            # Route: reporter != operator → GitHub comment; else defer to Slack.
            questions = [str(q) for q in (a.get("args", {}).get("questions") or [])]
            if operator_is_reporter:
                out.ask_target = "operator"  # caller routes to Slack (reply loop)
                out.missing_info = questions or out.missing_info
                out.auto_planned.append(PlannedAction(
                    "ask-operator", {"questions": questions}, gated=False,
                    note="reporter is you → ask in Slack (not GitHub)"))
            else:
                planned = PlannedAction("ask-reporter", {"questions": questions}, gated=False)
                if questions and not dry_run:
                    planned.executed = _post_questions(issue, questions, reporter, run=run)
                out.ask_target = "reporter"
                out.auto_planned.append(planned)
        elif name == "ask-operator":
            questions = [str(q) for q in (a.get("args", {}).get("questions") or [])]
            out.ask_target = "operator"
            out.missing_info = questions or out.missing_info
            out.auto_planned.append(PlannedAction(
                "ask-operator", {"questions": questions}, gated=False,
                note="ask in Slack (not GitHub)"))
    return out


def _apply_labels(issue: Dict[str, Any], labels: List[str], *, run: Run) -> bool:
    proc = _run(run, ["issue", "edit", str(issue.get("number")), "-R", issue.get("repo", ""),
                      "--add-label", ",".join(labels)])
    ok = proc.returncode == 0
    if not ok:
        logger.warning("[triage] apply-label failed for %s: %s",
                       issue.get("number"), (proc.stderr or "")[:160])
    return ok


def _post_questions(issue: Dict[str, Any], questions: List[str], reporter: str, *, run: Run) -> bool:
    lines = [f"Thanks for the report, @{reporter}! To move this forward, could you clarify:", ""]
    lines += [f"- {q}" for q in questions]
    body = "\n".join(lines)
    proc = _run(run, ["issue", "comment", str(issue.get("number")), "-R", issue.get("repo", ""),
                      "--body", body])
    ok = proc.returncode == 0
    if not ok:
        logger.warning("[triage] ask-reporter comment failed for %s: %s",
                       issue.get("number"), (proc.stderr or "")[:160])
    return ok
