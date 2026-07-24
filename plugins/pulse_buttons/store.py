"""Pending Project-Pulse item persistence for the pulse_buttons plugin.

The pulse cron *stages* each surfaced item (a PR or issue, tagged with the
bucket it landed in) here; a Slack button click reads the item back to act on
it. Storing server-side (rather than encoding into the button ``value``, capped
at 2000 chars by Slack) keeps the button payload to just the store key.

State file: ``~/.hermes/state/pulse_pending.json``

    {
      "Strike48-public/pick#302": {
        "repo": "Strike48-public/pick", "number": 302,
        "title": "docs(plg): ...", "url": "https://github.com/.../pull/302",
        "head_sha": "abc123", "bucket": "awaiting",
        "message_ts": "1700.1"   # set once posted to Slack
      }
    }

Writes are atomic (temp file + ``os.replace``). Pure functions of an explicit
``path`` — no global state — so the store is trivially unit-testable. This is a
SEPARATE file from pr_review_buttons' ``pr_review_pending.json``; the two flows
never share state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Persisted fields. Anything else in a staged item is dropped so a malformed
# cron payload can't bloat the file. ``message_ts`` is added by mark_published.
_FIELDS = ("repo", "number", "title", "url", "head_sha", "bucket")


def default_path() -> Path:
    """Resolve the on-disk state file, honouring ``HERMES_HOME``."""
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "state" / "pulse_pending.json"


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
    fd, tmp = tempfile.mkstemp(prefix=".pulse_pending.", dir=str(path.parent))
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
    return {k: entry.get(k) for k in _FIELDS}


def stage(entry: Dict[str, Any], *, path: Optional[Path] = None) -> str:
    """Persist one pulse item, keyed ``repo#number``. Returns the key.

    Re-staging the same key overwrites in place (a fresh scan at a new head
    SHA / different bucket), never duplicates. A freshly staged entry has no
    ``message_ts`` — that is set by :func:`mark_published` once posted.
    """
    path = path or default_path()
    data = load(path)
    key = key_for(entry["repo"], entry["number"])
    # Preserve an existing message_ts on re-stage so we don't lose the posted
    # marker if the same item is staged twice before publishing.
    prior_ts = data.get(key, {}).get("message_ts")
    cleaned = _clean(entry)
    if prior_ts:
        cleaned["message_ts"] = prior_ts
    data[key] = cleaned
    _write(path, data)
    return key


def get(key: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the staged entry for ``key`` or ``None``."""
    return load(path or default_path()).get(key)


def pop(key: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Atomically remove and return the entry for ``key`` (double-click guard)."""
    path = path or default_path()
    data = load(path)
    entry = data.pop(key, None)
    if entry is not None:
        _write(path, data)
    return entry


def mark_published(key: str, *, message_ts: str, path: Optional[Path] = None) -> None:
    """Record the Slack message ts for a staged entry (marks it as posted)."""
    path = path or default_path()
    data = load(path)
    if key in data:
        data[key]["message_ts"] = message_ts
        _write(path, data)


def clear_unpublished(*, path: Optional[Path] = None) -> None:
    """Drop staged-but-never-posted entries.

    The cron re-stages the full current picture every run, so stale unpublished
    items from a previous run (that failed to publish) must not accumulate. Only
    posted entries (with a ``message_ts``, still needed to service button clicks)
    survive. Called at the start of a publish cycle.
    """
    path = path or default_path()
    data = load(path)
    kept = {k: v for k, v in data.items() if v.get("message_ts")}
    if len(kept) != len(data):
        _write(path, kept)


def unpublished(*, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return staged entries not yet posted to Slack (each copy carries ``_key``)."""
    data = load(path or default_path())
    out: List[Dict[str, Any]] = []
    for key, entry in data.items():
        if entry.get("message_ts"):
            continue
        out.append(dict(entry, _key=key))
    return out
