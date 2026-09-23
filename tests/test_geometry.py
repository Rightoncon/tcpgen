import math

import pytest
from pyproj import Geod

from core.geometry import (
    MIN_EDGE_CLEARANCE_FT,
    METERS_PER_FT,
    Centerline,
    clamp_offset,
    to_utm,
    to_wgs84,
    utm_crs_for,
)

# A short real street in San Bruno, CA (the spec's own example address is on
# San Marco Ave). Coordinates are approximate — tests only rely on internal
# consistency (round trips, projection self-checks), never on matching a
# specific real-world measurement.
SAN_MARCO = [
    (37.63010, -122.41190),
    (37.63080, -122.41120),
    (37.63150, -122.41050),
]

GEOD = Geod(ellps="WGS84")


def test_utm_crs_for_bay_area_is_zone_10n():
    crs = utm_crs_for(37.63, -122.41)
    assert crs.to_epsg() == 32610


def test_to_utm_to_wgs84_round_trip():
    crs = utm_crs_for(*SAN_MARCO[0])
    utm_pts = to_utm(SAN_MARCO, crs)
    back = to_wgs84(utm_pts, crs)
    for (lat0, lng0), (lat1, lng1) in zip(SAN_MARCO, back):
        assert lat0 == pytest.approx(lat1, abs=1e-9)
        assert lng0 == pytest.approx(lng1, abs=1e-9)


def test_length_ft_matches_geodesic_distance():
    cl = Centerline(SAN_MARCO)
    geod_len_m = 0.0
    for (lat0, lng0), (lat1, lng1) in zip(SAN_MARCO, SAN_MARCO[1:]):
        _, _, dist = GEOD.inv(lng0, lat0, lng1, lat1)
        geod_len_m += dist
    expected_ft = geod_len_m / METERS_PER_FT
    assert cl.length_ft == pytest.approx(expected_ft, rel=0.01)


def test_point_at_start_and_end_match_input():
    cl = Centerline(SAN_MARCO)
    start = to_wgs84([cl.point_at(0)], cl.crs)[0]
    end = to_wgs84([cl.point_at(cl.length_ft)], cl.crs)[0]
    assert start[0] == pytest.approx(SAN_MARCO[0][0], abs=1e-6)
    assert start[1] == pytest.approx(SAN_MARCO[0][1], abs=1e-6)
    assert end[0] == pytest.approx(SAN_MARCO[-1][0], abs=1e-6)
    assert end[1] == pytest.approx(SAN_MARCO[-1][1], abs=1e-6)


def test_point_at_clamps_beyond_the_ends():
    cl = Centerline(SAN_MARCO)
    assert cl.point_at(-500) == pytest.approx(cl.point_at(0))
    assert cl.point_at(cl.length_ft + 500) == pytest.approx(cl.point_at(cl.length_ft))


def test_offset_point_lands_the_right_distance_away():
    cl = Centerline(SAN_MARCO)
    station = cl.length_ft / 2
    on_line = cl.point_at(station)
    for offset_ft in (10, -10, 18, -18):
        off_pt = cl.offset_point(station, offset_ft)
        dist_ft = math.hypot(off_pt[0] - on_line[0], off_pt[1] - on_line[1]) * (1 / METERS_PER_FT)
        assert dist_ft == pytest.approx(abs(offset_ft), rel=0.02)


def test_offset_point_is_perpendicular_to_bearing():
    cl = Centerline(SAN_MARCO)
    station = cl.length_ft / 2
    bearing = cl.bearing_at(station)
    on_line = cl.point_at(station)
    off_pt = cl.offset_point(station, 15)
    vec = (off_pt[0] - on_line[0], off_pt[1] - on_line[1])
    tangent = (math.cos(bearing), math.sin(bearing))
    dot = vec[0] * tangent[0] + vec[1] * tangent[1]
    assert dot == pytest.approx(0, abs=1e-6)


def test_station_of_nearest_round_trip():
    """spec §7.2's required test: a point generated at (station S, offset O)
    must return station ~S when fed to station_of_nearest."""
    cl = Centerline(SAN_MARCO)
    for station in (10, 75, 140):
        for offset in (-20, -5, 5, 20):
            utm_pt = cl.offset_point(station, offset)
            lat, lng = to_wgs84([utm_pt], cl.crs)[0]
            recovered = cl.station_of_nearest(lat, lng)
            assert recovered == pytest.approx(station, abs=1.0)


def test_station_of_nearest_on_the_line_is_in_range():
    cl = Centerline(SAN_MARCO)
    for lat, lng in SAN_MARCO:
        station = cl.station_of_nearest(lat, lng)
        assert 0 <= station <= cl.length_ft + 1


def test_centerline_needs_at_least_two_points():
    with pytest.raises(ValueError):
        Centerline([(37.63, -122.41)])


@pytest.mark.parametrize(
    "offset_ft,road_width_ft,expected",
    [
        (0, 36, 0),
        (20, 36, 17),  # 36/2 - 1 = 17, clamps down from 20
        (-20, 36, -17),
        (10, 36, 10),  # inside the limit, untouched
        (100, 24, 11),  # 24/2 - 1 = 11
    ],
)
def test_clamp_offset(offset_ft, road_width_ft, expected):
    assert clamp_offset(offset_ft, road_width_ft) == pytest.approx(expected)


def test_clamp_offset_uses_min_edge_clearance_constant():
    width = 40
    assert clamp_offset(1000, width) == pytest.approx(width / 2 - MIN_EDGE_CLEARANCE_FT)
