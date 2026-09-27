"""Dynamic devices, stale devices and how failures translate into entity availability."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import DOMAIN

from .conftest import DEVICE_IDS, DEVICES_URL, PUT_URL_RE, TOKEN_URL, sequence

DEVICES_PATH = "/v1/devices"
OFFLINE_500 = {"status": 500, "json": {"messages": "DeviceOfflineException"}}
BINARY_SENSOR = Platform.BINARY_SENSOR.value


@pytest.fixture(autouse=True)
def _deterministic_scheduling() -> Iterator[None]:
    """Pin both sources of scheduling randomness so tick() maths is exact."""
    with (
        patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0),
        patch("homeassistant.helpers.update_coordinator.randint", return_value=500000),
    ):
        yield


def _account(*, extra: dict[str, Any] | None = None, without: str | None = None) -> list[Any]:
    """The two-location device list, optionally with a device added or removed."""
    locations: list[Any] = load_json_array_fixture("devices.json")
    if extra is not None:
        locations[0]["devices"] = [*locations[0]["devices"], extra]
    if without is not None:
        for location in locations:
            location["devices"] = [d for d in location["devices"] if d["id"] != without]
    return locations


def _mock_account(
    mock: AiohttpClientMocker,
    *,
    devices: dict[str, Any] | None = None,
    details: dict[str, dict[str, Any]] | None = None,
    ids: tuple[str, ...] = DEVICE_IDS,
) -> None:
    """Register a full account; `devices` / `details` are raw mock kwargs when given."""
    mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    mock.get(DEVICES_URL, **(devices or {"json": load_json_array_fixture("devices.json")}))
    detail = load_json_object_fixture("device_oneplus.json")
    for device_id in ids:
        mock.get(f"{DEVICES_URL}/{device_id}", **((details or {}).get(device_id) or {"json": detail}))
    mock.put(PUT_URL_RE, json={"message": "Write sent"})


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up the entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int = 181) -> None:
    """Advance past the poll interval and let the refresh run."""
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    # Timer-driven refreshes run as background tasks, which the default wait skips.
    await hass.async_block_till_done(wait_background_tasks=True)


def _climate_id(hass: HomeAssistant, thermostat_id: str) -> str | None:
    """The entity id of a thermostat's climate entity, if it is registered."""
    return er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, f"{thermostat_id}-climate")


async def test_a_new_thermostat_appears_without_a_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A thermostat added to the account shows up on the next poll (quality scale: dynamic-devices)."""
    loft = {"id": "dev4", "name": "Loft", "model": "ONEPLUS", "firmwareVersion": "3.1.7"}
    _mock_account(
        aioclient_mock,
        devices={"side_effect": sequence({"json": _account()}, {"json": _account(extra=loft)})},
        ids=(*DEVICE_IDS, "dev4"),
    )
    await _setup(hass, mock_config_entry)
    assert _climate_id(hass, "dev4") is None

    await _poll(hass, freezer)

    entity_id = _climate_id(hass, "dev4")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "heat_cool"
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev4")}) is not None
    # The other platforms follow the same helper, so they gain the device too.
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, "dev4-schedule") is not None


async def test_a_removed_thermostat_takes_its_device_and_entities_with_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A thermostat that leaves the account is cleaned up (quality scale: stale-devices)."""
    _mock_account(
        aioclient_mock,
        devices={"side_effect": sequence({"json": _account()}, {"json": _account(without="dev3")})},
    )
    await _setup(hass, mock_config_entry)
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is not None
    assert _climate_id(hass, "dev3") is not None

    await _poll(hass, freezer)

    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev3")}) is None
    assert _climate_id(hass, "dev3") is None
    # The surviving thermostats are untouched.
    assert device_registry.async_get_device(identifiers={(DOMAIN, "dev1")}) is not None
    assert hass.states.get(_climate_id(hass, "dev1")).state == "heat_cool"


async def test_one_unreachable_thermostat_only_affects_its_own_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An offline thermostat goes unavailable, but keeps reporting that it is offline."""
    _mock_account(aioclient_mock, details={"dev2": OFFLINE_500})
    await _setup(hass, mock_config_entry)

    assert hass.states.get(_climate_id(hass, "dev2")).state == STATE_UNAVAILABLE
    online = er.async_get(hass).async_get_entity_id(BINARY_SENSOR, DOMAIN, "dev2-online")
    assert hass.states.get(online).state == STATE_OFF

    assert hass.states.get(_climate_id(hass, "dev1")).state == "heat_cool"
    dev1_online = er.async_get(hass).async_get_entity_id(BINARY_SENSOR, DOMAIN, "dev1-online")
    assert hass.states.get(dev1_online).state == STATE_ON


async def test_an_account_level_failure_makes_everything_unavailable_and_recovers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A device list failing twice running takes every entity down; the next good poll restores them."""
    _mock_account(
        aioclient_mock,
        devices={"side_effect": sequence({"json": _account()}, {"status": 500}, {"status": 500}, {"json": _account()})},
    )
    await _setup(hass, mock_config_entry)
    climates = [_climate_id(hass, device_id) for device_id in DEVICE_IDS]
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in climates)

    await _poll(hass, freezer)
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in climates)

    await _poll(hass, freezer)
    assert all(hass.states.get(entity_id).state == STATE_UNAVAILABLE for entity_id in climates)
    # Even the `online` sensor is unavailable: the account itself could not be read.
    online = er.async_get(hass).async_get_entity_id(BINARY_SENSOR, DOMAIN, "dev1-online")
    assert hass.states.get(online).state == STATE_UNAVAILABLE

    # One failure backs the next attempt off to 2 x 180 s.
    await _poll(hass, freezer, 361)
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in climates)
    assert hass.states.get(online).state == STATE_ON


async def test_undocumented_enum_values_leave_entities_unknown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Unknown API integers never turn into a wrong reading."""
    payload = load_json_object_fixture("device_oneplus.json") | {"fan": 9, "equipmentStatus": 9}
    _mock_account(aioclient_mock, details={device_id: {"json": payload} for device_id in DEVICE_IDS})
    mock_config_entry.add_to_hass(hass)
    with patch("homeassistant.helpers.entity.Entity.entity_registry_enabled_default", return_value=True):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, "dev1-system_fan")).state == "unknown"
    assert hass.states.get(registry.async_get_entity_id(BINARY_SENSOR, DOMAIN, "dev1-heating")).state == "unknown"


async def test_one_transient_thermostat_read_failure_keeps_it_available(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # GIVEN
    detail = load_json_object_fixture("device_oneplus.json")
    _mock_account(
        aioclient_mock,
        details={
            "dev1": {"side_effect": sequence({"json": detail}, {"status": 503}, {"status": 503}, {"json": detail})}
        },
    )
    await _setup(hass, mock_config_entry)
    climate = _climate_id(hass, "dev1")
    online = er.async_get(hass).async_get_entity_id(BINARY_SENSOR, DOMAIN, "dev1-online")

    # WHEN
    await _poll(hass, freezer)
    after_one = (hass.states.get(climate).state, hass.states.get(online).state)
    await _poll(hass, freezer)
    after_two = (hass.states.get(climate).state, hass.states.get(online).state)
    await _poll(hass, freezer)

    # THEN
    assert after_one == ("heat_cool", STATE_ON)
    assert after_two[0] == STATE_UNAVAILABLE
    assert hass.states.get(climate).state == "heat_cool"
    assert "keeping its last reading until the next poll" in caplog.text


async def test_daikins_offline_answer_is_never_masked(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    # GIVEN
    detail = load_json_object_fixture("device_oneplus.json")
    offline = {"status": 400, "json": {"messages": "DeviceOfflineException"}}
    _mock_account(aioclient_mock, details={"dev1": {"side_effect": sequence({"json": detail}, offline)}})
    await _setup(hass, mock_config_entry)

    # WHEN
    await _poll(hass, freezer)

    # THEN
    assert hass.states.get(_climate_id(hass, "dev1")).state == STATE_UNAVAILABLE


async def test_a_tolerated_account_failure_is_logged_with_its_cause(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # GIVEN
    _mock_account(aioclient_mock)
    await _setup(hass, mock_config_entry)
    climates = [_climate_id(hass, device_id) for device_id in DEVICE_IDS]
    aioclient_mock.clear_requests()
    _mock_account(aioclient_mock, devices={"exc": aiohttp.ServerDisconnectedError()})

    # WHEN
    await _poll(hass, freezer)
    after_one = [hass.states.get(entity_id).state for entity_id in climates]
    await _poll(hass, freezer)

    # THEN
    assert STATE_UNAVAILABLE not in after_one
    assert "Daikin One poll failed (transport_error: ServerDisconnectedError)" in caplog.text
    assert all(hass.states.get(entity_id).state == STATE_UNAVAILABLE for entity_id in climates)
    assert "Daikin One update failed (transport_error: ServerDisconnectedError)" in caplog.text
