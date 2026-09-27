"""Full coverage of the Daikin One config, reauth, reconfigure and options flows."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.config_flow import entry_title
from custom_components.daikinone.const import CONF_INTEGRATOR_TOKEN, CONF_READ_ONLY, DOMAIN, PLATFORMS

from .conftest import API_KEY, DEVICES_URL, EMAIL, INTEGRATOR_TOKEN, TOKEN_URL, sequence

USER_INPUT = {
    CONF_EMAIL: EMAIL,
    CONF_API_KEY: API_KEY,
    CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN,
}

TOKEN_OK: dict[str, Any] = {"json": load_json_object_fixture("token.json")}
DEVICES_OK: dict[str, Any] = {"json": load_json_array_fixture("devices.json")}


@pytest.fixture(autouse=True)
def _only_implemented_platforms() -> Iterator[None]:
    """Forward only the platform modules that exist on disk (no-op once all landed)."""
    integration = Path(__file__).parent.parent / "custom_components" / "daikinone"
    available = [platform for platform in PLATFORMS if (integration / f"{platform.value}.py").is_file()]
    with patch("custom_components.daikinone.PLATFORMS", available):
        yield


def _mock_flow_api(
    aioclient_mock: AiohttpClientMocker,
    *,
    token: list[dict[str, Any]] | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> None:
    """Register token/devices responses; the last entry of each list repeats."""
    aioclient_mock.post(TOKEN_URL, side_effect=sequence(*(token or [TOKEN_OK])))
    aioclient_mock.get(DEVICES_URL, side_effect=sequence(*(devices or [DEVICES_OK])))


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("Owner@Example.com", "O***@Example.com"),
        ("a.very.long.owner@some-domain.co.uk", "a***@some-domain.co.uk"),
        # Nothing to mask: a one-character local part or no domain at all falls back.
        ("a@example.com", "Daikin One"),
        ("not-an-email", "Daikin One"),
    ],
)
def test_entry_title_masks_the_email(email: str, expected: str) -> None:
    """The title never carries a full address, whatever the user typed."""
    assert entry_title(email) == expected
    assert email not in entry_title(email)


async def test_user_flow_creates_the_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The happy path stores exactly the three credentials, keyed by the lowercased email."""
    _mock_flow_api(aioclient_mock)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Core logs the title on every setup failure, so it carries a masked address only.
    assert result["title"] == entry_title(EMAIL) == "O***@Example.com"
    assert EMAIL not in result["title"]
    assert result["data"] == USER_INPUT
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == EMAIL.lower()


@pytest.mark.parametrize(
    ("token", "devices", "expected"),
    [
        ([{"status": 401, "json": {"messages": "NotAuthorizedException"}}, TOKEN_OK], None, "invalid_auth"),
        ([{"status": 403, "json": {"messages": "NotAuthorizedException"}}, TOKEN_OK], None, "invalid_api_key"),
        (None, [{"status": 429}, DEVICES_OK], "rate_limited"),
        (None, [{"status": 500}, DEVICES_OK], "cannot_connect"),
        (None, [{"json": []}, DEVICES_OK], "no_devices"),
    ],
    ids=["invalid_auth", "invalid_api_key", "rate_limited", "cannot_connect", "no_devices"],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    token: list[dict[str, Any]] | None,
    devices: list[dict[str, Any]] | None,
    expected: str,
) -> None:
    """Each failure shows its own error and the same flow succeeds on the retry."""
    _mock_flow_api(aioclient_mock, token=token, devices=devices)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unexpected_error_recovers(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A non-API exception surfaces as `unknown` without leaking anything."""
    _mock_flow_api(aioclient_mock)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(
        "custom_components.daikinone.config_flow.DaikinOneClient.async_get_devices",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The same account (case-insensitively) cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    _mock_flow_api(aioclient_mock)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_EMAIL: EMAIL.upper()}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_the_legacy_password(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Reauth writes the new credentials and drops the legacy password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=EMAIL.lower(),
        title=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "legacy-password-placeholder"},
    )
    entry.add_to_hass(hass)
    _mock_flow_api(aioclient_mock)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == USER_INPUT
    # An entry written before the title was masked is re-titled by a successful reauth.
    assert entry.title == entry_title(EMAIL)


async def test_reauth_shows_errors_before_succeeding(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rejected key keeps the reauth form open until valid credentials are given."""
    mock_config_entry.add_to_hass(hass)
    _mock_flow_api(aioclient_mock, token=[{"status": 403}, TOKEN_OK])

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "invalid_api_key"}

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reconfigure_updates_credentials(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Reconfigure replaces the stored key and token for the same account."""
    mock_config_entry.add_to_hass(hass)
    _mock_flow_api(aioclient_mock)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_input = {**USER_INPUT, CONF_API_KEY: "rotated-api-key-placeholder"}  # gitleaks:allow
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == new_input
    assert mock_config_entry.title == entry_title(EMAIL)


async def test_reconfigure_rejects_a_different_account(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Switching the email during reconfigure aborts instead of hijacking the entry."""
    mock_config_entry.add_to_hass(hass)
    _mock_flow_api(aioclient_mock)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_EMAIL: "someone.else@example.com"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert mock_config_entry.data[CONF_EMAIL] == EMAIL


async def test_options_accept_300_and_reload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """A valid interval is stored and the entry is reloaded with the new base interval."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 300})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_SCAN_INTERVAL: 300, CONF_READ_ONLY: False}
    assert init_integration.runtime_data.base_interval == 300


async def test_options_reject_below_daikins_floor(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The selector refuses anything under Daikin's documented 3-minute limit."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 60})

    assert init_integration.options == {CONF_READ_ONLY: False}


async def test_options_turn_read_only_off_and_reload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    # GIVEN
    hass.config_entries.async_update_entry(init_integration, options={CONF_READ_ONLY: True})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    # WHEN
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 180, CONF_READ_ONLY: False}
        )
        await hass.async_block_till_done()

    # THEN
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_SCAN_INTERVAL: 180, CONF_READ_ONLY: False}
    assert init_integration.runtime_data.read_only is False
