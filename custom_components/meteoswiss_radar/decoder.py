"""Pure-Python decoder for the MeteoSwiss radar composite JSON format.

The Niederschlag (Radar) application on https://www.meteoschweiz.admin.ch ships
two JSON products for each 5-minute time-step:

* ``radar_rzc.<TIMESTAMP>.json`` — past measurements (RZC = Radar Zentral Schweiz),
  one polygon list per intensity bin.
* ``rate_<TIMESTAMP>.json`` — INCA model forecast for the same time-step.

Both files use the same wire format, decoded here. The data describes *isohypse
polygons*: for each intensity bin (color), the radar emits the cell-blob outline
as a list of (i, j) starting coordinates plus an RLE-style walk (the ``d`` and
``o`` fields).

Reverse-engineered from the MeteoSwiss web component bundle:
  static/1569.*.js, function ``f`` (the polygon decoder)

Coordinate system caveat
-------------------------
The ``coords.system`` field says "LV95" but the values are in **LV03** (EPSG:21781)
kilometres. The web component multiplies by 1000 (km -> m) and feeds the result
to swisstopo's ``CHtoWGS`` REFRAME library, which treats the input as LV03.

RLE encoding
------------
For each step ``s`` from 0 to ``len(o)-1``:
  * ``u = int(o[s]) / 10 + 0.05`` is a sub-cell offset along the polygon's "long"
    axis (used to add detail to the outline).
  * The decoder computes the LV03 corner of the cell at (i, j) using
    even/odd cases for the doubled grid indices.
  * The polygon point is pushed.
  * If 2*s < len(d), the next (i, j) is reached by applying two ASCII deltas:
    ord(d[2s]) - 77 (delta-i) and ord(d[2s+1]) - 77 (delta-j). The characters
    K, L, M, N, O encode -2, -1, 0, +1, +2 in grid units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

# Polygon decoder geometry (replicates the JS f() function exactly) ----------------

_D_CHAR_BASE = 77  # ord('M') == 77; ord(c) - 77 yields the grid delta


def decode_shape(shape: dict, coords: dict) -> list[tuple[float, float]]:
    """Decode one RLE-encoded polygon into a list of LV03 (E, N) vertices in metres.

    The returned list is closed implicitly (MapLibre/Mapbox draw polygons by
    appending the first vertex). The decoder is a direct port of the JS
    function ``f`` from the MeteoSwiss mch-precipitation-map web component.

    Parameters
    ----------
    shape
        A single entry like ``{"i": 27, "j": 676, "d": "NLO...", "o": "5511...", "l": 0}``
    coords
        The ``coords`` block from the JSON file (x_min, x_max, x_count, ...).

    Returns
    -------
    list of (x, y) tuples in LV03 metres.
    """
    i = shape["i"]
    j = shape["j"]
    d_str = shape["d"]
    o_str = shape["o"]
    pts: list[tuple[float, float]] = []

    s = 0
    while s < len(o_str):
        u = int(o_str[s]) / 10.0 + 0.05
        if i % 2 == 0:
            x_km = coords["x_min"] + (coords["x_max"] - coords["x_min"]) * (i / 2) / coords["x_count"]
            y_km = coords["y_min"] + (coords["y_max"] - coords["y_min"]) * ((j - 1) / 2 + u) / coords["y_count"]
        else:
            x_km = coords["x_min"] + (coords["x_max"] - coords["x_min"]) * ((i - 1) / 2 + u) / coords["x_count"]
            y_km = coords["y_min"] + (coords["y_max"] - coords["y_min"]) * (j / 2) / coords["y_count"]
        # The web component calls CHtoWGS(1e3 * x_km, 1e3 * y_km), treating the
        # km values as LV03 metres. The cell size is 1 km, so the implicit unit
        # conversion is a no-op (km × 1000 = m).
        pts.append((x_km * 1000.0, y_km * 1000.0))
        if 2 * s < len(d_str):
            di = ord(d_str[2 * s]) - _D_CHAR_BASE
            dj = ord(d_str[2 * s + 1]) - _D_CHAR_BASE
            i += di
            j += dj
        s += 1
    return pts


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    Boundaries count as *outside* (matches the JS implementation in 1569.js)."""
    x, y = point
    n = len(polygon)
    inside = False
    p1x, p1y = polygon[0]
    for i in range(1, n + 1):
        p2x, p2y = polygon[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def polygons_for_areas(
    areas: list[dict], coords: dict
) -> Iterator[tuple[str, list[tuple[float, float]]]]:
    """Yield (color_hex, polygon) pairs for every polygon in the JSON.

    The radar JSON nests shapes as ``areas[*].shapes[*][*]`` where the inner
    array is a list of shape entries belonging to the same outer polygon at
    different "levels" (the ``l`` field). We yield each entry independently
    so callers can apply the level hierarchy themselves if needed."""
    for area in areas:
        color = area["color"].lower()
        for shape_list in area["shapes"]:
            for shape in shape_list:
                yield color, decode_shape(shape, coords)


# --- Intensity / colour mapping ----------------------------------------------------


@dataclass(frozen=True)
class ColorIntensity:
    """One bin of the MeteoSwiss precipitation colour scale.

    Attributes
    ----------
    mm_per_hour_lower
        The lower bound of the intensity bin in mm/h. The radar emits this
        colour when the rain rate is ``[mm_per_hour_lower, mm_per_hour_upper)``.
    mm_per_hour_upper
        Upper bound. ``None`` means "and above" (the top bin).
    is_no_data
        True for the white ``#ffffff`` areas which mark "no data" / outside
        the radar domain (not "no rain").
    is_warning
        True for ``#333e48`` dark-grey storm/hail warning overlays. These are
        categorical hazards, not continuous rate data.
    """

    mm_per_hour_lower: float
    mm_per_hour_upper: float | None = None
    is_no_data: bool = False
    is_warning: bool = False

    @property
    def representative_mm_per_hour(self) -> float:
        """A point-estimate of the rate (midpoint of the bin, or 0 for warnings)."""
        if self.is_no_data or self.is_warning:
            return 0.0
        if self.mm_per_hour_upper is None:
            return self.mm_per_hour_lower
        return (self.mm_per_hour_lower + self.mm_per_hour_upper) / 2.0


# Static map: 9 distinct rate bins (matches the legend in animation.json) +
# 2 special colours (white, storm).
_INTENSITY_TABLE: dict[str, ColorIntensity] = {
    # Animation legend (in order: highest rate -> lowest)
    "af00dd": ColorIntensity(60.0, None),  # 60+ mm/h
    "ff1900": ColorIntensity(40.0, 60.0),  # 40-60
    "ff7d01": ColorIntensity(20.0, 40.0),  # 20-40
    "ffc703": ColorIntensity(10.0, 20.0),  # 10-20
    "feff01": ColorIntensity(6.0, 10.0),   # 6-10
    "05ff05": ColorIntensity(4.0, 6.0),    # 4-6
    "058c2d": ColorIntensity(2.0, 4.0),    # 2-4
    "0001fc": ColorIntensity(1.0, 2.0),    # 1-2
    "9a7e95": ColorIntensity(0.0, 1.0),    # 0-1 (no-rain gray)
    # Radar-hex variants (lowercase, no leading #) ---------------------------------
    "9e849a": ColorIntensity(0.0, 1.0),  # same as 9a7e95
    "2a00fa": ColorIntensity(1.0, 2.0),  # same as 0001fc
    "2a933b": ColorIntensity(2.0, 4.0),  # same as 058c2d
    "49ff36": ColorIntensity(4.0, 6.0),  # same as 05ff05
    "fcff2d": ColorIntensity(6.0, 10.0), # same as feff01
    "faca1e": ColorIntensity(10.0, 20.0),  # same as ffc703
    "f87c00": ColorIntensity(20.0, 40.0),  # same as ff7d01
    "ac00db": ColorIntensity(60.0, None),  # same as af00dd
    # Specials ---------------------------------------------------------------------
    "ffffff": ColorIntensity(0.0, is_no_data=True),
    "333e48": ColorIntensity(0.0, is_warning=True),
}


def intensity_for_color(hex_color: str) -> ColorIntensity | None:
    """Resolve a colour from the radar/legend to its intensity bin.

    Returns ``None`` for unknown colours. Leading "#" and case are ignored."""
    key = hex_color.lstrip("#").lower()
    return _INTENSITY_TABLE.get(key)


def list_known_colors() -> Iterable[str]:
    """All known hex codes the radar/legend emits (useful for diagnostics)."""
    return sorted(_INTENSITY_TABLE.keys())
