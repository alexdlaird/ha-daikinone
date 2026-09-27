"""Select platform: fan circulation controls (unitary systems only).

Daikin exposes no capability field, so there is no way to know in advance whether the
equipment accepts `PUT /fan`. Both entities are therefore enabled by default and a rejected
write raises a translated error and files a repair issue instead of probing at start-up.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import Entity

from .api import FanCirculate, FanCirculateSpeed, ThermostatState
from .entity import DaikinOneEntity, async_setup_platform_entities

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import DaikinOneConfigEntry
    from .coordinator import DaikinOneCoordinator

# Writes must not overlap with the other control platforms.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DaikinOneSelectDescription(SelectEntityDescription):
    """A select plus how to read the current option and how to write a new one."""

    current_fn: Callable[[ThermostatState], str | None]
    select_fn: Callable[[DaikinOneCoordinator, str, str, Context | None], Coroutine[Any, Any, None]]


def _option(value: FanCirculate | FanCirculateSpeed | None) -> str | None:
    """Enum member to its lowercase option name; unknown values have no option."""
    if value is None or value.value < 0:
        return None
    return value.name.lower()


SELECTS: Final[tuple[DaikinOneSelectDescription, ...]] = (
    DaikinOneSelectDescription(
        key="fan_circulate",
        translation_key="fan_circulate",
        entity_category=EntityCategory.CONFIG,
        options=["off", "always_on", "schedule"],
        current_fn=lambda state: _option(state.fan_circulate),
        select_fn=lambda coordinator, thermostat_id, option, context: coordinator.async_set_fan(
            thermostat_id, circulate=FanCirculate[option.upper()], context=context
        ),
    ),
    DaikinOneSelectDescription(
        key="fan_speed",
        translation_key="fan_speed",
        entity_category=EntityCategory.CONFIG,
        options=["low", "medium", "high"],
        current_fn=lambda state: _option(state.fan_circulate_speed),
        select_fn=lambda coordinator, thermostat_id, option, context: coordinator.async_set_fan(
            thermostat_id, speed=FanCirculateSpeed[option.upper()], context=context
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the fan selects of every thermostat, now and as new ones appear."""
    coordinator = entry.runtime_data

    def _factory(thermostat_id: str) -> list[Entity]:
        return [DaikinOneSelect(coordinator, thermostat_id, description) for description in SELECTS]

    entry.async_on_unload(async_setup_platform_entities(coordinator, async_add_entities, _factory))


class DaikinOneSelect(DaikinOneEntity, SelectEntity):
    """One fan circulation setting of a thermostat."""

    entity_description: DaikinOneSelectDescription

    @property
    def current_option(self) -> str | None:
        """The setting the thermostat reports, or None when it is not understood."""
        return self.entity_description.current_fn(self.thermostat.state)

    async def async_select_option(self, option: str) -> None:
        """Write the setting; the coordinator carries the other field over."""
        await self.entity_description.select_fn(self.coordinator, self._thermostat_id, option, self._context)
