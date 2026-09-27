"""Tests for the Daikin One API client (custom_components/daikinone/api/client.py).

Covers the documented status -> exception table, request-body shapes for the three write
endpoints, the 3-open-request ceiling from Daikin's usage limits, and device-list parsing.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import (
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.daikinone.api import (
    DaikinOneClient,
    DaikinOneError,
    DeviceOfflineError,
    FanCirculate,
    FanCirculateSpeed,
    InvalidRequestError,
    MalformedResponseError,
    Mode,
    RateLimitedError,
    ServerError,
    TransportError,
    UnsupportedCapabilityError,
    error_for,
)
from custom_components.daikinone.api.exceptions import message_from_body, retry_after_from
from tests.conftest import (
    API_KEY,
    DEVICES_URL,
    EMAIL,
    INTEGRATOR_TOKEN,
    TOKEN_URL,
    calls,
    gated,
)

DEVICE_URL_RE = re.compile(rf"{re.escape(DEVICES_URL)}/[^/]+$")


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> DaikinOneClient:
    """A client bound to HA's shared session (which aioclient_mock intercepts)."""
    return DaikinOneClient(async_get_clientsession(hass), EMAIL, API_KEY, INTEGRATOR_TOKEN)


def _token_json() -> dict[str, Any]:
    return dict(load_json_object_fixture("token.json"))


@pytest.mark.parametrize(
    ("response", "expected", "retry_after"),
    [
        ({"status": 400, "json": {"messages": "Invalid request body"}}, InvalidRequestError, None),
        ({"status": 415, "json": {"messages": "Invalid request body"}}, InvalidRequestError, None),
        ({"status": 429, "headers": {"Retry-After": "600"}}, RateLimitedError, 600),
        ({"status": 429}, RateLimitedError, None),
        ({"status": 500}, ServerError, None),
        ({"status": 503}, ServerError, None),
        ({"status": 400, "json": {"messages": "DeviceOfflineException"}}, DeviceOfflineError, None),
        ({"status": 500, "json": {"messages": "DeviceOfflineException"}}, DeviceOfflineError, None),
        ({"exc": aiohttp.ClientError()}, TransportError, None),
        ({"status": 200, "text": "<html>gateway</html>"}, MalformedResponseError, None),
    ],
    ids=[
        "400-invalid-request",
        "415-invalid-request",
        "429-retry-after",
        "429-no-header",
        "500-server",
        "503-server",
        "offline-wins-over-400",
        "offline-wins-over-500",
        "transport",
        "not-json",
    ],
)
async def test_status_maps_to_exception(
    client: DaikinOneClient,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    expected: type[DaikinOneError],
    retry_after: float | None,
) -> None:
    """Every documented failure maps to its own exception class."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, **response)

    with pytest.raises(expected) as err:
        await client.async_get_devices()

    assert isinstance(err.value, DaikinOneError)
    if expected is RateLimitedError:
        assert isinstance(err.value, RateLimitedError)
        assert err.value.retry_after == retry_after


@pytest.mark.parametrize("key", ["message", "messages"])
async def test_error_message_read_from_both_body_keys(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker, key: str
) -> None:
    """Daikin documents 'messages' but returns 'message' on success payloads: read both."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, status=400, json={key: "DeviceOfflineException"})

    with pytest.raises(DeviceOfflineError):
        await client.async_get_devices()


async def test_never_more_than_three_open_requests(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Daikin's usage limit: at most 3 open requests, whatever the caller asks for."""
    counter = {"in_flight": 0, "peak": 0}
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(
        DEVICE_URL_RE,
        side_effect=gated(dict(load_json_object_fixture("device_oneplus.json")), counter),
    )

    states = await asyncio.gather(*(client.async_get_thermostat(f"dev{i}") for i in range(1, 7)))

    assert len(states) == 6
    assert counter["peak"] > 1, "requests never overlapped: the ceiling was not exercised"
    assert counter["peak"] == 3


async def test_msp_payload_has_exactly_the_documented_keys(
    client: DaikinOneClient, mock_api: AiohttpClientMocker
) -> None:
    """PUT /msp always sends mode + both setpoints, with the mode serialized as an int."""
    await client.async_set_mode_setpoints("dev1", Mode.HEAT, 20.0, 24.0)

    body = calls(mock_api, "put", "/v1/devices/dev1/msp")[0][2]
    assert body == {"mode": 1, "heatSetpoint": 20.0, "coolSetpoint": 24.0}
    assert set(body) == {"mode", "heatSetpoint", "coolSetpoint"}
    assert type(body["mode"]) is int


async def test_schedule_payload_is_a_bool(client: DaikinOneClient, mock_api: AiohttpClientMocker) -> None:
    """PUT /schedule sends the documented single boolean key."""
    await client.async_set_schedule_enabled("dev2", enabled=False)

    body = calls(mock_api, "put", "/v1/devices/dev2/schedule")[0][2]
    assert body == {"scheduleEnabled": False}
    assert body["scheduleEnabled"] is False


async def test_fan_payload_has_both_keys(client: DaikinOneClient, mock_api: AiohttpClientMocker) -> None:
    """PUT /fan sends both circulation keys as ints."""
    await client.async_set_fan("dev3", FanCirculate.ALWAYS_ON, FanCirculateSpeed.HIGH)

    body = calls(mock_api, "put", "/v1/devices/dev3/fan")[0][2]
    assert body == {"fanCirculate": 1, "fanCirculateSpeed": 2}
    assert type(body["fanCirculate"]) is int
    assert type(body["fanCirculateSpeed"]) is int


async def test_fan_rejected_means_unsupported_capability(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """VRV / split equipment rejects fan circulation: surface it as a capability problem."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.put(
        re.compile(rf"{re.escape(DEVICES_URL)}/[^/]+/fan$"),
        status=400,
        json={"messages": "Invalid request body"},
    )

    with pytest.raises(UnsupportedCapabilityError) as err:
        await client.async_set_fan("dev1", FanCirculate.SCHEDULE, FanCirculateSpeed.LOW)

    assert err.value.code == "unsupported_capability"


async def test_get_devices_parses_locations(client: DaikinOneClient, mock_api: AiohttpClientMocker) -> None:
    """Every thermostat of every location is returned with its location name attached."""
    devices = await client.async_get_devices()

    assert [d.id for d in devices] == ["dev1", "dev2", "dev3"]
    assert [d.location_name for d in devices] == ["Home", "Home", "Cabin"]
    assert devices[0].name == "Main Floor"
    assert devices[2].model == "TOUCH"
    assert devices[2].firmware_version == "2.3.5"


async def test_get_devices_skips_entries_without_id(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A device entry with no id cannot be addressed, so it is dropped rather than crashing."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, json=list(load_json_array_fixture("devices_missing_id.json")))

    devices = await client.async_get_devices()

    assert [d.id for d in devices] == ["dev1"]


async def test_get_devices_rejects_non_list_body(client: DaikinOneClient, aioclient_mock: AiohttpClientMocker) -> None:
    """GET /v1/devices is documented as an array; an object is a malformed response."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, json={"locationName": "Home"})

    with pytest.raises(MalformedResponseError):
        await client.async_get_devices()


async def test_get_devices_tolerates_malformed_entries(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Junk entries in the location/device arrays are skipped, not fatal."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(
        DEVICES_URL,
        json=["nonsense", {"locationName": "Home", "devices": ["nope", {"id": "dev1", "name": "Main"}]}],
    )

    devices = await client.async_get_devices()

    assert [d.id for d in devices] == ["dev1"]


async def test_get_thermostat_rejects_non_object_body(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """GET /v1/devices/{id} is documented as an object; an array is malformed."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICE_URL_RE, json=[1, 2, 3])

    with pytest.raises(MalformedResponseError):
        await client.async_get_thermostat("dev1")


def test_error_for_unmapped_status_is_the_base_error() -> None:
    """An undocumented status still produces a typed, message-safe error."""
    err = error_for(302)

    assert type(err) is DaikinOneError
    assert err.code == "unknown"
    assert err.status == 302
    assert "302" in str(err)


@pytest.mark.parametrize("body", ["<html>", '["a"]', "{}", '{"messages": 5}'])
def test_message_from_body_returns_none_when_absent(body: str) -> None:
    """A body without a documented string message yields no message at all."""
    assert message_from_body(body) is None


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}])
def test_retry_after_ignores_non_numeric_headers(headers: dict[str, str]) -> None:
    """Only the delay-seconds form of Retry-After is honoured."""
    assert retry_after_from(headers) is None


def _flaky(*responses: dict[str, Any] | BaseException) -> Any:
    queue = list(responses)

    async def _side_effect(method: str, url: Any, data: Any) -> Any:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return AiohttpClientMockResponse(method=method, url=url, **item)

    return _side_effect


@pytest.mark.parametrize("error", [aiohttp.ServerDisconnectedError(), TimeoutError(), aiohttp.ClientError()])
async def test_a_transport_failure_is_retried_once(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker, error: BaseException
) -> None:
    # GIVEN
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, side_effect=_flaky(error, {"json": load_json_array_fixture("devices.json")}))

    # WHEN
    devices = await client.async_get_devices()

    # THEN
    assert devices
    assert len(calls(aioclient_mock, "GET", "/v1/devices")) == 2


async def test_a_second_transport_failure_names_its_cause(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, side_effect=_flaky(aiohttp.ServerDisconnectedError()))

    # WHEN
    with pytest.raises(TransportError) as err:
        await client.async_get_devices()

    # THEN
    assert err.value.cause == "ServerDisconnectedError"
    assert len(calls(aioclient_mock, "GET", "/v1/devices")) == 2


async def test_a_token_request_transport_failure_is_retried(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    aioclient_mock.post(TOKEN_URL, side_effect=_flaky(TimeoutError(), {"json": _token_json()}))
    aioclient_mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))

    # WHEN
    devices = await client.async_get_devices()

    # THEN
    assert devices
    assert len(calls(aioclient_mock, "POST", "/v1/token")) == 2


async def test_a_write_is_retried_after_a_transport_failure(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.put(
        f"{DEVICES_URL}/dev1/msp", side_effect=_flaky(aiohttp.ClientError(), {"json": {"message": "ok"}})
    )

    # WHEN
    await client.async_set_mode_setpoints("dev1", Mode.HEAT, 20.0, 24.0)

    # THEN
    puts = calls(aioclient_mock, "PUT", "/v1/devices/dev1/msp")
    assert len(puts) == 2
    assert puts[0][2] == puts[1][2] == {"mode": 1, "heatSetpoint": 20.0, "coolSetpoint": 24.0}


async def test_http_errors_are_not_retried(client: DaikinOneClient, aioclient_mock: AiohttpClientMocker) -> None:
    # GIVEN
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, status=500)

    # WHEN
    with pytest.raises(ServerError):
        await client.async_get_devices()

    # THEN
    assert len(calls(aioclient_mock, "GET", "/v1/devices")) == 1
