# MeteoSwiss rain radar

Reimplements the [Niederschlag (Radar)](https://www.meteoschweiz.admin.ch/service-und-publikationen/applikationen/niederschlag.html) data pipeline in Python so the page's precipitation forecast shows up as Home Assistant sensors.

## What you get

For each configured location, the integration creates:

| Entity | Description |
|---|---|
| `sensor.<slug>_current_rate` | Instantaneous rate (mm/h) |
| `sensor.<slug>_current_state` | Intensity bin (e.g. "0–1 mm/h", "10–20 mm/h") |
| `sensor.<slug>_next_rain` | Minutes until the next non-zero forecast |
| `sensor.<slug>_forecast_max_6h` | Max predicted rate in the next 6 h |
| `sensor.<slug>_forecast_max_24h` | Max predicted rate in the next 24 h |
| `sensor.<slug>_timeline` | Full timeline (attribute); the card reads this |
| `binary_sensor.<slug>_is_raining` | On when current rate ≥ 1 mm/h |

## Setup

1. HACS → Frontend → ⋮ → Custom repositories → add this repo (type: **Integration**).
2. Restart Home Assistant.
3. Settings → Devices & Services → ⋮ → Add Integration → "MeteoSwiss rain radar".
4. Enter your location's name + WGS84 lat/lon. Done.

The card lives in a separate repository — see the project README for the dashboard card.

## Configuration

* **Scan interval** (default 5 min) — how often to fetch. The upstream updates every 5 min.
* **History window** (default 6 h) — how much past data to keep in the timeline sensor.
* **Forecast window** (default 24 h) — same for forecast.

## Pairing with a physical rain sensor

The radar is a 1 km × 1 km grid; a Tuya rain sensor on the windowsill is a great
ground-truth. See the project README for a template sensor that cross-checks
the two and an automation that flags divergence.
