"""Device layout (spec §8) — turns a RoadSegment + WorkArea + TaFigure into
a positioned list of Device rows. This is geometry (§7) plus rules (§6)
combined; still fully deterministic, no LLM.

A note on clamp_offset and signs: spec §7.3 says "every device offset
passes through" clamp_offset, and §14 makes "no device outside the road
edge" the single most important test in the whole project. Taken literally
that also clamps signs, even though §8.1's own arithmetic places signs at
`road_width_ft/2 + 6` — deliberately beyond the paved edge, which is where
real MUTCD signs stand. Rather than special-case signs out of the clamp
(quietly weakening the one invariant the spec calls out as most important),
this module clamps every device uniformly. In practice that means a sign's
*intended* shoulder offset gets pulled back to sit right at the clamped
edge instead of literally on the shoulder — a real simplification, not a
bug, and worth revisiting if a future phase wants a distinct "shoulder
easement" beyond road_width_ft. See test_layout.py for the invariant this
buys back.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional

from core.geometry import Centerline, clamp_offset, to_wgs84
from core.parcels import Parcel, frontage_stations, work_side
from core.roads import RoadSegment
from core.rules import (
    Scope,
    TaFigure,
    buffer_space_ft,
    device_spacing_ft,
    downstream_taper_ft,
    sign_spacing_ft,
    taper_length_ft,
)

DEFAULT_FRONTAGE_HALF_FT = 30.0  # spec §8.2 — 60 ft default frontage, centered
SIDEWALK_EDGE_BUFFER_FT = 4.0  # spec §8.1 behind_curb cone-line buffer
SHOULDER_EDGE_BUFFER_FT = 8.0  # spec §8.1 shoulder cone-line buffer (approximates a parking-lane width)
SIGN_SHOULDER_OFFSET_FT = 6.0  # spec §8.1: signs at road_width_ft/2 + 6
FLAGGER_EDGE_BUFFER_FT = 3.0  # spec §8.1: flaggers at road_width_ft/2 - 3
CONE_RUN_PAD_FT = 25.0  # spec §8.1: cone run extends 25 ft past the work area
FLAGGER_STANDOFF_FT = 50.0  # spec §8.1: flagger stands 50 ft upstream of the taper
CROSS_STREET_SIGN_STANDOFF_FT = 50.0  # sign stands this far back from the intersection
CROSS_STREET_SIGN_SIDE = 1  # fixed shoulder side -- see _build_cross_street_signs docstring


@dataclass
class Device:
    kind: str  # cone | sign | flagger | barricade | arrow | label
    station_ft: float
    offset_ft: float
    lat: float
    lng: float
    code: Optional[str] = None
    label: Optional[str] = None
    approach: Optional[str] = None  # 'A' | 'B'
    seq: int = 0


@dataclass
class WorkArea:
    start_station_ft: float
    end_station_ft: float
    near_offset_ft: float  # signed, road-side edge of the work
    far_offset_ft: float  # signed, far edge of the work
    side: int  # +1 or -1, matches near/far's sign


def default_work_area(
    parcel: Optional[Parcel],
    centerline: Centerline,
    road: RoadSegment,
    scope: Scope,
    geocoded_point: tuple[float, float],
) -> WorkArea:
    """spec §8.2 — synthesizes a work area when no polygon was drawn
    (always true for Phase 2's Express-mode CLI; the draw tool is a later,
    web-layer phase). Full frontage errs long, which the spec calls out as
    the safe direction — a cone run longer than strictly needed isn't a
    compliance problem, a short one is."""
    if parcel is not None:
        s0, s1 = frontage_stations(parcel, centerline)
    else:
        s = centerline.station_of_nearest(*geocoded_point)
        s0, s1 = s - DEFAULT_FRONTAGE_HALF_FT, s + DEFAULT_FRONTAGE_HALF_FT

    side = work_side(parcel, centerline, geocoded_point)

    if scope == Scope.BEHIND_CURB:
        near = side * (road.width_ft / 2 + 1)
        far = side * (road.width_ft / 2 + 12)
    else:  # SHOULDER / ONE_LANE / CUL_DE_SAC
        near = side * (road.width_ft / 2 - 10)
        far = side * (road.width_ft / 2 - 1)

    return WorkArea(start_station_ft=s0, end_station_ft=s1, near_offset_ft=near, far_offset_ft=far, side=side)


def work_area_from_polygon(polygon: list[tuple[float, float]], centerline: Centerline) -> WorkArea:
    """Builds a WorkArea directly from an operator-drawn polygon (each
    vertex projected to station/offset against the road centerline)
    instead of synthesizing one from parcel frontage -- the manual
    override for when the auto-computed footprint lands on the wrong
    address (the 631-vs-635-Costa-Rica class of bug: a real parcel
    existed, just not the operator's own). Side is decided the same way
    work_side() decides it for a parcel -- the sign of the median vertex
    offset -- so a slightly wobbly hand-drawn polygon still resolves to
    one consistent side of the road."""
    stations = []
    offsets = []
    for lat, lng in polygon:
        s, o = centerline.station_and_offset_of_nearest(lat, lng)
        stations.append(s)
        offsets.append(o)
    side = 1 if statistics.median(offsets) >= 0 else -1
    same_side = [o for o in offsets if (o >= 0) == (side >= 0)] or offsets
    near = side * min(abs(o) for o in same_side)
    far = side * max(abs(o) for o in same_side)
    return WorkArea(
        start_station_ft=min(stations),
        end_station_ft=max(stations),
        near_offset_ft=near,
        far_offset_ft=far,
        side=side,
    )


def check_scope_matches_geometry(work_area: WorkArea, road: RoadSegment, scope: Scope) -> Optional[str]:
    """spec §8.1 step 3: cross-check the scope dropdown against where the
    work area actually sits. Returns a warning string to show the operator,
    or None — never silently overrides their choice."""
    behind_curb_geometry = abs(work_area.near_offset_ft) >= road.width_ft / 2
    if behind_curb_geometry and scope != Scope.BEHIND_CURB:
        return (
            f"The work area (offset {work_area.near_offset_ft:.0f} ft) looks like it's "
            f"behind the curb, but scope is {scope.value!r} — double-check the scope."
        )
    if not behind_curb_geometry and scope == Scope.BEHIND_CURB:
        return (
            f"Scope is 'behind_curb' but the work area (offset {work_area.near_offset_ft:.0f} ft) "
            f"looks like it's inside the roadway (half-width {road.width_ft / 2:.0f} ft) — "
            f"double-check the scope."
        )
    return None


def _place(
    centerline: Centerline,
    road_width_ft: float,
    station_ft: float,
    offset_ft: float,
    *,
    kind: str,
    code: Optional[str] = None,
    label: Optional[str] = None,
    approach: Optional[str] = None,
    seq: int = 0,
) -> Device:
    clamped = clamp_offset(offset_ft, road_width_ft)
    utm_pt = centerline.offset_point(station_ft, clamped)
    lat, lng = to_wgs84([utm_pt], centerline.crs)[0]
    return Device(
        kind=kind,
        station_ft=station_ft,
        offset_ft=clamped,
        lat=lat,
        lng=lng,
        code=code,
        label=label,
        approach=approach,
        seq=seq,
    )


def _cone_run(centerline, road, start_station, end_station, offset_ft, *, spacing_ft, approach, start_seq):
    devices = []
    seq = start_seq
    stations = []
    s = start_station
    while s < end_station:
        stations.append(s)
        s += spacing_ft
    # Always include the endpoint, but not as a near-duplicate of the last
    # regular step — spacing rarely divides the run evenly, so without this
    # check a real rendered sheet shows two cone markers almost on top of
    # each other right at the end of the run.
    if not stations or (end_station - stations[-1]) > spacing_ft * 0.25:
        stations.append(end_station)
    for st in stations:
        devices.append(_place(centerline, road.width_ft, st, offset_ft, kind="cone", approach=approach, seq=seq))
        seq += 1
    return devices


def _pedestrian_package(centerline, road, work_area, side, seq_start):
    """spec §6.2 pedestrian package. R9-11's "nearest crossing" placement
    needs real intersection data this phase doesn't have — placed at the
    work area's near edge as a stand-in. Replace once a phase adds
    intersection detection.

    Also places an ADA-compliant pedestrian barricade at each end of the
    closed run — a real gap caught against a live reference plan (413
    Alameda de las Pulgas, City Rise Safety, 2026-09-23): their own
    General Note 19 is explicit that "ADA PEDESTRIAN BARRICADES ARE
    REQUIRED AT THE POINT OF CLOSURE" whenever a sidewalk is closed,
    which is exactly this flag — the device kind existed in the schema
    from Phase 1 but nothing ever actually placed one."""
    devices = []
    seq = seq_start
    offset = side * (road.width_ft / 2 + 1)
    for station in (work_area.start_station_ft, work_area.end_station_ft):
        devices.append(
            _place(centerline, road.width_ft, station, offset, kind="sign", code="R9-9", label="SIDEWALK CLOSED", seq=seq)
        )
        seq += 1
    devices.append(
        _place(
            centerline,
            road.width_ft,
            work_area.start_station_ft,
            offset,
            kind="sign",
            code="R9-11",
            label="SIDEWALK CLOSED CROSS HERE",
            seq=seq,
        )
    )
    seq += 1
    for station in (work_area.start_station_ft, work_area.end_station_ft):
        devices.append(
            _place(
                centerline, road.width_ft, station, offset, kind="barricade",
                label="ADA Pedestrian Barricade", seq=seq,
            )
        )
        seq += 1
    return devices


def _build_ta2(centerline, road, work_area, ta_figure, scope, sidewalk_affected):
    """spec §8.1 — TA-2/TA-3, no lane closure."""
    devices = []
    side = work_area.side
    buffer_in_ft = SIDEWALK_EDGE_BUFFER_FT if scope == Scope.BEHIND_CURB else SHOULDER_EDGE_BUFFER_FT
    cone_offset = side * (road.width_ft / 2 - buffer_in_ft)

    cone_start = work_area.start_station_ft - CONE_RUN_PAD_FT
    cone_end = work_area.end_station_ft + CONE_RUN_PAD_FT
    spacing = device_spacing_ft(road.speed_mph, on_taper=False)
    devices += _cone_run(centerline, road, cone_start, cone_end, cone_offset, spacing_ft=spacing, approach=None, start_seq=0)

    spacing_sign = sign_spacing_ft(road.speed_mph)
    for approach, anchor, direction, sign_side in (
        ("A", work_area.start_station_ft, -1, side),
        ("B", work_area.end_station_ft, 1, -side),
    ):
        for sign in ta_figure.signs_per_approach:
            station = anchor + direction * sign.station_multiplier * spacing_sign
            offset = sign_side * (road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
            devices.append(
                _place(centerline, road.width_ft, station, offset, kind="sign", code=sign.code, label=sign.text, approach=approach, seq=len(devices))
            )

    if ta_figure.end_sign:
        end_station = work_area.end_station_ft + CONE_RUN_PAD_FT
        offset = side * (road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
        devices.append(
            _place(
                centerline, road.width_ft, end_station, offset, kind="sign",
                code=ta_figure.end_sign_code, label=ta_figure.end_sign_text, approach="A", seq=len(devices),
            )
        )

    if sidewalk_affected:
        devices += _pedestrian_package(centerline, road, work_area, side, len(devices))

    return devices


def _build_ta10(centerline, road, work_area, ta_figure, speed_mph, sidewalk_affected):
    """spec §8.1 — TA-10, one lane closed with flaggers.

    Limitation: all stations resolve against a single OSM way. A short way
    segment can force an upstream flagger/taper station below 0 (or past
    the way's end), which `Centerline` clamps to the segment's own start/
    end rather than actually extending onto the next way. Stitching
    adjoining ways into one centerline is future work, not Phase 2.
    """
    devices = []
    side = work_area.side

    closure_width = road.width_ft / 2 - 2
    taper_len = taper_length_ft(closure_width, speed_mph)
    taper_spacing = device_spacing_ft(speed_mph, on_taper=True)
    tangent_spacing = device_spacing_ft(speed_mph, on_taper=False)
    downstream_len = downstream_taper_ft(ta_figure.lanes_closed)
    buffer_ft = buffer_space_ft(speed_mph)

    # Merging taper: upstream of the work area, offset ramps from the
    # closure width down to 0 over taper_len.
    taper_start_station = work_area.start_station_ft - taper_len
    n_taper = max(2, math.ceil(taper_len / taper_spacing) + 1)
    for i in range(n_taper):
        frac = i / (n_taper - 1)
        station = taper_start_station + frac * taper_len
        offset = side * closure_width * (1 - frac)
        devices.append(_place(centerline, road.width_ft, station, offset, kind="cone", approach="A", seq=len(devices)))

    # Tangent cone run at offset 0 through the work area plus buffer.
    tangent_start = work_area.start_station_ft
    tangent_end = work_area.end_station_ft + buffer_ft
    devices += _cone_run(centerline, road, tangent_start, tangent_end, 0.0, spacing_ft=tangent_spacing, approach=None, start_seq=len(devices))

    # Downstream taper back out, 100 ft per lane.
    n_down = max(2, math.ceil(downstream_len / taper_spacing) + 1)
    for i in range(n_down):
        frac = i / (n_down - 1)
        station = tangent_end + frac * downstream_len
        offset = side * closure_width * frac
        devices.append(_place(centerline, road.width_ft, station, offset, kind="cone", approach="B", seq=len(devices)))

    # Flaggers.
    flagger_offset = side * (road.width_ft / 2 - FLAGGER_EDGE_BUFFER_FT)
    flagger_a_station = work_area.start_station_ft - buffer_ft - taper_len - FLAGGER_STANDOFF_FT
    flagger_b_station = tangent_end + downstream_len + FLAGGER_STANDOFF_FT
    if ta_figure.flaggers >= 1:
        devices.append(
            _place(centerline, road.width_ft, flagger_a_station, flagger_offset, kind="flagger", label="Flagger A", approach="A", seq=len(devices))
        )
    if ta_figure.flaggers >= 2:
        devices.append(
            _place(centerline, road.width_ft, flagger_b_station, -flagger_offset, kind="flagger", label="Flagger B", approach="B", seq=len(devices))
        )

    # Advance-warning signs upstream of each flagger, 3x/2x/1x spacing.
    spacing_sign = sign_spacing_ft(speed_mph)
    for approach, anchor_station, direction, sign_side in (
        ("A", flagger_a_station, -1, side),
        ("B", flagger_b_station, 1, -side),
    ):
        for sign in ta_figure.signs_per_approach:
            station = anchor_station + direction * sign.station_multiplier * spacing_sign
            offset = sign_side * (road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
            devices.append(
                _place(centerline, road.width_ft, station, offset, kind="sign", code=sign.code, label=sign.text, approach=approach, seq=len(devices))
            )

    if ta_figure.end_sign:
        offset = side * (road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
        devices.append(
            _place(
                centerline, road.width_ft, tangent_end + downstream_len + 25, offset, kind="sign",
                code=ta_figure.end_sign_code, label=ta_figure.end_sign_text, approach="A", seq=len(devices),
            )
        )

    if sidewalk_affected:
        devices += _pedestrian_package(centerline, road, work_area, side, len(devices))

    return devices


def _build_cul_de_sac(centerline, road, work_area, ta_figure, speed_mph, sidewalk_affected):
    """spec §8.1: "one flagger at the junction station, signs on the
    approach road only, no devices inside the bulb." The "junction
    station" is approximated as the work area's near edge — this phase has
    no bulb-specific geometry, just the single approach road leading to
    it."""
    devices = []
    side = work_area.side
    flagger_offset = side * (road.width_ft / 2 - FLAGGER_EDGE_BUFFER_FT)
    flagger_station = work_area.start_station_ft
    devices.append(
        _place(centerline, road.width_ft, flagger_station, flagger_offset, kind="flagger", label="Flagger", approach="A", seq=len(devices))
    )

    spacing_sign = sign_spacing_ft(speed_mph)
    for sign in ta_figure.signs_per_approach:
        station = flagger_station - sign.station_multiplier * spacing_sign
        offset = side * (road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
        devices.append(
            _place(centerline, road.width_ft, station, offset, kind="sign", code=sign.code, label=sign.text, approach="A", seq=len(devices))
        )

    if sidewalk_affected:
        devices += _pedestrian_package(centerline, road, work_area, side, len(devices))

    return devices


def advance_warning_window(work_area: WorkArea, ta_figure: TaFigure, speed_mph: int) -> tuple[float, float]:
    """Station range covering both approaches' advance-warning signage —
    used to decide which cross streets count as "within the advance
    warning area" for side-street signage (reference plan General Note 8).
    A single symmetric window around both ends rather than exactly
    mirroring each TA-builder's own per-approach station math, which would
    mean duplicating each builder's internal logic here."""
    if not ta_figure.signs_per_approach:
        return work_area.start_station_ft, work_area.end_station_ft
    max_mult = max(s.station_multiplier for s in ta_figure.signs_per_approach)
    advance = max_mult * sign_spacing_ft(speed_mph)
    return work_area.start_station_ft - advance, work_area.end_station_ft + advance


def _build_cross_street_signs(cross_streets: list[dict], seq_start: int) -> list[Device]:
    """Reference plan (413 Alameda de las Pulgas), General Note 8: "PLACE
    W20-1 'ROAD WORK AHEAD' SIGNS ON ALL SIDE STREETS WITHIN THE ADVANCE
    WARNING AREA." `cross_streets` comes from core.roads.find_cross_streets
    — real geometric intersections, not a guess.

    Placed `CROSS_STREET_SIGN_STANDOFF_FT` back from the intersection
    along the cross street's own centerline, at a fixed shoulder side
    (`CROSS_STREET_SIGN_SIDE`) — this phase has no reliable way to know
    which side actually has a shoulder/is buildable on an arbitrary side
    street, so the side is a known simplification, same spirit as the
    R9-11 "nearest crossing" stand-in elsewhere in this file. `approach`
    is set to "X" (cross-street) so build_device_plan's road-edge
    invariant check — which is about the *main* road's width — skips
    these; they're clamped against their own cross street's width
    instead, via the same `_place` used everywhere else."""
    devices = []
    seq = seq_start
    for cs in cross_streets:
        cross_road = cs["road"]
        try:
            cross_cl = Centerline(cross_road.coords)
        except ValueError:
            continue  # degenerate geometry (shouldn't happen with real OSM data); skip rather than crash
        station_at_intersection = cross_cl.station_of_nearest(*cs["point"])
        sign_station = max(0.0, station_at_intersection - CROSS_STREET_SIGN_STANDOFF_FT)
        offset = CROSS_STREET_SIGN_SIDE * (cross_road.width_ft / 2 + SIGN_SHOULDER_OFFSET_FT)
        devices.append(
            _place(
                cross_cl, cross_road.width_ft, sign_station, offset,
                kind="sign", code="W20-1", label=f"ROAD WORK AHEAD — {cross_road.name}",
                approach="X", seq=seq,
            )
        )
        seq += 1
    return devices


def build_device_plan(
    centerline: Centerline,
    road: RoadSegment,
    work_area: WorkArea,
    scope: Scope,
    ta_figure: TaFigure,
    *,
    sidewalk_affected: bool = False,
    cross_streets: Optional[list[dict]] = None,
) -> tuple[list[Device], list[str]]:
    """spec §8.1. Returns (devices, warnings) — warnings are things the
    operator should double-check, not failures. `cross_streets` (optional)
    is core.roads.find_cross_streets' own output — pass it to get side-
    street "ROAD WORK AHEAD" signage; omit it (or pass []) to skip that."""
    warnings = []
    mismatch = check_scope_matches_geometry(work_area, road, scope)
    if mismatch:
        warnings.append(mismatch)

    if scope in (Scope.BEHIND_CURB, Scope.SHOULDER):
        devices = _build_ta2(centerline, road, work_area, ta_figure, scope, sidewalk_affected)
    elif scope == Scope.ONE_LANE:
        devices = _build_ta10(centerline, road, work_area, ta_figure, road.speed_mph, sidewalk_affected)
    elif scope == Scope.CUL_DE_SAC:
        devices = _build_cul_de_sac(centerline, road, work_area, ta_figure, road.speed_mph, sidewalk_affected)
    else:
        raise ValueError(f"Unsupported scope for layout: {scope}")

    if cross_streets:
        devices += _build_cross_street_signs(cross_streets, len(devices))

    # spec §7.3 bug-killer, restated as an explicit invariant: every device
    # must be inside the road edge. _place() clamps every device before
    # this ever runs, so this should never actually fire — it's the "fail
    # loudly in dev" backstop the spec asks for. Cross-street signs
    # (approach == "X") are clamped against their own street's width, not
    # this one, so they're exempt from this specific check by design.
    for d in devices:
        if d.approach == "X":
            continue
        if abs(d.offset_ft) > road.width_ft / 2 + 1e-6:
            raise AssertionError(
                f"{d.kind} {d.code or ''} ended up outside the road edge: "
                f"offset={d.offset_ft:.1f} ft, road half-width={road.width_ft / 2:.1f} ft"
            )

    return devices, warnings
