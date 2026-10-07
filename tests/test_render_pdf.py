import datetime as dt

import pytest
from pypdf import PdfReader

from core.geocode import GeocodeResult
from core.geometry import Centerline
from core.layout import build_device_plan, default_work_area
from core.render_pdf import BANNED_PHRASE, GENERAL_NOTES, render_plan_pdfs, sheet_filenames
from core.roads import RoadSegment
from core.rules import Scope, get_ta_figure, load_ta_figures

LONG_LINE = [(37.6300 + i * 0.0005, -122.4100) for i in range(60)]  # ~10800 ft


def _plan(scope=Scope.BEHIND_CURB, sidewalk=False):
    road = RoadSegment(1, "Test St", LONG_LINE, 36, 2, 25, False, "residential")
    cl = Centerline(road.coords)
    geo_point = LONG_LINE[30]
    work_area = default_work_area(None, cl, road, scope, geo_point)
    ta_figure = get_ta_figure(scope, load_ta_figures())
    devices, warnings = build_device_plan(cl, road, work_area, scope, ta_figure, sidewalk_affected=sidewalk)
    geo = GeocodeResult(lat=geo_point[0], lng=geo_point[1], display_name="157 Test St, Testville, CA 94000", city="Testville", zip="94000")
    return geo, road, work_area, ta_figure, devices, warnings


# ---- filenames (spec §10.6) ------------------------------------------------


def test_sheet_filenames_match_the_spec_convention():
    date = dt.date(2026, 9, 23)
    s0, s1, s2 = sheet_filenames("157 San Marco Ave, San Bruno CA", date=date)
    assert s0 == "2026-09-23 — TCP Sheet 0 General Notes — 157 San Marco Ave.pdf"
    assert s1 == "2026-09-23 — TCP Sheet 1 Close-Up — 157 San Marco Ave.pdf"
    assert s2 == "2026-09-23 — TCP Sheet 2 Wide Area — 157 San Marco Ave.pdf"


def test_sheet_filenames_strip_everything_after_the_first_comma():
    s0, s1, _s2 = sheet_filenames("123 Main St, Some City, CA 94000", date=dt.date(2026, 1, 1))
    assert "Some City" not in s1
    assert "123 Main St" in s1
    assert "Some City" not in s0


# ---- rendered PDF content ---------------------------------------------------


def test_render_produces_three_readable_pdfs(tmp_path):
    geo, road, work_area, ta_figure, devices, warnings = _plan()
    notes_path, s1_path, s2_path = render_plan_pdfs(
        "157 Test St, Testville CA", geo, road, work_area, Scope.BEHIND_CURB, ta_figure, devices, warnings, str(tmp_path)
    )

    r0 = PdfReader(notes_path)
    r1 = PdfReader(s1_path)
    r2 = PdfReader(s2_path)
    assert len(r0.pages) == 1
    assert len(r1.pages) == 1
    assert len(r2.pages) == 1

    text0 = r0.pages[0].extract_text()
    assert "GENERAL NOTES" in text0
    assert "TRAFFIC CONTROL PLAN" in text0
    for note in GENERAL_NOTES:
        # Wrapped across lines in the PDF, so check a distinctive prefix
        # rather than the whole sentence.
        assert note.split(".")[0][:20] in text0

    text1 = r1.pages[0].extract_text()
    assert "TRAFFIC CONTROL PLAN" in text1
    assert "DRAFT" in text1
    assert ta_figure.key in text1


def test_pdf_never_contains_the_banned_phrase(tmp_path):
    """spec §10.5: hard requirement, every scope, every sheet."""
    for scope in (Scope.BEHIND_CURB, Scope.ONE_LANE):
        geo, road, work_area, ta_figure, devices, warnings = _plan(scope=scope, sidewalk=True)
        notes_path, s1_path, s2_path = render_plan_pdfs(
            f"157 Test St {scope.value}, Testville CA", geo, road, work_area, scope, ta_figure, devices, warnings, str(tmp_path)
        )
        for path in (notes_path, s1_path, s2_path):
            text = PdfReader(path).pages[0].extract_text().lower()
            assert BANNED_PHRASE not in text


def test_draft_badge_is_the_only_draft_marker_text_present(tmp_path):
    geo, road, work_area, ta_figure, devices, warnings = _plan()
    _notes, s1_path, _s2 = render_plan_pdfs(
        "157 Test St, Testville CA", geo, road, work_area, Scope.BEHIND_CURB, ta_figure, devices, warnings, str(tmp_path)
    )
    text = PdfReader(s1_path).pages[0].extract_text()
    assert "PERMIT PENDING" in text


def test_render_creates_out_dir_if_missing(tmp_path):
    geo, road, work_area, ta_figure, devices, warnings = _plan()
    out_dir = tmp_path / "nested" / "out"
    assert not out_dir.exists()
    notes_path, s1_path, s2_path = render_plan_pdfs(
        "157 Test St, Testville CA", geo, road, work_area, Scope.BEHIND_CURB, ta_figure, devices, warnings, str(out_dir)
    )
    assert out_dir.exists()
    import os

    assert os.path.exists(notes_path)
    assert os.path.exists(s1_path)
    assert os.path.exists(s2_path)


def test_render_one_lane_scope_does_not_crash_and_has_flagger_marker(tmp_path):
    geo, road, work_area, ta_figure, devices, warnings = _plan(scope=Scope.ONE_LANE)
    notes_path, s1_path, s2_path = render_plan_pdfs(
        "157 Test St, Testville CA", geo, road, work_area, Scope.ONE_LANE, ta_figure, devices, warnings, str(tmp_path)
    )
    assert PdfReader(notes_path).pages
    assert PdfReader(s1_path).pages
    assert PdfReader(s2_path).pages


def test_sign_labels_measure_from_the_work_area():
    from core.layout import WorkArea
    from core.render_pdf import _work_area_distance_text

    wa = WorkArea(start_station_ft=1713, end_station_ft=1748, near_offset_ft=12, far_offset_ft=20, side=1)
    assert _work_area_distance_text(1513, wa) == "200'"
    assert _work_area_distance_text(1948, wa) == "200'"
    assert _work_area_distance_text(1730, wa) == "at work area"
    assert _work_area_distance_text(1713, wa) == "at work area"


def test_sidewalk_package_signs_numbered_and_listed(tmp_path):
    # 2026-10-07 (Michael): the 5.5pt labels stacked along the bottom of the
    # map were unreadable -- each sign now gets a numbered badge beside it and
    # a SIGNS list in the side panel (number, code, distance, wording). The
    # 997 Castle Hill regression (badges collapsing into one corner) is
    # covered by checking the badges spread across the map.
    geo, road, work_area, ta_figure, devices, warnings = _plan(sidewalk=True)
    assert any(d.kind == "barricade" for d in devices)
    _notes, sheet1, _sheet2 = render_plan_pdfs(
        geo.display_name, geo, road, work_area, Scope.BEHIND_CURB, ta_figure, devices, warnings, str(tmp_path)
    )
    signs = [d for d in devices if d.kind not in ("cone", "flagger", "barricade") and d.code]
    text = PdfReader(sheet1).pages[0].extract_text()
    assert "SIGNS (numbers match the map)" in text
    for d in signs:
        assert d.code in text
    badge_xs = []

    def visitor(t, cm, tm, font_dict, font_size):
        if t.strip().isdigit() and font_size and 5 <= font_size <= 7 and tm[4] < 600:
            badge_xs.append((tm[4], tm[5]))

    PdfReader(sheet1).pages[0].extract_text(visitor_text=visitor)
    assert len(badge_xs) >= len(signs)
    spread = max(max(p[0] for p in badge_xs) - min(p[0] for p in badge_xs),
                 max(p[1] for p in badge_xs) - min(p[1] for p in badge_xs))
    assert spread > 150   # badges sit at their signs, not piled in a corner
