"""Switch platform support."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Awaitable

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.clevererspa.clevererspa import CleverSpaApi, CleverSpaDeviceStatus

from . import CleverSpaUpdateCoordinator
from .const import DOMAIN
from .entity import CleverSpaEntity


@dataclass
class RequiredKeysMixin:
    """Mixin for required keys."""

    value_fn: Callable[[CleverSpaDeviceStatus], bool]
    turn_on_fn: Callable[[CleverSpaApi, str], Awaitable[None]]
    turn_off_fn: Callable[[CleverSpaApi, str], Awaitable[None]]


@dataclass
class CleverSpaSwitchEntityDescription(SwitchEntityDescription, RequiredKeysMixin):
    """Entity description for CleverSpa switches."""


_SENSOR_TYPES = [
    CleverSpaSwitchEntityDescription(
        key="filter_power",
        name="Spa Filter",
        icon="mdi:air-filter",
        value_fn=lambda s: s.filter_power,
        turn_on_fn=lambda api, device_id: api.set_filter(device_id, True),
        turn_off_fn=lambda api, device_id: api.set_filter(device_id, False),
    ),
    CleverSpaSwitchEntityDescription(
        key="bubble_power",
        name="Spa Bubbles",
        icon="mdi:chart-bubble",
        value_fn=lambda s: s.bubble_power,
        turn_on_fn=lambda api, device_id: api.set_bubbles(device_id, True),
        turn_off_fn=lambda api, device_id: api.set_bubbles(device_id, False),
    ),
    #CleverSpaSwitchEntityDescription(
    #    key="wave_locked",
    #    name="Spa Locked",
    #    icon="mdi:lock",
    #    value_fn=lambda s: s.locked,
    #    turn_on_fn=lambda api, device_id: api.set_locked(device_id, True),
    #    turn_off_fn=lambda api, device_id: api.set_locked(device_id, False),
    #),

]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: CleverSpaUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = [
        CleverSpaSwitch(coordinator, config_entry, device_id, description)
        for device_id in coordinator.data.keys()
        for description in _SENSOR_TYPES
    ]
    async_add_entities(entities)


class CleverSpaSwitch(CleverSpaEntity, SwitchEntity):
    """CleverSpa switch entity."""

    entity_description: CleverSpaSwitchEntityDescription

    def __init__(
        self,
        coordinator: CleverSpaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        description: CleverSpaSwitchEntityDescription,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if not self.device_status:
            return None

        return self.entity_description.value_fn(self.device_status)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.entity_description.turn_on_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.entity_description.turn_off_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_refresh()