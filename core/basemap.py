"""Static basemap raster fetch + cache (spec §3). The image is a
background only — it carries no geometry data. Every device position
still comes from the real station/offset pipeline in core/layout.py, never
from anything measured off this image (spec §1.1's core design rule).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import requests

from config import BASEMAP_PROVIDER, MAPBOX_TOKEN

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "basemaps"
MAPBOX_STYLE = "mapbox/light-v11"  # lighter/fewer labels than streets-v12 -- easy revert if Michael prefers streets-v12 back
MAX_DIMENSION_PX = 1280  # Mapbox Static Images API limit (logical size, pre-@2x)


class BasemapUnavailableError(Exception):
    """No basemap provider configured, the request was invalid, or the
    fetch failed. Callers should treat this as non-fatal — spec's own
    §1.2 fallback philosophy ("the app must still work end to end")
    applies here too: a missing/failed basemap degrades the sheet, it
    never breaks plan generation."""


def _cache_key(lat: float, lng: float, zoom: int, width_px: int, height_px: int, scale: int, style: str) -> str:
    raw = f"{style}|{lat:.6f}|{lng:.6f}|{zoom}|{width_px}|{height_px}|{scale}"
    return hashlib.sha1(raw.encode()).hexdigest()


def fetch_basemap_png(
    center_lat: float,
    center_lng: float,
    zoom: int,
    width_px: int,
    height_px: int,
    *,
    scale: int = 2,
    style: str = MAPBOX_STYLE,
    timeout: int = 15,
) -> bytes:
    """PNG bytes for a static Mapbox map centered on (center_lat,
    center_lng). Cached on disk by request parameters, so re-rendering the
    same plan never re-fetches or re-bills."""
    if BASEMAP_PROVIDER != "mapbox" or not MAPBOX_TOKEN:
        raise BasemapUnavailableError(
            "No basemap provider configured — set MAPBOX_TOKEN in .env (see config.py)."
        )
    if width_px > MAX_DIMENSION_PX or height_px > MAX_DIMENSION_PX:
        raise BasemapUnavailableError(
            f"Requested basemap {width_px}x{height_px} exceeds Mapbox's {MAX_DIMENSION_PX}px static-image limit."
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(center_lat, center_lng, zoom, width_px, height_px, scale, style)
    cache_path = CACHE_DIR / f"{key}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    scale_suffix = "@2x" if scale == 2 else ""
    url = (
        f"https://api.mapbox.com/styles/v1/{style}/static/"
        f"{center_lng},{center_lat},{zoom}/{width_px}x{height_px}{scale_suffix}"
    )
    try:
        resp = requests.get(url, params={"access_token": MAPBOX_TOKEN}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise BasemapUnavailableError(f"Mapbox static image fetch failed: {exc}") from exc

    cache_path.write_bytes(resp.content)
    return resp.content
