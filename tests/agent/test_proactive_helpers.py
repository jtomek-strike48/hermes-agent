"""Tests for agent.proactive_helpers — the shared, verified delivery path.

Regression coverage for the phantom-delivery bug: producers used to call
``send_message_tool({"message": text})`` (no target), which errors, and reported
success anyway. These tests assert the helper (a) resolves a target, (b) sends
WITH it, and (c) reports the REAL send outcome.
"""

import json
from unittest.mock import patch

import agent.proactive_helpers as ph


def test_resolve_target_prefers_config_deliver():
    cfg = {"notifications": {"deliver": "slack:C0ABC123"}}
    with patch.object(ph, "load_config", return_value=cfg):
        assert ph.resolve_target() == "slack:C0ABC123"


def test_resolve_target_falls_back_to_enabled_home_platform():
    # No config override -> discover first enabled platform with a home channel.
    with patch.object(ph, "load_config", return_value={}):
        class _P:
            value = "slack"

        class _PC:
            enabled = True

        class _GCfg:
            platforms = {_P(): _PC()}

            def get_home_channel(self, platform):
                return object()  # any non-None = has a home

        with patch("gateway.config.load_gateway_config", return_value=_GCfg()):
            assert ph.resolve_target() == "slack"


def test_resolve_target_none_when_no_target():
    with patch.object(ph, "load_config", return_value={}):
        class _GCfg:
            platforms = {}

            def get_home_channel(self, platform):
                return None

        with patch("gateway.config.load_gateway_config", return_value=_GCfg()):
            assert ph.resolve_target() is None


def test_send_succeeded_parses_tool_result():
    assert ph._send_succeeded(json.dumps({"success": True})) is True
    assert ph._send_succeeded(json.dumps({"skipped": True})) is True  # cron dup
    assert ph._send_succeeded(json.dumps({"error": "boom"})) is False
    assert ph._send_succeeded(json.dumps({"success": False})) is False
    assert ph._send_succeeded("not json") is False
    assert ph._send_succeeded(None) is False


def test_deliver_proactive_sends_with_resolved_target():
    with patch.object(ph, "resolve_target", return_value="slack:C0X"), patch(
        "tools.send_message_tool.send_message_tool",
        return_value=json.dumps({"success": True}),
    ) as send:
        assert ph.deliver_proactive("hello") is True
        send.assert_called_once()
        args = send.call_args[0][0]
        assert args["target"] == "slack:C0X"
        assert args["message"] == "hello"


def test_deliver_proactive_false_when_no_target():
    with patch.object(ph, "resolve_target", return_value=None), patch(
        "tools.send_message_tool.send_message_tool"
    ) as send:
        assert ph.deliver_proactive("hello") is False
        send.assert_not_called()  # no phantom send


def test_deliver_proactive_false_when_send_errors():
    # This is the exact historical bug: the tool returns an error, and the
    # helper must NOT report success.
    with patch.object(ph, "resolve_target", return_value="slack:C0X"), patch(
        "tools.send_message_tool.send_message_tool",
        return_value=json.dumps(
            {"error": "Both 'target' and 'message' are required"}
        ),
    ):
        assert ph.deliver_proactive("hello") is False


def test_deliver_proactive_false_on_empty_message():
    with patch("tools.send_message_tool.send_message_tool") as send:
        assert ph.deliver_proactive("   ") is False
        send.assert_not_called()


def test_deliver_proactive_fail_soft_on_exception():
    with patch.object(ph, "resolve_target", return_value="slack:C0X"), patch(
        "tools.send_message_tool.send_message_tool",
        side_effect=RuntimeError("network down"),
    ):
        assert ph.deliver_proactive("hello") is False
