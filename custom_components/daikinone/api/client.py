"""Async client for the official Daikin One Open API (integrator-api.daikinskyport.com)."""

from __future__ import annotations

import asyncio
from json import loads as json_loads
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from . import const
from .auth import IntegratorAuth
from .const import API_BASE_URL, DEVICES_PATH, MAX_CONCURRENT_REQUESTS, REQUEST_TIMEOUT
from .exceptions import (
    InvalidRequestError,
    MalformedResponseError,
    TokenExpiredError,
    TransportError,
    UnsupportedCapabilityError,
    error_for,
    message_from_body,
    retry_after_from,
)
from .models import DeviceSummary, FanCirculate, FanCirculateSpeed, Mode, ThermostatState

_LOGGER = logging.getLogger(__name__)

__all__ = ["DaikinOneClient"]


class DaikinOneClient:
    """Typed wrapper over the documented Integrator API endpoints."""

    def __init__(self, session: ClientSession, email: str, api_key: str, integrator_token: str) -> None:
        """Build a client on an injected session (HA's shared aiohttp session)."""
        self._session = session
        self._api_key = api_key
        # Daikin's usage limits: never more than 3 open requests, token POST included.
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.auth = IntegratorAuth(session, email, api_key, integrator_token, self._semaphore)

    async def async_get_devices(self) -> list[DeviceSummary]:
        """GET /v1/devices -- the only call that carries name, model, firmware and location."""
        data = await self._request("GET", DEVICES_PATH)
        if not isinstance(data, list):
            raise MalformedResponseError
        devices: list[DeviceSummary] = []
        for location in data:
            if not isinstance(location, dict):
                continue
            for device in location.get("devices") or ():
                if not isinstance(device, dict):
                    continue
                summary = DeviceSummary.from_json(location, device)
                if summary is None:
                    _LOGGER.debug("Skipping a device entry without an id")
                    continue
                devices.append(summary)
        return devices

    async def async_get_thermostat(self, device_id: str) -> ThermostatState:
        """GET /v1/devices/{id} -- the full thermostat state."""
        data = await self._request("GET", f"{DEVICES_PATH}/{device_id}")
        if not isinstance(data, dict):
            raise MalformedResponseError
        return ThermostatState.from_json(data)

    async def async_set_mode_setpoints(self, device_id: str, mode: Mode, heat: float, cool: float) -> None:
        """PUT /msp -- all three fields are required by the API on every write."""
        await self._request(
            "PUT",
            f"{DEVICES_PATH}/{device_id}/msp",
            {"mode": int(mode), "heatSetpoint": heat, "coolSetpoint": cool},
        )

    async def async_set_schedule_enabled(self, device_id: str, enabled: bool) -> None:
        """PUT /schedule -- turn the thermostat's own schedule on or off."""
        await self._request("PUT", f"{DEVICES_PATH}/{device_id}/schedule", {"scheduleEnabled": enabled})

    async def async_set_fan(self, device_id: str, circulate: FanCirculate, speed: FanCirculateSpeed) -> None:
        """PUT /fan -- unitary systems only.

        No capability field exists anywhere in the API, so a rejected request is the only way
        to learn that the equipment is VRV (P1P2) or split (S21); surface that as an
        ``UnsupportedCapabilityError`` rather than a generic bad-request failure.
        """
        try:
            await self._request(
                "PUT",
                f"{DEVICES_PATH}/{device_id}/fan",
                {"fanCirculate": int(circulate), "fanCirculateSpeed": int(speed)},
            )
        except InvalidRequestError as err:
            raise UnsupportedCapabilityError(err.status) from err

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        """Perform a request, retrying once when it never completed (dropped connection, timeout).

        Every endpoint is safe to repeat: reads are reads, and each write sets absolute values.
        """
        try:
            return await self._request_once(method, path, json)
        except TransportError as err:
            _LOGGER.debug("%s %s failed (%s); retrying once", method, path, err.cause)
            await asyncio.sleep(const.TRANSPORT_RETRY_DELAY)
            return await self._request_once(method, path, json)

    async def _request_once(self, method: str, path: str, json: Any = None) -> Any:
        """Perform one authenticated request, refreshing the token once on a 401."""
        url = f"{API_BASE_URL}{path}"
        for attempt in (0, 1):
            # Deliberately outside the semaphore: acquiring a slot while holding the auth lock
            # (or vice versa) would let three waiters deadlock behind a refresh.
            token = await self.auth.async_get_access_token()
            headers = {"x-api-key": self._api_key, "Authorization": f"Bearer {token}"}
            async with self._semaphore:
                try:
                    async with self._session.request(
                        method,
                        url,
                        json=json,
                        headers=headers,
                        timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp:
                        status = resp.status
                        retry_after = retry_after_from(resp.headers)
                        text = await resp.text()
                except (TimeoutError, ClientError) as err:
                    raise TransportError.from_cause(err) from err

            _LOGGER.debug("%s %s -> %s", method, path, status)

            if status == 401 and attempt == 0:
                self.auth.invalidate(token)
                continue
            if status >= 300:
                raise error_for(status, message_from_body(text), retry_after)
            try:
                parsed = json_loads(text)
            except ValueError:
                raise MalformedResponseError(status) from None
            return parsed

        raise TokenExpiredError(401)
