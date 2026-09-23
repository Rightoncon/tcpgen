"""MUTCD (California MUTCD) rules engine — pure lookup and formulas, no LLM
(spec §6). Deciding which typical application applies, sign spacing, taper
length, device spacing, and buffer space are all deterministic here.

Classifying free-text into a scope, or drafting notes prose, is optional
LLM assist reserved for a later phase and stays entirely out of this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


class Scope(str, Enum):
    BEHIND_CURB = "behind_curb"
    SHOULDER = "shoulder"
    ONE_LANE = "one_lane"
    CUL_DE_SAC = "cul_de_sac"
    FULL_CLOSURE = "full_closure"  # gated off in v1, spec §1.3/§6.1


class ScopeNotSupportedError(Exception):
    """Raised for FULL_CLOSURE, which spec §1.3 explicitly excludes from v1."""


class TaFigureNotBuiltError(Exception):
    """The rules table (§6.2) names a TA figure this phase's ta_figures.yaml
    doesn't define yet — Phase 2 only built 6H-2 and 6H-10."""


# spec §6.2 — pure lookup, scope -> TA figure key into ta_figures.yaml.
# Never infer this from anything; the dropdown that sets `scope` must start
# unset and force an explicit choice (spec: "Never default to one_lane").
SCOPE_TA_FIGURE = {
    Scope.BEHIND_CURB: "6H-2",
    Scope.SHOULDER: "6H-3",
    Scope.ONE_LANE: "6H-10",
    Scope.CUL_DE_SAC: "6H-10",  # "adapted" per spec — layout.py handles the difference
}


def ta_figure_for_scope(scope: Scope) -> str:
    """spec §6.2. Raises ScopeNotSupportedError for FULL_CLOSURE."""
    if scope == Scope.FULL_CLOSURE:
        raise ScopeNotSupportedError("full_closure is gated off in v1 (spec §1.3)")
    return SCOPE_TA_FIGURE[scope]


# spec §6.2 pedestrian package — applied regardless of scope whenever
# sidewalk_affected is set.
PEDESTRIAN_SIGNS = ("R9-9", "R9-11")
PEDESTRIAN_NOTE = (
    "Sidewalk closed: maintain a 48 in ADA-compliant pedestrian path, or "
    "provide a signed detour (MUTCD 6H-28/6H-29)."
)


@dataclass
class SignSpec:
    code: str
    text: str
    station_multiplier: int


@dataclass
class TaFigure:
    key: str
    name: str
    flaggers: int
    lanes_closed: int
    signs_per_approach: list[SignSpec]
    end_sign: bool
    end_sign_code: Optional[str]
    end_sign_text: Optional[str]
    device_type: str
    device_placement: str


def load_ta_figures(path: Optional[Path] = None) -> dict[str, TaFigure]:
    """Loads rules/ta_figures.yaml (spec §6.3). Only 6H-2 and 6H-10 exist
    as of Phase 2 — callers should use `get_ta_figure` for a clear error
    on a scope that maps to a figure not built yet (e.g. shoulder -> 6H-3)."""
    path = path or (RULES_DIR / "ta_figures.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    figures: dict[str, TaFigure] = {}
    for key, spec in raw.items():
        signs = [SignSpec(**s) for s in spec.get("signs_per_approach", [])]
        devices = spec.get("devices", {})
        figures[key] = TaFigure(
            key=key,
            name=spec["name"],
            flaggers=spec["flaggers"],
            lanes_closed=spec["lanes_closed"],
            signs_per_approach=signs,
            end_sign=spec.get("end_sign", False),
            end_sign_code=spec.get("end_sign_code"),
            end_sign_text=spec.get("end_sign_text"),
            device_type=devices.get("type", "cone"),
            device_placement=devices.get("placement", ""),
        )
    return figures


def get_ta_figure(scope: Scope, figures: dict[str, TaFigure]) -> TaFigure:
    key = ta_figure_for_scope(scope)
    try:
        return figures[key]
    except KeyError:
        raise TaFigureNotBuiltError(
            f"scope {scope.value!r} maps to TA figure {key!r}, which isn't "
            f"in ta_figures.yaml yet (Phase 2 only built 6H-2 and 6H-10)."
        ) from None


# spec §6.4, MUTCD/CA MUTCD Table 6H-3 — sign spacing by posted speed.
# (max_speed_inclusive, spacing_ft), checked in ascending order so an
# off-table speed (e.g. 32 mph) gets the next bracket up rather than
# silently falling through to the last (highest) one.
# California note (spec §6.4): certified examples are the authority where
# they disagree with this table — none ingested yet (that's Phase 5).
SIGN_SPACING_TABLE_FT = (
    (30, 100),  # urban, <=30 mph
    (40, 250),  # urban, 35-40 mph
    (55, 350),  # urban, 45-55 mph
    (65, 500),  # rural, 55-65 mph
)


def sign_spacing_ft(speed_mph: int) -> int:
    for max_speed, spacing in SIGN_SPACING_TABLE_FT:
        if speed_mph <= max_speed:
            return spacing
    return SIGN_SPACING_TABLE_FT[-1][1]


def taper_length_ft(offset_ft: float, speed_mph: int) -> float:
    """spec §6.5, MUTCD 6C.08."""
    if speed_mph <= 40:
        return offset_ft * speed_mph**2 / 60
    return offset_ft * speed_mph


def shifting_taper_ft(offset_ft: float, speed_mph: int) -> float:
    return taper_length_ft(offset_ft, speed_mph) / 2


def shoulder_taper_ft(offset_ft: float, speed_mph: int) -> float:
    return taper_length_ft(offset_ft, speed_mph) / 3


def downstream_taper_ft(lanes_closed: int) -> float:
    return 100.0 * max(1, lanes_closed)


def device_spacing_ft(speed_mph: int, *, on_taper: bool) -> float:
    """spec §6.6, MUTCD 6C.05: taper spacing = speed; tangent spacing = 2x speed."""
    return float(speed_mph) if on_taper else float(2 * speed_mph)


# spec §6.7, MUTCD Table 6C-2 — longitudinal buffer space by speed. This is
# a fixed lookup table, not a formula — extrapolating between rows would
# invent a number MUTCD doesn't specify, so off-table speeds snap to the
# nearest tabulated one instead.
BUFFER_SPACE_FT = {20: 115, 25: 155, 30: 200, 35: 250, 40: 305, 45: 360}


def buffer_space_ft(speed_mph: int) -> int:
    if speed_mph in BUFFER_SPACE_FT:
        return BUFFER_SPACE_FT[speed_mph]
    nearest = min(BUFFER_SPACE_FT, key=lambda s: abs(s - speed_mph))
    return BUFFER_SPACE_FT[nearest]
