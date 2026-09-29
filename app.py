"""Flask routes (spec §11) — thin. All real logic lives in core/; this
file just wires HTTP <-> those functions <-> db.py.

Phase 4 scope: the Express-mode flow end to end — form, live map
(pin/road/parcel), generate, preview, render PDF. Later additions: an
edit-in-place flow (POST /api/plan/<id>/regenerate), a Job/PO address
picker backed by the portal's internal API, and the Leaflet.draw
work-area polygon tool (an optional manual override -- see
core.layout.work_area_from_polygon). Still not built: device dragging
(PATCH /api/device/<id>) and POST /api/plan/<id>/reference. No
authentication either — this sits on the same VPS as the portal but
isn't gated by it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
import requests

import config

from core.geocode import GeocodeError, geocode_address
from core.geometry import Centerline
from core.layout import (
    Device,
    advance_warning_window,
    build_device_plan,
    default_work_area,
    work_area_from_polygon,
)
from core.parcels import Parcel, SanMateoArcGIS, detect_corner_lot, frontage_length_ft, is_implausible_frontage
from core.render_pdf import render_plan_pdfs
from core.roads import (
    RoadNotFoundError,
    RoadSegment,
    choose_road,
    find_cross_streets,
    find_roads_near,
    find_roads_within,
)
from core.rules import (
    PEDESTRIAN_NOTE,
    Scope,
    ScopeNotSupportedError,
    TaFigureNotBuiltError,
    get_ta_figure,
    load_ta_figures,
)
from db import get_conn, init_db, log_audit

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"

# Job type is a separate label from `scope` (Behind Curb/Shoulder/One Lane/
# Cul-de-sac), not a replacement for it -- a Trench job, for instance, can
# legitimately be any of those scopes depending on the specific job, so this
# is purely a categorization/labeling field, never used to restrict or
# infer the scope picker.
JOB_TYPES = ("trench", "sidewalk", "apron", "street")

app = Flask(__name__)
init_db()


# ---- helpers: dataclass <-> GeoJSON <-> DB row -----------------------------


def _house_number(address: str) -> str | None:
    """Leading digits of a typed address ("635 Costa Rica Ave" -> "635"),
    used to cross-check a geocoded point against the county's own SITUS_ADDR
    data (core.parcels.SanMateoArcGIS.parcel_for_address) -- a geocoder's
    interpolated point can land inside the wrong neighboring parcel."""
    m = re.match(r"\s*(\d+)", address)
    return m.group(1) if m else None


def _linestring_geojson(coords_latlng: list[tuple[float, float]]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [[lng, lat] for lat, lng in coords_latlng]})


def _polygon_geojson(ring_latlng: list[tuple[float, float]]) -> str:
    ring = [[lng, lat] for lat, lng in ring_latlng]
    if ring and ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def _road_to_dict(road: RoadSegment) -> dict:
    return {
        "osm_id": road.osm_id, "name": road.name, "coords": [[lat, lng] for lat, lng in road.coords],
        "width_ft": road.width_ft, "lanes": road.lanes, "speed_mph": road.speed_mph,
        "oneway": road.oneway, "highway": road.highway,
    }


def _road_from_plan_row(row) -> RoadSegment:
    geometry = json.loads(row["road_geometry"])
    coords = [(lat, lng) for lng, lat in geometry["coordinates"]]
    return RoadSegment(
        osm_id=row["road_osm_id"], name=row["road_name"], coords=coords,
        width_ft=row["road_width_ft"], lanes=row["road_lanes"] or 2,
        speed_mph=row["posted_speed"], oneway=False, highway="",
    )


def _parcel_from_plan_row(row) -> Parcel | None:
    if not row["parcel_polygon"]:
        return None
    geometry = json.loads(row["parcel_polygon"])
    ring = geometry["coordinates"][0]
    polygon = [(lat, lng) for lng, lat in ring]
    return Parcel(apn=row["parcel_apn"], polygon=polygon, situs_address=None, source="stored")


def _load_devices(conn, plan_id: int) -> list[Device]:
    rows = conn.execute("SELECT * FROM device WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
    return [
        Device(
            kind=r["kind"], station_ft=r["station_ft"], offset_ft=r["offset_ft"],
            lat=r["lat"], lng=r["lng"], code=r["code"], label=r["label"],
            approach=r["approach"], seq=r["seq"],
        )
        for r in rows
    ]


# ---- pages ------------------------------------------------------------


@app.route("/")
def index():
    with get_conn() as conn:
        plans = conn.execute(
            "SELECT id, created_at, job_type, address, scope, ta_figure, status FROM plan ORDER BY created_at DESC"
        ).fetchall()
    return render_template("index.html", plans=plans)


@app.route("/plan/new")
def plan_new():
    return render_template("express.html", edit_plan=None)


@app.route("/plan/<int:plan_id>/edit")
def plan_edit(plan_id: int):
    with get_conn() as conn:
        plan_row = conn.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
        if plan_row is None:
            abort(404)
    return render_template("express.html", edit_plan=dict(plan_row))


@app.route("/plan/<int:plan_id>/preview")
def plan_preview(plan_id: int):
    with get_conn() as conn:
        plan_row = conn.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
        if plan_row is None:
            abort(404)
        devices = _load_devices(conn, plan_id)
        create_audit = conn.execute(
            "SELECT detail FROM audit WHERE plan_id=? AND action='create' ORDER BY id DESC LIMIT 1", (plan_id,)
        ).fetchone()

    warnings: list[str] = []
    if create_audit and create_audit["detail"]:
        try:
            warnings = json.loads(create_audit["detail"]).get("warnings", [])
        except (json.JSONDecodeError, AttributeError):
            pass

    devices_json = json.dumps([dataclasses.asdict(d) for d in devices])
    return render_template("preview.html", plan=plan_row, devices=devices, devices_json=devices_json, warnings=warnings)


@app.route("/out/<path:filename>")
def serve_pdf(filename: str):
    # no-store: a re-render overwrites the same dated filename, so any
    # browser-cached copy is a stale plan -- 997 Castle Hill Rd
    # (2026-09-24) opened the pre-fix Glennan PDF after a correct re-render.
    resp = send_from_directory(OUT_DIR, filename, as_attachment=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- API: lookups used by the live map (spec §11, §12.2) ------------------


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    data = request.get_json(force=True) or {}
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "address is required"}), 400
    try:
        geo = geocode_address(address)
    except GeocodeError as exc:
        return jsonify({"error": str(exc)}), 422
    return jsonify(
        {"lat": geo.lat, "lng": geo.lng, "display_name": geo.display_name, "city": geo.city, "zip": geo.zip, "jurisdiction": None}
    )


@app.route("/api/roads", methods=["POST"])
def api_roads():
    data = request.get_json(force=True) or {}
    lat, lng = data.get("lat"), data.get("lng")
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    try:
        roads = find_roads_near(float(lat), float(lng))
    except RoadNotFoundError as exc:
        return jsonify({"error": str(exc)}), 422
    return jsonify({"roads": [_road_to_dict(r) for r in roads]})


def _push_permits_to_portal(plan_id: int) -> None:
    """Send this plan's permit # and USA North 811 ticket # to the Mi RoC
    portal so its job sheet and JobFlow job show them (2026-09-28). Sends the
    plan's FULL current state (or deleted=True) -- the portal re-files the
    plan under whatever job # it has now, so a changed or cleared job #
    moves/removes it. Best effort: the portal being down never blocks
    saving a plan; the next save re-sends."""
    if not config.PORTAL_INTERNAL_TOKEN:
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, job_number, permit_number, usa_ticket, address FROM plan WHERE id=?", (plan_id,)
        ).fetchone()
    body = {"plan_id": plan_id, "deleted": row is None}
    if row is not None:
        body.update(job_number=row["job_number"], permit_number=row["permit_number"],
                    usa_ticket=row["usa_ticket"], address=row["address"])
    try:
        requests.post(config.PORTAL_JOB_PERMITS_URL,
                      headers={"X-Internal-Token": config.PORTAL_INTERNAL_TOKEN},
                      json=body, timeout=8).raise_for_status()
    except requests.RequestException as exc:
        app.logger.warning("portal job-permits push failed for plan %s: %s", plan_id, exc)


@app.route("/api/portal-jobs")
def api_portal_jobs():
    """Proxies the Mi RoC portal's active Job/PO address list so the
    shared internal token never reaches the browser -- same reasoning as
    the portal's own job_quote() JobTable-credential proxy. Failure here
    (portal down, token unset) degrades to an empty list, never an error
    page -- manual address entry always still works."""
    if not config.PORTAL_INTERNAL_TOKEN:
        return jsonify({"jobs": []})
    try:
        r = requests.post(
            config.PORTAL_JOBS_URL,
            headers={"X-Internal-Token": config.PORTAL_INTERNAL_TOKEN},
            json={}, timeout=8,
        )
        r.raise_for_status()
        jobs = (r.json() or {}).get("jobs", [])
    except requests.RequestException:
        jobs = []
    return jsonify({"jobs": jobs})


@app.route("/api/parcel", methods=["POST"])
def api_parcel():
    data = request.get_json(force=True) or {}
    lat, lng = data.get("lat"), data.get("lng")
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    address = data.get("address") or ""
    parcel = SanMateoArcGIS().parcel_for_address(float(lat), float(lng), house_number=_house_number(address))
    if parcel is None:
        return jsonify({"parcel": None})
    centroid_lat = sum(p[0] for p in parcel.polygon) / len(parcel.polygon)
    centroid_lng = sum(p[1] for p in parcel.polygon) / len(parcel.polygon)
    return jsonify(
        {"parcel": {
            "apn": parcel.apn, "situs_address": parcel.situs_address,
            "polygon": [[lat, lng] for lat, lng in parcel.polygon],
            "centroid": [centroid_lat, centroid_lng],
        }}
    )


# ---- API: create + read a plan --------------------------------------------


class _PlanBuildError(Exception):
    """Carries the right HTTP status code alongside the message, so both
    create and regenerate can raise from the same shared pipeline and each
    just re-wrap it as its own jsonify() response."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _build_plan_from_payload(data: dict) -> dict:
    """The full geocode -> road -> parcel -> work_area -> ta_figure ->
    cross_streets -> devices pipeline, shared by plan creation (api_express)
    and plan editing (api_plan_regenerate) -- one code path for "here are
    the inputs, build the plan," regardless of whether the result becomes
    a new row or replaces an existing one."""
    address = (data.get("address") or "").strip()
    scope_str = data.get("scope")
    if not address or not scope_str:
        raise _PlanBuildError("address and scope are required")
    try:
        scope = Scope(scope_str)
    except ValueError:
        raise _PlanBuildError(f"invalid scope {scope_str!r}")

    sidewalk = bool(data.get("sidewalk"))
    parking = bool(data.get("parking"))
    street_hint = data.get("street") or None
    permit_number = data.get("permit_number") or None
    usa_ticket = (data.get("usa_ticket") or "").strip() or None
    job_number = data.get("job_number") or None
    notes = data.get("notes") or None

    # Job Type is multi-select (2026-09-24) -- a real job is often more
    # than one of Trench/Sidewalk/Apron/Street at once (Michael: "sometimes
    # all of the above"). Stored as a comma-joined string in the single
    # `job_type` TEXT column rather than a schema change -- display code
    # (templates, render_pdf.py) splits it back apart.
    job_type_list = data.get("job_type") or []
    if isinstance(job_type_list, str):
        job_type_list = [job_type_list] if job_type_list else []
    invalid = [jt for jt in job_type_list if jt not in JOB_TYPES]
    if invalid:
        raise _PlanBuildError(f"invalid job_type {invalid!r} (allowed: {', '.join(JOB_TYPES)})")
    job_type = ",".join(job_type_list) if job_type_list else None

    raw_polygon = data.get("work_area_polygon")
    work_area_polygon = None
    if raw_polygon and len(raw_polygon) >= 3:
        try:
            work_area_polygon = [(float(p[0]), float(p[1])) for p in raw_polygon]
        except (TypeError, ValueError, IndexError):
            raise _PlanBuildError("invalid work_area_polygon")

    try:
        geo = geocode_address(address)
    except GeocodeError as exc:
        raise _PlanBuildError(str(exc), 422)

    try:
        roads = find_roads_near(geo.lat, geo.lng)
        road = choose_road(roads, geo.lat, geo.lng, street_hint=street_hint)
    except RoadNotFoundError as exc:
        raise _PlanBuildError(str(exc), 422)

    if data.get("road_width_ft"):
        road.width_ft = float(data["road_width_ft"])
    if data.get("posted_speed_mph"):
        road.speed_mph = int(data["posted_speed_mph"])

    centerline = Centerline(road.coords)
    parcel = SanMateoArcGIS().parcel_for_address(geo.lat, geo.lng, house_number=_house_number(address))
    corner_lot = bool(parcel and detect_corner_lot(parcel, roads))

    warnings: list[str] = []
    if work_area_polygon:
        # Operator drew the footprint directly -- this is the deliberate
        # fix for a bad auto-guess, so skip the parcel-quality warnings
        # below; they'd just be noise once the operator has already
        # visually confirmed placement.
        frontage_source = "drawn"
    elif parcel is None:
        warnings.append("No parcel found — using a 60 ft default frontage. Adjust the length or draw the work area.")
        frontage_source = "default"
    else:
        if is_implausible_frontage(parcel, centerline):
            warnings.append("Frontage on this street is implausibly long (>250 ft) — likely a corner lot; confirm the fronting street.")
        if corner_lot:
            warnings.append("Parcel touches 2+ candidate streets within 15 ft — confirm the fronting street before generating.")
        frontage_source = "parcel"

    if work_area_polygon:
        work_area = work_area_from_polygon(work_area_polygon, centerline)
    else:
        work_area = default_work_area(parcel, centerline, road, scope, (geo.lat, geo.lng))

    try:
        ta_figure = get_ta_figure(scope, load_ta_figures())
    except (ScopeNotSupportedError, TaFigureNotBuiltError) as exc:
        raise _PlanBuildError(str(exc), 422)

    station_min, station_max = advance_warning_window(work_area, ta_figure, road.speed_mph)
    # A dedicated, full-radius candidate pool -- `roads` (find_roads_near's
    # output) stops widening as soon as it finds anything, which is almost
    # always just this job's own road, so it can't be reused here.
    cross_candidates = find_roads_within(geo.lat, geo.lng)
    cross_streets = find_cross_streets(road, centerline, cross_candidates, station_min, station_max)

    devices, layout_warnings = build_device_plan(
        centerline, road, work_area, scope, ta_figure, sidewalk_affected=sidewalk, cross_streets=cross_streets
    )
    warnings.extend(layout_warnings)
    if sidewalk:
        warnings.append(PEDESTRIAN_NOTE)

    if work_area_polygon:
        frontage_len = work_area.end_station_ft - work_area.start_station_ft
    else:
        frontage_len = frontage_length_ft(parcel, centerline) if parcel else None

    return {
        "job_type": job_type, "address": address, "geo": geo, "road": road, "parcel": parcel,
        "scope": scope, "sidewalk": sidewalk, "parking": parking, "permit_number": permit_number,
        "usa_ticket": usa_ticket,
        "job_number": job_number, "notes": notes, "ta_figure": ta_figure, "devices": devices,
        "warnings": warnings, "corner_lot": corner_lot, "frontage_len": frontage_len,
        "work_area_polygon": work_area_polygon, "frontage_source": frontage_source,
    }


def _plan_write_params(b: dict) -> tuple:
    """Column values in the same order for both the INSERT (create) and
    UPDATE (regenerate) statements below -- keeps the two from silently
    drifting apart."""
    return (
        b["job_type"], b["address"], b["geo"].city, b["geo"].zip,
        b["permit_number"], b["usa_ticket"], b["job_number"], b["geo"].lat, b["geo"].lng,
        _polygon_geojson(b["work_area_polygon"]) if b.get("work_area_polygon") else None,
        b["notes"],
        b["scope"].value, int(b["sidewalk"]), int(b["parking"]), b["road"].speed_mph, b["ta_figure"].key,
        b["road"].osm_id, b["road"].name, _linestring_geojson(b["road"].coords), b["road"].width_ft, b["road"].lanes,
        b["parcel"].apn if b["parcel"] else None, _polygon_geojson(b["parcel"].polygon) if b["parcel"] else None,
        b["frontage_source"], b["frontage_len"], int(b["corner_lot"]),
    )


def _insert_devices(conn, plan_id: int, devices: list[Device]) -> None:
    for d in devices:
        conn.execute(
            """INSERT INTO device (plan_id, kind, code, label, station_ft, offset_ft, lat, lng, approach, seq, locked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (plan_id, d.kind, d.code, d.label, d.station_ft, d.offset_ft, d.lat, d.lng, d.approach, d.seq),
        )


@app.route("/api/express", methods=["POST"])
def api_express():
    data = request.get_json(force=True) or {}
    try:
        b = _build_plan_from_payload(data)
    except _PlanBuildError as exc:
        return jsonify({"error": str(exc)}), exc.status_code

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO plan (
                created_at, updated_at, status, job_type, address, city, state, zip, jurisdiction,
                permit_number, usa_ticket, job_number, center_lat, center_lng, work_polygon, work_description,
                scope, sidewalk_affected, parking_affected, duration, posted_speed, ta_figure,
                road_osm_id, road_name, road_geometry, road_width_ft, road_lanes, road_bearing_deg,
                parcel_apn, parcel_polygon, frontage_source, frontage_length_ft, corner_lot
            ) VALUES (datetime('now'), datetime('now'), 'draft', ?, ?, ?, 'CA', ?, NULL,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, 'short_term', ?, ?,
                ?, ?, ?, ?, ?, NULL,
                ?, ?, ?, ?, ?)""",
            _plan_write_params(b),
        )
        plan_id = cur.lastrowid
        _insert_devices(conn, plan_id, b["devices"])
        log_audit(conn, plan_id, "web", "create", json.dumps({"warnings": b["warnings"]}))

    _push_permits_to_portal(plan_id)
    return jsonify({"id": plan_id, "redirect": f"/plan/{plan_id}/preview", "warnings": b["warnings"]})


@app.route("/api/plan/<int:plan_id>/regenerate", methods=["POST"])
def api_plan_regenerate(plan_id: int):
    """Edits a plan in place: re-runs the full build pipeline against new
    inputs (address/scope/etc. can all change) and replaces the existing
    plan row + its device rows, rather than creating a new plan. Any
    previously rendered PDFs are stale the moment the devices change, so
    they're deleted and cleared -- the preview page's Generate PDF button
    is what re-creates them."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pdf_notes_path, pdf_sheet1_path, pdf_sheet2_path FROM plan WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            abort(404)
        old_pdf_paths = (row["pdf_notes_path"], row["pdf_sheet1_path"], row["pdf_sheet2_path"])

    data = request.get_json(force=True) or {}
    try:
        b = _build_plan_from_payload(data)
    except _PlanBuildError as exc:
        return jsonify({"error": str(exc)}), exc.status_code

    with get_conn() as conn:
        conn.execute(
            """UPDATE plan SET
                job_type=?, address=?, city=?, zip=?,
                permit_number=?, usa_ticket=?, job_number=?, center_lat=?, center_lng=?, work_polygon=?, work_description=?,
                scope=?, sidewalk_affected=?, parking_affected=?, posted_speed=?, ta_figure=?,
                road_osm_id=?, road_name=?, road_geometry=?, road_width_ft=?, road_lanes=?,
                parcel_apn=?, parcel_polygon=?, frontage_source=?, frontage_length_ft=?, corner_lot=?,
                pdf_notes_path=NULL, pdf_sheet1_path=NULL, pdf_sheet2_path=NULL, updated_at=datetime('now')
               WHERE id=?""",
            (*_plan_write_params(b), plan_id),
        )
        conn.execute("DELETE FROM device WHERE plan_id=?", (plan_id,))
        _insert_devices(conn, plan_id, b["devices"])
        log_audit(conn, plan_id, "web", "regenerate", json.dumps({"warnings": b["warnings"]}))

    for path in old_pdf_paths:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    _push_permits_to_portal(plan_id)
    return jsonify({"id": plan_id, "redirect": f"/plan/{plan_id}/preview", "warnings": b["warnings"]})


@app.route("/api/plan/<int:plan_id>", methods=["GET"])
def api_get_plan(plan_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            abort(404)
        devices = _load_devices(conn, plan_id)
    return jsonify({"plan": dict(row), "devices": [dataclasses.asdict(d) for d in devices]})


@app.route("/api/plan/<int:plan_id>", methods=["PATCH"])
def api_update_plan(plan_id: int):
    data = request.get_json(force=True) or {}
    allowed = {"permit_number", "usa_ticket", "job_number", "notes_override", "status"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no updatable fields provided (allowed: " + ", ".join(sorted(allowed)) + ")"}), 400
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM plan WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            abort(404)
        conn.execute(f"UPDATE plan SET {set_clause}, updated_at=datetime('now') WHERE id=?", (*updates.values(), plan_id))
        log_audit(conn, plan_id, "web", "update", json.dumps(updates))
    _push_permits_to_portal(plan_id)
    return jsonify({"ok": True})


@app.route("/api/plan/<int:plan_id>", methods=["DELETE"])
def api_delete_plan(plan_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pdf_notes_path, pdf_sheet1_path, pdf_sheet2_path FROM plan WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            abort(404)
        for path in (row["pdf_notes_path"], row["pdf_sheet1_path"], row["pdf_sheet2_path"]):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        # device rows cascade via the FK (PRAGMA foreign_keys=ON in get_conn);
        # audit has no FK on this table, so it needs an explicit delete.
        conn.execute("DELETE FROM plan WHERE id=?", (plan_id,))
        conn.execute("DELETE FROM audit WHERE plan_id=?", (plan_id,))
    _push_permits_to_portal(plan_id)
    return jsonify({"ok": True})


@app.route("/api/plan/<int:plan_id>/render", methods=["POST"])
def api_render(plan_id: int):
    with get_conn() as conn:
        plan_row = conn.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
        if plan_row is None:
            abort(404)
        devices = _load_devices(conn, plan_id)

    road = _road_from_plan_row(plan_row)
    parcel = _parcel_from_plan_row(plan_row)
    centerline = Centerline(road.coords)
    scope = Scope(plan_row["scope"])
    if plan_row["work_polygon"]:
        ring = json.loads(plan_row["work_polygon"])["coordinates"][0]
        drawn_polygon = [(lat, lng) for lng, lat in ring]
        work_area = work_area_from_polygon(drawn_polygon, centerline)
    else:
        work_area = default_work_area(parcel, centerline, road, scope, (plan_row["center_lat"], plan_row["center_lng"]))
    ta_figure = get_ta_figure(scope, load_ta_figures())

    from core.geocode import GeocodeResult

    geo = GeocodeResult(lat=plan_row["center_lat"], lng=plan_row["center_lng"], display_name=plan_row["address"], city=plan_row["city"], zip=plan_row["zip"])

    notes, sheet1, sheet2 = render_plan_pdfs(
        plan_row["address"], geo, road, work_area, scope, ta_figure, devices, [],
        str(OUT_DIR), permit_number=plan_row["permit_number"], job_number=plan_row["job_number"],
        usa_ticket=plan_row["usa_ticket"],
        job_type=plan_row["job_type"], parcel=parcel,
    )

    with get_conn() as conn:
        conn.execute(
            "UPDATE plan SET pdf_notes_path=?, pdf_sheet1_path=?, pdf_sheet2_path=?, updated_at=datetime('now') WHERE id=?",
            (notes, sheet1, sheet2, plan_id),
        )
        log_audit(conn, plan_id, "web", "render", None)

    # ?v= per render: every re-render overwrites the same dated filename,
    # and Cloudflare/Chrome kept serving the stale copy under that URL.
    v = int(os.path.getmtime(sheet1))
    return jsonify({
        "notes_url": f"/out/{os.path.basename(notes)}?v={v}",
        "sheet1_url": f"/out/{os.path.basename(sheet1)}?v={v}",
        "sheet2_url": f"/out/{os.path.basename(sheet2)}?v={v}",
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8090)
