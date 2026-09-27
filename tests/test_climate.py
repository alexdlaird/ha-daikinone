"""Climate entity: mode/action mapping, targets and the write path through the coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.climate.const import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import DOMAIN

from .conftest import DEVICE_IDS, DEVICES_URL, PUT_URL_RE, TOKEN_URL, calls, gated, sequence

DEVICES_PATH = "/v1/devices"
MSP_PATH = f"{DEVICES_PATH}/dev1/msp"
WRITE_OK = {"message": "Write sent"}


@pytest.fixture(autouse=True)
def _deterministic_scheduling() -> Iterator[None]:
    """Pin both sources of scheduling randomness so tick() maths is exact."""
    with (
        patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0),
        patch("homeassistant.helpers.update_coordinator.randint", return_value=500000),
    ):
        yield


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int) -> None:
    """Advance the clock and run any timer that comes due (they are background tasks)."""
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _detail(**overrides: Any) -> dict[str, Any]:
    """The ONEPLUS detail fixture with individual fields overridden."""
    payload: dict[str, Any] = load_json_object_fixture("device_oneplus.json")
    payload.update(overrides)
    return payload


def _mock_account(
    mock: AiohttpClientMocker,
    *,
    detail: dict[str, Any] | None = None,
    put: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Register a full account. `put` overrides (keyed by '<id>/<endpoint>') come first."""
    for path, kwargs in (put or {}).items():
        mock.put(f"{DEVICES_URL}/{path}", **kwargs)
    mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    for device_id in DEVICE_IDS:
        mock.get(f"{DEVICES_URL}/{device_id}", **(detail or {"json": _detail()}))
    mock.put(PUT_URL_RE, json=WRITE_OK)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Set the entry up and return dev1's climate entity id."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, "dev1-climate")
    assert entity_id is not None
    return entity_id


# ------------------------------------------------------------------ read mapping


@pytest.mark.parametrize(
    ("api_mode", "hvac_mode", "preset"),
    [
        (0, HVACMode.OFF, "none"),
        (1, HVACMode.HEAT, "none"),
        (2, HVACMode.COOL, "none"),
        (3, HVACMode.HEAT_COOL, "none"),
        (4, HVACMode.HEAT, "emergency_heat"),
    ],
)
async def test_mode_maps_to_hvac_mode_and_preset(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    *,
    api_mode: int,
    hvac_mode: HVACMode,
    preset: str,
) -> None:
    """Every documented mode maps to an HVAC mode; emergency heat is HEAT + a preset."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=api_mode, modeEmHeatAvailable=1)})
    entity_id = await _setup(hass, mock_config_entry)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == hvac_mode
    assert state.attributes[ATTR_PRESET_MODE] == preset


async def test_unknown_mode_makes_the_entity_state_unknown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An undocumented mode integer never invents an HVAC mode."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=9)})
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).state == "unknown"


@pytest.mark.parametrize(
    ("mode_limit", "expected"),
    [
        (0, [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.FAN_ONLY]),
        (1, [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.FAN_ONLY]),
        (2, [HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]),
        (3, [HVACMode.OFF, HVACMode.COOL, HVACMode.FAN_ONLY]),
        (9, [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.FAN_ONLY]),
    ],
)
async def test_mode_limit_filters_the_offered_modes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mode_limit: int,
    expected: list[HVACMode],
) -> None:
    """`modeLimit` decides which HVAC modes the thermostat offers."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1, modeLimit=mode_limit)})
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).attributes[ATTR_HVAC_MODES] == expected


@pytest.mark.parametrize("em_heat", [0, False])
async def test_without_emergency_heat_there_is_no_preset(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    em_heat: int | bool,
) -> None:
    """Equipment without emergency heat advertises neither the feature nor the preset."""
    _mock_account(aioclient_mock, detail={"json": _detail(modeEmHeatAvailable=em_heat)})
    entity_id = await _setup(hass, mock_config_entry)

    state = hass.states.get(entity_id)
    assert ATTR_PRESET_MODE not in state.attributes
    assert ATTR_PRESET_MODES not in state.attributes
    assert not state.attributes[ATTR_SUPPORTED_FEATURES] & ClimateEntityFeature.PRESET_MODE


@pytest.mark.parametrize("em_heat", [1, True])
async def test_with_emergency_heat_the_preset_is_offered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    em_heat: int | bool,
) -> None:
    """Dual-fuel equipment gets the emergency-heat preset."""
    _mock_account(aioclient_mock, detail={"json": _detail(modeEmHeatAvailable=em_heat)})
    entity_id = await _setup(hass, mock_config_entry)

    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PRESET_MODES] == ["none", "emergency_heat"]
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & ClimateEntityFeature.PRESET_MODE


@pytest.mark.parametrize(
    ("mode", "status", "action"),
    [
        (3, 1, HVACAction.COOLING),
        (3, 2, HVACAction.DRYING),
        (3, 3, HVACAction.HEATING),
        (3, 4, HVACAction.FAN),
        (3, 5, HVACAction.IDLE),
        # The API reports "idle" for a system that is off; OFF is the honest action.
        (0, 5, HVACAction.OFF),
        (3, 9, None),
    ],
)
async def test_equipment_status_maps_to_hvac_action(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    *,
    mode: int,
    status: int,
    action: HVACAction | None,
) -> None:
    """Every documented equipment status maps to an action; unknown ones map to nothing."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=mode, equipmentStatus=status)})
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).attributes.get(ATTR_HVAC_ACTION) == action


async def test_off_without_a_status_still_reports_off(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A thermostat that is off but reports no status is not "unknown", it is off."""
    payload = _detail(mode=0)
    del payload["equipmentStatus"]
    _mock_account(aioclient_mock, detail={"json": payload})
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).attributes[ATTR_HVAC_ACTION] == HVACAction.OFF


@pytest.mark.parametrize(
    ("mode", "target", "low", "high"),
    [
        (0, None, None, None),
        (1, 20.0, None, None),
        (2, 24.0, None, None),
        (3, None, 20.0, 24.0),
        (4, 20.0, None, None),
    ],
)
async def test_targets_follow_the_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    *,
    mode: int,
    target: float | None,
    low: float | None,
    high: float | None,
) -> None:
    """Heat/cool modes expose one setpoint, auto exposes the range, off exposes neither."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=mode)})
    entity_id = await _setup(hass, mock_config_entry)

    attributes = hass.states.get(entity_id).attributes
    assert attributes[ATTR_TEMPERATURE] == target
    assert attributes[ATTR_TARGET_TEMP_LOW] == low
    assert attributes[ATTR_TARGET_TEMP_HIGH] == high
    assert attributes[ATTR_CURRENT_TEMPERATURE] == 21.1
    assert attributes[ATTR_CURRENT_HUMIDITY] == 45
    assert attributes[ATTR_MIN_TEMP] == 10.0
    assert attributes[ATTR_MAX_TEMP] == 32.0


async def test_missing_limits_fall_back_to_home_assistant_defaults(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A thermostat that reports no setpoint limits gets HA's defaults, not None."""
    payload = _detail()
    del payload["setpointMinimum"]
    del payload["setpointMaximum"]
    _mock_account(aioclient_mock, detail={"json": payload})
    entity_id = await _setup(hass, mock_config_entry)

    attributes = hass.states.get(entity_id).attributes
    assert attributes[ATTR_MIN_TEMP] == 7
    assert attributes[ATTR_MAX_TEMP] == 35


# ------------------------------------------------------------------------ writes


async def test_set_temperature_in_heat_writes_the_full_triple(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`PUT /msp` always carries mode + both setpoints; the cool side comes from the snapshot."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 21.0},
        blocking=True,
    )

    put = calls(aioclient_mock, "PUT", MSP_PATH)
    assert len(put) == 1
    assert put[0][2] == {"mode": 1, "heatSetpoint": 21.0, "coolSetpoint": 24.0}


async def test_single_setpoint_pushes_the_other_side_to_keep_the_delta(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Raising heat to 23.5 with a 2 °C delta moves cool from 24.0 to 25.5."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 23.5},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {
        "mode": 1,
        "heatSetpoint": 23.5,
        "coolSetpoint": 25.5,
    }


async def test_range_write_in_auto(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A range write in auto sends both setpoints and keeps the mode."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TARGET_TEMP_LOW: 19.0, ATTR_TARGET_TEMP_HIGH: 25.0},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {
        "mode": 3,
        "heatSetpoint": 19.0,
        "coolSetpoint": 25.0,
    }


async def test_set_temperature_can_switch_mode_in_the_same_write(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`hvac_mode` in the service call decides which setpoint a bare temperature is."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.COOL, ATTR_TEMPERATURE: 23.0},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {
        "mode": 2,
        "heatSetpoint": 20.0,
        "coolSetpoint": 23.0,
    }


async def test_single_temperature_in_auto_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Auto mode needs a range: a bare temperature never reaches the API."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 23.0},
            blocking=True,
        )

    assert err.value.translation_key == "single_setpoint_not_applicable"
    assert not calls(aioclient_mock, "PUT", MSP_PATH)


async def test_temperature_outside_the_thermostat_range_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """min_temp/max_temp are wired to the thermostat, so HA rejects 5 °C itself."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 5.0},
            blocking=True,
        )

    assert err.value.translation_key == "temp_out_of_range"
    assert not calls(aioclient_mock, "PUT", MSP_PATH)


async def test_range_narrower_than_the_delta_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Daikin requires heat <= cool - setpointDelta; the coordinator refuses locally."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_TARGET_TEMP_LOW: 23.0, ATTR_TARGET_TEMP_HIGH: 24.0},
            blocking=True,
        )

    assert err.value.translation_key == "setpoint_delta"
    assert not calls(aioclient_mock, "PUT", MSP_PATH)


async def test_set_hvac_mode_keeps_the_setpoints(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Switching mode still sends the full triple, unchanged setpoints included."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.OFF},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {
        "mode": 0,
        "heatSetpoint": 20.0,
        "coolSetpoint": 24.0,
    }


@pytest.mark.parametrize(("preset", "api_mode"), [("emergency_heat", 4), ("none", 1)])
async def test_preset_is_the_only_way_into_emergency_heat(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    preset: str,
    api_mode: int,
) -> None:
    """Selecting the preset writes mode 4; clearing it writes plain heat."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: preset},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2]["mode"] == api_mode


async def test_plain_heat_never_re_enters_emergency_heat(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Selecting HEAT while in emergency heat leaves emergency heat."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=4)})
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2]["mode"] == 1


# ------------------------------------------------------------------ unit handling


async def test_fahrenheit_display_and_celsius_payload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The UI works in °F while the API keeps receiving Celsius."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    attributes = hass.states.get(entity_id).attributes
    assert attributes[ATTR_CURRENT_TEMPERATURE] == 70.0
    assert attributes[ATTR_MIN_TEMP] == 50.0
    assert attributes[ATTR_MAX_TEMP] == 89.6

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 70},
        blocking=True,
    )

    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2]["heatSetpoint"] == 21.1


# ------------------------------------------------- serialisation and verification


async def test_concurrent_writes_are_serialised_on_the_latest_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The second write is built from the first write's optimistic result, not the poll."""
    counter = {"in_flight": 0, "peak": 0}
    _mock_account(
        aioclient_mock,
        detail={"json": _detail(mode=1)},
        put={"dev1/msp": {"side_effect": gated(WRITE_OK, counter)}},
    )
    entity_id = await _setup(hass, mock_config_entry)

    await asyncio.gather(
        hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 23.0},
            blocking=True,
        ),
        hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.COOL},
            blocking=True,
        ),
    )

    put = calls(aioclient_mock, "PUT", MSP_PATH)
    assert counter["peak"] == 1
    assert [call[2] for call in put] == [
        {"mode": 1, "heatSetpoint": 23.0, "coolSetpoint": 25.0},
        {"mode": 2, "heatSetpoint": 23.0, "coolSetpoint": 25.0},
    ]


async def test_msp_write_turns_the_schedule_switch_off_without_reading_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The documented schedule side effect shows immediately, and nothing is polled early."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)
    switch_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, "dev1-schedule")
    assert hass.states.get(switch_id).state == "on"
    before = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 21.0},
        blocking=True,
    )

    assert hass.states.get(switch_id).state == "off"
    await _advance(hass, freezer, 14)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before


async def test_optimistic_state_is_replaced_by_the_verification_read(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The optimistic value shows at once; what the thermostat actually did wins at 15 s."""
    _mock_account(
        aioclient_mock,
        detail={"side_effect": sequence({"json": _detail(mode=1)}, {"json": _detail(mode=1, heatSetpoint=22.0)})},
    )
    entity_id = await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 21.0},
        blocking=True,
    )
    assert hass.states.get(entity_id).attributes[ATTR_TEMPERATURE] == 21.0

    await _advance(hass, freezer, 16)

    assert hass.states.get(entity_id).attributes[ATTR_TEMPERATURE] == 22.0


async def test_offline_thermostat_rejects_the_write(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A DeviceOfflineException on the write surfaces as a translated error, state intact."""
    _mock_account(
        aioclient_mock,
        detail={"json": _detail(mode=1)},
        put={"dev1/msp": {"status": 500, "json": {"messages": "DeviceOfflineException"}}},
    )
    entity_id = await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 21.0},
            blocking=True,
        )

    assert err.value.translation_key == "device_offline"
    assert hass.states.get(entity_id).attributes[ATTR_TEMPERATURE] == 20.0


def _entity(hass: HomeAssistant, entity_id: str) -> Any:
    """The live climate entity object, for the paths no service call can reach."""
    return hass.data[DATA_INSTANCES][CLIMATE_DOMAIN].get_entity(entity_id)


async def test_a_thermostat_without_a_mode_limit_offers_every_mode(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A payload without `modeLimit` is treated as unrestricted."""
    payload = _detail(mode=1)
    del payload["modeLimit"]
    _mock_account(aioclient_mock, detail={"json": payload})
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).attributes[ATTR_HVAC_MODES] == [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
        HVACMode.FAN_ONLY,
    ]


async def test_preset_properties_are_none_without_emergency_heat(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The preset properties themselves report nothing, not just the state attributes."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=4, modeEmHeatAvailable=0)})
    entity_id = await _setup(hass, mock_config_entry)

    entity = _entity(hass, entity_id)
    assert entity.preset_modes is None
    assert entity.preset_mode is None


async def test_set_temperature_without_a_temperature_writes_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The service schema cannot produce this, but the entity method must stay a no-op."""
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    await _entity(hass, entity_id).async_set_temperature(hvac_mode=HVACMode.HEAT)

    assert not calls(aioclient_mock, "PUT", MSP_PATH)


# ------------------------------------------------------ ranges in single-setpoint modes


@pytest.mark.parametrize(
    ("api_mode", "expected"),
    [
        (1, {"mode": 1, "heatSetpoint": 21.0, "coolSetpoint": 23.0}),
        (4, {"mode": 4, "heatSetpoint": 21.0, "coolSetpoint": 23.0}),
        (2, {"mode": 2, "heatSetpoint": 19.0, "coolSetpoint": 21.0}),
    ],
)
async def test_collapsed_range_in_a_single_setpoint_mode_keeps_the_live_side(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    api_mode: int,
    expected: dict[str, Any],
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=api_mode, modeEmHeatAvailable=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 21.0, ATTR_TARGET_TEMP_LOW: 21.0, ATTR_TARGET_TEMP_HIGH: 21.0},
        blocking=True,
    )

    # THEN
    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == expected


async def test_valid_range_in_heat_mode_is_written_as_given(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TARGET_TEMP_LOW: 18.0, ATTR_TARGET_TEMP_HIGH: 26.0},
        blocking=True,
    )

    # THEN
    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {"mode": 1, "heatSetpoint": 18.0, "coolSetpoint": 26.0}


# ------------------------------------------------------------------------ fan only

FAN_PATH = f"{DEVICES_PATH}/dev1/fan"


def _put_paths(mock: AiohttpClientMocker) -> list[str]:
    return [c[1].path for c in mock.mock_calls if c[0].lower() == "put"]


async def test_off_with_circulation_always_on_is_fan_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1, equipmentStatus=4)})

    # WHEN
    entity_id = await _setup(hass, mock_config_entry)

    # THEN
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.FAN_ONLY
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.FAN


async def test_fan_only_reports_idle_rather_than_off_between_runs(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1, equipmentStatus=5)})

    # WHEN
    entity_id = await _setup(hass, mock_config_entry)

    # THEN
    assert hass.states.get(entity_id).attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE


@pytest.mark.parametrize("circulate", [-5, None])
async def test_no_fan_only_without_a_readable_circulation_setting(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    circulate: int | None,
) -> None:
    # GIVEN
    payload = _detail(mode=1)
    if circulate is None:
        del payload["fanCirculate"]
    else:
        payload["fanCirculate"] = circulate
    _mock_account(aioclient_mock, detail={"json": payload})

    # WHEN
    entity_id = await _setup(hass, mock_config_entry)

    # THEN
    assert HVACMode.FAN_ONLY not in hass.states.get(entity_id).attributes[ATTR_HVAC_MODES]


async def test_entering_fan_only_sets_circulation_before_turning_the_mode_off(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1, fanCirculate=2)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.FAN_ONLY},
        blocking=True,
    )

    # THEN
    assert _put_paths(aioclient_mock) == [FAN_PATH, MSP_PATH]
    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2] == {"fanCirculate": 1, "fanCirculateSpeed": 1}
    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2] == {"mode": 0, "heatSetpoint": 20.0, "coolSetpoint": 24.0}
    assert hass.states.get(entity_id).state == HVACMode.FAN_ONLY


async def test_entering_fan_only_from_off_only_touches_the_fan(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=0)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.FAN_ONLY},
        blocking=True,
    )

    # THEN
    assert _put_paths(aioclient_mock) == [FAN_PATH]


async def test_entering_fan_only_twice_writes_nothing_the_second_time(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await _entity(hass, entity_id).async_set_hvac_mode(HVACMode.FAN_ONLY)

    # THEN
    assert _put_paths(aioclient_mock) == []


@pytest.mark.parametrize(("before", "restored"), [(2, 2), (0, 0)])
async def test_leaving_fan_only_restores_the_previous_circulation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    before: int,
    restored: int,
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1, fanCirculate=before)})
    entity_id = await _setup(hass, mock_config_entry)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.FAN_ONLY},
        blocking=True,
    )
    aioclient_mock.mock_calls.clear()

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.HEAT}, blocking=True
    )

    # THEN
    assert _put_paths(aioclient_mock) == [MSP_PATH, FAN_PATH]
    assert calls(aioclient_mock, "PUT", MSP_PATH)[0][2]["mode"] == 1
    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2]["fanCirculate"] == restored
    assert hass.states.get(entity_id).state == HVACMode.HEAT


async def test_turning_fan_only_off_only_touches_the_fan(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.OFF}, blocking=True
    )

    # THEN
    assert _put_paths(aioclient_mock) == [FAN_PATH]
    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2]["fanCirculate"] == 0
    assert hass.states.get(entity_id).state == HVACMode.OFF


async def test_circulation_to_restore_survives_a_restart(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    mock_restore_cache_with_extra_data(
        hass, [(State("climate.home_main_floor", HVACMode.FAN_ONLY), {"circulate": "SCHEDULE"})]
    )
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1)})
    entity_id = await _setup(hass, mock_config_entry)
    assert entity_id == "climate.home_main_floor"

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.COOL}, blocking=True
    )

    # THEN
    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2]["fanCirculate"] == 2


@pytest.mark.parametrize("stored", [{"circulate": "ALWAYS_ON"}, {"circulate": "BOGUS"}, {"circulate": 7}, {}])
async def test_unusable_stored_circulation_falls_back_to_off(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    stored: dict[str, Any],
) -> None:
    # GIVEN
    mock_restore_cache_with_extra_data(hass, [(State("climate.home_main_floor", HVACMode.FAN_ONLY), stored)])
    _mock_account(aioclient_mock, detail={"json": _detail(mode=0, fanCirculate=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.OFF}, blocking=True
    )

    # THEN
    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2]["fanCirculate"] == 0


async def test_set_temperature_cannot_target_fan_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    # GIVEN
    _mock_account(aioclient_mock, detail={"json": _detail(mode=1)})
    entity_id = await _setup(hass, mock_config_entry)

    # WHEN
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: HVACMode.FAN_ONLY, ATTR_TEMPERATURE: 21.0},
            blocking=True,
        )

    # THEN
    assert err.value.translation_key == "single_setpoint_not_applicable"
    assert _put_paths(aioclient_mock) == []
