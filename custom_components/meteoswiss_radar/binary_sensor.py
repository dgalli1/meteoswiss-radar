"""Binary sensor: is it raining at the configured location right now?"""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MeteoSwissCoordinator

_LOGGER = logging.getLogger(__name__)


_DESCRIPTION = BinarySensorEntityDescription(
    key="is_raining",
    name="Is it raining?",
    device_class=BinarySensorDeviceClass.MOISTURE,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: MeteoSwissCoordinator = store["coordinator"]
    async_add_entities([MeteoSwissIsRainingBinarySensor(coordinator, entry)])


class MeteoSwissIsRainingBinarySensor(
    CoordinatorEntity[MeteoSwissCoordinator], BinarySensorEntity
):
    """On when the current rate is at least 1 mm/h (or a warning zone is active)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeteoSwissCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entity_description = _DESCRIPTION
        self._attr_unique_id = f"{entry.entry_id}_is_raining"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model="MeteoSwiss radar composite (RZC + INCA)",
            name=f"MeteoSwiss radar @ {coordinator.location.name}",
        )

    @property
    def is_on(self) -> bool:
        current = self.coordinator.current
        if current is None:
            return False
        if current.is_no_data or current.is_warning:
            return False
        return current.mm_per_hour_lower >= 1.0
