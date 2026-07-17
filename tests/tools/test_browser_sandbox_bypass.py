"""Tests for browser.sandbox_bypass control of --no-sandbox injection."""

from unittest.mock import patch

import pytest


def _reset_cache():
    import tools.browser_tool as bt
    bt._cached_sandbox_bypass = None


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_cache()
    yield
    _reset_cache()


class TestGetSandboxBypassMode:
    def test_default_is_auto(self):
        from tools.browser_tool import _get_sandbox_bypass_mode
        with patch("hermes_cli.config.read_raw_config", return_value={}):
            assert _get_sandbox_bypass_mode() == "auto"

    def test_config_never(self):
        from tools.browser_tool import _get_sandbox_bypass_mode
        cfg = {"browser": {"sandbox_bypass": "never"}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _get_sandbox_bypass_mode() == "never"

    def test_invalid_falls_back_to_auto(self):
        from tools.browser_tool import _get_sandbox_bypass_mode
        cfg = {"browser": {"sandbox_bypass": "yolo"}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _get_sandbox_bypass_mode() == "auto"

    def test_config_read_failure_falls_back_to_auto(self):
        from tools.browser_tool import _get_sandbox_bypass_mode
        with patch("hermes_cli.config.read_raw_config", side_effect=OSError("boom")):
            assert _get_sandbox_bypass_mode() == "auto"

    def test_cached_after_first_read(self):
        from tools.browser_tool import _get_sandbox_bypass_mode
        cfg = {"browser": {"sandbox_bypass": "never"}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg) as m:
            assert _get_sandbox_bypass_mode() == "never"
            assert _get_sandbox_bypass_mode() == "never"
            assert m.call_count == 1
