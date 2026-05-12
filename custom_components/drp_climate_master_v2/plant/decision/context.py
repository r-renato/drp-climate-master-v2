from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from ...domain.enums import HVACOperatingProfile
from .comfort_band.model import ComfortBandResult


@dataclass(slots=True, frozen=True)
class UserIntent:
    """Normalized user intent derived from the Home Assistant Climate entity.

    Notes
    - `hvac_mode` is normalized to a lower-case string (e.g. "auto", "off").
    - `profile` is normalized to HVACOperatingProfile (Comfort/Eco/Boost/...).
    - `forced_off` reflects an absolute user override (hvac_mode == "off").
    """

    hvac_mode: str
    profile: HVACOperatingProfile
    forced_off: bool


@dataclass(slots=True, frozen=True)
class SeasonContext:
    """Normalized season context used by plant decision logic.

    - `runtime` is the raw season classifier output (e.g. "winter", "summer", "unknown").
    - `operative` collapses runtime seasons into plant buckets ("winter"/"summer"/"shoulder").
    """

    runtime: str
    operative: str


@dataclass(slots=True, frozen=True)
class DecisionDerivedInputs:
    """Input derivati calcolati *solo* in fase di decisione.

    Questo oggetto esiste per evitare di "inquinare" i model osservativi (snapshot)
    con output o parametri decisionali.

    Attualmente include:
    - comfort_bands_by_zone: banda di comfort per ciascuna zona (T_op_min/max, PMV/PPD, debug)
    """

    comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None
