"""Switch platform: the thermostat's own schedule."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.helpers.entity import Entity

from .entity import DaikinOneEntity, async_setup_platform_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import DaikinOneConfigEntry

# Writes must not overlap with the other control platforms.
PARALLEL_UPDATES = 1

SCHEDULE: Final = SwitchEntityDescription(key="schedule", translation_key="schedule")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the schedule switch of every thermostat, now and as new ones appear."""
    coordinator = entry.runtime_data

    def _factory(thermostat_id: str) -> list[Entity]:
        return [DaikinOneScheduleSwitch(coordinator, thermostat_id, SCHEDULE)]

    entry.async_on_unload(async_setup_platform_entities(coordinator, async_add_entities, _factory))


class DaikinOneScheduleSwitch(DaikinOneEntity, SwitchEntity):
    """Turns the thermostat's built-in schedule on and off.

    Any setpoint or mode write turns the schedule off at the thermostat, so this entity also
    reflects that side effect as soon as the coordinator applies it.
    """

    @property
    def is_on(self) -> bool | None:
        """Whether the thermostat is following its schedule."""
        return self.thermostat.state.schedule_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let the thermostat follow its schedule again."""
        await self.coordinator.async_set_schedule_enabled(self._thermostat_id, True, context=self._context)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hold the current setpoints instead of following the schedule."""
        await self.coordinator.async_set_schedule_enabled(self._thermostat_id, False, context=self._context)
