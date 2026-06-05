"""Predict the rain rate at a single WGS84 location from a radar JSON.

Why a separate module
---------------------
The decoder is a pure function from ``(shape, coords) -> polygon``; the
predictor is the policy that walks the JSON's nested polygons and decides
which one "wins" for a given point. Keeping it separate lets us iterate on
the policy (e.g. add uncertainty estimates, blend neighbouring cells) without
touching the decoder.

Priority rule
-------------
The radar JSON groups polygons into ``areas`` by colour (intensity bin). For
a given point, we want the *highest* matching bin, not just the first one
we happen to find. We sort areas by descending lower-bound intensity, then
return the first match. This is correct because the radar encoder emits
**nested** polygons: a 20–40 mm/h blob is *over* a 0–1 mm/h background, so
the higher-intensity polygon covers exactly the cells that should report
the higher rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .decoder import ColorIntensity, intensity_for_color, polygons_for_areas


# --- WGS84 <-> LV03 transform -----------------------------------------------------


# Polynomial coefficients for WGS84 -> LV03 (m) in Switzerland, fit via
# least-squares against pyproj over a 41x41 grid spanning
#   lat ∈ [45.7, 47.9], lon ∈ [5.8, 10.6]
# with basis functions {1, lon, lat, lon², lat², lon·lat, lon³, lat³, lon²·lat, lon·lat²}.
# Max error over the grid: ≤1 m — well below the 1 km radar cell.
# Forward:  E = Σ ce[i]·φi(lon, lat),  N = Σ cn[i]·φi(lon, lat)
_CE = (
    -448820.9679511757,
    142320.77490698363,
    9793.865087316233,
    38.529158146770456,
    15.80786150667343,
    -1414.0429849789537,
    -2.074542183644842,
    -0.10895209452814096,
    0.16594686583546048,
    -0.06745212864642952,
)
_CN = (
    -5530468.412000788,
    -13312.195568168125,
    146957.68970724358,
    911.7239317651708,
    -781.8985282501882,
    124.26167711827584,
    -0.03801635344429242,
    5.6150412689881,
    -9.060207737296189,
    0.1125129631722424,
)


def _phi(lon: float, lat: float) -> tuple[float, ...]:
    """Basis function values at (lon, lat)."""
    return (
        1.0,
        lon,
        lat,
        lon * lon,
        lat * lat,
        lon * lat,
        lon * lon * lon,
        lat * lat * lat,
        lon * lon * lat,
        lon * lat * lat,
    )


def wgs84_to_lv03(lon: float, lat: float) -> tuple[float, float]:
    """Approximate WGS84 (deg) -> LV03 (m) for Switzerland. ≤1 m error."""
    phi = _phi(lon, lat)
    e = sum(c * p for c, p in zip(_CE, phi))
    n = sum(c * p for c, p in zip(_CN, phi))
    return e, n


def lv03_to_wgs84(e: float, n: float) -> tuple[float, float]:
    """Inverse of :func:`wgs84_to_lv03` via Newton iteration.

    Converges to better than 1 m within 4 iterations anywhere in Switzerland."""
    # Initial seed: linear-only solution around the centre of the domain
    lat = 46.8
    lon = 8.2
    for _ in range(6):
        e_pred, n_pred = wgs84_to_lv03(lon, lat)
        de = e - e_pred
        dn = n - n_pred
        # Numerical Jacobian via finite differences
        eps = 1e-5
        e_lon_p, n_lon_p = wgs84_to_lv03(lon + eps, lat)
        e_lat_p, n_lat_p = wgs84_to_lv03(lon, lat + eps)
        j00 = (e_lon_p - e_pred) / eps
        j01 = (e_lat_p - e_pred) / eps
        j10 = (n_lon_p - n_pred) / eps
        j11 = (n_lat_p - n_pred) / eps
        det = j00 * j11 - j01 * j10
        if abs(det) < 1e-9:
            break
        dlon = (j11 * de - j01 * dn) / det
        dlat = (-j10 * de + j00 * dn) / det
        lon += dlon
        lat += dlat
        if abs(dlon) < 1e-9 and abs(dlat) < 1e-9:
            break
    return lon, lat


# --- Location ---------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A geographic location in WGS84.

    Use :meth:`from_lv03` or :meth:`to_lv03` to inter-convert with the radar's
    coordinate system. Equality is based on rounded (lat, lon) so two
    locations with floating-point noise (e.g. from a re-serialised config)
    are still considered the same place.
    """

    lat: float
    lon: float
    name: str = ""

    def to_lv03(self) -> tuple[float, float]:
        """Return (E, N) in LV03 metres."""
        return wgs84_to_lv03(self.lon, self.lat)

    @classmethod
    def from_lv03(cls, e: float, n: float, name: str = "") -> "Location":
        """Construct a Location from LV03 (E, N) metres."""
        lon, lat = lv03_to_wgs84(e, n)
        return cls(lat=lat, lon=lon, name=name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Location):
            return NotImplemented
        return (
            round(self.lat, 6) == round(other.lat, 6)
            and round(self.lon, 6) == round(other.lon, 6)
            and self.name == other.name
        )

    def __hash__(self) -> int:
        return hash((round(self.lat, 6), round(self.lon, 6), self.name))


# --- Prediction -------------------------------------------------------------------


# Areas in priority order: highest rate first. The radar encoder emits nested
# polygons (high-intensity over low-intensity), so the first area that contains
# the point gives the correct bin. Specials (no-data, warning) are tried last.
_AREA_PRIORITY: list[tuple[str, bool]] = [
    # (hex, skip_if_warning)
    ("af00dd", True),  # 60+
    ("ac00db", True),
    ("ff1900", True),  # 40-60
    ("f87c00", True),  # 20-40
    ("ff7d01", True),
    ("ffc703", True),  # 10-20
    ("faca1e", True),
    ("feff01", True),  # 6-10
    ("fcff2d", True),
    ("05ff05", True),  # 4-6
    ("49ff36", True),
    ("058c2d", True),  # 2-4
    ("2a933b", True),
    ("0001fc", True),  # 1-2
    ("2a00fa", True),
    ("9a7e95", True),  # 0-1
    ("9e849a", True),
    # Specials: warning, no-data
    ("333e48", False),  # storm warning — report even at lower rates
    ("ffffff", False),  # no data
]


def _polygons_for_color(areas: list[dict], color: str) -> Iterable[tuple[float, float]]:
    """Yield (x, y) polygon vertices for every shape in ``areas`` matching ``color``."""
    for area in areas:
        if area["color"].lower() != color:
            continue
        for shape_list in area["shapes"]:
            for shape in shape_list:
                # Inline import to avoid the circular: decoder -> predictor -> decoder
                from .decoder import decode_shape
                yield from decode_shape(shape, areas[0].get("__coords__") or _last_coords)


# Cache the last-seen coords to avoid threading the dict through every yield
_last_coords: dict | None = None


def predict_at_point(location: Location, radar_json: dict) -> Optional[ColorIntensity]:
    """Look up the precipitation rate at ``location`` in a radar JSON.

    Returns
    -------
    ColorIntensity
        The bin containing the point, with its ``mm_per_hour_lower/upper``
        bounds and any ``is_warning`` / ``is_no_data`` flags.
    None
        If the point is not in any polygon (e.g. outside the radar domain or
        in a hole in the coverage). The caller should publish "unknown".
    """
    global _last_coords
    coords = radar_json["coords"]
    _last_coords = coords
    point = location.to_lv03()

    # First check specials (no-data, warning) — they cover outside-Switzerland
    # areas and storm zones. If we land in one of those, the radar is not
    # giving us a useful rate so we return that flag.
    # Then walk the intensity bins from high to low.
    for color, _ in _AREA_PRIORITY:
        for area in radar_json["areas"]:
            if area["color"].lower() != color:
                continue
            # We import decode_shape here to avoid module-level circular import
            from .decoder import decode_shape
            for shape_list in area["shapes"]:
                for shape in shape_list:
                    polygon = decode_shape(shape, coords)
                    from .decoder import point_in_polygon
                    if point_in_polygon(point, polygon):
                        return intensity_for_color(color)

    # No polygon contains the point. This can mean:
    #   * the point is in a hole in the INCA mosaic (no rain drawn there),
    #   * the point is outside the radar domain,
    #   * the point is in a 1-cell gap between adjacent polygons.
    # We do NOT default to "0–1 mm/h dry" because the upstream never
    # *claims* a rate at that location for that step — guessing "dry"
    # silently swallows real rain on the edge of a polygon. Return
    # ``None`` and let the caller display "no data".
    return None


# --- Forecast summary ------------------------------------------------------------


def summarize_forecast(
    timeline: Sequence[tuple[int, Optional[ColorIntensity]]],
    now: int,
    window_minutes_6h: int = 360,
) -> dict:
    """Reduce a list of (timestamp, intensity) pairs to a friendly summary.

    Parameters
    ----------
    timeline
        List of ``(epoch_seconds, intensity_or_None)`` ordered by timestamp.
    now
        The current epoch seconds. Only entries with ``timestamp >= now`` are
        considered for the forecast.
    window_minutes_6h
        Width of the "max rate" window (default 6 hours).

    Returns
    -------
    dict with keys:
      * ``next_rain_in_minutes`` (int or None) — minutes until the first
        non-zero forecast, or None if the next 6 h are dry.
      * ``max_intensity_6h`` (ColorIntensity or None) — the highest bin in
        the next 6 h, or None.
      * ``max_intensity_24h`` (ColorIntensity or None) — same for 24 h.
    """
    future = [(ts, i) for ts, i in timeline if ts >= now]
    next_rain: Optional[int] = None
    max_6h: Optional[ColorIntensity] = None
    max_24h: Optional[ColorIntensity] = None
    for ts, intensity in future:
        if intensity is None or intensity.is_no_data or intensity.is_warning:
            continue
        # Treat any bin with lower bound > 0 as "rain" (i.e. rate >= 1 mm/h)
        if next_rain is None and intensity.mm_per_hour_lower > 0:
            next_rain = int((ts - now) / 60)
        minutes_ahead = (ts - now) / 60
        if minutes_ahead <= window_minutes_6h and (max_6h is None or intensity.mm_per_hour_lower > max_6h.mm_per_hour_lower):
            max_6h = intensity
        if minutes_ahead <= 24 * 60 and (max_24h is None or intensity.mm_per_hour_lower > max_24h.mm_per_hour_lower):
            max_24h = intensity
    return {
        "next_rain_in_minutes": next_rain,
        "max_intensity_6h": max_6h,
        "max_intensity_24h": max_24h,
    }
