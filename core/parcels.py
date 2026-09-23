"""Parcel lookup for Express mode (spec §5A) — turns an address alone into
a default work area, no drawing required.

County GIS services differ from county to county, so this is behind a
`ParcelProvider` protocol. `SanMateoArcGIS` is the only real implementation
for v1; `NoParcelProvider` is the explicit "county not covered yet" stub
that callers use to trigger the 60 ft default-frontage fallback (§5A.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

import requests

from config import ARCGIS_SMC_PARCELS_BASE, PARCEL_LAYER_ID


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
