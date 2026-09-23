from unittest.mock import MagicMock, patch

from core.parcels import NoParcelProvider, Parcel, SanMateoArcGIS

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
