"""Config flow for the MeteoSwiss rain radar integration.

The flow is intentionally tiny: a single screen asks for the WGS84
coordinates and the refresh interval. No OAuth, no account — the
upstream is public.

We keep the user's location in the ``ConfigEntry.data`` dict under
``{"lat": float, "lon": float, "name": str}``. The optional
``ConfigEntry.options`` dict carries tunables (refresh interval,
history/forecast hours).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    DEFAULT_FORECAST_HOURS,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required("name", default=defaults.get("name", "Romanshorn")): str,
            vol.Required("lat", default=defaults.get("lat", 47.5656)): cv.latitude,
            vol.Required("lon", default=defaults.get("lon", 9.3788)): cv.longitude,
        }
    )


def _options_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                "scan_interval_minutes",
                default=defaults.get("scan_interval_minutes", 5),
            ): vol.All(int, vol.Range(min=1, max=60)),
            vol.Optional(
                "history_hours",
                default=defaults.get("history_hours", DEFAULT_HISTORY_HOURS),
            ): vol.All(int, vol.Range(min=1, max=48)),
            vol.Optional(
                "forecast_hours",
                default=defaults.get("forecast_hours", DEFAULT_FORECAST_HOURS),
            ): vol.All(int, vol.Range(min=1, max=48)),
        }
    )


class MeteoSwissConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the user-facing config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Use lat/lon as the unique id (so two entries for the same place
            # are de-duplicated; the user can adjust the name in options).
            unique_id = f"{user_input['lat']:.4f},{user_input['lon']:.4f}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get("name") or DEFAULT_NAME,
                data=user_input,
            )
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return MeteoSwissOptionsFlow(entry)


class MeteoSwissOptionsFlow(config_entries.OptionsFlow):
    """Options flow: refresh interval, history window, forecast window."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {
            "scan_interval_minutes": int(
                self.entry.options.get("scan_interval_minutes", 5)
            ),
            "history_hours": int(self.entry.options.get("history_hours", DEFAULT_HISTORY_HOURS)),
            "forecast_hours": int(
                self.entry.options.get("forecast_hours", DEFAULT_FORECAST_HOURS)
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current),
        )
