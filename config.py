"""Minimal config, grown phase by phase. Later phases add Flask secrets,
Drive folder ids, etc. here."""

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# spec §10.1 footer. CSLB_NUMBER is a placeholder — fill in the real
# license number before any sheet leaves this VPS for actual use.
CONTRACTOR_NAME = "Right On Construction, Inc."
CSLB_NUMBER = None  # e.g. "CSLB #123456" — TODO: Michael to confirm

# spec §2 BASEMAP_PROVIDER flag. No Mapbox/Google Static Maps token is
# configured yet, so render_pdf.py draws the road centerline as vector
# graphics instead of compositing onto a fetched raster tile — same pixel
# projection either way (core/geometry.py latlng_to_pixel), so plugging in
# a real basemap later is additive, not a rewrite. Set MAPBOX_TOKEN once
# Michael provides one.
BASEMAP_PROVIDER = "none"  # 'none' | 'mapbox' | 'google'
MAPBOX_TOKEN = None

# San Mateo County parcels — layer id VERIFIED 2026-09-23 (not a guess) by
# fetching https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer?f=pjson
# and its /0?f=pjson: layer 0 is "Active Parcels" (esriGeometryPolygon),
# displayField "APN", with a SITUS_ADDR field for the situs address.
ARCGIS_SMC_PARCELS_BASE = (
    "https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer"
)
PARCEL_LAYER_ID = 0
