"""Flask routes (spec §11) — thin. All real logic lives in core/; this
file just wires HTTP <-> those functions <-> db.py.

Phase 4 scope (this pass): the Express-mode flow end to end — form, live
map (pin/road/parcel), generate, preview, render PDF. Deliberately NOT
built yet, per spec's own "optional draw tool and reference-image upload
last": device dragging (PATCH /api/device/<id>), POST /api/plan/<id>/
regenerate, the Leaflet.draw polygon tool, and POST /api/plan/<id>/
reference. No authentication either — this sits on the same VPS as the
portal but isn't gated by it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from core.geocode import GeocodeError, geocode_address
from core.geometry import Centerline
from core.layout import Device, build_device_plan, default_work_area
from core.parcels import Parcel, SanMateoArcGIS, detect_corner_lot, frontage_length_ft, is_implausible_frontage
from core.render_pdf import render_plan_pdfs
from core.roads import RoadNotFoundError, RoadSegment, choose_road, find_roads_near
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

app = Flask(__name__)
init_db()


# ---- helpers: dataclass <-> GeoJSON <-> DB row -----------------------------


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
            "SELECT id, created_at, address, scope, ta_figure, status FROM plan ORDER BY created_at DESC"
        ).fetchall()
    return render_template("index.html", plans=plans)


@app.route("/plan/new")
def plan_new():
    return render_template("express.html")


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
    return send_from_directory(OUT_DIR, filename, as_attachment=False)


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


@app.route("/api/parcel", methods=["POST"])
def api_parcel():
    data = request.get_json(force=True) or {}
    lat, lng = data.get("lat"), data.get("lng")
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    parcel = SanMateoArcGIS().parcel_at(float(lat), float(lng))
    if parcel is None:
        return jsonify({"parcel": None})
    return jsonify(
        {"parcel": {"apn": parcel.apn, "situs_address": parcel.situs_address, "polygon": [[lat, lng] for lat, lng in parcel.polygon]}}
    )


# ---- API: create + read a plan --------------------------------------------


@app.route("/api/express", methods=["POST"])
def api_express():
    data = request.get_json(force=True) or {}
    address = (data.get("address") or "").strip()
    scope_str = data.get("scope")
    if not address or not scope_str:
        return jsonify({"error": "address and scope are required"}), 400
    try:
        scope = Scope(scope_str)
    except ValueError:
        return jsonify({"error": f"invalid scope {scope_str!r}"}), 400

    sidewalk = bool(data.get("sidewalk"))
    parking = bool(data.get("parking"))
    street_hint = data.get("street") or None
    permit_number = data.get("permit_number") or None
    job_number = data.get("job_number") or None
    notes = data.get("notes") or None

    try:
        geo = geocode_address(address)
    except GeocodeError as exc:
        return jsonify({"error": str(exc)}), 422

    try:
        roads = find_roads_near(geo.lat, geo.lng)
        road = choose_road(roads, geo.lat, geo.lng, street_hint=street_hint)
    except RoadNotFoundError as exc:
        return jsonify({"error": str(exc)}), 422

    if data.get("road_width_ft"):
        road.width_ft = float(data["road_width_ft"])
    if data.get("posted_speed_mph"):
        road.speed_mph = int(data["posted_speed_mph"])

    centerline = Centerline(road.coords)
    parcel = SanMateoArcGIS().parcel_at(geo.lat, geo.lng)
    corner_lot = bool(parcel and detect_corner_lot(parcel, roads))

    warnings: list[str] = []
    if parcel is None:
        warnings.append("No parcel found — using a 60 ft default frontage. Adjust the length or draw the work area.")
    else:
        if is_implausible_frontage(parcel, centerline):
            warnings.append("Frontage on this street is implausibly long (>250 ft) — likely a corner lot; confirm the fronting street.")
        if corner_lot:
            warnings.append("Parcel touches 2+ candidate streets within 15 ft — confirm the fronting street before generating.")

    work_area = default_work_area(parcel, centerline, road, scope, (geo.lat, geo.lng))

    try:
        ta_figure = get_ta_figure(scope, load_ta_figures())
    except (ScopeNotSupportedError, TaFigureNotBuiltError) as exc:
        return jsonify({"error": str(exc)}), 422

    devices, layout_warnings = build_device_plan(centerline, road, work_area, scope, ta_figure, sidewalk_affected=sidewalk)
    warnings.extend(layout_warnings)
    if sidewalk:
        warnings.append(PEDESTRIAN_NOTE)

    frontage_len = frontage_length_ft(parcel, centerline) if parcel else None

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO plan (
                created_at, updated_at, status, address, city, state, zip, jurisdiction,
                permit_number, job_number, center_lat, center_lng, work_polygon, work_description,
                scope, sidewalk_affected, parking_affected, duration, posted_speed, ta_figure,
                road_osm_id, road_name, road_geometry, road_width_ft, road_lanes, road_bearing_deg,
                parcel_apn, parcel_polygon, frontage_source, frontage_length_ft, corner_lot
            ) VALUES (datetime('now'), datetime('now'), 'draft', ?, ?, 'CA', ?, NULL,
                ?, ?, ?, ?, NULL, ?,
                ?, ?, ?, 'short_term', ?, ?,
                ?, ?, ?, ?, ?, NULL,
                ?, ?, ?, ?, ?)""",
            (
                address, geo.city, geo.zip,
                permit_number, job_number, geo.lat, geo.lng, notes,
                scope.value, int(sidewalk), int(parking), road.speed_mph, ta_figure.key,
                road.osm_id, road.name, _linestring_geojson(road.coords), road.width_ft, road.lanes,
                parcel.apn if parcel else None, _polygon_geojson(parcel.polygon) if parcel else None,
                "parcel" if parcel else "default", frontage_len, int(corner_lot),
            ),
        )
        plan_id = cur.lastrowid
        for d in devices:
            conn.execute(
                """INSERT INTO device (plan_id, kind, code, label, station_ft, offset_ft, lat, lng, approach, seq, locked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (plan_id, d.kind, d.code, d.label, d.station_ft, d.offset_ft, d.lat, d.lng, d.approach, d.seq),
            )
        log_audit(conn, plan_id, "web", "create", json.dumps({"warnings": warnings}))

    return jsonify({"id": plan_id, "redirect": f"/plan/{plan_id}/preview", "warnings": warnings})


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
    allowed = {"permit_number", "job_number", "notes_override", "status"}
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
    work_area = default_work_area(parcel, centerline, road, scope, (plan_row["center_lat"], plan_row["center_lng"]))
    ta_figure = get_ta_figure(scope, load_ta_figures())

    from core.geocode import GeocodeResult

    geo = GeocodeResult(lat=plan_row["center_lat"], lng=plan_row["center_lng"], display_name=plan_row["address"], city=plan_row["city"], zip=plan_row["zip"])

    sheet1, sheet2 = render_plan_pdfs(
        plan_row["address"], geo, road, work_area, scope, ta_figure, devices, [],
        str(OUT_DIR), permit_number=plan_row["permit_number"], job_number=plan_row["job_number"],
    )

    with get_conn() as conn:
        conn.execute(
            "UPDATE plan SET pdf_sheet1_path=?, pdf_sheet2_path=?, updated_at=datetime('now') WHERE id=?",
            (sheet1, sheet2, plan_id),
        )
        log_audit(conn, plan_id, "web", "render", None)

    return jsonify({"sheet1_url": f"/out/{os.path.basename(sheet1)}", "sheet2_url": f"/out/{os.path.basename(sheet2)}"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8090)
