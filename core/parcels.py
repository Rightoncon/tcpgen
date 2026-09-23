"""Parcel lookup for Express mode (spec §5A) — turns an address alone into
a default work area, no drawing required.

County GIS services differ from county to county, so this is behind a
`ParcelProvider` protocol. `SanMateoArcGIS` is the only real implementation
for v1; `NoParcelProvider` is the explicit "county not covered yet" stub
that callers use to trigger the 60 ft default-frontage fallback (§5A.3).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

import requests

from config import ARCGIS_SMC_PARCELS_BASE, PARCEL_LAYER_ID
from core.geometry import Centerline

if TYPE_CHECKING:
    from core.roads import RoadSegment


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
        resp = requests.get(self.query_url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return None
        return self._parcel_from_feature(features[0])

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


def work_side(parcel: Optional[Parcel], centerline: Centerline) -> int:
    """sign(median(perpendicular offsets of the parcel's vertices)).

    Defaults to the right side (+1) when there's no parcel — spec §8.2's
    `default_work_area` calls this even in the no-parcel branch, and
    nothing else is known at that point. A later phase with a UI should
    let the operator confirm/flip this."""
    if parcel is None:
        return 1
    offsets = [
        centerline.station_and_offset_of_nearest(lat, lng)[1] for lat, lng in parcel.polygon
    ]
    return 1 if statistics.median(offsets) >= 0 else -1


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
