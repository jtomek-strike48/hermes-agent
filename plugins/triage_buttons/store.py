"""Pending-answer state for the triage DM reply loop.

When the operator clicks "Answer in DM" on a needs-info issue, we record that a
DM conversation is now expecting their answer about a specific issue. The
``pre_gateway_dispatch`` hook (see ``__init__``) reads this back on the next DM
message from that user to route their free-text answer to the right issue.

Keyed by ``<platform>:<dm_chat_id>:<user_id>`` — the tuple that uniquely
identifies "this person, in this DM." An entry is one-shot: consumed (popped)
the moment the operator's next DM arrives, so a stale entry can never hijack an
unrelated later message.

State file: ``~/.hermes/state/triage_pending_answers.json``

    {
      "slack:D0123:U0456": {
        "repo": "Strike48/matrix", "number": 3061,
        "ref": "Strike48/matrix#3061",
        "questions": ["repro steps?", "expected vs actual?"],
        "asked_ts": "1700.5"    # digest msg ts, for optional threading
      }
    }

Atomic writes (temp + os.replace); pure functions of an explicit path — trivially
unit-testable. Separate file from pulse_pending.json / pr_review_pending.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

_FIELDS = ("repo", "number", "ref", "questions", "asked_ts")


def default_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "state" / "triage_pending_answers.json"


def key_for(platform: str, dm_chat_id: str, user_id: str) -> str:
    return f"{platform}:{dm_chat_id}:{user_id}"


def load(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        raw = Path(path).read_text()
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".triage_pending.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def put(key: str, entry: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    """Record a pending-answer session (overwrites any prior for this key —
    the newest ask wins; an operator who clicks Answer on a second issue before
    replying is now answering the second one)."""
    path = path or default_path()
    data = load(path)
    data[key] = {k: entry.get(k) for k in _FIELDS}
    _write(path, data)


def peek(key: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    return load(path or default_path()).get(key)


def pop(key: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """One-shot consume: return and remove the pending entry for ``key``."""
    path = path or default_path()
    data = load(path)
    entry = data.pop(key, None)
    if entry is not None:
        _write(path, data)
    return entry
