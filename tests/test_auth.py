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
