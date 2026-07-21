"""Tests for the server auth bootstrap (FMC-10)."""

import pytest

from fast_mcp_claude.auth import ApiKeyVerifier
from fast_mcp_claude.server import build_auth_provider


class TestBuildAuthProvider:
    def test_raises_when_enabled_but_key_is_none(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key=None)
        with pytest.raises(RuntimeError, match="MCP_API_KEY"):
            build_auth_provider(settings)

    def test_raises_when_enabled_but_key_is_empty_string(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key="")
        with pytest.raises(RuntimeError, match="MCP_API_KEY"):
            build_auth_provider(settings)

    def test_returns_verifier_when_key_is_set(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key="secret")
        provider = build_auth_provider(settings)
        assert isinstance(provider, ApiKeyVerifier)

    def test_returns_none_when_auth_explicitly_disabled(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=False, mcp_api_key=None)
        assert build_auth_provider(settings) is None


class TestMcpAuthEffective:
    """settings.mcp_auth_effective is the single source of truth server.py's
    build_auth_provider() and __main__.py's startup log both read -- without
    it, the two independently computed whether auth_enabled and drifted."""

    def test_true_when_enabled_and_key_set(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key="secret")
        assert settings.mcp_auth_effective is True

    def test_false_when_key_is_empty_string(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key="")
        assert settings.mcp_auth_effective is False

    def test_false_when_key_is_none(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=True, mcp_api_key=None)
        assert settings.mcp_auth_effective is False

    def test_false_when_auth_disabled_even_with_key_set(self, settings_factory):
        settings = settings_factory(mcp_auth_enabled=False, mcp_api_key="secret")
        assert settings.mcp_auth_effective is False
