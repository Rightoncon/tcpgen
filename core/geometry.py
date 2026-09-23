"""Station/offset geometry for TCP device placement (spec §7).

All distance math happens in UTM meters — never trig on raw lat/lng.
`station_ft`/`offset_ft` are the public boundary of this module: feet in,
feet out; everything stored internally is meters.

This is the foundation of the whole app's core design rule (spec §1.1):
road geometry never comes from a screenshot. A device's position is always
(station, offset) relative to a real centerline derived from OSM, converted
to lat/lng only at the very end for rendering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from pyproj import CRS, Transformer

METERS_PER_FT = 0.3048
FT_PER_M = 1 / METERS_PER_FT

# spec §7.3 — every device offset gets clamped this far inside the road
# edge before it is stored. This is what makes it physically impossible
# for a device to land on a sidewalk or a parcel.
MIN_EDGE_CLEARANCE_FT = 1.0

_WGS84 = CRS.from_epsg(4326)


def utm_crs_for(lat: float, lng: float) -> CRS:
    """UTM zone CRS covering (lat, lng). The Bay Area is zone 10N (EPSG:32610)."""
    zone = int((lng + 180) / 6) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def to_utm(
    coords: Sequence[tuple[float, float]], crs: Optional[CRS] = None
) -> list[tuple[float, float]]:
    """Project (lat, lng) points to UTM meters (easting, northing).

    If `crs` is omitted, one is derived from the centroid of `coords` via
    `utm_crs_for` — fine for a single short road segment or work area. Pass
    a shared `crs` when projecting several related point sets so they land
    in the same plane, and pass that same `crs` back into `to_wgs84`.
    """
    if crs is None:
        lat0 = sum(c[0] for c in coords) / len(coords)
        lng0 = sum(c[1] for c in coords) / len(coords)
        crs = utm_crs_for(lat0, lng0)
    transformer = Transformer.from_crs(_WGS84, crs, always_xy=True)
    return [transformer.transform(lng, lat) for lat, lng in coords]


def to_wgs84(points: Sequence[tuple[float, float]], crs: CRS) -> list[tuple[float, float]]:
    """Inverse of `to_utm`. Returns (lat, lng).

    Requires the CRS the points were projected with — a UTM point can't be
    inverted without knowing its zone, so (unlike the spec's illustrative
    sketch in §7.1) this takes `crs` explicitly rather than guessing one.
    """
    transformer = Transformer.from_crs(crs, _WGS84, always_xy=True)
    out = []
    for easting, northing in points:
        lng, lat = transformer.transform(easting, northing)
        out.append((lat, lng))
    return out


def clamp_offset(offset_ft: float, road_width_ft: float) -> float:
    """Clamp a device offset to stay `MIN_EDGE_CLEARANCE_FT` inside the
    road edge (spec §7.3 — the bug-killer). Apply this to every device
    offset before storing or passing it to `Centerline.offset_point`."""
    limit = road_width_ft / 2 - MIN_EDGE_CLEARANCE_FT
    return max(-limit, min(limit, offset_ft))


class Centerline:
    """A road centerline as an ordered polyline, addressable by station
    (feet along the line, from the first point) and offset (feet
    perpendicular to it, + = right of the direction of travel)."""

    def __init__(self, coords_latlng: Sequence[tuple[float, float]]):
        coords_latlng = list(coords_latlng)
        if len(coords_latlng) < 2:
            raise ValueError("Centerline needs at least 2 points")

        lat0 = sum(c[0] for c in coords_latlng) / len(coords_latlng)
        lng0 = sum(c[1] for c in coords_latlng) / len(coords_latlng)
        self.crs = utm_crs_for(lat0, lng0)
        self._points_m: list[tuple[float, float]] = to_utm(coords_latlng, self.crs)

        cum = [0.0]
        for (x0, y0), (x1, y1) in zip(self._points_m, self._points_m[1:]):
            cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
        self._cum_m = cum  # meters, one entry per vertex

    @property
    def length_ft(self) -> float:
        return self._cum_m[-1] * FT_PER_M

    def _segment_for_station(self, station_m: float) -> tuple[int, float]:
        """(segment index i, distance-into-segment in meters) for a station
        clamped to [0, length_m]."""
        station_m = max(0.0, min(self._cum_m[-1], station_m))
        last = len(self._cum_m) - 2
        for i in range(last + 1):
            if station_m <= self._cum_m[i + 1] or i == last:
                return i, station_m - self._cum_m[i]
        return 0, 0.0  # unreachable when len(coords) >= 2

    def point_at(self, station_ft: float) -> tuple[float, float]:
        """UTM (easting, northing) meters at `station_ft` along the line."""
        i, into_m = self._segment_for_station(station_ft * METERS_PER_FT)
        x0, y0 = self._points_m[i]
        x1, y1 = self._points_m[i + 1]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        t = 0.0 if seg_len == 0 else into_m / seg_len
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def bearing_at(self, station_ft: float) -> float:
        """Direction of travel at `station_ft` as a standard math angle in
        radians — atan2(dy, dx) in the projected UTM plane, not a compass
        heading."""
        i, _ = self._segment_for_station(station_ft * METERS_PER_FT)
        x0, y0 = self._points_m[i]
        x1, y1 = self._points_m[i + 1]
        return math.atan2(y1 - y0, x1 - x0)

    def offset_point(self, station_ft: float, offset_ft: float) -> tuple[float, float]:
        """UTM (easting, northing) meters at `offset_ft` perpendicular to
        the centerline at `station_ft`. Positive offset = right of the
        direction of travel.

        NOT clamped — callers must run the offset through `clamp_offset`
        against the actual road width before calling this, so nothing
        downstream ever asserts a device off the roadway (spec §7.3)."""
        x, y = self.point_at(station_ft)
        bearing = self.bearing_at(station_ft)
        # Right-hand normal of a tangent (cos b, sin b) is (sin b, -cos b).
        nx, ny = math.sin(bearing), -math.cos(bearing)
        offset_m = offset_ft * METERS_PER_FT
        return (x + nx * offset_m, y + ny * offset_m)

    def station_of_nearest(self, lat: float, lng: float) -> float:
        """Station in feet of the point on the centerline nearest to (lat, lng)."""
        station_m, _offset_m = self._nearest(lat, lng)
        return station_m * FT_PER_M

    def station_and_offset_of_nearest(self, lat: float, lng: float) -> tuple[float, float]:
        """(station_ft, offset_ft) of the point on the centerline nearest
        to (lat, lng). `offset_ft` uses the same +right-of-travel sign
        convention as `offset_point` — this is effectively its inverse,
        and is what spec §5A.4's frontage/work-side math needs (a parcel
        vertex's position relative to the road, not just its station)."""
        station_m, offset_m = self._nearest(lat, lng)
        return station_m * FT_PER_M, offset_m * FT_PER_M

    def _nearest(self, lat: float, lng: float) -> tuple[float, float]:
        """(station_m, signed_offset_m) of the point on the centerline
        nearest to (lat, lng), both in meters."""
        ((px, py),) = to_utm([(lat, lng)], self.crs)
        best_station_m = 0.0
        best_offset_m = 0.0
        best_dist2 = math.inf
        for i in range(len(self._points_m) - 1):
            x0, y0 = self._points_m[i]
            x1, y1 = self._points_m[i + 1]
            dx, dy = x1 - x0, y1 - y0
            seg_len2 = dx * dx + dy * dy
            seg_len = math.sqrt(seg_len2)
            if seg_len2 == 0:
                t = 0.0
            else:
                t = ((px - x0) * dx + (py - y0) * dy) / seg_len2
                t = max(0.0, min(1.0, t))
            cx, cy = x0 + dx * t, y0 + dy * t
            dist2 = (px - cx) ** 2 + (py - cy) ** 2
            if dist2 < best_dist2:
                best_dist2 = dist2
                best_station_m = self._cum_m[i] + t * seg_len
                if seg_len2 == 0:
                    best_offset_m = 0.0
                else:
                    vx, vy = px - cx, py - cy
                    # signed distance via the same right-hand normal
                    # (dy, -dx)/seg_len used by offset_point, so the two
                    # methods agree on what "positive" means.
                    best_offset_m = (vx * dy - vy * dx) / seg_len
        return best_station_m, best_offset_m


# spec §7.5 — lat/lng -> pixel on a static Web Mercator basemap image. Used
# both for compositing onto a fetched raster tile (once basemap.py exists)
# and for placing vector graphics (device dots, road lines) at the correct
# spot on a PDF page with no raster behind them at all — the projection
# math is the same either way.


def latlng_to_pixel(
    lat: float, lng: float, center_lat: float, center_lng: float, zoom: float, w: float, h: float, scale: float = 2
) -> tuple[float, float]:
    world = 256 * 2**zoom * scale

    def wx(lng_: float) -> float:
        return (lng_ + 180) / 360 * world

    def wy(lat_: float) -> float:
        s = math.sin(math.radians(lat_))
        return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * world

    px = wx(lng) - wx(center_lng) + w * scale / 2
    py = wy(lat) - wy(center_lat) + h * scale / 2
    return px, py


def _pixel_bbox(
    points: Sequence[tuple[float, float]], center_lat: float, center_lng: float, zoom: float, w: float, h: float, scale: float
) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for lat, lng in points:
        px, py = latlng_to_pixel(lat, lng, center_lat, center_lng, zoom, w, h, scale)
        xs.append(px)
        ys.append(py)
    return min(xs), max(xs), min(ys), max(ys)


def choose_zoom(
    points: Sequence[tuple[float, float]],
    center_lat: float,
    center_lng: float,
    w: float,
    h: float,
    *,
    scale: float = 2,
    margin: float = 0.15,
    zoom_min: int = 15,
    zoom_max: int = 20,
) -> int:
    """spec §7.5: "Zoom is chosen so the full device extent plus 15% margin
    fits the frame. Compute the bounding box of all devices, then
    binary-search zoom from 20 down to 15." Returns the most zoomed-in
    (highest) level at which every point in `points` still fits within
    `w`x`h` after shrinking the frame by `margin` on each axis."""
    if not points:
        return zoom_max
    target_w = w * scale * (1 - margin)
    target_h = h * scale * (1 - margin)
    best = zoom_min
    lo, hi = zoom_min, zoom_max
    while lo <= hi:
        mid = (lo + hi) // 2
        x0, x1, y0, y1 = _pixel_bbox(points, center_lat, center_lng, mid, w, h, scale)
        if (x1 - x0) <= target_w and (y1 - y0) <= target_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best
