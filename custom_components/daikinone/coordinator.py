"""Polling coordinator and write path for the Daikin One integration.

Daikin's published usage limits drive the design: never poll faster than every 3 minutes,
keep at most 3 requests open (enforced by the client's semaphore), and wait at least 15 s
after a write before reading back. Writes are therefore applied optimistically and a single
coalesced verification read reconciles them once the equipment has settled.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DaikinAuthError,
    DaikinOneClient,
    DaikinOneError,
    DeviceSummary,
    FanCirculate,
    FanCirculateSpeed,
    Mode,
    RateLimitedError,
    ServerError,
    Thermostat,
    ThermostatState,
    TransportError,
    UnsupportedCapabilityError,
)
from .const import (
    CONF_READ_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_READ_ONLY,
    DOMAIN,
    EVENT_WRITE_SUPPRESSED,
    JITTER_SECONDS,
    MAX_BACKOFF_SECONDS,
    MIN_SCAN_INTERVAL,
    VERIFY_DELAY_SECONDS,
)

if TYPE_CHECKING:
    from . import DaikinOneConfigEntry

_LOGGER = logging.getLogger(__name__)

#: Client error code -> `exceptions.*` translation key used for failed entity writes.
WRITE_ERROR_KEYS: Final[dict[str, str]] = {
    "invalid_credentials": "auth_failed",
    "invalid_api_key": "auth_failed",
    "token_expired": "auth_failed",
    "rate_limited": "rate_limited",
    "device_offline": "device_offline",
    "invalid_request": "invalid_request",
    "unsupported_capability": "unsupported_capability",
}

#: Failures that are usually over by the next poll: a dropped connection, a timeout, a 5xx.
TRANSIENT_ERRORS: Final = (TransportError, ServerError)
#: Consecutive polls that may fail transiently before entities go unavailable; the last good
#: reading is kept meanwhile (it is at most one poll interval older than it would be anyway).
TRANSIENT_POLLS_TOLERATED: Final = 1

#: Modes whose only live setpoint is the heat one.
_HEATING_MODES: Final = (Mode.HEAT, Mode.EMERGENCY_HEAT)

#: Slack for binary float error when comparing a computed setpoint gap against the delta.
_DELTA_TOLERANCE: Final = 1e-9

__all__ = ["WRITE_ERROR_KEYS", "DaikinOneCoordinator", "SuppressedWrite"]


@dataclass(frozen=True, kw_only=True)
class SuppressedWrite:
    """A validated write that read-only mode kept from Daikin, exactly as it would have been sent."""

    endpoint: str
    payload: dict[str, Any]
    at: datetime
    context: Context | None = None


def describe(err: DaikinOneError) -> str:
    """The error code, plus the underlying exception class for transport failures."""
    return f"{err.code}: {err.cause}" if isinstance(err, TransportError) else err.code


def _fan_issue_id(thermostat_id: str) -> str:
    """Repair issue id for a thermostat whose equipment rejects fan circulation."""
    return f"fan_unsupported_{thermostat_id}"


class DaikinOneCoordinator(TimestampDataUpdateCoordinator[dict[str, Thermostat]]):
    """Fetch every thermostat of one Daikin account and serialise writes per device."""

    config_entry: DaikinOneConfigEntry

    def __init__(self, hass: HomeAssistant, entry: DaikinOneConfigEntry, client: DaikinOneClient) -> None:
        """Set up the coordinator with the configured (floored) poll interval."""
        self.client = client
        self.base_interval = max(MIN_SCAN_INTERVAL, int(entry.options.get(CONF_SCAN_INTERVAL, MIN_SCAN_INTERVAL)))
        self.last_error_code: str | None = None
        self.read_only = bool(entry.options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY))
        #: Per thermostat, the latest suppressed write to each endpoint (msp / fan / schedule).
        self.suppressed_writes: defaultdict[str, dict[str, SuppressedWrite]] = defaultdict(dict)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._consecutive_failures = 0
        self._transient_streak = 0
        self._device_transient_streaks: dict[str, int] = {}
        self._verify_cancel: CALLBACK_TYPE | None = None
        self._closed = False
        self._offline_logged: set[str] = set()
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._jittered(),
        )

    def _jittered(self) -> timedelta:
        """Spread the account's polls so several HA instances do not sync up on Daikin."""
        return timedelta(seconds=self.base_interval + random.uniform(0, JITTER_SECONDS))

    # ------------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, Thermostat]:
        """Fetch the device list, then every thermostat state (client caps concurrency at 3)."""
        try:
            summaries = await self.client.async_get_devices()
        except DaikinAuthError as err:
            raise ConfigEntryAuthFailed("Daikin One credentials were rejected") from err
        except DaikinOneError as err:
            if self._tolerate_account_failure(err):
                return self.data
            raise self._account_failure(err) from err

        results = await asyncio.gather(
            *(self.client.async_get_thermostat(summary.id) for summary in summaries),
            return_exceptions=True,
        )

        previous = self.data or {}
        thermostats: dict[str, Thermostat] = {}
        for summary, result in zip(summaries, results, strict=True):
            if isinstance(result, ThermostatState):
                thermostats[summary.id] = Thermostat(summary, result, online=True)
                self._device_transient_streaks.pop(summary.id, None)
                if summary.id in self._offline_logged:
                    self._offline_logged.discard(summary.id)
                    _LOGGER.info("Thermostat %s is reachable again", summary.name)
                continue
            if isinstance(result, DaikinAuthError):
                raise ConfigEntryAuthFailed("Daikin One credentials were rejected") from result
            if isinstance(result, RateLimitedError):
                raise self._account_failure(result) from result
            if isinstance(result, DaikinOneError):
                self.last_error_code = result.code
                known = previous.get(summary.id)
                if known is not None and self._tolerate_device_failure(summary, result, known):
                    thermostats[summary.id] = replace(known, summary=summary)
                    continue
                thermostats[summary.id] = Thermostat(
                    summary,
                    known.state if known is not None else ThermostatState(),
                    online=False,
                )
                if summary.id not in self._offline_logged:
                    self._offline_logged.add(summary.id)
                    _LOGGER.info("Thermostat %s is unreachable: %s", summary.name, result.code)
                continue
            raise result

        self._offline_logged &= thermostats.keys()
        self._consecutive_failures = 0
        self._transient_streak = 0
        self.update_interval = self._jittered()
        self._async_remove_stale_devices(thermostats, previous)
        return thermostats

    def _tolerate_account_failure(self, err: DaikinOneError) -> bool:
        """Keep the last reading through one transient failure of the whole poll."""
        if (
            not isinstance(err, TRANSIENT_ERRORS)
            or not self.data
            or self._transient_streak >= TRANSIENT_POLLS_TOLERATED
        ):
            return False
        self._transient_streak += 1
        self.last_error_code = err.code
        _LOGGER.warning("Daikin One poll failed (%s); keeping the last reading until the next poll", describe(err))
        return True

    def _tolerate_device_failure(self, summary: DeviceSummary, err: DaikinOneError, known: Thermostat) -> bool:
        """Keep a reachable thermostat's last reading through one transient failure of its own read.

        Daikin's explicit DeviceOfflineException is never tolerated: it is the truth, not a blip.
        """
        streak = self._device_transient_streaks.get(summary.id, 0)
        if not isinstance(err, TRANSIENT_ERRORS) or not known.online or streak >= TRANSIENT_POLLS_TOLERATED:
            return False
        self._device_transient_streaks[summary.id] = streak + 1
        _LOGGER.warning(
            "Reading thermostat %s failed (%s); keeping its last reading until the next poll",
            summary.name,
            describe(err),
        )
        return True

    def _account_failure(self, err: DaikinOneError) -> UpdateFailed:
        """Build an UpdateFailed carrying the backoff HA should honour before retrying."""
        self._consecutive_failures += 1
        self.last_error_code = err.code
        delay = min(self.base_interval * 2**self._consecutive_failures, MAX_BACKOFF_SECONDS)
        # Retry-After may only ever lengthen the wait. Replacing the exponential term with it
        # would pin a repeated 429 that carries a small header at the floor for ever, and could
        # even shrink a backoff that has already grown. Read off the instance rather than one
        # exception type so any error that starts carrying the header (503s legitimately send
        # one) is honoured the same way.
        retry_after: float | None = getattr(err, "retry_after", None)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return UpdateFailed(f"Daikin One update failed ({describe(err)})", retry_after=delay)

    @callback
    def _async_remove_stale_devices(self, thermostats: dict[str, Thermostat], previous: dict[str, Thermostat]) -> None:
        """Drop registry devices the account no longer has (incl. legacy equipment devices)."""
        # The reconciliation below is destructive: entity ids, area assignments and the
        # automations referencing them do not come back. An empty device list is far more
        # likely to be a Daikin hiccup than a deletion of every thermostat, so it never
        # removes anything -- an account that really has no thermostats has nothing to
        # reconcile, and `async_remove_config_entry_device` still allows manual deletion.
        if not thermostats:
            _LOGGER.debug("Empty device list ignored; keeping known devices")
            return

        # The first refresh (no `previous`) runs after every restart and has nothing to
        # corroborate a short device list against, so it prunes only ha-daikinone's leftover
        # equipment children -- they hang off a thermostat via `via_device` and the official
        # API never returns them. Any other absence waits for the next successful poll, by
        # which time `previous` makes it a real disappearance rather than one bad reply.
        first_refresh = not previous
        entry_id = self.config_entry.entry_id
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, entry_id):
            if any(domain == DOMAIN and key in thermostats for domain, key in device.identifiers):
                continue
            if first_refresh and device.via_device_id is None:
                continue
            registry.async_remove_device(device.id)

    # ------------------------------------------------------------------- writes

    async def async_set_mode_setpoints(
        self,
        thermostat_id: str,
        *,
        mode: Mode | None = None,
        heat: float | None = None,
        cool: float | None = None,
        context: Context | None = None,
    ) -> None:
        """PUT /msp with the full triple, filling the unspecified fields from local state."""
        async with self._locks[thermostat_id]:
            state = self._require(thermostat_id).state
            resolved_mode = mode if mode is not None else state.mode
            if resolved_mode is None or resolved_mode is Mode.UNKNOWN:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="state_unknown")

            heat_value = heat if heat is not None else state.heat_setpoint
            cool_value = cool if cool is not None else state.cool_setpoint
            if heat_value is None or cool_value is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="state_unknown")

            delta = state.setpoint_delta or 0.0
            explicit_range_violates_delta = (
                heat is not None and cool is not None and cool_value - heat_value < delta - _DELTA_TOLERANCE
            )
            if heat is not None and cool is None:
                # Single-setpoint write: push the other side out to keep Daikin's delta.
                cool_value = max(cool_value, heat_value + delta)
            elif cool is not None and heat is None:
                heat_value = min(heat_value, cool_value - delta)
            elif explicit_range_violates_delta and resolved_mode in _HEATING_MODES:
                # Only the heat side is live in heat modes; an out-of-delta range (e.g. low == high from
                # a caller that always sends both) keeps it and pushes the inert cool side out.
                cool_value = heat_value + delta
            elif explicit_range_violates_delta and resolved_mode is Mode.COOL:
                heat_value = cool_value - delta

            # Checked on every path, not just an explicit pair: a mode-only write echoes the
            # snapshot's setpoints back, and Daikin rejects the whole PUT if they violate the
            # delta. The tolerance only absorbs float noise from the push arithmetic above
            # (real values are on a 0.1 grid, so a genuine violation is off by >= 0.1).
            if cool_value - heat_value < delta - _DELTA_TOLERANCE:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="setpoint_delta",
                    translation_placeholders={"delta": f"{delta:g}"},
                )

            self._check_range(state, heat_value, cool_value)
            heat_value = round(heat_value, 1)
            cool_value = round(cool_value, 1)

            payload = {"mode": int(resolved_mode), "heatSetpoint": heat_value, "coolSetpoint": cool_value}
            if self._suppress_write(thermostat_id, "msp", payload, context):
                return
            await self._call(self.client.async_set_mode_setpoints(thermostat_id, resolved_mode, heat_value, cool_value))
            # A /msp write turns the thermostat's own schedule (and away mode) off.
            self._apply(
                thermostat_id,
                mode=resolved_mode,
                heat_setpoint=heat_value,
                cool_setpoint=cool_value,
                schedule_enabled=False,
            )

    async def async_set_schedule_enabled(
        self, thermostat_id: str, enabled: bool, *, context: Context | None = None
    ) -> None:
        """PUT /schedule and apply the new value optimistically."""
        async with self._locks[thermostat_id]:
            self._require(thermostat_id)
            if self._suppress_write(thermostat_id, "schedule", {"scheduleEnabled": enabled}, context):
                return
            await self._call(self.client.async_set_schedule_enabled(thermostat_id, enabled))
            self._apply(thermostat_id, schedule_enabled=enabled)

    async def async_set_fan(
        self,
        thermostat_id: str,
        *,
        circulate: FanCirculate | None = None,
        speed: FanCirculateSpeed | None = None,
        context: Context | None = None,
    ) -> None:
        """PUT /fan with both documented fields; unitary systems only."""
        async with self._locks[thermostat_id]:
            state = self._require(thermostat_id).state
            resolved_circulate = circulate if circulate is not None else state.fan_circulate
            resolved_speed = speed if speed is not None else state.fan_circulate_speed
            # Never echo an unparsable enum back to Daikin: it would be rejected as a bad
            # request and misreported as unsupported equipment.
            if resolved_circulate is None or resolved_circulate is FanCirculate.UNKNOWN:
                resolved_circulate = FanCirculate.OFF
            if resolved_speed is None or resolved_speed is FanCirculateSpeed.UNKNOWN:
                resolved_speed = FanCirculateSpeed.LOW
            payload = {"fanCirculate": int(resolved_circulate), "fanCirculateSpeed": int(resolved_speed)}
            if self._suppress_write(thermostat_id, "fan", payload, context):
                return
            try:
                await self.client.async_set_fan(thermostat_id, resolved_circulate, resolved_speed)
            except DaikinOneError as err:
                if isinstance(err, UnsupportedCapabilityError):
                    self._async_create_fan_issue(thermostat_id)
                raise self._write_error(err) from err

            ir.async_delete_issue(self.hass, DOMAIN, _fan_issue_id(thermostat_id))
            self._apply(thermostat_id, fan_circulate=resolved_circulate, fan_circulate_speed=resolved_speed)

    @callback
    def _suppress_write(
        self, thermostat_id: str, endpoint: str, payload: dict[str, Any], context: Context | None
    ) -> bool:
        """In read-only mode, record the write in place of sending it; True when it was kept back.

        Nothing is applied optimistically: the local state keeps mirroring the thermostat, so a
        suppressed write never shows up as a setpoint (or a schedule change) that did not happen.
        """
        if not self.read_only:
            return False
        self.suppressed_writes[thermostat_id][endpoint] = SuppressedWrite(
            endpoint=endpoint, payload=payload, at=dt_util.utcnow(), context=context
        )
        _LOGGER.info("Read-only: not sending PUT /%s for %s: %s", endpoint, thermostat_id, payload)
        self.hass.bus.async_fire(
            EVENT_WRITE_SUPPRESSED,
            {"thermostat_id": thermostat_id, "endpoint": endpoint, **payload},
            context=context,
        )
        self.async_update_listeners()
        return True

    def _require(self, thermostat_id: str) -> Thermostat:
        """Return the local snapshot, or refuse the write when the state is unknown."""
        thermostat = (self.data or {}).get(thermostat_id)
        if thermostat is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="state_unknown")
        return thermostat

    @staticmethod
    def _check_range(state: ThermostatState, *values: float) -> None:
        """Reject setpoints outside the range the thermostat reported."""
        minimum, maximum = state.setpoint_minimum, state.setpoint_maximum
        for value in values:
            if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="setpoint_out_of_range")

    async def _call(self, coro: Coroutine[Any, Any, None]) -> None:
        """Await a client write, mapping API failures onto translated HA errors."""
        try:
            await coro
        except DaikinOneError as err:
            raise self._write_error(err) from err

    @staticmethod
    def _write_error(err: DaikinOneError) -> HomeAssistantError:
        """Translate a client error code into the user-facing write failure."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=WRITE_ERROR_KEYS.get(err.code, "write_failed"),
        )

    @callback
    def _async_create_fan_issue(self, thermostat_id: str) -> None:
        """Tell the user their equipment (VRV / split) has no fan circulation control."""
        thermostat = (self.data or {}).get(thermostat_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _fan_issue_id(thermostat_id),
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key="fan_controls_unsupported",
            translation_placeholders={"name": thermostat.summary.name if thermostat else thermostat_id},
        )

    @callback
    def _apply(self, thermostat_id: str, **changes: Any) -> None:
        """Apply an optimistic state change and arm the delayed verification read."""
        thermostat = self.data[thermostat_id]
        updated = replace(thermostat, state=replace(thermostat.state, **changes))
        data = {**self.data, thermostat_id: updated}
        if self.last_update_success:
            self.async_set_updated_data(data)
        else:
            # Mid-backoff. async_set_updated_data() would clear the failure state and restart
            # the refresh timer at the normal interval, throwing away a Retry-After Daikin just
            # asked us to honour. A local write is not an API read, so publish it by hand.
            self.data = data
            self.async_update_listeners()
        self._arm_verify()

    # ------------------------------------------------------- write verification

    @callback
    def _arm_verify(self) -> None:
        """(Re)start the single post-write read; consecutive writes coalesce into one."""
        self.cancel_verify()
        if self._closed:
            # The entry unloaded while this write was in flight: arming now would leave a
            # timer holding a dead coordinator that nobody can cancel any more.
            return
        self._verify_cancel = async_call_later(self.hass, VERIFY_DELAY_SECONDS, self._async_verify)

    async def _async_verify(self, _now: datetime) -> None:
        """Read the thermostats back once the equipment has had time to apply the write."""
        self._verify_cancel = None
        if not self.last_update_success:
            # Still inside a backoff window (Retry-After or the exponential term): reading now
            # would ignore exactly what the API asked for. The retry HA already has scheduled
            # is the earliest read we are allowed to make, and it reconciles the write too.
            _LOGGER.debug("Skipping the verification read: an account backoff is still active")
            return
        await self.async_refresh()

    @callback
    def cancel_verify(self) -> None:
        """Cancel a pending verification read (unload, shutdown, or a newer write)."""
        if self._verify_cancel is not None:
            self._verify_cancel()
            self._verify_cancel = None

    async def async_shutdown(self) -> None:
        """Cancel the verification timer along with the coordinator's own scheduling."""
        self._closed = True
        self.cancel_verify()
        await super().async_shutdown()
