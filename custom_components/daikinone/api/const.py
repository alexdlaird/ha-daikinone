"""Constants of the official Daikin One Open API.

Ground truth: the official documentation snapshot in tests/spec/daikin_open_api.json.
The unofficial private Skyport API (password login, device-data endpoints) is out of scope.
"""

from __future__ import annotations

from typing import Final

API_BASE_URL: Final = "https://integrator-api.daikinskyport.com"
TOKEN_PATH: Final = "/v1/token"
DEVICES_PATH: Final = "/v1/devices"

#: Total timeout for a single HTTP request, in seconds.
REQUEST_TIMEOUT: Final = 30
#: Seconds before the single retry of a request that failed at the transport level.
TRANSPORT_RETRY_DELAY: Final = 2.0
#: Refresh the access token this many seconds before it expires (documented lifetime: 900 s).
TOKEN_REFRESH_MARGIN: Final = 60
#: Daikin's "API USAGE LIMITS": no more than 3 open requests at a time.
MAX_CONCURRENT_REQUESTS: Final = 3

__all__ = [
    "API_BASE_URL",
    "DEVICES_PATH",
    "MAX_CONCURRENT_REQUESTS",
    "REQUEST_TIMEOUT",
    "TOKEN_PATH",
    "TOKEN_REFRESH_MARGIN",
    "TRANSPORT_RETRY_DELAY",
]
