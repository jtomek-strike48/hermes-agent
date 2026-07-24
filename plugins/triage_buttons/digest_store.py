"""Staged triaged-issue persistence for the buttoned triage digest.

The triage cron *stages* each button-worthy issue (a needs-info issue you
reported, or one with gated actions the reviewer proposed) here; a Slack button
click reads the entry back to act on it. Storing server-side (rather than
encoding the whole verdict into a Slack button ``value``, capped at ~2000 chars)
keeps the gated-action button payload down to just the store key.

State file: ``~/.hermes/state/triage_digest.json``

    {
      "Strike48/matrix#3061": {
        "repo": "Strike48/matrix", "number": 3061,
        "ref": "Strike48/matrix#3061",
        "title": "Crash on empty scope", "url": "https://github.com/.../issues/3061",
        "verdict": "needs-info", "summary": "crashes when scope is empty",
        "questions": ["repro steps?", "expected vs actual?"],
        "ask_operator": true,
        "gated": [ {"action": "set-priority", "args": {"label": "P1: High"}} ],
        "message_ts": "1700.1",   # set once posted to Slack
        "done": ["set-priority"]  # gated actions already executed (double-click guard)
      }
    }

This is a SEPARATE file from the reply loop's ``triage_pending_answers.json``
(``store.py``) — the digest stages issues; the reply loop tracks a single DM
conversation. The two never share state.

Writes are atomic (temp file + ``os.replace``); pure functions of an explicit
``path`` — no global state — so the store is trivially unit-testable.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# Persisted fields. Anything else in a staged item is dropped so a malformed
# payload can't bloat the file. ``message_ts``/``done`` are managed separately.
_FIELDS = (
    "repo", "number", "ref", "title", "url", "verdict", "summary",
    "questions", "ask_operator", "gated",
)


def default_path() -> Path:
    """Resolve the on-disk state file, honouring ``HERMES_HOME``."""
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "state" / "triage_digest.json"


def key_for(repo: str, number: Any) -> str:
    """Build the canonical store key: ``owner/repo#N``."""
    return f"{repo}#{number}"


def load(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return the full store, or ``{}`` when the file is missing/unreadable."""
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
    """Atomically write ``data`` to ``path`` (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".triage_digest.", dir=str(path.parent))
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


def _clean(entry: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = {k: entry.get(k) for k in _FIELDS}
    # Normalize list-typed fields so downstream never sees None where it iterates.
    cleaned["questions"] = list(cleaned.get("questions") or [])
    cleaned["gated"] = list(cleaned.get("gated") or [])
    cleaned["ask_operator"] = bool(cleaned.get("ask_operator"))
    return cleaned


def stage(entry: Dict[str, Any], *, path: Optional[Path] = None) -> str:
    """Persist one triaged issue, keyed ``repo#number``. Returns the key.

    Re-staging the same key overwrites in place (a fresh triage run), never
    duplicates. A freshly staged entry has no ``message_ts`` — that is set by
    :func:`mark_published` once posted — and an empty ``done`` list.
    """
    path = path or default_path()
    data = load(path)
    key = key_for(entry["repo"], entry["number"])
    cleaned = _clean(entry)
    # A fresh stage resets the posted marker + executed-action guard: it's a new
    # digest cycle, so prior button state should not leak into it.
    cleaned["done"] = []
    data[key] = cleaned
    _write(path, data)
    return key


def get(key: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the staged entry for ``key`` or ``None``."""
    return load(path or default_path()).get(key)


def mark_published(key: str, *, message_ts: str, path: Optional[Path] = None) -> None:
    """Record the Slack message ts for a staged entry (marks it as posted)."""
    path = path or default_path()
    data = load(path)
    if key in data:
        data[key]["message_ts"] = message_ts
        _write(path, data)


def mark_action_done(key: str, action: str, *, path: Optional[Path] = None) -> bool:
    """Record that a gated ``action`` was executed for ``key`` (double-click
    guard). Returns True if newly recorded, False if it was already done or the
    entry is gone (caller treats False as "don't execute again")."""
    path = path or default_path()
    data = load(path)
    entry = data.get(key)
    if entry is None:
        return False
    done = list(entry.get("done") or [])
    if action in done:
        return False
    done.append(action)
    entry["done"] = done
    _write(path, data)
    return True


def clear_unpublished(*, path: Optional[Path] = None) -> None:
    """Drop staged-but-never-posted entries.

    The cron re-stages the full current picture every run, so stale unpublished
    items from a previous failed run must not accumulate. Only posted entries
    (with a ``message_ts``, still needed to service button clicks) survive.
    Called at the start of a publish cycle.
    """
    path = path or default_path()
    data = load(path)
    kept = {k: v for k, v in data.items() if v.get("message_ts")}
    if len(kept) != len(data):
        _write(path, kept)


def list_all(*, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return the whole store (for the ``hermes triage list`` CLI)."""
    return load(path or default_path())
