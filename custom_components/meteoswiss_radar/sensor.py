"""Sensor platform: current rate, state, next-rain, forecast max, timeline."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MeteoSwissCoordinator
from .decoder import ColorIntensity

_LOGGER = logging.getLogger(__name__)

_PRECIPITATION_INTENSITY_UNIT = "mm/h"


# Map a 1-2 letter "type code" to a function that pulls the value from the
# coordinator. Keeping them as plain callables avoids a giant if-ladder.
_VALUE_GETTERS = {
    "current_rate":     lambda c: _safe_rate(c.current),
    "current_state":    lambda c: _bin_label(c.current),
    "next_rain":        lambda c: _next_rain_minutes(c.summary),
    "forecast_max_6h":  lambda c: _max_rate(c.summary.get("max_intensity_6h")),
    "forecast_max_24h": lambda c: _max_rate(c.summary.get("max_intensity_24h")),
    "timeline":         lambda c: len(c.timeline),
}

_DESCRIPTIONS = {
    "current_rate":     SensorEntityDescription(
        key="current_rate", name="Current rate",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=_PRECIPITATION_INTENSITY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "current_state":    SensorEntityDescription(
        key="current_state", name="Current intensity bin",
    ),
    "next_rain":        SensorEntityDescription(
        key="next_rain", name="Next rain in",
        native_unit_of_measurement="min",
    ),
    "forecast_max_6h":  SensorEntityDescription(
        key="forecast_max_6h", name="Forecast max 6h",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=_PRECIPITATION_INTENSITY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "forecast_max_24h": SensorEntityDescription(
        key="forecast_max_24h", name="Forecast max 24h",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=_PRECIPITATION_INTENSITY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "timeline":         SensorEntityDescription(
        key="timeline", name="Timeline steps",
        native_unit_of_measurement="steps",
        state_class=SensorStateClass.TOTAL,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one sensor per description, all sharing the entry's coordinator."""
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: MeteoSwissCoordinator = store["coordinator"]
    entities = [
        MeteoSwissSensor(coordinator, entry, _DESCRIPTIONS[key])
        for key in _DESCRIPTIONS
    ]
    async_add_entities(entities)


class MeteoSwissSensor(CoordinatorEntity[MeteoSwissCoordinator], SensorEntity):
    """One of the per-location sensor entities."""

    _attr_has_entity_name = True  # sensor name gets a prefix from the device

    def __init__(
        self,
        coordinator: MeteoSwissCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model="MeteoSwiss radar composite (RZC + INCA)",
            name=f"MeteoSwiss radar @ {coordinator.location.name}",
            configuration_url="https://www.meteoschweiz.admin.ch/service-und-publikationen/applikationen/niederschlag.html",
        )

    @property
    def native_value(self) -> Any:
        getter = _VALUE_GETTERS[self.entity_description.key]
        return getter(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-sensor attributes.

        The ``timeline`` sensor carries the full ``{ts: {ts, rate, bin}}`` map
        so the Lovelace card can render the bar chart. The others carry
        the bin label and the current colour so the card / template
        sensors can render swatches.
        """
        coord = self.coordinator
        attrs: dict[str, Any] = {
            "location": f"{coord.location.lat:.4f}, {coord.location.lon:.4f}",
            "last_refresh": coord.data.get("now") if coord.data else None,
        }
        key = self.entity_description.key
        if key == "current_rate":
            attrs["bin_label"] = _bin_label(coord.current)
            attrs["colour"] = _colour(coord.current)
        elif key == "current_state":
            attrs["colour"] = _colour(coord.current)
        elif key == "timeline":
            # Full timeline: {ts: {ts, rate, bin}} for the dashboard card.
            attrs["timeline"] = {
                str(ts): {
                    "ts": ts,
                    "rate": _safe_rate(intensity),
                    "bin": _bin_label(intensity),
                }
                for ts, intensity in sorted(coord.timeline.items())
            }
            attrs["history_hours"] = coord._history_h
            attrs["forecast_hours"] = coord._forecast_h
        elif key in ("forecast_max_6h", "forecast_max_24h"):
            intensity = coord.summary.get(key.replace("forecast_", "max_intensity_"))
            attrs["bin_label"] = _bin_label(intensity)
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        # Default behaviour is fine; this is just a hook point for debugging.
        super()._handle_coordinator_update()


# --- Value helpers ----------------------------------------------------------------


def _safe_rate(intensity: Optional[ColorIntensity]) -> Any:
    if intensity is None:
        return "unknown"
    if intensity.is_no_data:
        return "unknown"
    if intensity.is_warning:
        return 0.0
    return round(intensity.representative_mm_per_hour, 2)


def _bin_label(intensity: Optional[ColorIntensity]) -> str:
    if intensity is None:
        return "no data"
    if intensity.is_no_data:
        return "no data"
    if intensity.is_warning:
        return "storm warning"
    upper = (
        f"{int(intensity.mm_per_hour_upper)}"
        if intensity.mm_per_hour_upper is not None
        else "+"
    )
    return f"{int(intensity.mm_per_hour_lower)}–{upper} mm/h"


def _colour(intensity: Optional[ColorIntensity]) -> str:
    if intensity is None or intensity.is_no_data:
        return "#888888"
    if intensity.is_warning:
        return "#333e48"
    return {
        0.0: "#9a7e95",
        1.0: "#0001fc",
        2.0: "#058c2d",
        4.0: "#05ff05",
        6.0: "#feff01",
        10.0: "#ffc703",
        20.0: "#ff7d01",
        40.0: "#ff1900",
        60.0: "#af00dd",
    }.get(intensity.mm_per_hour_lower, "#888888")


def _next_rain_minutes(summary: dict) -> Any:
    n = summary.get("next_rain_in_minutes")
    return n if n is not None else "unknown"


def _max_rate(intensity: Optional[ColorIntensity]) -> float:
    if intensity is None:
        return 0.0
    if intensity.is_no_data or intensity.is_warning:
        return 0.0
    return round(intensity.representative_mm_per_hour, 2)
