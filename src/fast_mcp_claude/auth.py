"""API key authentication for the MCP endpoint.

When MCP_API_KEY is set, clients must send `Authorization: Bearer <key>`.
Includes timing-safe comparison and a failure-rate limiter.
"""

import asyncio
import hmac
import logging
from time import monotonic

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 60.0
ATTEMPT_WINDOW = 300.0


class AuthRateLimiter:
    """Tracks failed auth attempts; locks out after too many failures."""

    def __init__(
        self,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        lockout_duration: float = LOCKOUT_DURATION,
        window: float = ATTEMPT_WINDOW,
    ):
        self.max_attempts = max_attempts
        self.lockout_duration = lockout_duration
        self.window = window
        self._failed_attempts: list[float] = []
        self._lockout_until: float = 0
        self._lock = asyncio.Lock()

    async def check_rate_limit(self) -> bool:
        async with self._lock:
            now = monotonic()
            if now < self._lockout_until:
                logger.warning(
                    "Auth rate limited - lockout remaining: %.1fs",
                    self._lockout_until - now,
                )
                return False
            self._failed_attempts = [t for t in self._failed_attempts if now - t < self.window]
            return True

    async def record_failure(self) -> None:
        async with self._lock:
            now = monotonic()
            self._failed_attempts.append(now)
            self._failed_attempts = [t for t in self._failed_attempts if now - t < self.window]
            if len(self._failed_attempts) >= self.max_attempts:
                self._lockout_until = now + self.lockout_duration
                logger.warning(
                    "Auth rate limit triggered - locked out for %.1fs",
                    self.lockout_duration,
                )

    async def record_success(self) -> None:
        async with self._lock:
            self._failed_attempts.clear()
            self._lockout_until = 0


class ApiKeyVerifier(TokenVerifier):
    """Verifies bearer tokens against a configured API key (timing-safe)."""

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key
        self._rate_limiter = AuthRateLimiter()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not await self._rate_limiter.check_rate_limit():
            return None

        if not hmac.compare_digest(token, self.api_key):
            await self._rate_limiter.record_failure()
            return None

        await self._rate_limiter.record_success()
        return AccessToken(
            token=token,
            client_id="api-key-client",
            scopes=[],
            expires_at=None,
            claims={},
        )
