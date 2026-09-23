"""Minimal config for Phase 1 (roads/geometry/parcels only). Later phases
add Flask secrets, Mapbox/Google keys, Drive folder ids, etc. here."""

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# San Mateo County parcels — layer id VERIFIED 2026-09-23 (not a guess) by
# fetching https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer?f=pjson
# and its /0?f=pjson: layer 0 is "Active Parcels" (esriGeometryPolygon),
# displayField "APN", with a SITUS_ADDR field for the situs address.
ARCGIS_SMC_PARCELS_BASE = (
    "https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer"
)
PARCEL_LAYER_ID = 0
