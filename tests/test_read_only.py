"""Read-only mode: every write is validated and recorded, and nothing reaches Daikin."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.select import ATTR_OPTION, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, SERVICE_TURN_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import CONF_READ_ONLY, DOMAIN, EVENT_WRITE_SUPPRESSED

from .test_climate import _detail, _mock_account

LAST_WRITE = "sensor.home_main_floor_last_intended_write"


def _puts(mock: AiohttpClientMocker) -> list[Any]:
    return [c for c in mock.mock_calls if c[0].lower() == "put"]


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _setup_read_only(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_READ_ONLY: True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _capture_events(hass: HomeAssistant) -> list[Event]:
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_WRITE_SUPPRESSED, _record)
    return events


async def test_new_entries_start_read_only(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    # GIVEN
    _mock_account(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={"email": "a@example.com", "api_key": "k", "integrator_token": "t"},
    )
    entry.add_to_hass(hass)

    # WHEN
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # THEN
    assert entry.runtime_data.read_only is True
    assert hass.states.get(LAST_WRITE).attributes["read_only"] is True


async def test_setpoint_write_is_recorded_and_never_sent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_admin_user: MockUser,
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    await _setup_read_only(hass, mock_config_entry)
    climate = _entity_id(hass, CLIMATE_DOMAIN, "dev1-climate")
    events = _capture_events(hass)
    context = Context(user_id=hass_admin_user.id)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: climate, ATTR_TEMPERATURE: 21.0},
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()

    # THEN
    assert _puts(aioclient_mock) == []
    state = hass.states.get(climate)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == 20.0
    assert hass.states.get(_entity_id(hass, SWITCH_DOMAIN, "dev1-schedule")).state == STATE_ON
    last = hass.states.get(LAST_WRITE)
    assert last.state != STATE_UNKNOWN
    assert last.attributes["msp"] | {"at": None} == {
        "mode": 1,
        "heatSetpoint": 21.0,
        "coolSetpoint": 24.0,
        "at": None,
        "user_id": hass_admin_user.id,
    }
    assert len(events) == 1
    assert events[0].data == {
        "thermostat_id": "dev1",
        "endpoint": "msp",
        "mode": 1,
        "heatSetpoint": 21.0,
        "coolSetpoint": 24.0,
    }
    assert events[0].context is context


async def test_fan_only_records_both_halves(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    await _setup_read_only(hass, mock_config_entry)
    climate = _entity_id(hass, CLIMATE_DOMAIN, "dev1-climate")

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: climate, ATTR_HVAC_MODE: HVACMode.FAN_ONLY},
        blocking=True,
    )

    # THEN
    assert _puts(aioclient_mock) == []
    assert hass.states.get(climate).state == HVACMode.HEAT
    attributes = hass.states.get(LAST_WRITE).attributes
    assert attributes["fan"]["fanCirculate"] == 1
    assert attributes["msp"]["mode"] == 0


async def test_fan_select_write_is_recorded_and_never_sent(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock)
    await _setup_read_only(hass, mock_config_entry)
    select = _entity_id(hass, SELECT_DOMAIN, "dev1-fan_circulate")

    # WHEN
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: select, ATTR_OPTION: "always_on"}, blocking=True
    )

    # THEN
    assert _puts(aioclient_mock) == []
    assert hass.states.get(select).state == "off"
    assert hass.states.get(LAST_WRITE).attributes["fan"]["fanCirculate"] == 1


async def test_schedule_write_is_recorded_and_never_sent(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock)
    await _setup_read_only(hass, mock_config_entry)
    switch = _entity_id(hass, SWITCH_DOMAIN, "dev1-schedule")

    # WHEN
    await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: switch}, blocking=True)

    # THEN
    assert _puts(aioclient_mock) == []
    assert hass.states.get(switch).state == STATE_ON
    assert hass.states.get(LAST_WRITE).attributes["schedule"]["scheduleEnabled"] is False


async def test_invalid_writes_are_still_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock)
    await _setup_read_only(hass, mock_config_entry)
    climate = _entity_id(hass, CLIMATE_DOMAIN, "dev1-climate")

    # WHEN
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: climate, ATTR_TARGET_TEMP_LOW: 23.0, ATTR_TARGET_TEMP_HIGH: 24.0},
            blocking=True,
        )

    # THEN
    assert err.value.translation_key == "setpoint_delta"
    assert hass.states.get(LAST_WRITE).state == STATE_UNKNOWN


async def test_writable_entries_record_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    climate = _entity_id(hass, CLIMATE_DOMAIN, "dev1-climate")

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: climate, ATTR_TEMPERATURE: 21.0}, blocking=True
    )

    # THEN
    assert len(_puts(aioclient_mock)) == 1
    last = hass.states.get(LAST_WRITE)
    assert last.state == STATE_UNKNOWN
    assert last.attributes["read_only"] is False
    assert "msp" not in last.attributes
