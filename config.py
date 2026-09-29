"""Minimal config, grown phase by phase. Later phases add Flask secrets,
Drive folder ids, etc. here.

Secrets live in .env (spec §15: "never committed") — gitignored since
Phase 1, loaded here with a tiny dependency-free parser rather than
pulling in python-dotenv for one file."""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).resolve().parent / ".env")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# spec §10.1 footer.
CONTRACTOR_NAME = "Right On Construction, Inc."
CSLB_NUMBER = "CSLB #1092689"

# spec §2 BASEMAP_PROVIDER flag. MAPBOX_TOKEN comes from .env, never from
# this file. Until it's set, render_pdf.py draws the road centerline as
# vector graphics instead of compositing onto a fetched raster tile — same
# pixel projection either way (core/geometry.py latlng_to_pixel), so wiring
# in the real basemap fetch later is additive, not a rewrite.
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN")
BASEMAP_PROVIDER = "mapbox" if MAPBOX_TOKEN else "none"  # 'none' | 'mapbox' | 'google'

# San Mateo County parcels — layer id VERIFIED 2026-09-23 (not a guess) by
# fetching https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer?f=pjson
# and its /0?f=pjson: layer 0 is "Active Parcels" (esriGeometryPolygon),
# displayField "APN", with a SITUS_ADDR field for the situs address.
ARCGIS_SMC_PARCELS_BASE = (
    "https://gis.smcgov.org/maps/rest/services/PLANNING/COUNTY_PARCELS/FeatureServer"
)
PARCEL_LAYER_ID = 0

# Mi RoC portal's internal Job/PO address list -- the "use a Job/PO
# address" picker in Express mode, called server-side so the shared
# secret never reaches the browser. Same-VPS loopback call, no public
# DNS/TLS hop needed. PORTAL_INTERNAL_TOKEN comes from .env (shared with
# the portal's own config.json internal_token, copied once).
PORTAL_INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN")
PORTAL_JOBS_URL = "http://127.0.0.1:8100/internal/tcpgen/jobs"
# Where a plan's permit # / USA North 811 ticket # are sent so the portal's
# job sheet and JobFlow job can show them (2026-09-28).
PORTAL_JOB_PERMITS_URL = "http://127.0.0.1:8100/internal/tcpgen/job-permits"
