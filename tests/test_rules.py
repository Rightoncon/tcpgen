import pytest

from core.rules import (
    Scope,
    ScopeNotSupportedError,
    TaFigureNotBuiltError,
    buffer_space_ft,
    device_spacing_ft,
    downstream_taper_ft,
    get_ta_figure,
    load_ta_figures,
    shifting_taper_ft,
    shoulder_taper_ft,
    sign_spacing_ft,
    ta_figure_for_scope,
    taper_length_ft,
)


def test_scope_to_ta_figure_lookup():
    assert ta_figure_for_scope(Scope.BEHIND_CURB) == "6H-2"
    assert ta_figure_for_scope(Scope.SHOULDER) == "6H-3"
    assert ta_figure_for_scope(Scope.ONE_LANE) == "6H-10"
    assert ta_figure_for_scope(Scope.CUL_DE_SAC) == "6H-10"


def test_full_closure_is_blocked():
    with pytest.raises(ScopeNotSupportedError):
        ta_figure_for_scope(Scope.FULL_CLOSURE)


def test_load_ta_figures_has_6h2_6h3_and_6h10():
    figures = load_ta_figures()
    assert set(figures) == {"6H-2", "6H-3", "6H-10"}

    fig = figures["6H-2"]
    assert fig.name == "Work Beyond the Shoulder"
    assert fig.flaggers == 0
    assert fig.lanes_closed == 0
    assert [s.code for s in fig.signs_per_approach] == ["W20-1", "W21-5"]
    assert fig.end_sign is True
    assert fig.end_sign_code == "G20-2"

    fig3 = figures["6H-3"]
    assert fig3.name == "Shoulder Work"
    assert fig3.flaggers == 0
    assert fig3.lanes_closed == 0
    assert [s.code for s in fig3.signs_per_approach] == ["W20-1", "W21-5"]
    assert fig3.end_sign is True
    assert fig3.end_sign_code == "G20-2"

    fig10 = figures["6H-10"]
    assert fig10.flaggers == 2
    assert fig10.lanes_closed == 1
    assert [s.code for s in fig10.signs_per_approach] == ["W20-1", "W20-4", "W20-7a"]


def test_get_ta_figure_behind_curb_resolves():
    figures = load_ta_figures()
    fig = get_ta_figure(Scope.BEHIND_CURB, figures)
    assert fig.key == "6H-2"


def test_get_ta_figure_raises_for_figure_not_in_table():
    # Exercises the KeyError -> TaFigureNotBuiltError path directly (every
    # real Scope resolves to a built figure now that 6H-3 exists) by
    # passing a table missing the target key, same as an unbuilt figure
    # would look to get_ta_figure.
    with pytest.raises(TaFigureNotBuiltError):
        get_ta_figure(Scope.BEHIND_CURB, figures={})


@pytest.mark.parametrize(
    "speed_mph,expected",
    [
        (25, 100),
        (30, 100),
        (32, 250),  # off-table speed rounds up to the next bracket, not down
        (40, 250),
        (50, 350),
        (60, 500),
        (70, 500),  # above the table entirely -> highest bracket, not a crash
    ],
)
def test_sign_spacing_ft(speed_mph, expected):
    assert sign_spacing_ft(speed_mph) == expected


def test_sign_spacing_matches_25mph_documented_case():
    """spec §6.4: 'at 25 mph the TA-10 set lands at 100 / 200 / 300 ft'."""
    spacing = sign_spacing_ft(25)
    assert [spacing * m for m in (1, 2, 3)] == [100, 200, 300]


def test_taper_length_low_speed_formula():
    # offset_ft * speed^2 / 60, speed <= 40
    assert taper_length_ft(12, 25) == pytest.approx(12 * 625 / 60)


def test_taper_length_high_speed_formula():
    # offset_ft * speed, speed > 40
    assert taper_length_ft(12, 45) == pytest.approx(12 * 45)


def test_derived_tapers():
    offset, speed = 16, 30
    L = taper_length_ft(offset, speed)
    assert shifting_taper_ft(offset, speed) == pytest.approx(L / 2)
    assert shoulder_taper_ft(offset, speed) == pytest.approx(L / 3)


def test_downstream_taper_is_100ft_per_lane():
    assert downstream_taper_ft(1) == 100
    assert downstream_taper_ft(2) == 200
    assert downstream_taper_ft(0) == 100  # at least one lane's worth


def test_device_spacing_taper_vs_tangent():
    assert device_spacing_ft(25, on_taper=True) == 25
    assert device_spacing_ft(25, on_taper=False) == 50


@pytest.mark.parametrize(
    "speed_mph,expected",
    [(20, 115), (25, 155), (30, 200), (35, 250), (40, 305), (45, 360)],
)
def test_buffer_space_ft_tabulated(speed_mph, expected):
    assert buffer_space_ft(speed_mph) == expected


def test_buffer_space_ft_snaps_to_nearest_for_off_table_speed():
    assert buffer_space_ft(23) == buffer_space_ft(25)
    assert buffer_space_ft(50) == buffer_space_ft(45)
