from unittest.mock import patch

import pytest
import requests

from core.roads import (
    RoadNotFoundError,
    RoadSegment,
    _derive_speed_mph,
    _derive_width_ft,
    _parking_width_bonus_ft,
    _road_from_element,
    choose_road,
    find_roads_near,
)


def _overpass_response(elements):
    return {"version": 0.6, "elements": elements}


def _way(osm_id, name, highway, tags_extra=None, geometry=None):
    tags = {"highway": highway}
    if name:
        tags["name"] = name
    if tags_extra:
        tags.update(tags_extra)
    return {
        "type": "way",
        "id": osm_id,
        "tags": tags,
        "geometry": geometry
        or [{"lat": 37.630, "lon": -122.412}, {"lat": 37.631, "lon": -122.411}],
    }


def test_derive_width_ft_explicit_width_tag_wins():
    assert _derive_width_ft({"width": "12"}, "residential") == pytest.approx(12 / 0.3048)


def test_derive_width_ft_explicit_width_tag_with_ft_suffix():
    assert _derive_width_ft({"width": "40 ft"}, "residential") == pytest.approx(40)


def test_derive_width_ft_from_lanes():
    assert _derive_width_ft({"lanes": "2"}, "residential") == pytest.approx(22.0)


def test_derive_width_ft_lanes_plus_parking_both_sides():
    tags = {"lanes": "2", "parking:lane:both": "parallel"}
    assert _derive_width_ft(tags, "residential") == pytest.approx(22.0 + 16.0)


def test_derive_width_ft_lanes_plus_parking_one_side():
    tags = {"lanes": "2", "parking:lane:right": "parallel"}
    assert _derive_width_ft(tags, "residential") == pytest.approx(22.0 + 8.0)


def test_derive_width_ft_falls_back_to_highway_class():
    assert _derive_width_ft({}, "primary") == 60
    assert _derive_width_ft({}, "service") == 24


def test_parking_bonus_no_means_no_parking():
    assert _parking_width_bonus_ft({"parking:lane:right": "no"}) == 0.0


def test_derive_speed_mph_from_maxspeed_tag():
    assert _derive_speed_mph({"maxspeed": "25 mph"}, "residential") == 25
    assert _derive_speed_mph({"maxspeed": "35"}, "secondary") == 35


def test_derive_speed_mph_fallback_by_class():
    assert _derive_speed_mph({}, "residential") == 25
    assert _derive_speed_mph({}, "tertiary") == 30
    assert _derive_speed_mph({}, "primary") == 35
    assert _derive_speed_mph({}, "service") == 15


def test_road_from_element_parses_tags():
    el = _way(123, "San Marco Ave", "residential", {"oneway": "yes", "lanes": "2"})
    road = _road_from_element(el)
    assert road.osm_id == 123
    assert road.name == "San Marco Ave"
    assert road.oneway is True
    assert road.lanes == 2
    assert road.coords == [(37.630, -122.412), (37.631, -122.411)]


def test_road_from_element_unnamed_road_gets_placeholder():
    el = _way(1, None, "service")
    road = _road_from_element(el)
    assert road.name == "Unnamed Road"


@patch("core.roads.query_overpass")
def test_find_roads_near_widens_search_radius(mock_query):
    mock_query.side_effect = [
        _overpass_response([]),
        _overpass_response([]),
        _overpass_response([_way(1, "Example Ave", "residential")]),
    ]
    roads = find_roads_near(37.63, -122.41)
    assert len(roads) == 1
    assert roads[0].name == "Example Ave"
    assert mock_query.call_count == 3
    radii_used = [call.args[2] for call in mock_query.call_args_list]
    assert radii_used == [60, 120, 250]


@patch("core.roads.query_overpass")
def test_find_roads_near_raises_when_nothing_found(mock_query):
    mock_query.return_value = _overpass_response([])
    with pytest.raises(RoadNotFoundError):
        find_roads_near(37.63, -122.41)


@patch("core.roads.query_overpass")
def test_find_roads_near_raises_clear_error_on_network_failure(mock_query):
    mock_query.side_effect = requests.RequestException("boom")
    with pytest.raises(RoadNotFoundError):
        find_roads_near(37.63, -122.41)


def test_choose_road_prefers_nearest():
    near = RoadSegment(1, "Near St", [(37.6300, -122.4120), (37.6301, -122.4119)], 36, 2, 25, False, "residential")
    far = RoadSegment(2, "Far St", [(37.6400, -122.4220), (37.6401, -122.4219)], 36, 2, 25, False, "residential")
    chosen = choose_road([far, near], 37.6300, -122.4120)
    assert chosen.name == "Near St"


def test_choose_road_tie_breaks_on_street_hint():
    coords = [(37.6300, -122.4120), (37.6301, -122.4119)]
    a = RoadSegment(1, "Main St", coords, 36, 2, 25, False, "residential")
    b = RoadSegment(2, "Oak Ave", coords, 36, 2, 25, False, "residential")
    chosen = choose_road([a, b], 37.6300, -122.4120, street_hint="Oak")
    assert chosen.name == "Oak Ave"


def test_choose_road_tie_breaks_on_highway_class():
    coords = [(37.6300, -122.4120), (37.6301, -122.4119)]
    a = RoadSegment(1, "A St", coords, 36, 2, 25, False, "residential")
    b = RoadSegment(2, "B St", coords, 48, 2, 35, False, "primary")
    chosen = choose_road([a, b], 37.6300, -122.4120)
    assert chosen.name == "B St"


def test_choose_road_raises_on_empty_candidates():
    with pytest.raises(RoadNotFoundError):
        choose_road([], 37.63, -122.41)
