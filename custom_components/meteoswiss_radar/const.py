"""Constants for the MeteoSwiss rain radar integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "meteoswiss_radar"
MANUFACTURER = "MeteoSwiss (data) / damian (integration)"
DEFAULT_NAME = "MeteoSwiss rain radar"

# Platforms we register
PLATFORMS = ["sensor", "binary_sensor"]

# How often we poll the upstream
DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)

# How much history / forecast we keep in the timeline entity
DEFAULT_HISTORY_HOURS = 6
DEFAULT_FORECAST_HOURS = 24

# Storage keys
DATA_COORDINATOR = "coordinator"
DATA_TIMELINE = "timeline"
