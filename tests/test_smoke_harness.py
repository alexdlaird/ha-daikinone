"""Scaffold smoke test: proves the harness wiring works before implementers start.

Verifies the hass fixture, aioclient_mock interception of HA's shared session (including
recorded headers and JSON bodies), fixture loading, and that the custom integration is
discoverable by the loader.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import DOMAIN
from tests.conftest import API_KEY, DEVICES_URL, calls, mock_api  # noqa: F401


async def test_harness_wiring(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,  # noqa: F811
) -> None:
    """The mocked shared session records method, URL, body and headers."""
    session = async_get_clientsession(hass)
    resp = await session.get(DEVICES_URL, headers={"x-api-key": API_KEY})
    body = await resp.json()

    assert resp.status == 200
    assert body[0]["locationName"] == "Home"
    recorded = calls(mock_api, "get", "/v1/devices")
    assert len(recorded) == 1
    assert recorded[0][3]["x-api-key"] == API_KEY


async def test_custom_integration_loadable(hass: HomeAssistant) -> None:
    """The daikinone custom integration is visible to the HA loader."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.version is not None
    assert str(integration.version) == "1.3.0"
    assert integration.iot_class == "cloud_polling"
