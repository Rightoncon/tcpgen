"""Address geocoding — free, no API key, via OSM Nominatim.

The spec's directory layout (§3) lists this module without assigning it to
a specific phase, but Phase 2's CLI (`python -m tools.plan --address ...`)
can't run without it, so it's built here as necessary plumbing for that
explicit deliverable.

Restricted to California, mirroring the Mi RoC portal's hard CA
restriction on its own geocoder (`_geocode_address()` in that app's
app.py) — added there after a real address ("2648 Howard Ave. SC", SC =
San Carlos) geocoded to South Carolina. A free-text geocoder with no
restriction is trivially exposed to the same class of bug here.

Nominatim's usage policy caps free use at ~1 request/second, which is fine
for this tool's actual call volume (one geocode per plan). If usage ever
grows, swap in a paid provider (Google, Mapbox) — same GeocodeResult shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "tcpgen/0.1 (Right On Construction; michael@rightonconcrete.com)"


class GeocodeError(Exception):
    """No match, or the match wasn't in California."""


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    display_name: str
    city: str
    zip: Optional[str]


def geocode_address(address: str, *, timeout: int = 10) -> GeocodeResult:
    params = {
        "q": address,
        "format": "jsonv2",
        "addressdetails": 1,
        "countrycodes": "us",
        "limit": 1,
    }
    resp = requests.get(
        NOMINATIM_URL, params=params, headers={"User-Agent": _USER_AGENT}, timeout=timeout
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise GeocodeError(f"No match for address: {address!r}")

    top = results[0]
    addr = top.get("address", {})
    state = addr.get("state", "")
    if state and state != "California":
        raise GeocodeError(
            f"{address!r} geocoded to {state}, not California — tcpgen is "
            f"CA-only. Check the address."
        )

    city = addr.get("city") or addr.get("town") or addr.get("village") or ""
    return GeocodeResult(
        lat=float(top["lat"]),
        lng=float(top["lon"]),
        display_name=top.get("display_name", address),
        city=city,
        zip=addr.get("postcode"),
    )
