"""Express-mode CLI (spec §13 Phase 2 deliverable, now with Phase 3 PDF
output) — no web UI. Proves the whole address-to-device-list pipeline
works end to end:

    python -m tools.plan --address "157 San Marco Ave, San Bruno CA" --scope behind_curb
    python -m tools.plan --address "..." --scope one_lane --pdf

Per spec §13: "If it produces a correct device list from an address and a
scope, the hard part is done."
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from core.geocode import GeocodeError, GeocodeResult, geocode_address
from core.geometry import Centerline
from core.layout import Device, WorkArea, build_device_plan, default_work_area
from core.parcels import (
    NoParcelProvider,
    Parcel,
    SanMateoArcGIS,
    detect_corner_lot,
    is_implausible_frontage,
)
from core.roads import RoadNotFoundError, RoadSegment, choose_road, find_roads_near
from core.rules import (
    PEDESTRIAN_NOTE,
    Scope,
    ScopeNotSupportedError,
    TaFigure,
    TaFigureNotBuiltError,
    get_ta_figure,
    load_ta_figures,
)


def build_plan(
    address: str,
    scope_str: str,
    *,
    sidewalk: bool = False,
    street_hint: Optional[str] = None,
) -> tuple[GeocodeResult, RoadSegment, Optional[Parcel], WorkArea, TaFigure, list[Device], list[str]]:
    scope = Scope(scope_str)
    geo = geocode_address(address)

    roads = find_roads_near(geo.lat, geo.lng)
    road = choose_road(roads, geo.lat, geo.lng, street_hint=street_hint)
    centerline = Centerline(road.coords)

    parcel = SanMateoArcGIS().parcel_at(geo.lat, geo.lng)
    if parcel is None:
        parcel = NoParcelProvider().parcel_at(geo.lat, geo.lng)  # always None; documents the fallback path

    warnings: list[str] = []
    if parcel is None:
        warnings.append("No parcel found — using a 60 ft default frontage. Adjust the length or draw the work area.")
    else:
        if is_implausible_frontage(parcel, centerline):
            warnings.append(
                "Frontage on this street is implausibly long (>250 ft) — likely a corner lot; confirm the fronting street."
            )
        if detect_corner_lot(parcel, roads):
            warnings.append(
                "Parcel touches 2+ candidate streets within 15 ft — confirm the fronting street before generating."
            )

    work_area = default_work_area(parcel, centerline, road, scope, (geo.lat, geo.lng))

    figures = load_ta_figures()
    ta_figure = get_ta_figure(scope, figures)

    devices, layout_warnings = build_device_plan(
        centerline, road, work_area, scope, ta_figure, sidewalk_affected=sidewalk
    )
    warnings.extend(layout_warnings)
    if sidewalk:
        warnings.append(PEDESTRIAN_NOTE)

    return geo, road, parcel, work_area, ta_figure, devices, warnings


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="tcpgen Express-mode plan builder (no PDF/UI yet)")
    parser.add_argument("--address", required=True)
    parser.add_argument("--scope", required=True, choices=[s.value for s in Scope])
    parser.add_argument("--sidewalk", action="store_true", help="sidewalk is affected (adds the pedestrian package)")
    parser.add_argument("--street", default=None, help="street name hint, for intersections")
    parser.add_argument("--pdf", action="store_true", help="also render both PDF sheets (spec §10)")
    parser.add_argument("--out-dir", default="out", help="directory for rendered PDFs (default: out/)")
    parser.add_argument("--permit", default=None, help="permit number, printed on the sheets")
    parser.add_argument("--job", default=None, help="job number, printed on the sheets")
    args = parser.parse_args(argv)

    try:
        geo, road, parcel, work_area, ta_figure, devices, warnings = build_plan(
            args.address, args.scope, sidewalk=args.sidewalk, street_hint=args.street
        )
    except (GeocodeError, RoadNotFoundError, ScopeNotSupportedError, TaFigureNotBuiltError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Address: {geo.display_name}")
    print(f"Road: {road.name} ({road.highway}, {road.width_ft:.0f} ft wide, {road.speed_mph} mph, {road.lanes} lanes)")
    if parcel:
        print(f"Parcel: APN {parcel.apn} ({parcel.situs_address})")
    print(f"TA figure: {ta_figure.key} — {ta_figure.name}")
    side_sign = "+" if work_area.side > 0 else "-"
    print(f"Work area: station {work_area.start_station_ft:.0f}-{work_area.end_station_ft:.0f} ft, side {side_sign}")

    print(f"\n{len(devices)} devices:")
    for d in sorted(devices, key=lambda d: (d.approach or "", d.seq)):
        code_label = f" {d.code} {d.label}" if d.code else ""
        approach = f"[{d.approach}] " if d.approach else ""
        print(
            f"  {approach}{d.kind:<8}{code_label:<35} "
            f"station={d.station_ft:7.1f}ft offset={d.offset_ft:6.1f}ft  ({d.lat:.6f}, {d.lng:.6f})"
        )

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")

    if args.pdf:
        from core.render_pdf import render_plan_pdfs

        sheet1, sheet2 = render_plan_pdfs(
            args.address, geo, road, work_area, Scope(args.scope), ta_figure, devices, warnings,
            args.out_dir, permit_number=args.permit, job_number=args.job,
        )
        print(f"\nWrote:\n  {sheet1}\n  {sheet2}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
