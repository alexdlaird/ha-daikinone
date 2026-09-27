"""Exception hierarchy for the official Daikin One Open API.

Every message is a FIXED string built from the class and (optionally) the HTTP status code.
Response bodies, headers, tokens and credentials must never reach an exception message,
because those messages end up in logs, repair issues and diagnostics dumps.
"""

from __future__ import annotations

from collections.abc import Mapping
from json import loads as json_loads

#: Message returned by the API when the thermostat itself cannot be reached.
DEVICE_OFFLINE_MESSAGE = "DeviceOfflineException"

__all__ = [
    "AuthError",
    "DaikinAuthError",
    "DaikinOneError",
    "DeviceOfflineError",
    "InvalidApiKeyError",
    "InvalidCredentialsError",
    "InvalidRequestError",
    "MalformedResponseError",
    "RateLimitedError",
    "ServerError",
    "TokenExpiredError",
    "TransportError",
    "UnsupportedCapabilityError",
    "error_for",
]


class DaikinOneError(Exception):
    """Base class for every Daikin One API failure.

    ``code`` is the stable identifier used as a translation key by the HA layer.
    """

    code: str = "unknown"
    message: str = "Daikin One API request failed"

    def __init__(self, status: int | None = None) -> None:
        """Build the error from a fixed message plus an optional HTTP status code."""
        super().__init__(self.message if status is None else f"{self.message} (HTTP {status})")
        self.status: int | None = status


class DaikinAuthError(DaikinOneError):
    """Authentication or authorisation failure: the config entry needs attention."""

    code = "auth_failed"
    message = "Daikin One authentication failed"


#: Alias kept because the architecture document refers to this base class as ``AuthError``.
AuthError = DaikinAuthError


class InvalidCredentialsError(DaikinAuthError):
    """The email or the integrator token was rejected by POST /v1/token."""

    code = "invalid_credentials"
    message = "Invalid Daikin One email or integrator token"


class InvalidApiKeyError(DaikinAuthError):
    """The integrator API key was rejected (HTTP 403)."""

    code = "invalid_api_key"
    message = "Invalid Daikin One API key"


class TokenExpiredError(DaikinAuthError):
    """The access token was rejected (HTTP 401) and refreshing it did not help."""

    code = "token_expired"
    message = "Daikin One access token was rejected"


class RateLimitedError(DaikinOneError):
    """Too many requests (HTTP 429)."""

    code = "rate_limited"
    message = "Rate limited by the Daikin One API"

    def __init__(self, status: int | None = None, retry_after: float | None = None) -> None:
        """Record the seconds to wait, when the API sent a numeric Retry-After header."""
        super().__init__(status)
        self.retry_after: float | None = retry_after


class DeviceOfflineError(DaikinOneError):
    """The thermostat is not reachable by Daikin's cloud (DeviceOfflineException)."""

    code = "device_offline"
    message = "The thermostat is offline"


class InvalidRequestError(DaikinOneError):
    """The request was rejected as malformed (HTTP 400 / 415)."""

    code = "invalid_request"
    message = "The Daikin One API rejected the request"


class UnsupportedCapabilityError(DaikinOneError):
    """The equipment does not support the requested feature (VRV / split fan circulation)."""

    code = "unsupported_capability"
    message = "This equipment does not support the requested setting"


class ServerError(DaikinOneError):
    """The Daikin One API returned a server error (HTTP 5xx)."""

    code = "server_error"
    message = "The Daikin One API returned a server error"


class TransportError(DaikinOneError):
    """The request never completed (connection error or timeout)."""

    code = "transport_error"
    message = "Could not reach the Daikin One API"
    #: The underlying exception's class name (never its text, which can carry request details).
    cause: str = "unknown"

    @classmethod
    def from_cause(cls, err: BaseException) -> TransportError:
        error = cls()
        error.cause = type(err).__name__
        return error


class MalformedResponseError(DaikinOneError):
    """The API answered with something that is not the documented JSON payload."""

    code = "malformed_response"
    message = "The Daikin One API returned an unexpected response"


def error_for(status: int, message: str | None = None, retry_after: float | None = None) -> DaikinOneError:
    """Map an HTTP status (and the documented body message) to an exception instance.

    ``DeviceOfflineException`` wins over the status code: Daikin does not document which
    status accompanies it, so the message is the only reliable signal.
    """
    if message is not None and DEVICE_OFFLINE_MESSAGE in message:
        return DeviceOfflineError(status)
    if status in (400, 415):
        return InvalidRequestError(status)
    if status == 401:
        return TokenExpiredError(status)
    if status == 403:
        return InvalidApiKeyError(status)
    if status == 429:
        return RateLimitedError(status, retry_after)
    if status >= 500:
        return ServerError(status)
    return DaikinOneError(status)


def message_from_body(text: str) -> str | None:
    """Extract the documented error message from a response body.

    The docs use ``messages`` (plural) for errors and ``message`` for write acknowledgements,
    so both are read. The value is used for classification only, never for display.
    """
    try:
        body = json_loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("messages", "message"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return None


def retry_after_from(headers: Mapping[str, str]) -> float | None:
    """Parse a ``Retry-After`` header given in seconds; the HTTP-date form is ignored."""
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(int(raw.strip()))
    except ValueError:
        return None
