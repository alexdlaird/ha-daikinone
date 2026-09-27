"""Sensor platform: the measurements the official API exposes for a thermostat."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfRatio, UnitOfTemperature
from homeassistant.helpers.entity import Entity

from .api import SystemFan, ThermostatState
from .const import CONF_UID_SCHEMA
from .entity import DaikinOneEntity, async_setup_platform_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from . import DaikinOneConfigEntry
    from .coordinator import DaikinOneCoordinator

# Read-only entities driven entirely by the coordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DaikinOneSensorDescription(SensorEntityDescription):
    """A sensor plus how to read it and the unique id ha-daikinone gave it."""

    value_fn: Callable[[ThermostatState], StateType]
    #: Historic (schema 0) unique-id suffix; keeps entities of migrated installs intact.
    legacy_name: str | None = None


def _system_fan(state: ThermostatState) -> StateType:
    """The read-only system fan setting as an enum option."""
    if state.fan is None or state.fan is SystemFan.UNKNOWN:
        return None
    return state.fan.name.lower()


LAST_INTENDED_WRITE: Final = SensorEntityDescription(
    key="last_intended_write",
    translation_key="last_intended_write",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SENSORS: Final[tuple[DaikinOneSensorDescription, ...]] = (
    DaikinOneSensorDescription(
        key="indoor_temperature",
        translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: state.temp_indoor,
        legacy_name="Indoor Temperature",
    ),
    DaikinOneSensorDescription(
        key="indoor_humidity",
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        value_fn=lambda state: state.hum_indoor,
        legacy_name="Indoor Humidity",
    ),
    DaikinOneSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: state.temp_outdoor,
        legacy_name="Outdoor Temperature",
    ),
    DaikinOneSensorDescription(
        key="outdoor_humidity",
        translation_key="outdoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        value_fn=lambda state: state.hum_outdoor,
        legacy_name="Outdoor Humidity",
    ),
    DaikinOneSensorDescription(
        key="system_fan",
        translation_key="system_fan",
        device_class=SensorDeviceClass.ENUM,
        options=["auto", "on"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_system_fan,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of every thermostat, now and as new ones appear."""
    coordinator = entry.runtime_data
    legacy_ids = entry.data.get(CONF_UID_SCHEMA) == 0

    def _factory(thermostat_id: str) -> list[Entity]:
        return [
            *(
                DaikinOneSensor(coordinator, thermostat_id, description, legacy_ids=legacy_ids)
                for description in SENSORS
            ),
            DaikinOneLastIntendedWriteSensor(coordinator, thermostat_id, LAST_INTENDED_WRITE),
        ]

    entry.async_on_unload(async_setup_platform_entities(coordinator, async_add_entities, _factory))


class DaikinOneSensor(DaikinOneEntity, SensorEntity):
    """One measurement of a thermostat."""

    entity_description: DaikinOneSensorDescription

    def __init__(
        self,
        coordinator: DaikinOneCoordinator,
        thermostat_id: str,
        description: DaikinOneSensorDescription,
        *,
        legacy_ids: bool = False,
    ) -> None:
        """Use the historic unique id when the entry came from ha-daikinone schema 0."""
        super().__init__(coordinator, thermostat_id, description)
        if legacy_ids and description.legacy_name is not None:
            self._attr_unique_id = f"{thermostat_id}-{description.legacy_name}"

    @property
    def native_value(self) -> StateType:
        """The measurement, or None while the thermostat has not reported it."""
        return self.entity_description.value_fn(self.thermostat.state)


class DaikinOneLastIntendedWriteSensor(DaikinOneEntity, SensorEntity):
    """When read-only mode last kept a write from Daikin, with that write as attributes."""

    @property
    def native_value(self) -> datetime | None:
        """Time of the most recent suppressed write (None until one happens)."""
        writes = self.coordinator.suppressed_writes.get(self._thermostat_id)
        return max((write.at for write in writes.values()), default=None) if writes else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Whether read-only is on, plus the latest would-be request per endpoint and who asked."""
        writes = self.coordinator.suppressed_writes.get(self._thermostat_id) or {}
        attributes: dict[str, Any] = {"read_only": self.coordinator.read_only}
        for endpoint, write in sorted(writes.items()):
            attributes[endpoint] = {
                **write.payload,
                "at": write.at.isoformat(),
                "user_id": write.context.user_id if write.context else None,
            }
        return attributes
