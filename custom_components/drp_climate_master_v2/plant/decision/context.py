from __future__ import annotations

from dataclasses import dataclass

from ...domain.enums import HVACOperatingProfile


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
