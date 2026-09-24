"""Road geometry lookup via the OSM Overpass API (spec §5).

This module is the only source of truth for where a road actually is. The
basemap image built in later phases is a background raster with no data —
never derive geometry from it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import requests

from core.geometry import FT_PER_M, to_utm, to_wgs84, utm_crs_for

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Overpass returns 406 Not Acceptable for requests' default User-Agent
# (`python-requests/x.y`) — confirmed live 2026-09-23. A descriptive UA is
# also just the polite/expected thing per Overpass's usage policy.
_USER_AGENT = "tcpgen/0.1 (Right On Construction; michael@rightonconcrete.com)"

# spec §5.2 — highway classes worth matching, ascending "importance" order.
# Used both to filter the Overpass query and as the highway-class tie-break
# in choose_road (prefer the higher class).
HIGHWAY_CLASSES = [
    "service",
    "residential",
    "unclassified",
    "tertiary",
    "secondary",
    "primary",
]

# spec §5.3 — fallback total road width (feet) when no width/lanes tag exists.
FALLBACK_WIDTH_FT = {
    "residential": 36,
    "unclassified": 36,
    "tertiary": 40,
    "secondary": 48,
    "primary": 60,
    "service": 24,
}

# spec §5.4 — fallback posted speed (mph) when no maxspeed tag exists.
# `service` isn't in the spec's table; 15 mph (typical alley/driveway) is
# this module's own conservative default, not from the doc.
FALLBACK_SPEED_MPH = {
    "residential": 25,
    "unclassified": 25,
    "tertiary": 30,
    "secondary": 35,
    "primary": 35,
    "service": 15,
}

# spec §5.2 — widen 60m -> 120m -> 250m before giving up.
SEARCH_RADII_M = (60, 120, 250)


class RoadNotFoundError(Exception):
    """No named road found within SEARCH_RADII_M[-1] meters, or Overpass
    was unreachable. Message is meant to be shown to the operator directly:
    move the marker closer to the street."""


@dataclass
class RoadSegment:
    osm_id: int
    name: str
    coords: list[tuple[float, float]]  # lat, lng, ordered
    width_ft: float
    lanes: int
    speed_mph: int
    oneway: bool
    highway: str = ""  # raw OSM highway= tag, used for the class tie-break


def query_overpass(lat: float, lng: float, radius_m: int, *, timeout: int = 25) -> dict:
    """Raw Overpass query for named highway ways within radius_m of
    (lat, lng). See spec §5.1."""
    query = (
        f"[out:json][timeout:{timeout}];\n"
        f"way(around:{radius_m},{lat},{lng})\n"
        f'  ["highway"~"^({"|".join(HIGHWAY_CLASSES)})$"];\n'
        f"out geom tags;"
    )
    resp = requests.post(
        OVERPASS_URL,
        data={"data": query},
        timeout=timeout + 5,
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json()


def _parse_width_ft(tags: dict) -> Optional[float]:
    """OSM `width` values are meters unless explicitly suffixed ft/'."""
    raw = tags.get("width")
    if not raw:
        return None
    raw = raw.strip()
    try:
        if raw.endswith("ft") or raw.endswith("'"):
            return float(raw.rstrip("ft'").strip())
        return float(raw.rstrip("m").strip()) * FT_PER_M
    except ValueError:
        return None


def _parse_lanes(tags: dict) -> Optional[int]:
    raw = tags.get("lanes")
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parking_width_bonus_ft(tags: dict) -> float:
    """spec §5.3: +8 ft per side that has a parking lane."""
    both = tags.get("parking:lane:both")
    if both and both != "no":
        return 16.0
    bonus = 0.0
    for side in ("left", "right"):
        val = tags.get(f"parking:lane:{side}")
        if val and val != "no":
            bonus += 8.0
    return bonus


def _derive_width_ft(tags: dict, highway: str) -> float:
    """spec §5.3 priority: explicit width tag > lanes*11+parking > highway
    class fallback. Always meant to be shown as an editable field in the UI
    (later phase) — this is a starting estimate, not a survey."""
    explicit = _parse_width_ft(tags)
    if explicit is not None:
        return explicit
    lanes = _parse_lanes(tags)
    if lanes is not None:
        return lanes * 11.0 + _parking_width_bonus_ft(tags)
    return FALLBACK_WIDTH_FT.get(highway, FALLBACK_WIDTH_FT["residential"])


def _derive_speed_mph(tags: dict, highway: str) -> int:
    """spec §5.4. This app is CA-only (see the portal's geocoding
    restriction this project mirrors), so a bare maxspeed number is
    treated as mph, not km/h."""
    raw = tags.get("maxspeed")
    if raw:
        raw = raw.strip().lower()
        try:
            if "mph" in raw:
                return int(float(raw.replace("mph", "").strip()))
            return int(float(raw))
        except ValueError:
            pass
    return FALLBACK_SPEED_MPH.get(highway, FALLBACK_SPEED_MPH["residential"])


def _road_from_element(el: dict) -> RoadSegment:
    tags = el.get("tags", {})
    highway = tags.get("highway", "")
    coords = [(pt["lat"], pt["lon"]) for pt in el.get("geometry") or [] if pt]
    oneway_raw = (tags.get("oneway") or "").lower()
    return RoadSegment(
        osm_id=el["id"],
        name=tags.get("name") or "Unnamed Road",
        coords=coords,
        width_ft=_derive_width_ft(tags, highway),
        lanes=_parse_lanes(tags) or 2,
        speed_mph=_derive_speed_mph(tags, highway),
        oneway=oneway_raw in ("yes", "1", "true"),
        highway=highway,
    )


def find_roads_near(lat: float, lng: float) -> list[RoadSegment]:
    """spec §5.2: widen 60m -> 120m -> 250m until something named is found.
    Raises RoadNotFoundError (with a message meant for the operator) if
    nothing turns up, or if Overpass itself is unreachable."""
    last_error: Optional[Exception] = None
    for radius_m in SEARCH_RADII_M:
        try:
            data = query_overpass(lat, lng, radius_m)
        except requests.RequestException as exc:
            last_error = exc
            continue
        elements = data.get("elements", [])
        roads = [_road_from_element(el) for el in elements if el.get("geometry")]
        if roads:
            return roads
    if last_error is not None:
        raise RoadNotFoundError(
            f"No road found near ({lat}, {lng}) and the Overpass API was "
            f"unreachable ({last_error}). Move the marker closer to the "
            f"street and try again."
        ) from last_error
    raise RoadNotFoundError(
        f"No named road found within {SEARCH_RADII_M[-1]}m of ({lat}, {lng}). "
        f"Move the marker closer to the street."
    )


def find_roads_within(lat: float, lng: float, radius_m: int = SEARCH_RADII_M[-1]) -> list[RoadSegment]:
    """A single, non-widening query at `radius_m` — unlike find_roads_near
    (which stops at the *first* radius that returns anything, tuned for
    picking the one road the job site sits on), this returns everything
    named within the full radius. Needed for cross-street detection: the
    job site's own road almost always already matches at 60m, so
    find_roads_near would never widen out to where a cross street 200+ ft
    away actually lives — confirmed live (157 San Marco Ave, San Bruno:
    real nearby cross streets existed but never appeared as candidates
    because the main-road search stopped at 60m). Returns [] on a network
    failure rather than raising — a missing cross-street candidate list
    should degrade to "no side-street signs," never break plan generation."""
    try:
        data = query_overpass(lat, lng, radius_m)
    except requests.RequestException:
        return []
    elements = data.get("elements", [])
    return [_road_from_element(el) for el in elements if el.get("geometry")]


def _perpendicular_distance_m(lat: float, lng: float, coords: list[tuple[float, float]]) -> float:
    """Nearest distance in meters from (lat, lng) to the polyline `coords`."""
    crs = utm_crs_for(lat, lng)
    ((px, py),) = to_utm([(lat, lng)], crs)
    line = to_utm(coords, crs)
    best = math.inf
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        dx, dy = x1 - x0, y1 - y0
        seg_len2 = dx * dx + dy * dy
        t = 0.0 if seg_len2 == 0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg_len2))
        cx, cy = x0 + dx * t, y0 + dy * t
        best = min(best, math.hypot(px - cx, py - cy))
    return best


def choose_road(
    candidates: list[RoadSegment],
    lat: float,
    lng: float,
    *,
    street_hint: Optional[str] = None,
) -> RoadSegment:
    """spec §5.2 ranking: nearest wins, tie-break on higher highway class.
    A `street_hint` is the operator's explicit Street-dropdown pick, so it
    is a filter, not a tie-break: only roads with that name are ranked
    (exact name first, then substring), falling back to every candidate
    only if nothing matches. As a mere tie-break it lost to distance on
    corner lots -- 997 Glennan Dr (2026-09-24): Castle Hill Rd was picked
    and a work area drawn on it, but Glennan was nearer the geocoded
    point, so the whole plan was built along Glennan instead."""
    if not candidates:
        raise RoadNotFoundError("No candidate roads to choose from")

    if street_hint:
        hint = street_hint.strip().lower()
        pool = (
            [r for r in candidates if r.name.lower() == hint]
            or [r for r in candidates if hint in r.name.lower()]
            or candidates
        )
    else:
        pool = candidates

    def rank(road: RoadSegment) -> tuple[float, int]:
        # Round to treat near-ties as ties instead of always falling
        # through to distance as the sole tiebreak.
        dist = round(_perpendicular_distance_m(lat, lng, road.coords), 1)
        class_rank = -HIGHWAY_CLASSES.index(road.highway) if road.highway in HIGHWAY_CLASSES else 0
        return (dist, class_rank)

    return sorted(pool, key=rank)[0]


# ---- side-street signage (spec via reference plan, General Note 8) --------
#
# "PLACE W20-1 'ROAD WORK AHEAD' SIGNS ON ALL SIDE STREETS WITHIN THE
# ADVANCE WARNING AREA" -- verbatim from a real production TCP (413 Alameda
# de las Pulgas, City Rise Safety, 2026-09-23). Needs real intersection
# geometry, not a guess: find every named road whose polyline actually
# crosses the main centerline within the advance-warning window.


def _segment_intersection(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], p4: tuple[float, float]
) -> Optional[tuple[float, float]]:
    """2D intersection point of segment p1-p2 and segment p3-p4 (all in the
    same planar CRS, e.g. UTM meters), or None if they don't cross. Treats
    a shared endpoint as a valid crossing (t/u == 0 or 1 inclusive) since
    OSM ways are routinely split exactly at intersection nodes."""
    d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
    d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-9:
        return None  # parallel or coincident
    t = ((p3[0] - p1[0]) * d2y - (p3[1] - p1[1]) * d2x) / denom
    u = ((p3[0] - p1[0]) * d1y - (p3[1] - p1[1]) * d1x) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (p1[0] + t * d1x, p1[1] + t * d1y)
    return None


def _polyline_intersections(
    line_a: list[tuple[float, float]], line_b: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    points = []
    for i in range(len(line_a) - 1):
        for j in range(len(line_b) - 1):
            pt = _segment_intersection(line_a[i], line_a[i + 1], line_b[j], line_b[j + 1])
            if pt is not None:
                points.append(pt)
    return points


def find_cross_streets(
    main_road: RoadSegment,
    main_centerline,  # core.geometry.Centerline; not type-hinted to avoid a circular import
    candidates: list[RoadSegment],
    station_min: float,
    station_max: float,
) -> list[dict]:
    """Named roads from `candidates` (e.g. find_roads_near's own output —
    no extra Overpass call needed) that geometrically cross
    `main_centerline`, with the crossing's station on the main road inside
    [station_min, station_max]. Returns a list of
    {"road": RoadSegment, "main_station": float, "point": (lat, lng)},
    sorted by station, deduped by osm_id (a curving side street that
    crosses twice only gets one sign).

    Unnamed ways are skipped — an unnamed alley/driveway isn't worth a
    device and its OSM geometry is often unreliable anyway. Limited to
    whatever `candidates` already covers (the same search radius used to
    choose the main road, up to 250m) — a very long advance-warning window
    on a high-speed road could in principle reach further than that; a
    real but minor limitation, not fixed here."""
    main_line_m = to_utm(main_road.coords, main_centerline.crs)
    results: list[dict] = []
    seen_ids: set[int] = set()
    for cand in candidates:
        if cand.osm_id == main_road.osm_id or cand.osm_id in seen_ids:
            continue
        if not cand.name or cand.name == "Unnamed Road":
            continue
        if len(cand.coords) < 2:
            continue
        cand_line_m = to_utm(cand.coords, main_centerline.crs)
        for pt_m in _polyline_intersections(main_line_m, cand_line_m):
            lat, lng = to_wgs84([pt_m], main_centerline.crs)[0]
            station = main_centerline.station_of_nearest(lat, lng)
            if station_min <= station <= station_max:
                results.append({"road": cand, "main_station": station, "point": (lat, lng)})
                seen_ids.add(cand.osm_id)
                break
    results.sort(key=lambda r: r["main_station"])
    return results
