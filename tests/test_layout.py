import pytest
from hypothesis import given
from hypothesis.strategies import floats, sampled_from

from core.geometry import Centerline, to_wgs84
from core.layout import (
    WorkArea,
    build_device_plan,
    check_scope_matches_geometry,
    default_work_area,
)
from core.parcels import Parcel
from core.roads import RoadSegment
from core.rules import Scope, get_ta_figure, load_ta_figures

# A long straight north-south line so every combination of road width and
# speed the property test explores has room for tapers/buffers/standoffs
# without every device clamping to the segment's start/end station.
LONG_LINE = [(37.6300 + i * 0.0005, -122.4100) for i in range(60)]  # ~10800 ft


def _road(width_ft=36, speed_mph=25, lanes=2, highway="residential"):
    return RoadSegment(
        osm_id=1, name="Test St", coords=LONG_LINE, width_ft=width_ft,
        lanes=lanes, speed_mph=speed_mph, oneway=False, highway=highway,
    )


def _figures():
    return load_ta_figures()


# ---- default_work_area ----------------------------------------------------


def test_default_work_area_no_parcel_uses_60ft_default():
    road = _road()
    cl = Centerline(road.coords)
    midpoint = cl.point_at(cl.length_ft / 2)
    lat, lng = to_wgs84([midpoint], cl.crs)[0]
    wa = default_work_area(None, cl, road, Scope.BEHIND_CURB, (lat, lng))
    assert wa.end_station_ft - wa.start_station_ft == pytest.approx(60, abs=1)
    # A point sitting exactly on the centerline has no well-defined side --
    # side determination itself is covered properly (with a clearly offset
    # point) in test_parcels.py::test_work_side_uses_geocoded_point_when_no_parcel.


def test_default_work_area_no_parcel_side_follows_the_geocoded_point():
    """spec-fix 2026-09-23: a real address with no parcel on file must still
    land on the correct side of the street, using the geocoded point's own
    offset -- not a hardcoded +1 (that bug put a real job's work area across
    the street: 635 Costa Rica Ave, San Mateo)."""
    road = _road()
    cl = Centerline(road.coords)
    on_the_right = to_wgs84([cl.offset_point(cl.length_ft / 2, 30)], cl.crs)[0]
    on_the_left = to_wgs84([cl.offset_point(cl.length_ft / 2, -30)], cl.crs)[0]
    wa_right = default_work_area(None, cl, road, Scope.BEHIND_CURB, on_the_right)
    wa_left = default_work_area(None, cl, road, Scope.BEHIND_CURB, on_the_left)
    assert wa_right.side == 1
    assert wa_left.side == -1


def test_default_work_area_behind_curb_offsets_are_outside_the_road():
    road = _road(width_ft=36)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.BEHIND_CURB, (LONG_LINE[30][0], LONG_LINE[30][1]))
    assert abs(wa.near_offset_ft) > road.width_ft / 2
    assert abs(wa.far_offset_ft) > abs(wa.near_offset_ft)


def test_default_work_area_one_lane_offsets_are_inside_the_road():
    road = _road(width_ft=36)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.ONE_LANE, (LONG_LINE[30][0], LONG_LINE[30][1]))
    assert abs(wa.near_offset_ft) < road.width_ft / 2
    assert abs(wa.far_offset_ft) < road.width_ft / 2


def test_default_work_area_uses_parcel_frontage_when_available():
    road = _road(width_ft=36)
    cl = Centerline(road.coords)
    # A small rectangular parcel straddling stations ~100-140 ft, offset to
    # one side of the centerline (built from real UTM offsets, not guessed
    # lat/lng deltas, so frontage_stations resolves to a known range).
    corners_utm = [
        cl.offset_point(100, 25),
        cl.offset_point(140, 25),
        cl.offset_point(140, 60),
        cl.offset_point(100, 60),
    ]
    polygon = to_wgs84(corners_utm, cl.crs)
    parcel = Parcel(apn="TEST-1", polygon=polygon, situs_address="1 Test St", source="test")

    wa = default_work_area(parcel, cl, road, Scope.BEHIND_CURB, (LONG_LINE[0][0], LONG_LINE[0][1]))
    assert wa.start_station_ft == pytest.approx(100, abs=1)
    assert wa.end_station_ft == pytest.approx(140, abs=1)
    assert wa.side == 1


# ---- check_scope_matches_geometry -----------------------------------------


def test_scope_mismatch_warns_when_behind_curb_geometry_but_one_lane_scope():
    road = _road(width_ft=36)
    wa = WorkArea(start_station_ft=0, end_station_ft=40, near_offset_ft=25, far_offset_ft=30, side=1)
    warning = check_scope_matches_geometry(wa, road, Scope.ONE_LANE)
    assert warning is not None
    assert "behind the curb" in warning


def test_scope_mismatch_warns_when_in_roadway_but_behind_curb_scope():
    road = _road(width_ft=36)
    wa = WorkArea(start_station_ft=0, end_station_ft=40, near_offset_ft=8, far_offset_ft=15, side=1)
    warning = check_scope_matches_geometry(wa, road, Scope.BEHIND_CURB)
    assert warning is not None
    assert "roadway" in warning


def test_scope_matches_geometry_no_warning():
    road = _road(width_ft=36)
    wa = WorkArea(start_station_ft=0, end_station_ft=40, near_offset_ft=25, far_offset_ft=30, side=1)
    assert check_scope_matches_geometry(wa, road, Scope.BEHIND_CURB) is None


# ---- build_device_plan: behind_curb (the CLI's own worked example) --------


def test_behind_curb_plan_has_cones_and_signs_and_end_sign():
    road = _road(width_ft=36, speed_mph=25)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.BEHIND_CURB, (LONG_LINE[30][0], LONG_LINE[30][1]))
    ta_figure = get_ta_figure(Scope.BEHIND_CURB, _figures())

    devices, warnings = build_device_plan(cl, road, wa, Scope.BEHIND_CURB, ta_figure)

    kinds = {d.kind for d in devices}
    assert "cone" in kinds
    assert "sign" in kinds
    assert any(d.code == "W20-1" for d in devices)
    assert any(d.code == "W21-5" for d in devices)
    assert any(d.code == "G20-2" for d in devices)  # END ROAD WORK
    # 6H-2 has zero flaggers
    assert not any(d.kind == "flagger" for d in devices)
    assert warnings == []  # geometry and scope agree by construction


def test_behind_curb_sidewalk_affected_adds_pedestrian_signs():
    road = _road(width_ft=36, speed_mph=25)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.BEHIND_CURB, (LONG_LINE[30][0], LONG_LINE[30][1]))
    ta_figure = get_ta_figure(Scope.BEHIND_CURB, _figures())

    devices, _warnings = build_device_plan(cl, road, wa, Scope.BEHIND_CURB, ta_figure, sidewalk_affected=True)
    codes = [d.code for d in devices]
    assert codes.count("R9-9") == 2
    assert codes.count("R9-11") == 1

    barricades = [d for d in devices if d.kind == "barricade"]
    assert len(barricades) == 2  # one at each end of the closed run, spec §8.1 note 19
    assert {round(d.station_ft) for d in barricades} == {round(wa.start_station_ft), round(wa.end_station_ft)}


def test_no_sidewalk_no_barricades():
    road = _road(width_ft=36, speed_mph=25)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.BEHIND_CURB, (LONG_LINE[30][0], LONG_LINE[30][1]))
    ta_figure = get_ta_figure(Scope.BEHIND_CURB, _figures())

    devices, _warnings = build_device_plan(cl, road, wa, Scope.BEHIND_CURB, ta_figure, sidewalk_affected=False)
    assert not any(d.kind == "barricade" for d in devices)


# ---- build_device_plan: one_lane (TA-10) -----------------------------------


def test_one_lane_plan_has_two_flaggers_and_merging_taper():
    road = _road(width_ft=40, speed_mph=30)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.ONE_LANE, (LONG_LINE[30][0], LONG_LINE[30][1]))
    ta_figure = get_ta_figure(Scope.ONE_LANE, _figures())

    devices, _warnings = build_device_plan(cl, road, wa, Scope.ONE_LANE, ta_figure)

    flaggers = [d for d in devices if d.kind == "flagger"]
    assert len(flaggers) == 2
    assert {f.approach for f in flaggers} == {"A", "B"}
    # taper cones should span a range of offsets, not sit at one offset
    cone_offsets = {round(d.offset_ft, 1) for d in devices if d.kind == "cone"}
    assert len(cone_offsets) > 1
    assert any(d.code == "W20-7a" for d in devices)  # FLAGGER AHEAD


# ---- build_device_plan: cul_de_sac -----------------------------------------


def test_cul_de_sac_plan_has_one_flagger_and_one_approach():
    road = _road(width_ft=36, speed_mph=25)
    cl = Centerline(road.coords)
    wa = default_work_area(None, cl, road, Scope.CUL_DE_SAC, (LONG_LINE[30][0], LONG_LINE[30][1]))
    ta_figure = get_ta_figure(Scope.CUL_DE_SAC, _figures())

    devices, _warnings = build_device_plan(cl, road, wa, Scope.CUL_DE_SAC, ta_figure)

    flaggers = [d for d in devices if d.kind == "flagger"]
    assert len(flaggers) == 1
    assert all(d.approach in (None, "A") for d in devices)


# ---- the property test spec §14 calls out as the most important one -------


@given(road_width=floats(min_value=20, max_value=60), speed=sampled_from([25, 30, 35, 40, 45]))
def test_no_device_leaves_the_roadway(road_width, speed):
    """spec §14: 'the most important single test' — for scope=one_lane,
    every device's offset must stay within the road's half-width, for any
    plausible road width and speed."""
    road = _road(width_ft=road_width, speed_mph=speed)
    cl = Centerline(road.coords)
    ta_figure = get_ta_figure(Scope.ONE_LANE, _figures())
    mid = cl.length_ft / 2
    wa = WorkArea(start_station_ft=mid - 20, end_station_ft=mid + 20, near_offset_ft=-5, far_offset_ft=5, side=1)

    devices, _warnings = build_device_plan(cl, road, wa, Scope.ONE_LANE, ta_figure)
    for d in devices:
        assert abs(d.offset_ft) <= road_width / 2 + 1e-6, f"{d.kind} {d.code} off the road: {d.offset_ft}"
