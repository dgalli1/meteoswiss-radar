"""DataUpdateCoordinator that polls the MeteoSwiss endpoint.

The coordinator owns the aiohttp session, fetches the manifest + per-step
JSON, runs the RLE decoder + point-in-polygon lookup, and exposes the
result to the sensor and binary_sensor platforms.

Why a coordinator
-----------------
Home Assistant's DataUpdateCoordinator gives us:
  * A single shared aiohttp session (one TCP connection pool).
  * Bounded concurrent fetches (via ``max_concurrent_requests``).
  * Retry/backoff on transient failures without us hand-rolling it.
  * A single point that fires ``async_set_updated_data`` whenever new
    data is available — every entity listens to the same event.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_FORECAST_HOURS,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .decoder import ColorIntensity
from .predictor import (
    Location,
    predict_at_point,
    summarize_forecast,
)

_LOGGER = logging.getLogger(__name__)


class MeteoSwissCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches + decodes + summarises MeteoSwiss data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        location: Location,
        max_concurrent: int = 4,
    ) -> None:
        self.entry = entry
        self.location = location
        self._sem = asyncio.Semaphore(max_concurrent)
        self._client = None  # lazy: built in async_setup

        # Read user-tunable config (or fall back to defaults)
        opts = entry.options
        self._history_h = int(opts.get("history_hours", DEFAULT_HISTORY_HOURS))
        self._forecast_h = int(opts.get("forecast_hours", DEFAULT_FORECAST_HOURS))
        self._scan_interval = opts.get("scan_interval", DEFAULT_SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{location.name.lower()}",
            update_interval=self._scan_interval,
        )

    # -- public API --------------------------------------------------------

    @property
    def timeline(self) -> dict[int, ColorIntensity | None]:
        """The most recent fetch: ``{epoch: intensity}`` for past+future."""
        return self.data.get("timeline", {}) if self.data else {}

    @property
    def current(self) -> ColorIntensity | None:
        return self.data.get("current") if self.data else None

    @property
    def summary(self) -> dict[str, Any]:
        return self.data.get("summary", {}) if self.data else {}

    # -- internals ---------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch + decode the timeline. Called by the coordinator framework."""
        from .client import MeteoSwissClient, MeteoSwissError  # local: avoid HA cost on import

        if self._client is None:
            session = async_get_clientsession(self.hass)
            self._client = MeteoSwissClient(session=session, max_concurrent=4)

        now = int(datetime.now(timezone.utc).timestamp())
        try:
            async with async_timeout.timeout(30):
                # 1) Fetch the latest animation manifest
                timeline = await self._client.fetch_animation_manifest()
                # 2) Fetch every step we care about (history + forecast window)
                wanted = []
                for pic in timeline.past:
                    if now - pic.timestamp <= self._history_h * 3600:
                        wanted.append(pic)
                for pic in timeline.future:
                    if pic.timestamp - now <= self._forecast_h * 3600:
                        wanted.append(pic)

                # Concurrent fan-out (semaphore-bounded inside client)
                results: dict[int, ColorIntensity | None] = {}
                if wanted:
                    payloads = await asyncio.gather(
                        *(self._safe_fetch(pic) for pic in wanted),
                        return_exceptions=False,
                    )
                    for pic, payload in zip(wanted, payloads):
                        if payload is None:
                            continue
                        results[pic.timestamp] = predict_at_point(self.location, payload)
        except (MeteoSwissError, asyncio.TimeoutError) as exc:
            raise UpdateFailed(f"MeteoSwiss fetch failed: {exc}") from exc

        # Pick the "current" reading: latest past <= now, or the now-snapshot
        past_keys = sorted(t for t in results if t <= now)
        first_future = min((t for t in results if t >= now), default=None)
        current = results[past_keys[-1]] if past_keys else None
        if first_future == now and now in results:
            current = results[now]

        summary = summarize_forecast(sorted(results.items()), now=now)
        return {
            "now": now,
            "timeline": results,
            "current": current,
            "summary": summary,
        }

    async def _safe_fetch(self, pic) -> Optional[dict]:
        """Wrap the per-step fetch in the semaphore + error handling."""
        from .client import MeteoSwissClient  # local import

        try:
            async with self._sem:
                return await self._client.fetch_radar_step(pic.url)
        except MeteoSwissClient.MeteoSwissError as exc:
            _LOGGER.warning("Step fetch failed %s: %s", pic.url, exc)
            return None
