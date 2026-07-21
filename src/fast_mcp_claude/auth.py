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
    """Verifies bearer tokens against a configured API key (timing-safe).

    `admin_api_key`, when set, is a SECOND, distinct credential (FMC-9): every peer in a mesh
    shares the same `api_key`, so matching it alone proves only "some peer with the shared
    secret", not "a designated trusted hub/admin origin". A caller authenticated with
    `admin_api_key` instead gets `claims={"admin": True}` on its AccessToken -- the single
    source of truth tools/messaging.py::send_prompt consults before honoring a caller's claim
    to set metadata.triggering_admin.
    """

    def __init__(self, api_key: str, admin_api_key: str | None = None):
        super().__init__()
        self.api_key = api_key
        self.admin_api_key = admin_api_key or None
        self._rate_limiter = AuthRateLimiter()

    async def verify_token(self, token: str) -> AccessToken | None:
        # Compare first so a correct credential always succeeds, even during an
        # active lockout - the limiter has no per-connection identity to scope
        # itself to the attacker, so it must never gate the legitimate peer.
        token_bytes = token.encode("utf-8")
        if hmac.compare_digest(token_bytes, self.api_key.encode("utf-8")):
            await self._rate_limiter.record_success()
            return AccessToken(
                token=token,
                client_id="api-key-client",
                scopes=[],
                expires_at=None,
                claims={},
            )

        if self.admin_api_key and hmac.compare_digest(
            token_bytes, self.admin_api_key.encode("utf-8")
        ):
            await self._rate_limiter.record_success()
            return AccessToken(
                token=token,
                client_id="admin-api-key-client",
                scopes=[],
                expires_at=None,
                claims={"admin": True},
            )

        if not await self._rate_limiter.check_rate_limit():
            return None

        await self._rate_limiter.record_failure()
        return None
