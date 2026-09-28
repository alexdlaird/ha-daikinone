"""Polling behaviour, backoff, offline handling and the post-write verification read."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.api import DeviceSummary, FanCirculate, FanCirculateSpeed, Mode, Thermostat
from custom_components.daikinone.api.const import MAX_CONCURRENT_REQUESTS
from custom_components.daikinone.const import DOMAIN, PLATFORMS
from custom_components.daikinone.coordinator import DaikinOneCoordinator
from custom_components.daikinone.entity import DaikinOneEntity, async_setup_platform_entities

from .conftest import ACCESS_TOKEN, DEVICE_IDS, DEVICES_URL, PUT_URL_RE, TOKEN_URL, calls, gated, sequence

DEVICES_PATH = "/v1/devices"
TOKEN_PATH = "/v1/token"
OFFLINE_500 = {"status": 500, "json": {"messages": "DeviceOfflineException"}}


@pytest.fixture(autouse=True)
def _deterministic_scheduling() -> Iterator[None]:
    """Remove both sources of scheduling randomness so intervals are exact.

    * the coordinator's own 0-10 s jitter, and
    * HA's 0.05-0.50 s stagger, pinned to the 0.5 s that ``async_fire_time_changed`` adds.
    """
    integration = Path(__file__).parent.parent / "custom_components" / "daikinone"
    available = [platform for platform in PLATFORMS if (integration / f"{platform.value}.py").is_file()]
    with (
        patch("custom_components.daikinone.PLATFORMS", available),
        patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0),
        patch("homeassistant.helpers.update_coordinator.randint", return_value=500000),
    ):
        yield


def _mock_account(
    mock: AiohttpClientMocker,
    *,
    devices: str | list[Any] | dict[str, Any] = "devices.json",
    ids: tuple[str, ...] = DEVICE_IDS,
    details: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Register a full account: token, device list and one detail response per thermostat.

    ``devices`` is a fixture name, a literal payload, or raw ``aioclient_mock`` kwargs.
    """
    mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    if isinstance(devices, dict):
        mock.get(DEVICES_URL, **devices)
    else:
        mock.get(DEVICES_URL, json=load_json_array_fixture(devices) if isinstance(devices, str) else devices)
    detail = load_json_object_fixture("device_oneplus.json")
    for device_id in ids:
        override = (details or {}).get(device_id)
        mock.get(f"{DEVICES_URL}/{device_id}", **(override if override is not None else {"json": detail}))
    mock.put(PUT_URL_RE, json=load_json_object_fixture("write_ok.json"))


def _account(*, without: str | None = None) -> list[Any]:
    """The two-location device list, optionally with one thermostat taken out."""
    locations: list[Any] = load_json_array_fixture("devices.json")
    for location in locations:
        location["devices"] = [d for d in location["devices"] if d["id"] != without]
    return locations


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> DaikinOneCoordinator:
    """Add and set up the entry, returning its coordinator."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int = 181) -> None:
    """Advance past the poll interval and let the timer-driven refresh finish."""
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _climate_id(hass: HomeAssistant, thermostat_id: str) -> str | None:
    """The entity id of a thermostat's climate entity, if it is registered."""
    return er.async_get(hass).async_get_entity_id("climate", DOMAIN, f"{thermostat_id}-climate")


def _levels(caplog: pytest.LogCaptureFixture, needle: str) -> list[int]:
    """Levels of the captured records containing ``needle``, in order."""
    return [record.levelno for record in caplog.records if needle in record.getMessage()]


def _seed_devices(registry: dr.DeviceRegistry, entry: MockConfigEntry, *device_ids: str) -> None:
    """Pre-register thermostat devices, as HA restores them from a previous run."""
    for device_id in device_ids:
        registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, device_id)}, name=device_id)


async def test_thermostats_from_several_locations(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """All thermostats load, and their device names carry the location prefix."""
    coordinator = init_integration.runtime_data
    assert set(coordinator.data) == set(DEVICE_IDS)

    assert DaikinOneEntity(coordinator, "dev1").device_info["name"] == "Home Main Floor"
    assert DaikinOneEntity(coordinator, "dev3").device_info["name"] == "Cabin Main Room"


async def test_single_location_names_are_not_prefixed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """With one location the thermostat name is used as-is."""
    six = tuple(f"dev{index}" for index in range(1, 7))
    _mock_account(aioclient_mock, devices="devices_six.json", ids=six)
    coordinator = await _setup(hass, mock_config_entry)

    assert DaikinOneEntity(coordinator, "dev1").device_info["name"] == "T1"


async def test_empty_account_loads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An account without thermostats still sets up cleanly."""
    _mock_account(aioclient_mock, devices=[], ids=())
    coordinator = await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert coordinator.data == {}


async def test_offline_thermostat_is_isolated_and_logged_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One unreachable thermostat keeps its last state; the others are unaffected."""
    detail = load_json_object_fixture("device_oneplus.json")
    _mock_account(
        aioclient_mock,
        details={
            "dev2": {
                "side_effect": sequence({"json": detail}, OFFLINE_500, OFFLINE_500, {"json": detail}),
            }
        },
    )

    with caplog.at_level(logging.INFO, logger="custom_components.daikinone.coordinator"):
        coordinator = await _setup(hass, mock_config_entry)
        assert coordinator.data["dev2"].online is True

        await coordinator.async_refresh()
        assert coordinator.data["dev2"].online is False
        # The last known state survives, so entity attributes do not flap to None.
        assert coordinator.data["dev2"].state.heat_setpoint == 20.0
        assert coordinator.data["dev1"].online is True
        assert coordinator.data["dev3"].online is True
        assert coordinator.last_error_code == "device_offline"
        # The level is load-bearing: HA's log-when-unavailable rule is about *not* spamming
        # WARNING/ERROR for a device that is merely offline.
        assert _levels(caplog, "is unreachable") == [logging.INFO]

        await coordinator.async_refresh()
        assert coordinator.data["dev2"].online is False
        assert _levels(caplog, "is unreachable") == [logging.INFO]

        await coordinator.async_refresh()
        assert coordinator.data["dev2"].online is True
        assert _levels(caplog, "is reachable again") == [logging.INFO]

    offline_entity = DaikinOneEntity(coordinator, "dev2")
    offline_entity.hass = hass
    assert offline_entity.available is True


async def test_offline_entity_is_unavailable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`_requires_online` entities of an unreachable thermostat go unavailable."""
    _mock_account(aioclient_mock, details={"dev2": OFFLINE_500})
    coordinator = await _setup(hass, mock_config_entry)

    entity = DaikinOneEntity(coordinator, "dev2")
    entity.hass = hass
    assert entity.available is False

    entity._requires_online = False
    assert entity.available is True


async def test_detail_requests_respect_the_three_request_ceiling(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Six thermostats never produce more than Daikin's three open requests."""
    counter = {"in_flight": 0, "peak": 0}
    detail = load_json_object_fixture("device_oneplus.json")
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(DEVICES_URL, json=load_json_array_fixture("devices_six.json"))
    for index in range(1, 7):
        aioclient_mock.get(f"{DEVICES_URL}/dev{index}", side_effect=gated(detail, counter))

    coordinator = await _setup(hass, mock_config_entry)

    assert len(coordinator.data) == 6
    assert counter["peak"] <= 3
    assert counter["peak"] > 1, "requests never overlapped: the ceiling was not exercised"


async def test_the_token_request_waits_for_a_free_request_slot(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Daikin's ceiling of three open requests covers the token POST, not only data calls."""
    coordinator = await _setup(hass, mock_config_entry)
    client = coordinator.client
    # Force the next call to mint a token, then hand every request slot to someone else.
    client.auth.invalidate(ACCESS_TOKEN)
    semaphore = client._semaphore
    for _ in range(MAX_CONCURRENT_REQUESTS):
        await semaphore.acquire()

    before = len(calls(mock_api, "POST", TOKEN_PATH))
    task = hass.async_create_task(client.async_get_devices())
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(calls(mock_api, "POST", TOKEN_PATH)) == before, (
        "the token POST went out while all three request slots were taken"
    )

    # One slot frees up: the token POST takes it, releases it, and the device list follows.
    semaphore.release()
    await task
    assert len(calls(mock_api, "POST", TOKEN_PATH)) == before + 1

    semaphore.release()
    semaphore.release()


async def test_poll_interval_is_exactly_the_base_interval(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """No poll happens at 179 s; the next one lands just after 180 s."""
    coordinator = init_integration.runtime_data
    unsub = coordinator.async_add_listener(lambda: None)
    before = len(calls(mock_api, "GET", f"{DEVICES_PATH}/dev1"))

    freezer.tick(179)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", f"{DEVICES_PATH}/dev1")) == before

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", f"{DEVICES_PATH}/dev1")) == before + 1

    unsub()


async def test_rate_limit_without_header_backs_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 429 without Retry-After doubles the interval before the next attempt."""
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(
        DEVICES_URL,
        side_effect=sequence({"json": load_json_array_fixture("devices.json")}, {"status": 429}),
    )
    for device_id in DEVICE_IDS:
        aioclient_mock.get(f"{DEVICES_URL}/{device_id}", json=load_json_object_fixture("device_oneplus.json"))

    coordinator = await _setup(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert coordinator.last_error_code == "rate_limited"
    after_failure = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    # Backoff is base_interval * 2**1 = 360 s, so 180 s later there is still no request.
    freezer.tick(359)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure + 1

    unsub()


async def test_retry_after_header_is_honoured(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 429 with `Retry-After: 600` suppresses requests for the full 600 s."""
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(
        DEVICES_URL,
        side_effect=sequence(
            {"json": load_json_array_fixture("devices.json")},
            {"status": 429, "headers": {"Retry-After": "600"}},
        ),
    )
    for device_id in DEVICE_IDS:
        aioclient_mock.get(f"{DEVICES_URL}/{device_id}", json=load_json_object_fixture("device_oneplus.json"))

    coordinator = await _setup(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    after_failure = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    freezer.tick(599)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure + 1

    unsub()


async def test_server_error_then_recovery(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """One account-level 500 is ridden out on the last reading; a second fails; then it recovers."""
    devices = load_json_array_fixture("devices.json")
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(
        DEVICES_URL,
        side_effect=sequence({"json": devices}, {"status": 500}, {"status": 500}, {"json": devices}),
    )
    for device_id in DEVICE_IDS:
        aioclient_mock.get(f"{DEVICES_URL}/{device_id}", json=load_json_object_fixture("device_oneplus.json"))

    coordinator = await _setup(hass, mock_config_entry)

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.last_error_code == "server_error"
    assert set(coordinator.data) == set(DEVICE_IDS)

    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert set(coordinator.data) == set(DEVICE_IDS)


async def test_server_error_defers_the_next_poll_by_the_exponential_backoff(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Once a 5xx is no longer tolerated, it backs the poll off like a 429: base * 2**1 = 360 s."""
    _mock_account(
        aioclient_mock,
        devices={"side_effect": sequence({"json": _account()}, {"status": 500})},
    )
    coordinator = await _setup(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_update_success is True

    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert coordinator.last_error_code == "server_error"
    after_failure = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    freezer.tick(359)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_failure + 1

    unsub()


async def test_a_small_retry_after_never_shortens_the_exponential_backoff(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Retry-After may only lengthen the wait: max(min(base * 2**n, 1800), Retry-After).

    A 429 carrying a short header must not pin repeated failures at the 180 s floor, nor
    shrink a backoff that has already grown.
    """
    _mock_account(
        aioclient_mock,
        devices={
            "side_effect": sequence({"json": _account()}, {"status": 429, "headers": {"Retry-After": "5"}}),
        },
    )
    coordinator = await _setup(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    # First 429: n == 1, so 360 s -- not the 180 s floor and certainly not 5 s.
    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_error_code == "rate_limited"
    after_first = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    freezer.tick(359)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_first

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    after_second = len(calls(aioclient_mock, "GET", DEVICES_PATH))
    assert after_second == after_first + 1
    assert coordinator._consecutive_failures == 2

    # Second 429: n == 2, so 720 s. The header must not drag it back down.
    freezer.tick(719)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_second

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_second + 1

    unsub()


async def test_a_write_does_not_read_back_during_an_active_backoff(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A write inside a Retry-After window must not trigger the 15 s verification read.

    The API asked for 600 s of quiet; the retry HA has already scheduled is the earliest
    read we are allowed to make, and it reconciles the write anyway.
    """
    _mock_account(
        aioclient_mock,
        devices={
            "side_effect": sequence(
                {"json": _account()},
                {"status": 429, "headers": {"Retry-After": "600"}},
                {"json": _account()},
            ),
        },
    )
    coordinator = await _setup(hass, mock_config_entry)
    unsub = coordinator.async_add_listener(lambda: None)

    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert coordinator.last_error_code == "rate_limited"
    after_429 = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    # A user write lands during the backoff window. It is applied optimistically...
    freezer.tick(10)
    await coordinator.async_set_schedule_enabled("dev1", False)
    assert coordinator.data["dev1"].state.schedule_enabled is False

    # ...but neither the verification read nor a reset poll timer may break the quiet period.
    freezer.tick(16)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_429

    freezer.tick(400)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_429

    # 600 s after the 429, the scheduled retry reads -- and reconciles the write with it.
    freezer.tick(180)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == after_429 + 1
    assert coordinator.last_update_success is True
    assert coordinator.data["dev1"].state.schedule_enabled is True

    unsub()


async def test_write_is_verified_once_after_the_settle_delay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """No read for 15 s after a write, then exactly one, then normal polling resumes."""
    coordinator = init_integration.runtime_data
    unsub = coordinator.async_add_listener(lambda: None)
    before = len(calls(mock_api, "GET", DEVICES_PATH))

    await coordinator.async_set_schedule_enabled("dev1", False)
    assert coordinator.data["dev1"].state.schedule_enabled is False

    freezer.tick(14)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", DEVICES_PATH)) == before

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", DEVICES_PATH)) == before + 1

    # The regular 180 s cadence restarts from the verification read.
    freezer.tick(181)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", DEVICES_PATH)) == before + 2

    unsub()


async def test_consecutive_writes_coalesce_into_one_verification(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """Writes at 0, 5 and 10 s produce a single read 15 s after the last one."""
    coordinator = init_integration.runtime_data
    unsub = coordinator.async_add_listener(lambda: None)
    before = len(calls(mock_api, "GET", DEVICES_PATH))

    await coordinator.async_set_schedule_enabled("dev1", False)
    freezer.tick(5)
    await coordinator.async_set_schedule_enabled("dev1", True)
    freezer.tick(5)
    await coordinator.async_set_schedule_enabled("dev1", False)

    freezer.tick(14)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", DEVICES_PATH)) == before

    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls(mock_api, "GET", DEVICES_PATH)) == before + 1

    unsub()


async def test_legacy_equipment_devices_are_removed_on_the_first_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """ha-daikinone's equipment children are dropped; the thermostat device survives."""
    mock_config_entry.add_to_hass(hass)
    # Upstream registered one device per thermostat plus a child device per piece of
    # equipment, identified by "<model>-<serial>" and linked with via_device.
    device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "dev1")},
        name="Main Floor",
    )
    equipment = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "DM96VC0803BNAB-PLACEHOLDER-SERIAL")},
        via_device=(DOMAIN, "dev1"),
        manufacturer="Daikin",
        model="DM96VC0803BNAB",
        name="Main Floor Furnace",
    )
    assert equipment.via_device_id is not None

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert device_registry.async_get_device(identifiers={(DOMAIN, "DM96VC0803BNAB-PLACEHOLDER-SERIAL")}) is None
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev1")}) is not None


async def test_a_thermostat_that_returns_is_registered_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A thermostat that leaves the account and comes back needs no reload to return."""
    _mock_account(
        aioclient_mock,
        devices={
            "side_effect": sequence({"json": _account()}, {"json": _account(without="dev3")}, {"json": _account()})
        },
    )
    await _setup(hass, mock_config_entry)
    assert _climate_id(hass, "dev3") is not None

    await _poll(hass, freezer)
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is None
    assert _climate_id(hass, "dev3") is None

    await _poll(hass, freezer)
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is not None
    entity_id = _climate_id(hass, "dev3")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "heat_cool"


async def test_one_blank_device_list_never_wipes_the_registry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An empty ``/v1/devices`` reply is not evidence that every thermostat was deleted."""
    _mock_account(
        aioclient_mock,
        devices={"side_effect": sequence({"json": _account()}, {"json": []}, {"json": _account()})},
    )
    await _setup(hass, mock_config_entry)

    await _poll(hass, freezer)
    for device_id in DEVICE_IDS:
        assert device_registry.async_get_device(identifiers={(DOMAIN, device_id)}) is not None
        assert _climate_id(hass, device_id) is not None
    # Entity ids, areas and automations survive; the entities merely go unavailable.
    assert hass.states.get(_climate_id(hass, "dev1")).state == STATE_UNAVAILABLE

    await _poll(hass, freezer)
    assert hass.states.get(_climate_id(hass, "dev1")).state == "heat_cool"


async def test_a_blank_first_poll_never_wipes_the_registry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A blank ``/v1/devices`` on the very first poll destroys nothing either."""
    _mock_account(aioclient_mock, devices={"side_effect": sequence({"json": []}, {"json": _account()})})
    mock_config_entry.add_to_hass(hass)
    _seed_devices(device_registry, mock_config_entry, *DEVICE_IDS)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for device_id in DEVICE_IDS:
        assert device_registry.async_get_device(identifiers={(DOMAIN, device_id)}) is not None

    await _poll(hass, freezer)
    for device_id in DEVICE_IDS:
        assert device_registry.async_get_device(identifiers={(DOMAIN, device_id)}) is not None
        assert _climate_id(hass, device_id) is not None


async def test_a_partial_first_poll_defers_removal_to_the_next_one(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """One short device list right after a restart is not evidence of a deletion."""
    _mock_account(aioclient_mock, devices=_account(without="dev3"), ids=("dev1", "dev2"))
    mock_config_entry.add_to_hass(hass)
    _seed_devices(device_registry, mock_config_entry, *DEVICE_IDS)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is not None

    # The second poll corroborates it, and only then is the device reconciled away.
    await _poll(hass, freezer)
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is None
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev1")}) is not None


async def test_new_thermostats_get_entities_without_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The shared platform helper creates entities for thermostats seen later."""
    coordinator = init_integration.runtime_data
    added: list[Entity] = []

    def _factory(thermostat_id: str) -> list[Entity]:
        return [DaikinOneEntity(coordinator, thermostat_id)]

    unsub = async_setup_platform_entities(coordinator, added.extend, _factory)
    assert len(added) == 3

    summary = DeviceSummary(id="dev4", name="Loft", model="ONEPLUS", firmware_version="3.1.7", location_name="Home")
    coordinator.async_set_updated_data({**coordinator.data, "dev4": Thermostat(summary)})
    await hass.async_block_till_done()

    assert len(added) == 4
    assert added[-1].device_info["identifiers"] == {(DOMAIN, "dev4")}

    unsub()


# --------------------------------------------------------------------------- writes


async def test_single_heat_setpoint_pushes_the_cool_setpoint(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Writing only the heat setpoint moves the cool setpoint to keep Daikin's delta."""
    coordinator = init_integration.runtime_data

    await coordinator.async_set_mode_setpoints("dev1", heat=23.0)

    put = calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/msp")
    assert len(put) == 1
    assert put[0][2] == {"mode": 3, "heatSetpoint": 23.0, "coolSetpoint": 25.0}

    state = coordinator.data["dev1"].state
    assert (state.heat_setpoint, state.cool_setpoint) == (23.0, 25.0)
    # The API documents that an /msp write turns the thermostat schedule off.
    assert state.schedule_enabled is False


async def test_single_cool_setpoint_pushes_the_heat_setpoint(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Writing only the cool setpoint moves the heat setpoint down."""
    coordinator = init_integration.runtime_data

    await coordinator.async_set_mode_setpoints("dev1", cool=21.0, mode=Mode.COOL)

    put = calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/msp")
    assert put[0][2] == {"mode": 2, "heatSetpoint": 19.0, "coolSetpoint": 21.0}


async def test_the_minimum_gap_is_shown_in_home_assistants_unit(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    # GIVEN
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = init_integration.runtime_data

    # WHEN
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", heat=20.0, cool=21.0)

    # THEN
    assert err.value.translation_placeholders == {"delta": "3.6 °F"}


async def test_range_write_below_the_delta_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A range narrower than setpointDelta never reaches the API."""
    coordinator = init_integration.runtime_data

    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", heat=20.0, cool=21.0)

    assert err.value.translation_key == "setpoint_delta"
    assert err.value.translation_placeholders == {"delta": "2 °C"}
    assert not calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/msp")


async def test_setpoint_outside_the_thermostat_range_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Setpoints below setpointMinimum are refused locally."""
    coordinator = init_integration.runtime_data

    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", heat=5.0)

    assert err.value.translation_key == "setpoint_out_of_range"
    assert not calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/msp")


async def test_write_to_an_unknown_thermostat_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A thermostat the coordinator has never seen cannot be written to."""
    with pytest.raises(ServiceValidationError) as err:
        await init_integration.runtime_data.async_set_mode_setpoints("nope", heat=21.0)

    assert err.value.translation_key == "state_unknown"


async def test_write_without_a_known_mode_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The full /msp triple cannot be built when the mode is unknown."""
    _mock_account(
        aioclient_mock,
        details={"dev1": {"json": {"heatSetpoint": 20.0, "coolSetpoint": 24.0, "setpointDelta": 2.0}}},
    )
    coordinator = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", heat=21.0)

    assert err.value.translation_key == "state_unknown"


async def test_write_errors_are_translated(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An API failure on a write becomes a translated HomeAssistantError."""
    aioclient_mock.put(f"{DEVICES_URL}/dev1/schedule", status=429)
    _mock_account(aioclient_mock)
    coordinator = await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_schedule_enabled("dev1", False)

    assert err.value.translation_key == "rate_limited"
    # Nothing optimistic was applied, so no verification read is pending either.
    assert coordinator.data["dev1"].state.schedule_enabled is True


async def test_fan_write_fills_in_the_missing_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Both documented fan fields are always sent, defaulting to the current state."""
    coordinator = init_integration.runtime_data

    await coordinator.async_set_fan("dev1", circulate=FanCirculate.ALWAYS_ON)

    put = calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/fan")
    assert put[0][2] == {"fanCirculate": 1, "fanCirculateSpeed": 1}

    state = coordinator.data["dev1"].state
    assert state.fan_circulate is FanCirculate.ALWAYS_ON
    assert state.fan_circulate_speed is FanCirculateSpeed.MEDIUM


async def test_unsupported_fan_write_raises_a_repair_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rejected /fan write flags VRV/split equipment and clears once one succeeds."""
    aioclient_mock.put(
        f"{DEVICES_URL}/dev1/fan",
        side_effect=sequence(
            {"status": 400, "json": {"messages": "Invalid request body"}},
            {"json": load_json_object_fixture("write_ok.json")},
        ),
    )
    _mock_account(aioclient_mock)
    coordinator = await _setup(hass, mock_config_entry)
    issue_registry = ir.async_get(hass)

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_fan("dev1", speed=FanCirculateSpeed.HIGH)

    assert err.value.translation_key == "unsupported_capability"
    issue = issue_registry.async_get_issue(DOMAIN, "fan_unsupported_dev1")
    assert issue is not None
    assert issue.translation_placeholders == {"name": "Main Floor"}
    # The state is untouched, so the select entity snaps back to what the API reports.
    assert coordinator.data["dev1"].state.fan_circulate_speed is FanCirculateSpeed.MEDIUM

    await coordinator.async_set_fan("dev1", speed=FanCirculateSpeed.HIGH)
    assert issue_registry.async_get_issue(DOMAIN, "fan_unsupported_dev1") is None
    assert coordinator.data["dev1"].state.fan_circulate_speed is FanCirculateSpeed.HIGH


async def test_entity_description_drives_the_unique_id(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Described entities get the documented `{thermostat}-{key}` unique id."""
    coordinator = init_integration.runtime_data
    entity = DaikinOneEntity(coordinator, "dev1", EntityDescription(key="indoor_temperature"))

    assert entity.unique_id == "dev1-indoor_temperature"
    assert entity.thermostat is coordinator.data["dev1"]
    assert entity.device_info["model"] == "ONEPLUS"
    assert entity.device_info["sw_version"] == "3.1.7"


# ------------------------------------------------------------------ edge branches


async def test_per_device_auth_failure_is_account_level(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 on one detail request means the token is bad for the whole account."""
    _mock_account(aioclient_mock, details={"dev2": {"status": 401, "json": {"messages": "NotAuthorizedException"}}})
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_per_device_rate_limit_is_account_level(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 429 on one detail request stops the whole poll instead of marking one device offline."""
    _mock_account(aioclient_mock, details={"dev2": {"status": 429}})
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_known_devices_are_kept_while_ghosts_are_removed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Stale-device cleanup leaves the devices the account still reports alone."""
    mock_config_entry.add_to_hass(hass)
    for identifier in ("dev1", "ghost"):
        device_registry.async_get_or_create(
            config_entry_id=mock_config_entry.entry_id, identifiers={(DOMAIN, identifier)}
        )

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # The first poll after a restart is never trusted to delete a top-level device.
    assert device_registry.async_get_device(identifiers={(DOMAIN, "ghost")}) is not None

    await _poll(hass, freezer)

    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev1")}) is not None
    assert device_registry.async_get_device(identifiers={(DOMAIN, "ghost")}) is None


async def test_write_without_known_setpoints_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a heat or cool setpoint the required /msp triple cannot be built."""
    _mock_account(aioclient_mock, details={"dev1": {"json": {"mode": 1}}})
    coordinator = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", heat=21.0)

    assert err.value.translation_key == "state_unknown"


async def test_range_write_respecting_the_delta_is_sent(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A valid heat/cool pair is passed through untouched."""
    coordinator = init_integration.runtime_data

    await coordinator.async_set_mode_setpoints("dev1", heat=19.5, cool=25.5, mode=Mode.AUTO)

    assert calls(mock_api, "PUT", f"{DEVICES_PATH}/dev1/msp")[0][2] == {
        "mode": 3,
        "heatSetpoint": 19.5,
        "coolSetpoint": 25.5,
    }


async def test_other_fan_errors_do_not_raise_a_repair_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Only an outright rejection means "unsupported"; a 429 is just a transient failure."""
    aioclient_mock.put(f"{DEVICES_URL}/dev1/fan", status=429)
    _mock_account(aioclient_mock)
    coordinator = await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_fan("dev1", circulate=FanCirculate.SCHEDULE)

    assert err.value.translation_key == "rate_limited"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "fan_unsupported_dev1") is None


async def test_entity_of_a_vanished_thermostat_is_unavailable(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An entity whose thermostat left the account reports unavailable, not an error."""
    coordinator = init_integration.runtime_data
    entity = DaikinOneEntity(coordinator, "dev3")
    entity.hass = hass

    coordinator.async_set_updated_data({k: v for k, v in coordinator.data.items() if k != "dev3"})
    assert entity.available is False
    # HA reads capability attributes even from an unavailable entity, so the last known
    # snapshot has to survive rather than raise.
    assert entity.thermostat.summary.id == "dev3"


async def test_platform_helper_ignores_updates_without_new_devices(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A poll that returns the same thermostats adds no entities."""
    coordinator = init_integration.runtime_data
    added: list[Entity] = []
    unsub = async_setup_platform_entities(coordinator, added.extend, lambda tid: [DaikinOneEntity(coordinator, tid)])
    assert len(added) == 3

    coordinator.async_set_updated_data(dict(coordinator.data))
    await hass.async_block_till_done()

    assert len(added) == 3
    unsub()


async def test_unknown_fan_enums_fall_back_to_the_documented_defaults(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An undocumented enum value from Daikin is not echoed back on the next write."""
    detail = load_json_object_fixture("device_oneplus.json")
    unknown = {"json": {**detail, "fanCirculate": 9, "fanCirculateSpeed": 9}}
    _mock_account(aioclient_mock, details={"dev1": unknown, "dev2": unknown})
    coordinator = await _setup(hass, mock_config_entry)
    assert coordinator.data["dev1"].state.fan_circulate is FanCirculate.UNKNOWN

    await coordinator.async_set_fan("dev1", speed=FanCirculateSpeed.HIGH)
    await coordinator.async_set_fan("dev2", circulate=FanCirculate.ALWAYS_ON)

    assert calls(aioclient_mock, "PUT", f"{DEVICES_PATH}/dev1/fan")[0][2] == {
        "fanCirculate": 0,
        "fanCirculateSpeed": 2,
    }
    assert calls(aioclient_mock, "PUT", f"{DEVICES_PATH}/dev2/fan")[0][2] == {
        "fanCirculate": 1,
        "fanCirculateSpeed": 0,
    }


async def test_concurrent_msp_writes_are_serialised_by_the_coordinator_lock(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Pin the coordinator's own per-thermostat lock.

    Service calls on one platform are already serialised by HA's ``PARALLEL_UPDATES``
    semaphore, but that semaphore is per platform: climate, switch and select all write the
    same thermostat through this coordinator. Only the coordinator lock stops those racing,
    so this drives the write methods directly, bypassing the platform semaphore entirely.
    """
    counter = {"in_flight": 0, "peak": 0}
    aioclient_mock.put(
        f"{DEVICES_URL}/dev1/msp",
        side_effect=gated(load_json_object_fixture("write_ok.json"), counter),
    )
    _mock_account(aioclient_mock)
    coordinator = await _setup(hass, mock_config_entry)

    await asyncio.gather(
        coordinator.async_set_mode_setpoints("dev1", heat=23.0),
        coordinator.async_set_mode_setpoints("dev1", mode=Mode.COOL),
    )

    assert counter["peak"] == 1
    # The second payload is built from the first write's optimistic state, not the snapshot
    # both calls started from - otherwise the 23.0 write is silently lost.
    assert [c[2] for c in calls(aioclient_mock, "PUT", f"{DEVICES_PATH}/dev1/msp")] == [
        {"mode": 3, "heatSetpoint": 23.0, "coolSetpoint": 25.0},
        {"mode": 2, "heatSetpoint": 23.0, "coolSetpoint": 25.0},
    ]


async def test_mode_only_write_still_validates_the_delta(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A write that only changes the mode echoes the snapshot's setpoints, so they are checked too."""
    detail = load_json_object_fixture("device_oneplus.json")
    _mock_account(aioclient_mock, details={"dev1": {"json": {**detail, "heatSetpoint": 23.0, "coolSetpoint": 24.0}}})
    coordinator = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints("dev1", mode=Mode.COOL)

    assert err.value.translation_key == "setpoint_delta"
    assert not calls(aioclient_mock, "PUT", f"{DEVICES_PATH}/dev1/msp")
