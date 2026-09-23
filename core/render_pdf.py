"""PDF sheet rendering (spec §10). Two landscape-letter sheets per plan:
a close-up and a wide area, both built from the same real device geometry
(spec §1.1 — never a screenshot).

The road/work-zone/device overlay is always drawn as vector graphics from
real computed geometry. Underneath it, `_draw_map` composites a fetched
Mapbox Static Images raster when config.MAPBOX_TOKEN is set (core/
basemap.py); if it's missing or the fetch fails, that's non-fatal — the
sheet still renders on the plain background, just without real-world
context. Same pixel projection (`core.geometry.latlng_to_pixel`) drives
both the raster placement and the vector overlay, so they line up.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import os
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from config import CONTRACTOR_NAME, CSLB_NUMBER, MAPBOX_TOKEN
from core.basemap import BasemapUnavailableError, fetch_basemap_png
from core.geometry import Centerline, choose_zoom, latlng_to_pixel, to_wgs84
from core.geocode import GeocodeResult
from core.layout import Device, WorkArea
from core.roads import RoadSegment
from core.rules import Scope, TaFigure

PAGE_W, PAGE_H = landscape(letter)  # 792 x 612 pt, spec §10.1
HEADER_H = 35
FOOTER_H = 18
PANEL_W = 196
MARGIN = 8

# spec §10.2 palette
NAVY = colors.HexColor("#0f3460")
RED_RULE = colors.HexColor("#e94560")
WORK_ZONE = colors.HexColor("#ea580c")
WARN_FILL = colors.HexColor("#f59e0b")
WARN_BORDER = colors.HexColor("#78350f")
REG_BORDER = colors.HexColor("#111111")
FLAGGER_COLOR = colors.HexColor("#dc2626")
DIR_A = colors.HexColor("#2563eb")
DIR_B = colors.HexColor("#16a34a")
PANEL_BG = colors.HexColor("#0d1b2a")
PANEL_WARNING_BG = colors.HexColor("#2d1f00")
PANEL_LABEL = colors.HexColor("#93a5c4")

# spec §10.5 — mandatory prohibition, removed at the client's instruction.
# Kept here (not spelled out further) so grep/tests can find the one
# canonical string this module must never emit.
BANNED_PHRASE = "not for permit submittal without licensed engineer review"
DRAFT_BADGE_TEXT = "DRAFT — PERMIT PENDING"


# ---- filenames (spec §10.6) -------------------------------------------


def _short_address(raw_address: str) -> str:
    return raw_address.split(",")[0].strip()


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-")


def sheet_filenames(raw_address: str, *, date: Optional[dt.date] = None) -> tuple[str, str]:
    date = date or dt.date.today()
    date_str = date.isoformat()
    short = _sanitize_filename(_short_address(raw_address))
    sep = " — "  # space, em dash, space
    return (
        f"{date_str}{sep}TCP Sheet 1 Close-Up{sep}{short}.pdf",
        f"{date_str}{sep}TCP Sheet 2 Wide Area{sep}{short}.pdf",
    )


# ---- small drawing helpers ----------------------------------------------


def _wrap_text(text: str, max_width: float, font: str, size: float, c: canvas.Canvas) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if not current or c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _device_fill_and_border(d: Device) -> tuple[colors.Color, colors.Color]:
    if d.kind == "flagger":
        return FLAGGER_COLOR, FLAGGER_COLOR
    if d.kind == "cone":
        return WORK_ZONE, WORK_ZONE
    if d.kind == "barricade":
        return colors.white, REG_BORDER
    if d.code and d.code.startswith("R9"):
        return colors.white, REG_BORDER
    return WARN_FILL, WARN_BORDER


def _draw_header(c: canvas.Canvas, title: str, subtitle: str) -> None:
    c.setFillColor(NAVY)
    c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
    c.setStrokeColor(RED_RULE)
    c.setLineWidth(2)
    c.line(0, PAGE_H - HEADER_H, PAGE_W, PAGE_H - HEADER_H)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN, PAGE_H - HEADER_H + 18, title)
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, PAGE_H - HEADER_H + 6, subtitle)

    c.setFont("Helvetica-Bold", 10)
    badge_w = c.stringWidth(DRAFT_BADGE_TEXT, "Helvetica-Bold", 10) + 14
    badge_x = PAGE_W - MARGIN - badge_w
    badge_y = PAGE_H - HEADER_H + 8
    c.setFillColor(RED_RULE)
    c.roundRect(badge_x, badge_y, badge_w, 18, 3, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.drawCentredString(badge_x + badge_w / 2, badge_y + 5, DRAFT_BADGE_TEXT)


def _draw_footer(c: canvas.Canvas, address: str, ta_key: str) -> None:
    c.setFillColor(colors.HexColor("#f3f4f6"))
    c.rect(0, 0, PAGE_W, FOOTER_H, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor("#d1d5db"))
    c.setLineWidth(0.5)
    c.line(0, FOOTER_H, PAGE_W, FOOTER_H)

    c.setFillColor(colors.HexColor("#111111"))
    c.setFont("Helvetica", 7)
    cslb = f"  |  {CSLB_NUMBER}" if CSLB_NUMBER else ""
    c.drawString(MARGIN, 6, f"{address}  |  {CONTRACTOR_NAME}{cslb}")
    c.drawRightString(PAGE_W - MARGIN, 6, f"TA {ta_key}   DRAFT")


_LEGEND_ITEMS = (
    ("Cone", WORK_ZONE, WORK_ZONE),
    ("Warning sign", WARN_FILL, WARN_BORDER),
    ("Regulatory sign", colors.white, REG_BORDER),
    ("Flagger", FLAGGER_COLOR, FLAGGER_COLOR),
    ("Barricade", colors.white, REG_BORDER),
)


def _draw_legend(c: canvas.Canvas, x: float, y: float) -> None:
    c.setFont("Helvetica-Bold", 6.5)
    c.setFillColor(PANEL_LABEL)
    c.drawString(x, y + 12, "LEGEND")
    for i, (label, fill, border) in enumerate(_LEGEND_ITEMS):
        row_y = y - i * 10
        c.setFillColor(fill)
        c.setStrokeColor(border)
        c.circle(x + 4, row_y + 2, 3, fill=1, stroke=1)
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.white)
        c.drawString(x + 12, row_y, label)


def _draw_info_panel(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    sheet_label: str,
    geo_display: str,
    road: RoadSegment,
    scope: Scope,
    ta_figure: TaFigure,
    permit_number: Optional[str],
    job_number: Optional[str],
    warnings: list[str],
) -> None:
    c.setFillColor(PANEL_BG)
    c.rect(x, y, w, h, fill=1, stroke=0)
    text_x = x + 10
    cursor_y = y + h - 20

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(text_x, cursor_y, sheet_label)
    cursor_y -= 20

    def field(label: str, value: str) -> None:
        nonlocal cursor_y
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(PANEL_LABEL)
        c.drawString(text_x, cursor_y, label.upper())
        cursor_y -= 10
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.white)
        for line in _wrap_text(value, w - 20, "Helvetica", 8, c):
            c.drawString(text_x, cursor_y, line)
            cursor_y -= 10
        cursor_y -= 5

    field("Address", geo_display)
    field("Road", f"{road.name} ({road.width_ft:.0f} ft, {road.speed_mph} mph, {road.lanes} lanes)")
    field("Scope", scope.value.replace("_", " ").title())
    field("TA Figure", f"{ta_figure.key} — {ta_figure.name}")
    field("Flaggers", str(ta_figure.flaggers))
    field("Permit #", permit_number or "____________")
    field("Job #", job_number or "____________")

    if warnings:
        cursor_y -= 4
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(colors.HexColor("#fbbf24"))
        c.drawString(text_x, cursor_y, "REVIEW BEFORE USE")
        cursor_y -= 10
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.white)
        for warn in warnings:
            for line in _wrap_text(f"• {warn}", w - 20, "Helvetica", 6.5, c):
                c.drawString(text_x, cursor_y, line)
                cursor_y -= 8
            cursor_y -= 2

    # _draw_legend anchors its header 12pt above `y` and stacks len(_LEGEND_ITEMS)
    # rows at 10pt each below that — needs ~10pt per row + 12pt header + a
    # few pt of margin above the panel's own bottom edge (`y`) or the
    # bottom rows land in the footer band and get painted over. Found by
    # actually rendering a sheet and looking at it, not by inspection —
    # see test_render_pdf.py for the regression.
    _draw_legend(c, text_x, y + 14 + 10 * (len(_LEGEND_ITEMS) - 1))


def _stack_labels(c: canvas.Canvas, x: float, y: float, w: float, targets: list[tuple]) -> None:
    """spec §10.4 Sheet 1: 'labels, sign boxes stacked in the white parcel
    margins with dashed leaders back to their station.' Stacked along the
    bottom of the map frame, sorted by station, each with a dashed leader
    back to its marker."""
    if not targets:
        return
    targets = sorted(targets, key=lambda t: t[4])
    strip_h = 14
    col_w = w / len(targets)
    for i, (px, py, code, _label, station) in enumerate(targets):
        col_x = x + i * col_w + 2
        text_y = y + 3
        c.setStrokeColor(colors.HexColor("#94a3b8"))
        c.setLineWidth(0.5)
        c.setDash(2, 2)
        c.line(px, py, col_x, text_y + strip_h)
        c.setDash()
        c.setFillColor(REG_BORDER)
        c.setFont("Helvetica", 5.5)
        c.drawString(col_x, text_y, f"{code} {station:.0f}'")


def _draw_arrow(c: canvas.Canvas, x1: float, y1: float, x2: float, y2: float, color: colors.Color, label: str) -> None:
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(2)
    c.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    head_len = 6
    for da in (-0.4, 0.4):
        hx = x2 - head_len * math.cos(angle + da)
        hy = y2 - head_len * math.sin(angle + da)
        c.line(x2, y2, hx, hy)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(x2 + 3, y2 - 2, label)


def _draw_map(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    road: RoadSegment,
    centerline: Centerline,
    devices: list[Device],
    work_area: WorkArea,
    zoom: int,
    stack_labels: bool,
    work_zone_shape: str,
) -> None:
    c.setFillColor(colors.HexColor("#eef1f5"))
    c.rect(x, y, w, h, fill=1, stroke=0)

    device_points = [(d.lat, d.lng) for d in devices]
    center_lat = sum(p[0] for p in device_points) / len(device_points)
    center_lng = sum(p[1] for p in device_points) / len(device_points)
    scale = 2

    # Real satellite/street imagery underneath the vector overlay, when a
    # basemap token is configured. Non-fatal if it isn't, or if the fetch
    # fails — the sheet still renders, just without real-world context
    # (spec §1.2's fallback philosophy, applied here too).
    try:
        png_bytes = fetch_basemap_png(center_lat, center_lng, zoom, int(round(w)), int(round(h)), scale=scale)
        c.drawImage(ImageReader(io.BytesIO(png_bytes)), x, y, width=w, height=h, preserveAspectRatio=False, mask="auto")
    except BasemapUnavailableError:
        pass

    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.setLineWidth(1)
    c.rect(x, y, w, h, fill=0, stroke=1)

    def to_page(lat: float, lng: float) -> tuple[float, float]:
        px, py = latlng_to_pixel(lat, lng, center_lat, center_lng, zoom, w, h, scale)
        # latlng_to_pixel's origin is top-left in web-mercator convention;
        # PDF's is bottom-left, so flip y. Divide by scale since it returns
        # "retina" (2x) pixels per spec §7.5's own signature.
        return x + px / scale, y + h - py / scale

    # Road centerline — real OSM geometry, drawn as a vector line on top of
    # whatever's underneath (spec §1.1: the map is never the source of
    # truth for geometry, only a background — this line is the actual
    # computed centerline, shown as a cross-check against the basemap's
    # own rendering of the road).
    c.setStrokeColor(colors.HexColor("#f8fafc") if MAPBOX_TOKEN else colors.HexColor("#9ca3af"))
    c.setLineWidth(1.5)
    path = c.beginPath()
    for i, (lat, lng) in enumerate(road.coords):
        px, py = to_page(lat, lng)
        if i == 0:
            path.moveTo(px, py)
        else:
            path.lineTo(px, py)
    c.drawPath(path, stroke=1, fill=0)

    # Work area — spec §10.3: dashed outline, never filled.
    c.setStrokeColor(WORK_ZONE)
    c.setLineWidth(1.5)
    c.setDash(4, 3)
    if work_zone_shape == "circle":
        mid_station = (work_area.start_station_ft + work_area.end_station_ft) / 2
        mid_utm = centerline.offset_point(mid_station, 0)
        mid_lat, mid_lng = to_wgs84([mid_utm], centerline.crs)[0]
        cx, cy = to_page(mid_lat, mid_lng)
        c.circle(cx, cy, max(10.0, w * 0.06), fill=0, stroke=1)
    else:
        corners = [
            (work_area.start_station_ft, work_area.near_offset_ft),
            (work_area.end_station_ft, work_area.near_offset_ft),
            (work_area.end_station_ft, work_area.far_offset_ft),
            (work_area.start_station_ft, work_area.far_offset_ft),
        ]
        corner_px = []
        for station, offset in corners:
            utm_pt = centerline.offset_point(station, offset)
            lat, lng = to_wgs84([utm_pt], centerline.crs)[0]
            corner_px.append(to_page(lat, lng))
        wz_path = c.beginPath()
        wz_path.moveTo(*corner_px[0])
        for pt in corner_px[1:]:
            wz_path.lineTo(*pt)
        wz_path.close()
        c.drawPath(wz_path, stroke=1, fill=0)
    c.setDash()

    if work_zone_shape == "circle":
        stations = [d.station_ft for d in devices]
        near_station, far_station = min(stations) - 20, max(stations) + 20
        for station, color, label, direction in (
            (near_station, DIR_A, "A", -1),
            (far_station, DIR_B, "B", 1),
        ):
            base_utm = centerline.offset_point(station, 0)
            tip_utm = centerline.offset_point(station + direction * 40, 0)
            bx, by = to_page(*to_wgs84([base_utm], centerline.crs)[0])
            tx, ty = to_page(*to_wgs84([tip_utm], centerline.crs)[0])
            _draw_arrow(c, bx, by, tx, ty, color, label)

    label_targets = []
    for d in devices:
        px, py = to_page(d.lat, d.lng)
        fill, border = _device_fill_and_border(d)
        c.setFillColor(fill)
        c.setStrokeColor(border)
        c.setLineWidth(0.75)
        if d.kind == "cone":
            c.circle(px, py, 2.2, fill=1, stroke=1)
        elif d.kind == "flagger":
            c.circle(px, py, 3.5, fill=1, stroke=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 5)
            c.drawCentredString(px, py - 1.8, "F")
        elif d.kind == "barricade":
            # Small white rail with diagonal orange stripes -- the classic
            # Type I/II barricade look, distinct from a plain sign square.
            w, h = 6.0, 2.2
            c.rect(px - w / 2, py - h / 2, w, h, fill=1, stroke=1)
            c.setStrokeColor(WORK_ZONE)
            c.setLineWidth(1)
            for i in range(3):
                sx = px - w / 2 + (i + 0.5) * (w / 3)
                c.line(sx - h / 2, py - h / 2, sx + h / 2, py + h / 2)
        else:  # sign
            size = 4
            c.rect(px - size / 2, py - size / 2, size, size, fill=1, stroke=1)
            if stack_labels and d.code:
                label_targets.append((px, py, d.code, d.label or "", d.station_ft))

    if stack_labels:
        _stack_labels(c, x, y, w, label_targets)


def _build_sheet(
    c: canvas.Canvas,
    *,
    sheet_label: str,
    geo_display: str,
    road: RoadSegment,
    centerline: Centerline,
    devices: list[Device],
    work_area: WorkArea,
    scope: Scope,
    ta_figure: TaFigure,
    permit_number: Optional[str],
    job_number: Optional[str],
    warnings: list[str],
    zoom_offset: int,
    stack_labels: bool,
    work_zone_shape: str,
) -> None:
    map_x, map_y = 0.0, float(FOOTER_H)
    map_w = PAGE_W - PANEL_W
    map_h = PAGE_H - HEADER_H - FOOTER_H

    device_points = [(d.lat, d.lng) for d in devices]
    center_lat = sum(p[0] for p in device_points) / len(device_points)
    center_lng = sum(p[1] for p in device_points) / len(device_points)
    base_zoom = choose_zoom(device_points, center_lat, center_lng, map_w, map_h)
    zoom = max(1, base_zoom + zoom_offset)

    _draw_map(
        c, map_x, map_y, map_w, map_h,
        road=road, centerline=centerline, devices=devices, work_area=work_area,
        zoom=zoom, stack_labels=stack_labels, work_zone_shape=work_zone_shape,
    )
    _draw_info_panel(
        c, map_w, float(FOOTER_H), PANEL_W, PAGE_H - HEADER_H - FOOTER_H,
        sheet_label=sheet_label, geo_display=geo_display, road=road, scope=scope,
        ta_figure=ta_figure, permit_number=permit_number, job_number=job_number, warnings=warnings,
    )
    _draw_header(c, "TRAFFIC CONTROL PLAN", f"{road.name} — {scope.value.replace('_', ' ').title()}")
    _draw_footer(c, geo_display, ta_figure.key)
    c.showPage()


def render_plan_pdfs(
    raw_address: str,
    geo: GeocodeResult,
    road: RoadSegment,
    work_area: WorkArea,
    scope: Scope,
    ta_figure: TaFigure,
    devices: list[Device],
    warnings: list[str],
    out_dir: str,
    *,
    permit_number: Optional[str] = None,
    job_number: Optional[str] = None,
) -> tuple[str, str]:
    """spec §10.4/§10.6 — builds both sheets and writes them to `out_dir`.
    Returns (sheet1_path, sheet2_path)."""
    os.makedirs(out_dir, exist_ok=True)
    centerline = Centerline(road.coords)
    sheet1_name, sheet2_name = sheet_filenames(raw_address)
    sheet1_path = os.path.join(out_dir, sheet1_name)
    sheet2_path = os.path.join(out_dir, sheet2_name)

    common = dict(
        geo_display=geo.display_name, road=road, centerline=centerline, devices=devices,
        work_area=work_area, scope=scope, ta_figure=ta_figure,
        permit_number=permit_number, job_number=job_number, warnings=warnings,
    )

    c1 = canvas.Canvas(sheet1_path, pagesize=(PAGE_W, PAGE_H))
    _build_sheet(c1, sheet_label="SHEET 1 — CLOSE-UP", zoom_offset=0, stack_labels=True, work_zone_shape="rect", **common)
    c1.save()

    c2 = canvas.Canvas(sheet2_path, pagesize=(PAGE_W, PAGE_H))
    _build_sheet(c2, sheet_label="SHEET 2 — WIDE AREA", zoom_offset=-2, stack_labels=False, work_zone_shape="circle", **common)
    c2.save()

    return sheet1_path, sheet2_path
