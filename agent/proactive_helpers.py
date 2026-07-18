"""Shared delivery helper for the proactive open-loops producers.

All four producers — ``omi_commitments``, ``stalled_threads``, ``deadline_radar``
and ``morning_brief`` — need to route ONE governed message to the user's chat.
Historically each called::

    send_message_tool({"message": text})   # no target
    return True                            # return value ignored

which is a latent bug: ``send_message_tool`` REQUIRES a ``target`` and returns
``{"error": ...}`` when it is absent, so the message never left the process —
yet the producer reported success and the governor ledger recorded an
``allowed`` row (with ``platform=None, chat_id=None``). The result was a
phantom-delivery: the budget was spent, nothing reached the user.

``deliver_proactive`` centralises the correct path:
  1. Resolve a delivery target (config ``notifications.deliver`` override, else
     the first enabled platform that has a home channel).
  2. Send WITH that target.
  3. Parse the tool's JSON result and only report success when the send
     actually succeeded — so a broken target surfaces as ``delivered=0`` in the
     producer's return dict instead of a silent lie.

FAIL-SOFT: any unexpected error returns False (never raises into a producer).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from hermes_cli.config import load_config

logger = logging.getLogger(__name__)


def resolve_target() -> Optional[str]:
    """Return the delivery target string for proactive sends, or None.

    Priority:
      1. ``notifications.deliver`` in config.yaml (e.g. ``"slack:C0..."`` or a
         bare platform name ``"slack"`` which resolves to that platform's home
         channel inside ``send_message_tool``).
      2. The first enabled platform that has a configured home channel.

    Returns None when no usable target exists (caller treats as "cannot
    deliver" and reports ``delivered=0`` rather than pretending success).
    """
    # 1. Explicit config override.
    try:
        cfg = load_config()
        notif = cfg.get("notifications", {}) if isinstance(cfg, dict) else {}
        deliver = notif.get("deliver") if isinstance(notif, dict) else None
        if isinstance(deliver, str) and deliver.strip():
            return deliver.strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("proactive_helpers: config deliver lookup failed: %s", exc)

    # 2. First enabled platform with a home channel.
    try:
        from gateway.config import load_gateway_config

        gcfg = load_gateway_config()
        for platform, pconfig in gcfg.platforms.items():
            if not getattr(pconfig, "enabled", False):
                continue
            home = gcfg.get_home_channel(platform)
            if home is not None:
                return platform.value if hasattr(platform, "value") else str(platform)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("proactive_helpers: home-channel discovery failed: %s", exc)

    return None


def _send_succeeded(raw_result: object) -> bool:
    """True when ``send_message_tool``'s result reports a successful send.

    The tool returns a JSON string; a successful send has ``success: true`` and
    no ``error``. A skipped-duplicate (cron auto-delivery) also counts as
    success — the message reaches the user via the scheduler's own delivery.
    """
    if not isinstance(raw_result, str):
        return False
    try:
        result = json.loads(raw_result)
    except (ValueError, TypeError):
        return False
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    return bool(result.get("success") or result.get("skipped"))


def deliver_proactive(message: str) -> bool:
    """Send ONE proactive message to the resolved target. True iff it landed.

    Callers own the governor decision (``should_deliver``) BEFORE calling this;
    this function is purely the verified transport. It never raises.
    """
    if not message or not message.strip():
        logger.debug("proactive_helpers: refusing to send empty message")
        return False

    # Ensure ~/.hermes/.env is populated (bot tokens, *_HOME_CHANNEL). Producers
    # can run from a bare subprocess whose parent did not load it; the loader is
    # idempotent, so calling it here makes delivery independent of entry path.
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("proactive_helpers: dotenv load skipped: %s", exc)

    target = resolve_target()
    if not target:
        logger.warning(
            "proactive_helpers: no delivery target resolved (set "
            "notifications.deliver in config.yaml or a platform home channel) "
            "— proactive message NOT sent"
        )
        return False

    try:
        from tools.send_message_tool import send_message_tool

        result = send_message_tool({"target": target, "message": message})
    except Exception as exc:
        logger.warning(
            "proactive_helpers: send to %s failed: %s", target, exc, exc_info=True
        )
        return False

    if not _send_succeeded(result):
        logger.warning(
            "proactive_helpers: send to %s did not succeed: %s",
            target,
            str(result)[:300],
        )
        return False
    return True
