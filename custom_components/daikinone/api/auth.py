"""Access-token handling for the official Daikin One Open API.

There is no refresh flow: a token simply expires after ``accessTokenExpiresIn`` seconds and a
new one is minted by POSTing the credentials again. Tokens do not invalidate each other, so a
concurrent refresh is harmless -- but wasteful, hence the single-flight lock.
"""

from __future__ import annotations

import asyncio
from json import loads as json_loads
import logging
from time import monotonic

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import API_BASE_URL, REQUEST_TIMEOUT, TOKEN_PATH, TOKEN_REFRESH_MARGIN
from .exceptions import (
    InvalidApiKeyError,
    InvalidCredentialsError,
    MalformedResponseError,
    TransportError,
    error_for,
    message_from_body,
    retry_after_from,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["IntegratorAuth"]


class IntegratorAuth:
    """Mints and caches the Integrator API access token for one account."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        api_key: str,
        integrator_token: str,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Store the credentials and the request budget shared with the client."""
        self._session = session
        self._email = email
        self._api_key = api_key
        self._integrator_token = integrator_token
        self._semaphore = semaphore
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def expires_in(self) -> float | None:
        """Seconds left on the cached token, or None when no token is held (diagnostics)."""
        if self._token is None:
            return None
        return self._expires_at - monotonic()

    def invalidate(self, token: str) -> None:
        """Drop the cached token, but only if it is still the one that just failed.

        Without the equality check, three concurrent 401s would each throw away a freshly
        minted token and trigger another refresh.
        """
        if self._token == token:
            self._token = None
            self._expires_at = 0.0

    async def async_get_access_token(self) -> str:
        """Return a valid access token, minting one only when needed."""
        if (token := self._cached_token()) is not None:
            return token
        # Single-flight: everyone else waits here and then sees the freshly cached token.
        async with self._lock:
            if (token := self._cached_token()) is not None:
                return token
            return await self._async_fetch_token()

    def _cached_token(self) -> str | None:
        """Return the cached token while it is still comfortably valid."""
        if self._token is not None and self._expires_at - TOKEN_REFRESH_MARGIN > monotonic():
            return self._token
        return None

    async def _async_fetch_token(self) -> str:
        """POST /v1/token and cache the result. Caller must hold the lock."""
        payload = {"email": self._email, "integratorToken": self._integrator_token}
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        async with self._semaphore:
            try:
                async with self._session.post(
                    f"{API_BASE_URL}{TOKEN_PATH}",
                    json=payload,
                    headers=headers,
                    timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    status = resp.status
                    retry_after = retry_after_from(resp.headers)
                    text = await resp.text()
            except (TimeoutError, ClientError) as err:
                raise TransportError.from_cause(err) from err

        _LOGGER.debug("POST %s -> %s", TOKEN_PATH, status)

        if status >= 300:
            # The docs do not pin down the status for a bad integrator token, so 400 and 401
            # here both mean "credentials rejected" (403 stays the API-key signal).
            if status in (400, 401):
                raise InvalidCredentialsError(status)
            if status == 403:
                raise InvalidApiKeyError(status)
            raise error_for(status, message_from_body(text), retry_after)

        try:
            body = json_loads(text)
        except ValueError:
            raise MalformedResponseError(status) from None
        if not isinstance(body, dict):
            raise MalformedResponseError(status)

        access_token = body.get("accessToken")
        expires_in = body.get("accessTokenExpiresIn")
        if not isinstance(access_token, str) or not access_token:
            raise MalformedResponseError(status)
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise MalformedResponseError(status)

        self._token = access_token
        self._expires_at = monotonic() + float(expires_in)
        return access_token
