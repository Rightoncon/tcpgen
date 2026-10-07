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
import re
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
from core.parcels import Parcel
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
WORK_AREA_OUTLINE = colors.HexColor("#ff6a00")  # bold safety orange, the work-area boundary specifically
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

# Cover-sheet general notes (added 2026-09-24, Michael's request to always
# submit a 3-sheet packet like a real permit application) -- written fresh
# in Right On Construction's own words, not copied from any other TCP
# vendor's document. Covers the same regulatory ground real submitted
# plans carry: CA MUTCD/Caltrans/CATTCH conformance, agency notification,
# PPE, no-parking signage, ADA pedestrian access, and Right On
# Construction's own liability language (not anyone else's).
GENERAL_NOTES: tuple[str, ...] = (
    "All traffic control devices and layouts shall conform to the current California Manual on Uniform Traffic Control Devices (CA MUTCD), the Caltrans Standard Plans, and the California Temporary Traffic Control Handbook (CA TTCH).",
    "Notify the local fire department, police/sheriff, the California Highway Patrol (state highways), the local school district, transit providers, and USPS at least 72 hours before starting work.",
    "All personnel on site shall wear a high-visibility vest and hard hat meeting ANSI/ISEA 107. Flaggers shall carry an R1-1/W20-8 STOP/SLOW paddle and be trained in proper flagging procedure.",
    "Post \"No Parking\" signs per local agency requirements, or at least 72 hours before setup where none applies — one sign at each end of the restricted space, spaced no more than 20 ft apart.",
    "Cover or remove any existing sign that conflicts with this plan for the duration of the work.",
    "Maintain emergency-vehicle and driveway access at all times.",
    "Keep buffer and transition areas clear of equipment, materials, and parked vehicles unless this plan specifically shows otherwise.",
    "Where a sidewalk is closed, place ADA-compliant pedestrian barricades at the point of closure (or Type I barricades placed in advance of it) and maintain a compliant path or detour per CA MUTCD 6H-28/6H-29.",
    "Place a W20-1 \"Road Work Ahead\" sign on every side street that falls within the advance warning area.",
    "Do not leave an open excavation unattended. Plate or backfill it during non-working hours, and post a W8-24 \"Steel Plate Ahead\" sign in advance whenever a plate is left in the right-of-way overnight.",
    "Keep signs and devices clear of any bike lane.",
    "For an open trench, place C27(CA) \"Open Trench\" signs at each end facing traffic within 15 ft of the trench, repeated at intervals no greater than 2,000 ft.",
    "Use flashing warning lights on advance-warning signs for work that occurs at night or extends into the hours of darkness.",
    "This plan governs the traffic control layout only. Right On Construction, Inc. is not responsible for field changes made without our written approval, or for use of this plan on any job or date other than the one shown above. Do not copy or reuse this plan for other work.",
)


# ---- filenames (spec §10.6) -------------------------------------------


def _short_address(raw_address: str) -> str:
    return raw_address.split(",")[0].strip()


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-")


def sheet_filenames(raw_address: str, *, date: Optional[dt.date] = None) -> tuple[str, str, str]:
    date = date or dt.date.today()
    date_str = date.isoformat()
    short = _sanitize_filename(_short_address(raw_address))
    sep = " — "  # space, em dash, space
    return (
        f"{date_str}{sep}TCP Sheet 0 General Notes{sep}{short}.pdf",
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


# (label, glyph, fill, border) -- glyph matches how the device is drawn on the
# map below, so the legend actually identifies the symbols (2026-09-28: every
# row used to be a dot, and Regulatory sign / Barricade were identical).
_LEGEND_ITEMS = (
    ("Cone", "cone", WORK_ZONE, WORK_ZONE),
    ("Warning sign", "diamond", WARN_FILL, WARN_BORDER),
    ("Regulatory sign", "diamond", colors.white, REG_BORDER),
    ("Flagger", "flagger", FLAGGER_COLOR, FLAGGER_COLOR),
    ("Barricade", "barricade", colors.white, REG_BORDER),
)


# Same type sizes as the info-panel fields above it (label 7pt bold, value
# 8pt) -- the legend was 6.5pt and too small to read (Michael, 2026-09-28).
_LEGEND_ROW_PT = 12      # vertical spacing between legend rows
_LEGEND_HEADER_PT = 14   # header sits this far above the first row


def _legend_items(only: Optional[set] = None):
    return [it for it in _LEGEND_ITEMS if only is None or it[1] in only]


def _draw_legend(c: canvas.Canvas, x: float, y: float, only: Optional[set] = None, title: str = "LEGEND") -> None:
    """`only` = glyph kinds to include (close-up: just the non-sign symbols on
    the plan, since each sign shows its own symbol in the SIGNS list)."""
    items = _legend_items(only)
    if not items:
        return
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(PANEL_LABEL)
    c.drawString(x, y + _LEGEND_HEADER_PT, title)
    for i, (label, glyph, fill, border) in enumerate(items):
        row_y = y - i * _LEGEND_ROW_PT
        gx, gy = x + 4.5, row_y + 2.8   # glyph centre, level with the text
        _draw_glyph(c, gx, gy, glyph, fill, border)
        c.setLineWidth(0.75)
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.white)
        c.drawString(x + 14, row_y, label)


def _draw_glyph(c: canvas.Canvas, gx: float, gy: float, glyph: str, fill, border) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(border)
    c.setLineWidth(0.75)
    if glyph == "cone":
        c.circle(gx, gy, 2.6, fill=1, stroke=1)
    elif glyph == "flagger":
        c.circle(gx, gy, 3.8, fill=1, stroke=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 5)
        c.drawCentredString(gx, gy - 1.8, "F")
    elif glyph == "barricade":
        bw, bh = 8.0, 3.0
        c.rect(gx - bw / 2, gy - bh / 2, bw, bh, fill=1, stroke=1)
        c.setStrokeColor(WORK_ZONE)
        c.setLineWidth(1)
        for k in range(3):
            sx = gx - bw / 2 + (k + 0.5) * (bw / 3)
            c.line(sx - bh / 2, gy - bh / 2, sx + bh / 2, gy + bh / 2)
    else:  # diamond sign
        r = 4.2
        path = c.beginPath()
        path.moveTo(gx, gy + r)
        path.lineTo(gx + r, gy)
        path.lineTo(gx, gy - r)
        path.lineTo(gx - r, gy)
        path.close()
        c.drawPath(path, fill=1, stroke=1)


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
    job_type: Optional[str] = None,
    usa_ticket: Optional[str] = None,
    signs: Optional[list] = None,
    other_kinds: Optional[set] = None,
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

    if job_type:
        field("Job Type", ", ".join(p.strip().title() for p in job_type.split(",") if p.strip()))
    field("Address", geo_display)
    field("Road", f"{road.name} ({road.width_ft:.0f} ft, {road.speed_mph} mph, {road.lanes} lanes)")
    field("Scope", scope.value.replace("_", " ").title())
    field("TA Figure", f"{ta_figure.key} — {ta_figure.name}")
    field("Flaggers", str(ta_figure.flaggers))
    # "TBD" until the number is issued (Michael, 2026-09-28).
    field("Permit #", permit_number or "TBD")
    field("USA North 811 Ticket #", usa_ticket or "TBD")
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

    # _draw_legend anchors its header _LEGEND_HEADER_PT above `y` and stacks len(_LEGEND_ITEMS)
    # rows at _LEGEND_ROW_PT each below that — needs ~10pt per row + 12pt header + a
    # few pt of margin above the panel's own bottom edge (`y`) or the
    # bottom rows land in the footer band and get painted over. Found by
    # actually rendering a sheet and looking at it, not by inspection —
    # see test_render_pdf.py for the regression.
    legend_only = (other_kinds or set()) if signs else None
    n_legend = len(_legend_items(legend_only))
    legend_base = y + 24 + _LEGEND_ROW_PT * max(n_legend - 1, 0)
    legend_top = (legend_base + _LEGEND_HEADER_PT + 10) if n_legend else y + 16

    if signs:
        # SIGNS list: number, code, wording, distance -- same sizes as the
        # legend; shrinks a step if a long sign set would reach the legend.
        rows = []
        for size in (8, 7, 6.5):
            rows = []
            for sg in signs:
                head = f"{sg['n']}. {sg['code']}  {sg['where']}"
                body = _wrap_text(sg["text"], w - 44, "Helvetica", size - 0.5, c) if sg["text"] else []
                rows.append((head, body, sg))
            need = 14 + sum((size + 2) * (1 + len(b)) + 3 for _h, b, _s in rows)
            if cursor_y - need > legend_top:
                break
        cursor_y -= 6
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(PANEL_LABEL)
        c.drawString(text_x, cursor_y, "SIGNS (numbers match the map)")
        cursor_y -= size + 6
        for head, body, sg in rows:
            if cursor_y < legend_top:
                break
            # the sign's own symbol, as drawn on the map, beside its entry
            _draw_glyph(c, text_x + 4.5, cursor_y + 2.8, "diamond", sg["fill"], sg["border"])
            c.setLineWidth(0.75)
            c.setFont("Helvetica-Bold", size)
            c.setFillColor(colors.white)
            c.drawString(text_x + 14, cursor_y, head)
            cursor_y -= size + 2
            c.setFont("Helvetica", size - 0.5)
            c.setFillColor(PANEL_LABEL)
            for line in body:
                c.drawString(text_x + 24, cursor_y, line)
                cursor_y -= size + 2
            cursor_y -= 3

    if signs:
        _draw_legend(c, text_x, legend_base, only=legend_only, title="OTHER SYMBOLS")
    else:
        _draw_legend(c, text_x, legend_base)


def _work_area_distance_text(station: float, work_area: WorkArea) -> str:
    """A sign's distance from the nearest end of the work area, which is
    what a reader of the plan cares about -- raw stations are measured
    from wherever the OSM way happens to start and mean nothing on paper."""
    if station < work_area.start_station_ft:
        return f"{work_area.start_station_ft - station:.0f}'"
    if station > work_area.end_station_ft:
        return f"{station - work_area.end_station_ft:.0f}'"
    return "at work area"


# Plain-English wording for sign codes whose Device carries no label.
_SIGN_TEXT = {
    "W20-1": "ROAD WORK AHEAD", "W21-5": "SHOULDER WORK", "G20-2": "END ROAD WORK",
    "R9-9": "SIDEWALK CLOSED", "R9-11": "SIDEWALK CLOSED AHEAD, CROSS HERE",
    "R9-11a": "SIDEWALK CLOSED, CROSS HERE", "W20-4": "ONE LANE ROAD AHEAD",
    "W20-7": "FLAGGER AHEAD", "W3-4": "BE PREPARED TO STOP", "W8-24": "STEEL PLATE AHEAD",
    "C27(CA)": "OPEN TRENCH", "W20-5": "LANE CLOSED AHEAD", "R11-2": "ROAD CLOSED",
}


def _number_signs(c: canvas.Canvas, targets: list[tuple], work_area: WorkArea) -> list[dict]:
    """Numbers each sign in station order with a small badge beside its
    diamond and returns the schedule for the side panel (Michael, 2026-10-07:
    the 5.5pt labels stacked along the bottom with long dashed leaders were
    too small to read -- the panel list is the same size as the legend)."""
    out, placed = [], []
    for n, (px, py, code, label, station, fill, border) in enumerate(sorted(targets, key=lambda t: t[4]), start=1):
        bx, by = px + 7.5, py + 7.5
        # Signs at the work area sit on top of each other -- step a badge
        # around its sign until it clears the badges already drawn.
        for dx, dy in ((7.5, 7.5), (7.5, -8), (-8, 7.5), (-8, -8), (7.5, 18), (-8, 18), (7.5, -18), (-8, -18), (18, 0), (-18, 0)):
            bx, by = px + dx, py + dy
            if all(math.hypot(bx - qx, by - qy) >= 11 for qx, qy in placed):
                break
        placed.append((bx, by))
        c.setStrokeColor(colors.HexColor("#64748b"))
        c.setLineWidth(0.4)
        c.line(px, py, bx, by)
        c.setFillColor(REG_BORDER)
        c.setStrokeColor(colors.white)
        c.setLineWidth(0.8)
        c.circle(bx, by, 5.2, fill=1, stroke=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 6.5 if n < 10 else 5.5)
        c.drawCentredString(bx, by - 2.3, str(n))
        out.append({"n": n, "code": code, "text": label or _SIGN_TEXT.get(code, ""), "fill": fill, "border": border,
                    "where": _work_area_distance_text(station, work_area)})
    return out


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
    parcel: Optional[Parcel] = None,
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

    # "NOT TO SCALE" disclaimer -- standard on a schematic TCP sheet since
    # device positions come from real station/offset math but the sheet
    # itself isn't drawn to an engineering scale. Top-left corner so it
    # never collides with the Mapbox attribution text (bottom-left).
    label = "NOT TO SCALE"
    c.setFont("Helvetica-Bold", 7.5)
    label_w = c.stringWidth(label, "Helvetica-Bold", 7.5) + 8
    c.setFillColor(colors.white)
    c.rect(x + 6, y + h - 20, label_w, 14, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#111111"))
    c.drawString(x + 10, y + h - 16, label)

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

    # Job-site marker: a small dot at the parcel centroid so the intended
    # house is unambiguous on the printed sheet, independent of whatever
    # address labels the Mapbox basemap does or doesn't render at this
    # crop (Michael, 2026-09-24: wants "just a small pin or dot" marking
    # the actual house, not reliance on reading a basemap label).
    if parcel is not None and parcel.polygon:
        centroid_lat = sum(p[0] for p in parcel.polygon) / len(parcel.polygon)
        centroid_lng = sum(p[1] for p in parcel.polygon) / len(parcel.polygon)
        pin_x, pin_y = to_page(centroid_lat, centroid_lng)
        c.setFillColor(colors.HexColor("#16a34a"))
        c.setStrokeColor(colors.white)
        c.setLineWidth(1)
        c.circle(pin_x, pin_y, 4.5, fill=1, stroke=1)

    # Work area — spec §10.3: dashed outline, never filled. Bold and
    # distinctly colored (2026-09-24, Michael) so it reads clearly against
    # a busy basemap instead of blending into the Mapbox road/label lines.
    c.setStrokeColor(WORK_AREA_OUTLINE)
    c.setLineWidth(3)
    c.setDash(5, 3)
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
            # bw/bh, not w/h -- reusing w/h here clobbered the map frame's
            # width, which squashed every stacked label into the frame's
            # bottom-left corner (997 Castle Hill Rd, 2026-09-24).
            bw, bh = 6.0, 2.2
            c.rect(px - bw / 2, py - bh / 2, bw, bh, fill=1, stroke=1)
            c.setStrokeColor(WORK_ZONE)
            c.setLineWidth(1)
            for i in range(3):
                sx = px - bw / 2 + (i + 0.5) * (bw / 3)
                c.line(sx - bh / 2, py - bh / 2, sx + bh / 2, py + bh / 2)
        else:  # sign -- diamond, matching the real MUTCD warning-sign
            # shape and the reference plan (413 Alameda de las Pulgas)'s
            # own schematic convention of drawing every sign as a diamond
            # regardless of its actual real-world shape.
            r = 4.5
            path = c.beginPath()
            path.moveTo(px, py + r)
            path.lineTo(px + r, py)
            path.lineTo(px, py - r)
            path.lineTo(px - r, py)
            path.close()
            c.drawPath(path, fill=1, stroke=1)
            if stack_labels and d.code:
                label_targets.append((px, py, d.code, d.label or "", d.station_ft, fill, border))

    if stack_labels:
        return _number_signs(c, label_targets, work_area)
    return []


def _build_notes_sheet(
    c: canvas.Canvas,
    *,
    raw_address: str,
    geo_display: str,
    road: RoadSegment,
    scope: Scope,
    ta_figure: TaFigure,
    permit_number: Optional[str],
    job_number: Optional[str],
    job_type: Optional[str],
    usa_ticket: Optional[str] = None,
) -> None:
    """Cover sheet: same header/footer chrome as sheets 1-2, a condensed
    job summary line, and the numbered GENERAL_NOTES list in two columns.
    Always generated alongside the other two -- a real permit packet, not
    an optional extra (Michael, 2026-09-24: "doesn\u2019t hurt to submit
    all three")."""
    house_number_match = re.match(r"\s*(\d+)", raw_address)
    road_label = f"{house_number_match.group(1)} {road.name}" if house_number_match else road.name
    _draw_header(c, "TRAFFIC CONTROL PLAN", f"{road_label} — {scope.value.replace('_', ' ').title()}")
    _draw_footer(c, geo_display, ta_figure.key)

    body_x = MARGIN
    body_w = PAGE_W - 2 * MARGIN
    body_top = PAGE_H - HEADER_H - 14
    min_y = FOOTER_H + 10

    c.setFillColor(colors.HexColor("#111111"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(body_x, body_top, "SHEET 0 — COVER / GENERAL NOTES")

    summary_parts = [geo_display]
    if job_type:
        summary_parts.append(", ".join(p.strip().title() for p in job_type.split(",") if p.strip()))
    summary_parts.append(f"TA {ta_figure.key} — {ta_figure.name}")
    summary_parts.append(f"Permit # {permit_number or 'TBD'}")
    summary_parts.append(f"USA North 811 Ticket # {usa_ticket or 'TBD'}")
    if job_number:
        summary_parts.append(f"Job # {job_number}")
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#4b5563"))
    c.drawString(body_x, body_top - 14, "   \u2022   ".join(summary_parts))

    c.setStrokeColor(colors.HexColor("#d1d5db"))
    c.setLineWidth(0.5)
    c.line(body_x, body_top - 22, body_x + body_w, body_top - 22)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.HexColor("#111111"))
    c.drawString(body_x, body_top - 36, "GENERAL NOTES")

    col_gap = 24.0
    col_w = (body_w - col_gap) / 2
    col_x = [body_x, body_x + col_w + col_gap]
    font, text_size, line_h, note_gap = "Helvetica", 7.5, 10.0, 4.0

    # Split by count (not a fill-then-overflow greedy pack) so the page
    # reads as two evenly balanced columns instead of one full column and
    # one mostly-empty one -- 15 short notes never come close to
    # overflowing a single column at this font size otherwise.
    split_at = -(-len(GENERAL_NOTES) // 2)  # ceil division
    columns = [
        list(enumerate(GENERAL_NOTES[:split_at], start=1)),
        list(enumerate(GENERAL_NOTES[split_at:], start=split_at + 1)),
    ]
    for col, notes in enumerate(columns):
        y = body_top - 52
        for i, note in notes:
            prefix = f"{i}. "
            indent = c.stringWidth(prefix, font, text_size)
            lines = _wrap_text(note, col_w - indent, font, text_size, c)
            needed_h = len(lines) * line_h + note_gap
            if y - needed_h < min_y:
                break  # ran out of room -- shouldn’t happen at 15 notes, but never overlap the footer
            c.setFont(font, text_size)
            c.setFillColor(colors.HexColor("#111111"))
            c.drawString(col_x[col], y, prefix)
            for j, line in enumerate(lines):
                c.drawString(col_x[col] + indent, y - j * line_h, line)
            y -= needed_h


def _build_sheet(
    c: canvas.Canvas,
    *,
    sheet_label: str,
    raw_address: str,
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
    job_type: Optional[str] = None,
    parcel: Optional[Parcel] = None,
    usa_ticket: Optional[str] = None,
) -> None:
    map_x, map_y = 0.0, float(FOOTER_H)
    map_w = PAGE_W - PANEL_W
    map_h = PAGE_H - HEADER_H - FOOTER_H

    device_points = [(d.lat, d.lng) for d in devices]
    center_lat = sum(p[0] for p in device_points) / len(device_points)
    center_lng = sum(p[1] for p in device_points) / len(device_points)
    base_zoom = choose_zoom(device_points, center_lat, center_lng, map_w, map_h)
    zoom = max(1, base_zoom + zoom_offset)

    signs = _draw_map(
        c, map_x, map_y, map_w, map_h,
        road=road, centerline=centerline, devices=devices, work_area=work_area,
        zoom=zoom, stack_labels=stack_labels, work_zone_shape=work_zone_shape,
        parcel=parcel,
    )
    _draw_info_panel(
        c, map_w, float(FOOTER_H), PANEL_W, PAGE_H - HEADER_H - FOOTER_H,
        sheet_label=sheet_label, geo_display=geo_display, road=road, scope=scope,
        ta_figure=ta_figure, permit_number=permit_number, job_number=job_number, warnings=warnings,
        job_type=job_type, usa_ticket=usa_ticket, signs=signs,
        other_kinds={d.kind for d in devices if d.kind in ("cone", "flagger", "barricade")},
    )
    house_number_match = re.match(r"\s*(\d+)", raw_address)
    road_label = f"{house_number_match.group(1)} {road.name}" if house_number_match else road.name
    _draw_header(c, "TRAFFIC CONTROL PLAN", f"{road_label} — {scope.value.replace('_', ' ').title()}")
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
    job_type: Optional[str] = None,
    parcel: Optional[Parcel] = None,
    usa_ticket: Optional[str] = None,
) -> tuple[str, str, str]:
    """spec §10.4/§10.6 — builds all three sheets (cover/general notes,
    close-up, wide area) and writes them to `out_dir`. Always all three --
    a real permit packet, not an a-la-carte set (2026-09-24).
    Returns (notes_path, sheet1_path, sheet2_path)."""
    os.makedirs(out_dir, exist_ok=True)
    centerline = Centerline(road.coords)
    notes_name, sheet1_name, sheet2_name = sheet_filenames(raw_address)
    notes_path = os.path.join(out_dir, notes_name)
    sheet1_path = os.path.join(out_dir, sheet1_name)
    sheet2_path = os.path.join(out_dir, sheet2_name)

    common = dict(
        raw_address=raw_address, geo_display=geo.display_name, road=road, centerline=centerline, devices=devices,
        work_area=work_area, scope=scope, ta_figure=ta_figure,
        permit_number=permit_number, job_number=job_number, warnings=warnings, job_type=job_type,
        parcel=parcel, usa_ticket=usa_ticket,
    )

    c0 = canvas.Canvas(notes_path, pagesize=(PAGE_W, PAGE_H))
    _build_notes_sheet(
        c0, raw_address=raw_address, geo_display=geo.display_name, road=road, scope=scope,
        ta_figure=ta_figure, permit_number=permit_number, job_number=job_number, job_type=job_type,
        usa_ticket=usa_ticket,
    )
    c0.save()

    c1 = canvas.Canvas(sheet1_path, pagesize=(PAGE_W, PAGE_H))
    _build_sheet(c1, sheet_label="SHEET 1 — CLOSE-UP", zoom_offset=0, stack_labels=True, work_zone_shape="rect", **common)
    c1.save()

    c2 = canvas.Canvas(sheet2_path, pagesize=(PAGE_W, PAGE_H))
    _build_sheet(c2, sheet_label="SHEET 2 — WIDE AREA", zoom_offset=-2, stack_labels=False, work_zone_shape="circle", **common)
    c2.save()

    return notes_path, sheet1_path, sheet2_path
