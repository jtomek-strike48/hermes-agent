"""Write the operator's DM answer back to the GitHub issue that was awaiting it.

When the operator answers a needs-info issue's questions in DM, this posts their
answer as a structured comment on the issue — pairing each question with the
answer text so the issue record is self-explanatory. Pure decision logic with an
injected ``run`` (subprocess.run) so tests never hit the network.

The comment is authored by the operator's own gh identity (jtomek-strike48) —
posting on your OWN issue is always allowed, and it's exactly the enrichment you
intended: turning a vague issue into an actionable one with your own words.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Callable, List

logger = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class WritebackResult:
    ok: bool
    detail: str = ""


def build_answer_comment(questions: List[str], answer: str) -> str:
    """Render the issue comment body. Pairs the questions Mercury asked with the
    operator's answer so the enriched issue reads coherently later."""
    lines = ["**Triage clarification** (via Mercury)", ""]
    if questions:
        lines.append("_Questions asked:_")
        lines += [f"> {q}" for q in questions]
        lines.append("")
    lines.append(answer.strip())
    return "\n".join(lines)


def post_answer(repo: str, number, questions: List[str], answer: str,
                *, run: Run = subprocess.run) -> WritebackResult:
    """Post the operator's answer as a comment on the issue. Fail-soft: returns
    ok=False with detail on any gh error (the caller surfaces it in the DM)."""
    if not answer.strip():
        return WritebackResult(ok=False, detail="empty answer — nothing posted")
    body = build_answer_comment(questions, answer)
    try:
        proc = run(
            ["gh", "issue", "comment", str(number), "-R", repo, "--body", body],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return WritebackResult(ok=False, detail=f"gh invocation failed: {exc}")
    if proc.returncode != 0:
        return WritebackResult(ok=False, detail=(proc.stderr or "gh issue comment failed").strip()[:300])
    return WritebackResult(ok=True, detail="answer posted to issue")
