"""Diagnostics: nothing identifying or secret leaves the process, everything useful stays."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import CONF_INTEGRATOR_TOKEN, PLATFORMS
from custom_components.daikinone.diagnostics import async_get_config_entry_diagnostics

from .conftest import ACCESS_TOKEN, API_KEY, DEVICE_IDS, DEVICES_URL, EMAIL, INTEGRATOR_TOKEN, TOKEN_URL

#: Every string that must never appear in a diagnostics dump: the three credentials, the
#: minted access token, the header prefix that would carry it, and the account's identifiers.
FORBIDDEN = (INTEGRATOR_TOKEN, API_KEY, ACCESS_TOKEN, EMAIL, "Bearer ", "dev1", "Main Floor", "Cabin", "Home")


@pytest.fixture(autouse=True)
def _only_implemented_platforms() -> Iterator[None]:
    """Forward only the platform modules that exist, so setup works during the build."""
    integration = Path(__file__).parent.parent / "custom_components" / "daikinone"
    available = [platform for platform in PLATFORMS if (integration / f"{platform.value}.py").is_file()]
    with patch("custom_components.daikinone.PLATFORMS", available):
        yield


def _dump(result: dict[str, Any]) -> str:
    """Serialise diagnostics the way HA's download view does."""
    return json.dumps(result, default=str)


def _assert_no_secrets(serialized: str) -> None:
    """Fail with the offending needle rather than a bare False."""
    for needle in FORBIDDEN:
        assert needle not in serialized, f"diagnostics leaked {needle[:24]!r}"


async def test_diagnostics_redacts_credentials_and_identifiers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Credentials, the access token and every account identifier are redacted."""
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    serialized = _dump(result)

    _assert_no_secrets(serialized)
    assert result["entry"]["data"] == {
        CONF_EMAIL: REDACTED,
        CONF_API_KEY: REDACTED,
        CONF_INTEGRATOR_TOKEN: REDACTED,
    }
    assert result["entry"]["title"] == REDACTED
    assert result["entry"]["unique_id"] == REDACTED
    # Thermostats are a plain list: their ids are not usable as keys either.
    assert isinstance(result["thermostats"], list)
    assert len(result["thermostats"]) == len(DEVICE_IDS)
    for thermostat in result["thermostats"]:
        assert {"id", "name", "location_name"}.isdisjoint(thermostat)


async def test_diagnostics_reports_state_and_scheduling(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The useful part survives redaction: version, host, polling, token life, full state."""
    result = await async_get_config_entry_diagnostics(hass, init_integration)

    assert result["integration_version"] == "1.2.0"
    assert result["api_host"] == "https://integrator-api.daikinskyport.com"
    assert result["base_interval_seconds"] >= 180
    assert result["current_update_interval_seconds"] >= 180
    assert result["last_update_success"] is True
    assert result["last_update_success_time"] is not None
    assert result["last_error_code"] is None

    # Seconds left on the token, never the token: documented lifetime is 900 s.
    expires_in = result["token_expires_in_seconds"]
    assert isinstance(expires_in, int)
    assert 0 < expires_in <= 900

    thermostat = result["thermostats"][0]
    assert thermostat["online"] is True
    assert thermostat["model"] == "ONEPLUS"
    assert thermostat["firmware_version"] == "3.1.7"
    assert thermostat["capabilities"] == {
        "mode_limit": "ALL",
        "em_heat_available": True,
        "hvac_modes_exposed": ["off", "heat", "cool", "heat_cool"],
        "setpoint_minimum": 10.0,
        "setpoint_maximum": 32.0,
        "setpoint_delta": 2.0,
    }
    # Enums are dumped by name so a dump is readable without the enum tables.
    assert thermostat["state"]["heat_setpoint"] == 20.0
    assert thermostat["state"]["equipment_status"] == "COOL"
    assert thermostat["state"]["mode"] == "AUTO"
    assert thermostat["state"]["schedule_enabled"] is True


async def test_diagnostics_with_an_offline_thermostat(
    hass: HomeAssistant, init_integration: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An unreachable thermostat is reported as offline with the error code that caused it."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    aioclient_mock.get(f"{DEVICES_URL}/dev1", status=500, json=load_json_object_fixture("device_offline.json"))
    for device_id in DEVICE_IDS[1:]:
        aioclient_mock.get(f"{DEVICES_URL}/{device_id}", json=load_json_object_fixture("device_oneplus.json"))

    await init_integration.runtime_data.async_refresh()
    result = await async_get_config_entry_diagnostics(hass, init_integration)

    _assert_no_secrets(_dump(result))
    assert result["last_update_success"] is True
    assert result["last_error_code"] == "device_offline"
    assert [thermostat["online"] for thermostat in result["thermostats"]] == [False, True, True]


async def test_diagnostics_of_an_entry_that_never_set_up(
    hass: HomeAssistant, legacy_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A migrated ha-daikinone entry awaiting reauth has no runtime data and must not crash."""
    legacy_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(legacy_config_entry.entry_id)
    await hass.async_block_till_done()
    assert legacy_config_entry.state is ConfigEntryState.SETUP_ERROR

    result = await async_get_config_entry_diagnostics(hass, legacy_config_entry)
    serialized = _dump(result)

    _assert_no_secrets(serialized)
    assert "legacy-password-placeholder" not in serialized
    assert result["entry"]["data"]["password"] == REDACTED
    assert result["integration_version"] == "1.2.0"
    assert "thermostats" not in result
