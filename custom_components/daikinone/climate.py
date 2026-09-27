"""Climate platform: one thermostat entity per Daikin One device.

All validation of setpoints (Daikin's delta rule and the thermostat's own range) lives in
the coordinator, which owns the per-device lock and the local snapshot every write is built
from. This module only translates between Home Assistant's climate vocabulary and the
documented API enums.

Daikin has no fan-only mode, and fan circulation is set independently of the mode. The two
"off" modes are therefore composed from mode + circulation: `OFF` is mode off with
circulation off, `FAN_ONLY` is mode off with circulation always on. Entering either from an
active mode remembers the circulation setting (persisted across restarts) and leaving them
puts it back, so an always-on or scheduled circulation preference survives an off period.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .api import EquipmentStatus, FanCirculate, Mode, ModeLimit, ThermostatState
from .const import DOMAIN, PRESET_EMERGENCY_HEAT
from .entity import DaikinOneEntity, async_setup_platform_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import DaikinOneConfigEntry
    from .coordinator import DaikinOneCoordinator

# Writes must not overlap: the coordinator builds every /msp payload from the local snapshot.
PARALLEL_UPDATES = 1

#: Emergency heat is surfaced as HEAT + a preset, matching ha-daikinone's entity contract.
HVAC_MODE_BY_MODE: Final[dict[Mode, HVACMode]] = {
    Mode.OFF: HVACMode.OFF,
    Mode.HEAT: HVACMode.HEAT,
    Mode.COOL: HVACMode.COOL,
    Mode.AUTO: HVACMode.HEAT_COOL,
    Mode.EMERGENCY_HEAT: HVACMode.HEAT,
}
MODE_BY_HVAC_MODE: Final[dict[HVACMode, Mode]] = {
    HVACMode.OFF: Mode.OFF,
    HVACMode.HEAT: Mode.HEAT,
    HVACMode.COOL: Mode.COOL,
    HVACMode.HEAT_COOL: Mode.AUTO,
}
ALL_HVAC_MODES: Final[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
#: Circulation settings worth restoring (anything the thermostat reported and we understood).
RESTORABLE_CIRCULATION: Final = (FanCirculate.OFF, FanCirculate.ALWAYS_ON, FanCirculate.SCHEDULE)
#: The composed modes that turn the thermostat's mode off.
OFF_FAMILY: Final = (HVACMode.OFF, HVACMode.FAN_ONLY)
HVAC_MODES_BY_LIMIT: Final[dict[ModeLimit, list[HVACMode]]] = {
    ModeLimit.HEAT_ONLY: [HVACMode.OFF, HVACMode.HEAT],
    ModeLimit.COOL_ONLY: [HVACMode.OFF, HVACMode.COOL],
}
HVAC_ACTION_BY_STATUS: Final[dict[EquipmentStatus, HVACAction]] = {
    EquipmentStatus.COOL: HVACAction.COOLING,
    EquipmentStatus.OVERCOOL_DEHUM: HVACAction.DRYING,
    EquipmentStatus.HEAT: HVACAction.HEATING,
    EquipmentStatus.FAN: HVACAction.FAN,
    EquipmentStatus.IDLE: HVACAction.IDLE,
}
#: Modes whose single target temperature is the heat setpoint.
HEATING_MODES: Final = (Mode.HEAT, Mode.EMERGENCY_HEAT)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a climate entity for every thermostat, now and as new ones appear."""
    coordinator = entry.runtime_data

    def _factory(thermostat_id: str) -> list[Entity]:
        return [DaikinOneClimate(coordinator, thermostat_id)]

    entry.async_on_unload(async_setup_platform_entities(coordinator, async_add_entities, _factory))


@dataclass
class CirculationBeforeOff(ExtraStoredData):
    """The fan circulation setting to restore when off or fan only ends."""

    circulate: FanCirculate | None

    def as_dict(self) -> dict[str, Any]:
        """Stored as the enum name, or None."""
        return {"circulate": None if self.circulate is None else self.circulate.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Tolerate missing or unrecognised stored values."""
        name = data.get("circulate")
        circulate = FanCirculate[name] if isinstance(name, str) and name in FanCirculate.__members__ else None
        return cls(circulate if circulate in RESTORABLE_CIRCULATION else None)


class DaikinOneClimate(DaikinOneEntity, ClimateEntity, RestoreEntity):
    """The thermostat itself: mode, action, setpoints and emergency heat."""

    _attr_name = None
    _attr_translation_key = "thermostat"
    # The API is Celsius-only; Home Assistant converts for display and for service calls.
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = PRECISION_TENTHS

    def __init__(self, coordinator: DaikinOneCoordinator, thermostat_id: str) -> None:
        """Build the entity with the unique id ha-daikinone used, so entities survive."""
        super().__init__(coordinator, thermostat_id)
        self._attr_unique_id = f"{thermostat_id}-climate"
        self._circulation_before_off: FanCirculate | None = None
        self._last_seen_mode: Mode | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the circulation setting that leaving off / fan only will hand back."""
        await super().async_added_to_hass()
        self._last_seen_mode = self._state.mode
        if (stored := await self.async_get_last_extra_data()) is not None:
            self._circulation_before_off = CirculationBeforeOff.from_dict(stored.as_dict()).circulate

    @property
    def extra_restore_state_data(self) -> CirculationBeforeOff:
        """Persist the circulation setting to restore when off / fan only ends."""
        return CirculationBeforeOff(self._circulation_before_off)

    @property
    def _state(self) -> ThermostatState:
        """The thermostat's last known state."""
        return self.thermostat.state

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Presets are only offered when the equipment has emergency heat."""
        features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        )
        if self._state.em_heat_available:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def _has_fan_circulation(self) -> bool:
        """Fan only needs a readable circulation setting (unitary systems report one)."""
        return self._state.fan_circulate not in (None, FanCirculate.UNKNOWN)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Offer only the modes the thermostat's `modeLimit` allows, plus fan only when it can circulate."""
        limit = self._state.mode_limit
        modes = ALL_HVAC_MODES if limit is None else HVAC_MODES_BY_LIMIT.get(limit, ALL_HVAC_MODES)
        return [*modes, HVACMode.FAN_ONLY] if self._has_fan_circulation else list(modes)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Forget a remembered circulation when the thermostat leaves off on its own (e.g. from the app)."""
        mode = self._state.mode
        if self._last_seen_mode is Mode.OFF and mode not in (None, Mode.OFF, Mode.UNKNOWN):
            self._circulation_before_off = None
        self._last_seen_mode = mode
        super()._handle_coordinator_update()

    @property
    def hvac_mode(self) -> HVACMode | None:
        """The current mode; unknown API values make the entity state unknown."""
        mode = self._state.mode
        if mode is Mode.OFF and self._state.fan_circulate is FanCirculate.ALWAYS_ON:
            return HVACMode.FAN_ONLY
        return None if mode is None else HVAC_MODE_BY_MODE.get(mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the equipment is doing right now."""
        status = self._state.equipment_status
        if self.hvac_mode is HVACMode.OFF and status in (EquipmentStatus.IDLE, None):
            # The API keeps reporting "idle" when the system is off; OFF is more truthful.
            return HVACAction.OFF
        return None if status is None else HVAC_ACTION_BY_STATUS.get(status)

    @property
    def preset_modes(self) -> list[str] | None:
        """Emergency heat is the only preset, and only on dual-fuel equipment."""
        if not self._state.em_heat_available:
            return None
        return [PRESET_NONE, PRESET_EMERGENCY_HEAT]

    @property
    def preset_mode(self) -> str | None:
        """`emergency_heat` while the thermostat runs in emergency heat."""
        if not self._state.em_heat_available:
            return None
        return PRESET_EMERGENCY_HEAT if self._state.mode is Mode.EMERGENCY_HEAT else PRESET_NONE

    @property
    def current_temperature(self) -> float | None:
        """Indoor temperature reported by the thermostat."""
        return self._state.temp_indoor

    @property
    def current_humidity(self) -> float | None:
        """Indoor relative humidity reported by the thermostat."""
        return self._state.hum_indoor

    @property
    def target_temperature(self) -> float | None:
        """The single setpoint that applies in the current mode (None in auto/off)."""
        mode = self._state.mode
        if mode in HEATING_MODES:
            return self._state.heat_setpoint
        if mode is Mode.COOL:
            return self._state.cool_setpoint
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Heat setpoint of the auto-mode range."""
        return self._state.heat_setpoint if self._state.mode is Mode.AUTO else None

    @property
    def target_temperature_high(self) -> float | None:
        """Cool setpoint of the auto-mode range."""
        return self._state.cool_setpoint if self._state.mode is Mode.AUTO else None

    @property
    def min_temp(self) -> float:
        """Lowest setpoint the thermostat accepts (HA's default until it reports one)."""
        minimum = self._state.setpoint_minimum
        return super().min_temp if minimum is None else minimum

    @property
    def max_temp(self) -> float:
        """Highest setpoint the thermostat accepts (HA's default until it reports one)."""
        maximum = self._state.setpoint_maximum
        return super().max_temp if maximum is None else maximum

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch mode, keeping the setpoints; off and fan only also set the circulation."""
        if hvac_mode in OFF_FAMILY:
            await self._async_enter_off_family(hvac_mode)
            return
        await self._async_write_active(MODE_BY_HVAC_MODE[hvac_mode])

    async def _async_enter_off_family(self, hvac_mode: HVACMode) -> None:
        """Remember circulation when coming from an active mode, then write only what differs.

        Fan only sets circulation before turning the mode off (a failed second write leaves the
        equipment conditioning rather than doing nothing); off turns the mode off first.
        """
        if self.hvac_mode not in OFF_FAMILY:
            current = self._state.fan_circulate
            self._circulation_before_off = current if current in RESTORABLE_CIRCULATION else None
        circulation = FanCirculate.ALWAYS_ON if hvac_mode is HVACMode.FAN_ONLY else FanCirculate.OFF
        needs_fan = self._has_fan_circulation and self._state.fan_circulate is not circulation
        needs_mode = self._state.mode is not Mode.OFF
        if needs_fan and hvac_mode is HVACMode.FAN_ONLY:
            await self._async_set_circulation(circulation)
        if needs_mode:
            await self.coordinator.async_set_mode_setpoints(self._thermostat_id, mode=Mode.OFF, context=self._context)
        if needs_fan and hvac_mode is HVACMode.OFF:
            await self._async_set_circulation(circulation)

    async def _async_write_active(
        self, mode: Mode | None, *, heat: float | None = None, cool: float | None = None
    ) -> None:
        """Write an active mode and/or setpoints, handing circulation back when leaving off / fan only."""
        restore = self._circulation_before_off if mode is not None and self.hvac_mode in OFF_FAMILY else None
        await self.coordinator.async_set_mode_setpoints(
            self._thermostat_id, mode=mode, heat=heat, cool=cool, context=self._context
        )
        if mode is not None and restore is not None and restore is not self._state.fan_circulate:
            await self._async_set_circulation(restore)
        if mode is not None:
            self._circulation_before_off = None

    async def _async_set_circulation(self, circulate: FanCirculate) -> None:
        await self.coordinator.async_set_fan(self._thermostat_id, circulate=circulate, context=self._context)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Enter or leave emergency heat (the only way to reach `Mode.EMERGENCY_HEAT`)."""
        await self._async_write_active(Mode.EMERGENCY_HEAT if preset_mode == PRESET_EMERGENCY_HEAT else Mode.HEAT)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a target temperature or range, optionally switching mode in the same write."""
        hvac_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode in OFF_FAMILY:
            # Off and fan only have no setpoint to move.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="single_setpoint_not_applicable",
            )
        mode = None if hvac_mode is None else MODE_BY_HVAC_MODE[hvac_mode]
        low: float | None = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high: float | None = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        if low is not None or high is not None:
            await self._async_write_active(mode, heat=low, cool=high)
            return

        temperature: float | None = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        effective = mode if mode is not None else self._state.mode
        if effective in HEATING_MODES:
            await self._async_write_active(mode, heat=temperature)
        elif effective is Mode.COOL:
            await self._async_write_active(mode, cool=temperature)
        else:
            # Auto needs a range and off has no setpoint to move.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="single_setpoint_not_applicable",
            )
