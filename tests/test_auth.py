"""Tests for API-key authentication and rate-limit lockout."""

import pytest

from fast_mcp_claude.auth import ApiKeyVerifier, AuthRateLimiter


class TestApiKeyVerifier:
    @pytest.fixture
    def verifier(self) -> ApiKeyVerifier:
        return ApiKeyVerifier(api_key="test-secret-key-123")

    @pytest.mark.asyncio
    async def test_valid_key_returns_access_token(self, verifier):
        result = await verifier.verify_token("test-secret-key-123")
        assert result is not None
        assert result.client_id == "api-key-client"
        assert result.scopes == []

    @pytest.mark.asyncio
    async def test_wrong_key_returns_none(self, verifier):
        assert await verifier.verify_token("wrong") is None

    @pytest.mark.asyncio
    async def test_empty_key_returns_none(self, verifier):
        assert await verifier.verify_token("") is None

    @pytest.mark.asyncio
    async def test_partial_key_returns_none(self, verifier):
        assert await verifier.verify_token("test-secret") is None

    @pytest.mark.asyncio
    async def test_case_sensitive(self, verifier):
        assert await verifier.verify_token("TEST-SECRET-KEY-123") is None

    @pytest.mark.asyncio
    async def test_non_ascii_key_returns_none_not_raises(self, verifier):
        assert await verifier.verify_token("tëst-sëcret-këy-123") is None

    @pytest.mark.asyncio
    async def test_non_ascii_failure_counts_toward_lockout(self, verifier):
        for _ in range(5):
            assert await verifier.verify_token("tëst-wröng") is None
        # The 5 non-ASCII failures above must have registered with the rate
        # limiter (not skipped via an uncaught TypeError) to trigger lockout.
        assert await verifier._rate_limiter.check_rate_limit() is False

    @pytest.mark.asyncio
    async def test_valid_key_succeeds_during_active_lockout(self, verifier):
        for _ in range(5):
            assert await verifier.verify_token("wrong") is None
        assert await verifier._rate_limiter.check_rate_limit() is False
        result = await verifier.verify_token("test-secret-key-123")
        assert result is not None
        assert result.client_id == "api-key-client"

    @pytest.mark.asyncio
    async def test_general_key_has_no_admin_claim(self, verifier):
        # FMC-9: the general key is shared by every mesh peer, so it must never carry admin
        # trust on its own.
        result = await verifier.verify_token("test-secret-key-123")
        assert result is not None
        assert result.claims.get("admin") is not True


class TestApiKeyVerifierAdminKey:
    """FMC-9: a distinct admin_api_key is the ONLY way an AccessToken gets claims={"admin": True}
    -- the sole source of truth tools/messaging.py::send_prompt consults before letting a caller's
    metadata.triggering_admin claim become True."""

    @pytest.fixture
    def verifier(self) -> ApiKeyVerifier:
        return ApiKeyVerifier(api_key="general-key-123", admin_api_key="admin-key-456")

    @pytest.mark.asyncio
    async def test_admin_key_returns_admin_claim(self, verifier):
        result = await verifier.verify_token("admin-key-456")
        assert result is not None
        assert result.client_id == "admin-api-key-client"
        assert result.claims.get("admin") is True

    @pytest.mark.asyncio
    async def test_general_key_still_works_without_admin_claim(self, verifier):
        result = await verifier.verify_token("general-key-123")
        assert result is not None
        assert result.client_id == "api-key-client"
        assert result.claims.get("admin") is not True

    @pytest.mark.asyncio
    async def test_wrong_key_still_rejected(self, verifier):
        assert await verifier.verify_token("neither-key") is None

    @pytest.mark.asyncio
    async def test_wrong_key_still_counts_toward_lockout(self, verifier):
        for _ in range(5):
            assert await verifier.verify_token("neither-key") is None
        assert await verifier._rate_limiter.check_rate_limit() is False

    @pytest.mark.asyncio
    async def test_no_admin_key_configured_means_no_admin_claim_ever_possible(self):
        # Default posture (Settings.mcp_admin_api_key unset): nobody is ever admin-trusted.
        verifier = ApiKeyVerifier(api_key="general-key-123")
        result = await verifier.verify_token("general-key-123")
        assert result is not None
        assert result.claims.get("admin") is not True


class TestAuthRateLimiter:
    @pytest.mark.asyncio
    async def test_lockout_after_max_failures(self):
        limiter = AuthRateLimiter(max_attempts=3, lockout_duration=10.0, window=60.0)
        for _ in range(3):
            await limiter.record_failure()
        # 4th call should find us locked out.
        assert await limiter.check_rate_limit() is False

    @pytest.mark.asyncio
    async def test_success_clears_failures(self):
        limiter = AuthRateLimiter(max_attempts=3, lockout_duration=10.0, window=60.0)
        await limiter.record_failure()
        await limiter.record_failure()
        await limiter.record_success()
        # Should not be locked out and the failure count is cleared.
        assert await limiter.check_rate_limit() is True
        await limiter.record_failure()
        await limiter.record_failure()
        assert await limiter.check_rate_limit() is True  # only 2 failures, max is 3
