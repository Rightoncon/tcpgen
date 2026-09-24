"""Parcel lookup for Express mode (spec §5A) — turns an address alone into
a default work area, no drawing required.

County GIS services differ from county to county, so this is behind a
`ParcelProvider` protocol. `SanMateoArcGIS` is the only real implementation
for v1; `NoParcelProvider` is the explicit "county not covered yet" stub
that callers use to trigger the 60 ft default-frontage fallback (§5A.3).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

import requests

from config import ARCGIS_SMC_PARCELS_BASE, PARCEL_LAYER_ID
from core.geometry import Centerline

if TYPE_CHECKING:
    from core.roads import RoadSegment


def _situs_house_number(situs_address: Optional[str]) -> Optional[str]:
    """First whitespace-delimited token of a county SITUS_ADDR string
    (e.g. "635 COSTA RICA AVE " -> "635"), or None if there's no address
    on file for that parcel."""
    if not situs_address:
        return None
    parts = situs_address.strip().split()
    return parts[0] if parts else None


@dataclass
class Parcel:
    apn: Optional[str]
    polygon: list[tuple[float, float]]  # lat, lng ring
    situs_address: Optional[str]
    source: str  # 'smc_arcgis' | 'none'


class ParcelProvider(Protocol):
    def parcel_at(self, lat: float, lng: float) -> Optional[Parcel]: ...


class SanMateoArcGIS:
    """spec §5A.1 — San Mateo County's ArcGIS FeatureServer.

    The layer id was VERIFIED 2026-09-23, not guessed: enumerating
    `{base}?f=pjson` lists layer 0 as "Active Parcels"
    (esriGeometryPolygon), and `{base}/0?f=pjson` confirms
    `displayField: "APN"` plus a `SITUS_ADDR` field. See config.py.
    """

    source = "smc_arcgis"

    def __init__(self, base: str = ARCGIS_SMC_PARCELS_BASE, layer_id: int = PARCEL_LAYER_ID):
        self.query_url = f"{base}/{layer_id}/query"

    def parcel_at(self, lat: float, lng: float, *, timeout: int = 15) -> Optional[Parcel]:
        """Returns None on a network failure (timeout, unreachable, bad
        response) exactly like a genuine zero-features response -- callers
        already treat None as "no parcel on file, fall back to the 60 ft
        default frontage" (spec Sec5A.3), same spirit as
        core.roads.find_roads_within degrading to [] rather than raising.
        Caught for real 2026-09-24: a slow gis.smcgov.org response turned
        an /api/express call into an unhandled ReadTimeout, which Flask
        rendered as an HTML 500 page -- the browser's res.json() then
        threw "Unexpected token '<'" trying to parse it as JSON."""
        params = {
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        }
        try:
            resp = requests.get(self.query_url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            return None
        features = data.get("features", [])
        if not features:
            return None
        return self._parcel_from_feature(features[0])

    def parcel_for_address(
        self, lat: float, lng: float, *, house_number: Optional[str] = None, timeout: int = 15
    ) -> Optional[Parcel]:
        """The real parcel lookup this app should call everywhere (not
        parcel_at() directly) -- adds a same-address cross-check against
        county SITUS_ADDR data on top of the exact point-in-polygon match.

        A geocoder's address-interpolated point can land inside the WRONG
        neighboring parcel, or in the gap between two parcels entirely.
        Confirmed real 2026-09-24: Nominatim's point for "635 Costa Rica
        Ave, San Mateo" sat 11 ft from 631's parcel boundary but 23 ft
        from 635's own -- an unverified point-in-polygon match would have
        silently returned the wrong house's frontage (or, since it missed
        both polygons narrowly, nothing at all -- 631's neighbor problem
        either way once the default-frontage fallback centered on that
        same inaccurate point). When `house_number` is given, the
        exact-point result is verified against it; on a mismatch or an
        empty result, a small-radius search finds the parcel whose own
        SITUS_ADDR actually carries that house number instead. Returns
        None (triggering the existing 60 ft default-frontage fallback)
        rather than ever returning a parcel known not to match."""
        exact = self.parcel_at(lat, lng, timeout=timeout)
        if house_number is None:
            return exact
        if exact is not None and _situs_house_number(exact.situs_address) == house_number:
            return exact
        return self._parcel_by_house_number(house_number, lat, lng, timeout=timeout)

    def _parcel_by_house_number(
        self, house_number: str, lat: float, lng: float, *, radius_ft: float = 250.0, timeout: int = 15
    ) -> Optional[Parcel]:
        """Bounding-envelope search around (lat, lng) for the parcel whose
        SITUS_ADDR starts with `house_number` -- real county data, not a
        geocoder guess. 250 ft comfortably covers the ~50-90 ft gaps
        between adjacent houses seen on real streets in this app's test
        cases without pulling in enough of the block to risk ambiguity."""
        ft_per_deg_lat = 365000.0
        dlat = radius_ft / ft_per_deg_lat
        dlng = radius_ft / (ft_per_deg_lat * max(0.01, math.cos(math.radians(lat))))
        params = {
            "geometry": f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        }
        try:
            resp = requests.get(self.query_url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            return None
        for feature in data.get("features", []):
            situs = (feature.get("properties") or {}).get("SITUS_ADDR")
            if _situs_house_number(situs) == house_number:
                return self._parcel_from_feature(feature)
        return None

    @staticmethod
    def _parcel_from_feature(feature: dict) -> Parcel:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiPolygon":
            outer_ring = coords[0][0] if coords and coords[0] else []
        else:
            outer_ring = coords[0] if coords else []
        # GeoJSON rings are [lng, lat]; Parcel.polygon is (lat, lng) to
        # match every other coordinate in this app.
        polygon = [(lat, lng) for lng, lat in outer_ring]
        return Parcel(
            apn=props.get("APN"),
            polygon=polygon,
            situs_address=props.get("SITUS_ADDR"),
            source=SanMateoArcGIS.source,
        )


class NoParcelProvider:
    """spec §5A.3 fallback for a county with no provider registered yet.
    Always returns None so the caller falls back to the 60 ft default
    frontage instead of failing the whole plan."""

    source = "none"

    def parcel_at(self, lat: float, lng: float) -> Optional[Parcel]:
        return None


# spec §5A.4 — frontage extent

def frontage_stations(parcel: Parcel, centerline: Centerline) -> tuple[float, float]:
    """Project every parcel vertex onto `centerline` and take the station
    range they cover."""
    stations = [
        centerline.station_and_offset_of_nearest(lat, lng)[0] for lat, lng in parcel.polygon
    ]
    return min(stations), max(stations)


def frontage_length_ft(parcel: Parcel, centerline: Centerline) -> float:
    s0, s1 = frontage_stations(parcel, centerline)
    return s1 - s0


def work_side(
    parcel: Optional[Parcel],
    centerline: Centerline,
    geocoded_point: Optional[tuple[float, float]] = None,
) -> int:
    """sign(median(perpendicular offsets of the parcel's vertices)).

    When there's no parcel (a real, expected case — ArcGIS coverage gaps
    exist even inside the county), fall back to the sign of the geocoded
    address point's own offset from the centerline rather than blindly
    guessing +1. That point is real address-level data and is *almost
    always* correctly offset to whichever side the house is actually on
    (confirmed live: a hardcoded +1 put a work area across the street from
    635 Costa Rica Ave, San Mateo, where the point itself was clearly
    offset -28.8 ft — the correct side was sitting right there, just
    discarded). Only fully blind (no parcel AND no point) does this still
    default to +1."""
    if parcel is not None:
        offsets = [
            centerline.station_and_offset_of_nearest(lat, lng)[1] for lat, lng in parcel.polygon
        ]
        return 1 if statistics.median(offsets) >= 0 else -1
    if geocoded_point is not None:
        _, offset = centerline.station_and_offset_of_nearest(*geocoded_point)
        return 1 if offset >= 0 else -1
    return 1


CORNER_LOT_FRONTAGE_THRESHOLD_FT = 250.0


def is_implausible_frontage(parcel: Parcel, centerline: Centerline) -> bool:
    """spec §5A.4: a frontage over 250 ft on the chosen street usually
    means the parcel actually fronts a different, cross street. Flag it —
    same intent as the geometric corner-lot check below, cheaper to run."""
    return frontage_length_ft(parcel, centerline) > CORNER_LOT_FRONTAGE_THRESHOLD_FT


# spec §5A.5 — corner lots

def detect_corner_lot(
    parcel: Parcel, candidates: list["RoadSegment"], *, threshold_ft: float = 15.0
) -> bool:
    """True if `parcel` touches 2+ of the candidate roads (spec §5.2's
    output) within `threshold_ft`. When True, the operator must pick the
    fronting street before generating — spec §5A.5 is explicit that this
    is never guessed."""
    touching = 0
    for road in candidates:
        cl = Centerline(road.coords)
        min_dist_ft = min(
            abs(cl.station_and_offset_of_nearest(lat, lng)[1]) for lat, lng in parcel.polygon
        )
        if min_dist_ft <= threshold_ft:
            touching += 1
    return touching >= 2
