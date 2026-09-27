"""Shared fixtures for the daikinone test suite.

All HTTP traffic is mocked with pytest-homeassistant-custom-component's ``aioclient_mock``
(it patches HA's shared aiohttp session factory). No test needs live credentials; every
secret below is an obvious placeholder.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.const import CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.daikinone.const import (
    CONF_INTEGRATOR_TOKEN,
    CONF_READ_ONLY,
    CONF_UID_SCHEMA,
    DOMAIN,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

BASE = "https://integrator-api.daikinskyport.com"
TOKEN_URL = f"{BASE}/v1/token"
DEVICES_URL = f"{BASE}/v1/devices"

# Mixed case on purpose: Daikin documents the email as case-sensitive.
EMAIL = "Owner@Example.com"
API_KEY = "test-api-key-placeholder-0123456789"  # gitleaks:allow
# Real integrator tokens are >1700-char JWEs; the spec requires supporting >2000 chars.
INTEGRATOR_TOKEN = "test-integrator-token-placeholder." + "x" * 2400  # gitleaks:allow
ACCESS_TOKEN = "test-access-token-not-a-real-secret"  # gitleaks:allow

DEVICE_IDS = ("dev1", "dev2", "dev3")

PUT_URL_RE = re.compile(rf"{re.escape(DEVICES_URL)}/[^/]+/(msp|schedule|fan)$")


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture(autouse=True)
def _no_transport_retry_delay() -> Iterator[None]:
    """The single transport retry waits 2 s in production; tests must not."""
    with patch("custom_components.daikinone.api.const.TRANSPORT_RETRY_DELAY", 0):
        yield


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Pin the Home Assistant snapshot extension.

    Both syrupy and pytest-homeassistant-custom-component provide a ``snapshot``
    fixture; which one wins depends on plugin load order, which varies between
    environments (it differed between macOS and the Linux CI runner). A conftest
    fixture always outranks plugin fixtures, so this makes the HA extension (and
    its ``snapshots/`` directory) deterministic everywhere.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Config entry for the current (v2) schema."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=EMAIL.lower(),
        title=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_API_KEY: API_KEY,
            CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN,
        },
        options={CONF_READ_ONLY: False},
    )


@pytest.fixture
def legacy_config_entry() -> MockConfigEntry:
    """A ha-daikinone v1.2 entry (email/password) awaiting migration."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        title="Daikin One",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "legacy-password-placeholder",
            CONF_UID_SCHEMA: 0,
        },
    )


@pytest.fixture
def mock_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Happy-path Daikin API: token, 2 locations / 3 thermostats, all writes accepted."""
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    for dev_id in DEVICE_IDS:
        aioclient_mock.get(
            f"{DEVICES_URL}/{dev_id}",
            json=load_json_object_fixture("device_oneplus.json"),
        )
    aioclient_mock.put(PUT_URL_RE, json=load_json_object_fixture("write_ok.json"))
    return aioclient_mock


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration with the happy-path API and deterministic jitter."""
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        yield mock_config_entry


def calls(mock: AiohttpClientMocker, method: str, path: str) -> list[tuple[Any, ...]]:
    """Return recorded calls for METHOD + exact URL path (e.g. '/v1/devices/dev1')."""
    return [c for c in mock.mock_calls if c[0].lower() == method.lower() and c[1].path == path]


def sequence(*responses: dict[str, Any]) -> Callable[..., Coroutine[Any, Any, AiohttpClientMockResponse]]:
    """Async side_effect returning the given response kwargs in order; the last repeats.

    Each item is a kwargs dict for AiohttpClientMockResponse, e.g. {"status": 401, "json": {...}}.
    """
    queue = list(responses)

    async def _side_effect(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        kwargs = queue.pop(0) if len(queue) > 1 else queue[0]
        return AiohttpClientMockResponse(method=method, url=url, **kwargs)

    return _side_effect


def gated(
    json: dict[str, Any] | list[Any], counter: dict[str, int]
) -> Callable[..., Coroutine[Any, Any, AiohttpClientMockResponse]]:
    """Async side_effect that yields control so concurrent requests interleave.

    Tracks peak in-flight request count in counter["peak"]. Initialise the counter with
    {"in_flight": 0, "peak": 0}.
    """

    async def _side_effect(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        counter["in_flight"] += 1
        counter["peak"] = max(counter["peak"], counter["in_flight"])
        for _ in range(3):
            await asyncio.sleep(0)
        counter["in_flight"] -= 1
        return AiohttpClientMockResponse(method=method, url=url, json=json)

    return _side_effect
