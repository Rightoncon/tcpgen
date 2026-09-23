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

from core.geometry import FT_PER_M, to_utm, utm_crs_for

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
    """spec §5.2 ranking: nearest wins, tie-break on a name match to
    `street_hint`, then on higher highway class. This never silently
    guesses at an intersection beyond picking the top-ranked candidate —
    the caller (web layer, later phase) is responsible for showing the
    other candidates in a dropdown."""
    if not candidates:
        raise RoadNotFoundError("No candidate roads to choose from")

    def rank(road: RoadSegment) -> tuple[float, int, int]:
        # Round to treat near-ties as ties instead of always falling
        # through to distance as the sole tiebreak.
        dist = round(_perpendicular_distance_m(lat, lng, road.coords), 1)
        name_match = 0 if (street_hint and street_hint.lower() in road.name.lower()) else 1
        class_rank = -HIGHWAY_CLASSES.index(road.highway) if road.highway in HIGHWAY_CLASSES else 0
        return (dist, name_match, class_rank)

    return sorted(candidates, key=rank)[0]
