from unittest.mock import MagicMock, patch

import pytest

from core.geometry import Centerline, to_wgs84
from core.parcels import (
    NoParcelProvider,
    Parcel,
    SanMateoArcGIS,
    detect_corner_lot,
    frontage_stations,
    is_implausible_frontage,
    work_side,
)
from core.roads import RoadSegment

LONG_LINE = [(37.6300 + i * 0.0005, -122.4100) for i in range(60)]  # ~10800 ft, N-S

SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "APN": "015-123-456",
                "SITUS_ADDR": "157 SAN MARCO AVE",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-122.4119, 37.6301],
                        [-122.4118, 37.6301],
                        [-122.4118, 37.6302],
                        [-122.4119, 37.6302],
                        [-122.4119, 37.6301],
                    ]
                ],
            },
        }
    ],
}


def _mock_response(json_body):
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


@patch("core.parcels.requests.get")
def test_parcel_at_parses_apn_and_polygon(mock_get):
    mock_get.return_value = _mock_response(SAMPLE_GEOJSON)
    provider = SanMateoArcGIS()
    parcel = provider.parcel_at(37.63015, -122.41185)

    assert isinstance(parcel, Parcel)
    assert parcel.apn == "015-123-456"
    assert parcel.situs_address == "157 SAN MARCO AVE"
    assert parcel.source == "smc_arcgis"
    # GeoJSON rings are [lng, lat]; Parcel.polygon must come back (lat, lng)
    assert parcel.polygon[0] == (37.6301, -122.4119)


@patch("core.parcels.requests.get")
def test_parcel_at_returns_none_when_no_features(mock_get):
    mock_get.return_value = _mock_response({"type": "FeatureCollection", "features": []})
    provider = SanMateoArcGIS()
    assert provider.parcel_at(0, 0) is None


@patch("core.parcels.requests.get")
def test_parcel_at_queries_the_verified_layer(mock_get):
    mock_get.return_value = _mock_response({"type": "FeatureCollection", "features": []})
    provider = SanMateoArcGIS()
    provider.parcel_at(37.63, -122.41)
    called_url = mock_get.call_args.args[0]
    assert called_url.endswith("/0/query")


def test_parcel_at_handles_multipolygon():
    provider = SanMateoArcGIS()
    feature = {
        "properties": {"APN": "1", "SITUS_ADDR": None},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [[[[-122.41, 37.63], [-122.409, 37.63], [-122.409, 37.631]]]],
        },
    }
    parcel = provider._parcel_from_feature(feature)
    assert parcel.polygon[0] == (37.63, -122.41)


def test_no_parcel_provider_always_returns_none():
    provider = NoParcelProvider()
    assert provider.parcel_at(37.63, -122.41) is None


# ---- spec §5A.4 frontage extent, §5A.5 corner lots -------------------------


def _rect_parcel(cl: Centerline, station_lo, station_hi, offset_lo, offset_hi, apn="TEST"):
    corners_utm = [
        cl.offset_point(station_lo, offset_lo),
        cl.offset_point(station_hi, offset_lo),
        cl.offset_point(station_hi, offset_hi),
        cl.offset_point(station_lo, offset_hi),
    ]
    polygon = to_wgs84(corners_utm, cl.crs)
    return Parcel(apn=apn, polygon=polygon, situs_address="1 Test St", source="test")


def test_frontage_stations_matches_the_rectangle_built_from():
    cl = Centerline(LONG_LINE)
    parcel = _rect_parcel(cl, 200, 260, 20, 55)
    s0, s1 = frontage_stations(parcel, cl)
    assert s0 == pytest.approx(200, abs=1)
    assert s1 == pytest.approx(260, abs=1)


def test_work_side_positive_when_parcel_is_on_the_positive_offset_side():
    cl = Centerline(LONG_LINE)
    parcel = _rect_parcel(cl, 200, 260, 20, 55)
    assert work_side(parcel, cl) == 1


def test_work_side_negative_when_parcel_is_on_the_negative_offset_side():
    cl = Centerline(LONG_LINE)
    parcel = _rect_parcel(cl, 200, 260, -55, -20)
    assert work_side(parcel, cl) == -1


def test_work_side_defaults_to_positive_with_no_parcel():
    cl = Centerline(LONG_LINE)
    assert work_side(None, cl) == 1


def test_is_implausible_frontage_flags_long_frontage():
    cl = Centerline(LONG_LINE)
    short_parcel = _rect_parcel(cl, 200, 260, 20, 55)
    long_parcel = _rect_parcel(cl, 200, 600, 20, 55)
    assert is_implausible_frontage(short_parcel, cl) is False
    assert is_implausible_frontage(long_parcel, cl) is True


def test_detect_corner_lot_true_when_touching_two_roads():
    cl = Centerline(LONG_LINE)
    parcel = _rect_parcel(cl, 200, 260, 0, 10)  # right up against the centerline

    close_road = RoadSegment(1, "Cross St", LONG_LINE, 36, 2, 25, False, "residential")
    far_line = [(lat, lng + 0.01) for lat, lng in LONG_LINE]  # ~880m away
    far_road = RoadSegment(2, "Far St", far_line, 36, 2, 25, False, "residential")

    assert detect_corner_lot(parcel, [close_road, close_road]) is True
    assert detect_corner_lot(parcel, [close_road, far_road]) is False
    assert detect_corner_lot(parcel, [close_road]) is False
