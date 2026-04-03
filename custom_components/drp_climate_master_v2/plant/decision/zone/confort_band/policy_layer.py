"""Policy layer for DRP Climate Master v2 comfort calculations.

Refactor goals (no logic changes)
--------------------------------
- Keep physics in ComfortBandCalculator.
- Keep policy decisions transparent (reasons).
- Remove duplicate room-classification logic by using domain.is_living().
- Keep existing public types/behavior.

Notes
-----
- Italian climate zones A..F are typically defined by degree-days (DPR 412/1993).
  This module intentionally does not implement municipality->zone lookup.
  Provide climate_zone in config (or compute upstream).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Dict, Literal, Optional, Tuple

from .....domain.enums import HVACOperatingProfile

try:
    # Preferred in Home Assistant for correct timezone handling
    from homeassistant.util import dt as dt_util  # type: ignore
except Exception:  # pragma: no cover
    dt_util = None

from .model import PolicyContext, PolicyDecision, HumiditySolveMode, is_living


# -----------------------------
# Public types
# -----------------------------


class ClimateZoneIT(StrEnum):
    """Italian climate zone (A-F)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"

    @classmethod
    def is_member(cls, value: Any) -> bool:
        """Return True if value represents a valid climate zone."""
        if isinstance(value, cls):
            return True
        if isinstance(value, str):
            v = value.strip().upper()
            return v in cls._value2member_map_
        return False

    @classmethod
    def from_str(cls, value: Optional[str]) -> Optional["ClimateZoneIT"]:
        """Parse a string into ClimateZoneIT; returns None if invalid."""
        if value is None:
            return None
        v = value.strip().upper()
        if not v:
            return None
        try:
            return cls(v)
        except ValueError:
            return None


class ComplianceMode(StrEnum):
    """Compliance gating mode for policy decisions."""

    OFF = "off"
    WARN = "warn"
    ENFORCE = "enforce"


# -----------------------------
# Configuration / data model
# -----------------------------


@dataclass(slots=True)
class ConfortPolicyConfig:
    """Static configuration for the comfort policy layer."""

    # Context
    climate_zone: Optional[ClimateZoneIT] = None

    # Comfort profile
    base_met: float = 1.10
    base_clo_summer: float = 0.50
    base_clo_shoulder: float = 0.70
    default_clo_winter: float = 1.00

    # If True and climate_zone is set, policy may apply a zone-specific winter CLO.
    zone_clo_delta_enabled: bool = True

    # Draft calibration
    non_living_high_speed_hi_scale: float = 0.90
    living_high_speed_hi_scale: float = 1.05

    # Compliance gating
    compliance_mode: ComplianceMode = ComplianceMode.OFF
    heating_allowed_from: Optional[time] = None
    heating_allowed_to: Optional[time] = None
    cooling_allowed_from: Optional[time] = None
    cooling_allowed_to: Optional[time] = None

    # ---------------------------------------------------------------------
    # Helper methods
    # ---------------------------------------------------------------------

    def clo_for_season(self, season: Literal["summer", "shoulder", "winter"]) -> float:
        if season == "summer":
            return self.base_clo_summer
        if season == "shoulder":
            return self.base_clo_shoulder
        return self.default_clo_winter

    def winter_clo(self, *, zone_delta: Optional[float] = None) -> float:
        clo = self.default_clo_winter
        if self.zone_clo_delta_enabled and zone_delta is not None:
            clo += zone_delta
        return clo

    def draft_hi_scale(self, *, is_living_room: bool) -> float:
        return self.living_high_speed_hi_scale if is_living_room else self.non_living_high_speed_hi_scale

    def allowed_window(self, mode: Literal["heating", "cooling"]) -> tuple[Optional[time], Optional[time]]:
        if mode == "heating":
            return self.heating_allowed_from, self.heating_allowed_to
        return self.cooling_allowed_from, self.cooling_allowed_to

    def is_within_allowed_window(self, *, mode: Literal["heating", "cooling"], now_local: time) -> bool:
        """Return True if now_local is inside the configured window.

        Semantics preserved:
        - If window not configured (start or end is None) => True.
        - Supports overnight windows.
        """
        start, end = self.allowed_window(mode)
        if start is None or end is None:
            return True
        if start <= end:
            return start <= now_local < end
        return now_local >= start or now_local < end

    def compliance_enabled(self) -> bool:
        return self.compliance_mode in (ComplianceMode.WARN, ComplianceMode.ENFORCE)

    def validate(self) -> None:
        if self.base_met <= 0:
            raise ValueError("base_met must be > 0")
        for name, v in (
            ("base_clo_summer", self.base_clo_summer),
            ("base_clo_shoulder", self.base_clo_shoulder),
            ("default_clo_winter", self.default_clo_winter),
            ("non_living_high_speed_hi_scale", self.non_living_high_speed_hi_scale),
            ("living_high_speed_hi_scale", self.living_high_speed_hi_scale),
        ):
            if v <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.compliance_mode not in (ComplianceMode.OFF, ComplianceMode.WARN, ComplianceMode.ENFORCE):
            raise ValueError("compliance_mode must be 'off', 'warn' or 'enforce'")


# -----------------------------
# Defaults / tables
# -----------------------------


# Heuristic mapping of winter clothing insulation by climate zone.
ZONE_CLO_WINTER: Dict[ClimateZoneIT, float] = {
    ClimateZoneIT.A: 0.90,
    ClimateZoneIT.B: 0.95,
    ClimateZoneIT.C: 1.00,
    ClimateZoneIT.D: 1.05,
    ClimateZoneIT.E: 1.15,
    ClimateZoneIT.F: 1.25,
}


MODE_PMV_DEFAULTS: Dict[HVACOperatingProfile, Dict[str, float]] = {
    HVACOperatingProfile.COMFORT: {"pmv_center": 0.00, "pmv_band": 0.50},
    HVACOperatingProfile.BOOST: {"pmv_center": 0.00, "pmv_band": 0.50},
    HVACOperatingProfile.ECO: {"pmv_center": -0.10, "pmv_band": 0.35},
    HVACOperatingProfile.SLEEP: {"pmv_center": -0.40, "pmv_band": 0.45},
    HVACOperatingProfile.AWAY: {"pmv_center": -0.60, "pmv_band": 1.20},
    HVACOperatingProfile.VACATION: {"pmv_center": -0.60, "pmv_band": 1.20},
}


MODE_CTRL_DEFAULTS: Dict[HVACOperatingProfile, float] = {
    HVACOperatingProfile.COMFORT: 1.00,
    HVACOperatingProfile.BOOST: 1.35,
    HVACOperatingProfile.ECO: 0.85,
    HVACOperatingProfile.SLEEP: 0.75,
    HVACOperatingProfile.AWAY: 0.50,
    HVACOperatingProfile.VACATION: 0.50,
}


# Frazione di interpolazione clo in caso di cold_snap (shoulder → winter).
# Valore 0.40 = 40 % del delta shoulder→winter: persone più vestite ma non
# al livello pieno invernale.  Valori utili: 0.25 (debole) … 0.60 (forte).
_COLD_SNAP_CLO_FRACTION: float = 0.40

# -----------------------------
# Implementation
# -----------------------------

class ComfortPolicyLayer:
    """Policy layer that selects comfort-model inputs."""

    def __init__(self, cfg: ConfortPolicyConfig) -> None:
        self._cfg = cfg

    def decide(self, ctx: PolicyContext) -> PolicyDecision:
        reasons: list[str] = []

        # 0) Validate inputs lightly
        vmc_speed = ctx.vmc_speed
        if not isinstance(vmc_speed, int):
            reasons.append(f"vmc_speed:invalid_type={type(vmc_speed).__name__}")
            try:
                vmc_speed = int(vmc_speed)  # best effort
            except Exception:
                vmc_speed = 0
        if vmc_speed < 0 or vmc_speed > 5:
            reasons.append(f"vmc_speed:clamped_from={vmc_speed}")
            vmc_speed = max(0, min(5, vmc_speed))

        # 1) met (mode-aware)
        met = float(self._cfg.base_met)
        if ctx.mode == HVACOperatingProfile.SLEEP:
            met = 0.90
            reasons.append("met:sleep=0.90")
        elif ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            met = 1.00
            reasons.append("met:away/vacation=1.00")

        # 2) clo (season + climate zone + mode)
        clo = self._clo_for(ctx, reasons)

        # 3) PMV targets from mode
        pmv_center, pmv_band = self._pmv_targets(ctx, reasons)

        # 3b) Controller aggressiveness (decoupled from PMV)
        ctrl_aggr = float(MODE_CTRL_DEFAULTS.get(ctx.mode, 1.0))
        reasons.append(f"ctrl:aggr={ctrl_aggr:.2f}")

        # 4) v_air scaling (draft calibration)
        v_best_s, v_hi_s, v_lo_override = self._v_air_policy(ctx, vmc_speed, reasons)

        # Draft robustness (optional): used by ComfortBandCalculator to blend v_best/v_hi
        draft_alpha: Optional[float] = None
        living = is_living(ctx.room)
        draft_alpha = 0.55 if living else 0.35
        if ctx.mode == HVACOperatingProfile.SLEEP:
            # At night avoid being too draft-conservative (prevents early heating)
            draft_alpha = max(0.05, draft_alpha - 0.10)
            reasons.append("SLEEP_delta_draft=-0.10")
        if (vmc_speed >= 4) and (not living):
            draft_alpha = max(0.0, draft_alpha - 0.05)

        # 4b) Humidity solving mode for comfort-band bounds (policy choice)
        # Default strategy:
        # - SUMMER/WINTER: favor PA_CONST (more physical for typical homes without tight RH control)
        # - SHOULDER: AUTO (lets the calculator fall back to RH_CONST if no anchor temperature is available)
        from .....domain.models.season import OperativeSeason

        if ctx.season in (OperativeSeason.SUMMER, OperativeSeason.WINTER):
            h_mode = HumiditySolveMode.PA_CONST
        else:
            h_mode = HumiditySolveMode.AUTO
        reasons.append(f"humidity_solve_mode={h_mode}")

        # 5) optional compliance gating
        heating_allowed, cooling_allowed = self._compliance(ctx, reasons)

        return PolicyDecision(
            met=float(met),
            clo=float(clo),
            pmv_center=float(pmv_center),
            pmv_band=float(pmv_band),
            ctrl_aggressiveness=float(ctrl_aggr),
            v_air_best_scale=float(v_best_s),
            v_air_hi_scale=float(v_hi_s),
            v_air_lo_override=v_lo_override,
            draft_robustness=draft_alpha,
            humidity_solve_mode=h_mode,
            heating_allowed=heating_allowed,
            cooling_allowed=cooling_allowed,
            reasons=tuple(reasons),
        )

    # -------------------------
    # Internals
    # -------------------------

    def _clo_for(self, ctx: PolicyContext, reasons: list[str]) -> float:
        from .....domain.models.season import OperativeSeason

        # Base by season
        if ctx.season == OperativeSeason.SUMMER:
            clo = float(self._cfg.base_clo_summer)
            reasons.append(f"clo:summer={clo:.2f}")
        elif ctx.season == OperativeSeason.SHOULDER:
            clo = float(self._cfg.base_clo_shoulder)
            reasons.append(f"clo:shoulder={clo:.2f}")
            # cold_snap: interpola clo verso inverno.
            # Le persone si vestono più pesante nei giorni freddi per la stagione;
            # il PMV calcolato con clo più alto è fisicamente più corretto.
            if ctx.cold_snap:
                if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                    clo_winter = float(ZONE_CLO_WINTER.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                else:
                    clo_winter = float(self._cfg.default_clo_winter)
                clo = clo + _COLD_SNAP_CLO_FRACTION * (clo_winter - clo)
                reasons.append(f"clo:cold_snap:interp={clo:.2f}")
        else:
            # Winter: use climate zone mapping if enabled and zone is known
            if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                clo = float(ZONE_CLO_WINTER.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                reasons.append(f"clo:winter:zone={self._cfg.climate_zone}={clo:.2f}")
            else:
                clo = float(self._cfg.default_clo_winter)
                reasons.append(f"clo:winter:default={clo:.2f}")

        # Mode adjustments
        if ctx.mode == HVACOperatingProfile.SLEEP and ctx.season.name.lower() == "winter":
            clo += 0.95
            reasons.append(f"clo:sleep:+0.95 -> {clo:.2f}")

        if ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION) and ctx.season.name.lower() == "winter":
            clo = max(0.70, clo - 0.10)
            reasons.append(f"clo:away:-0.10 -> {clo:.2f}")

        clo_cap = 2.4 if ctx.mode == HVACOperatingProfile.SLEEP else 1.6
        clo = min(clo_cap, clo)
        return float(clo)

    def _pmv_targets(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[float, float]:
        from .....domain.models.season import OperativeSeason

        md = MODE_PMV_DEFAULTS.get(ctx.mode, MODE_PMV_DEFAULTS[HVACOperatingProfile.COMFORT])
        pmv_center = float(md["pmv_center"])
        pmv_band = float(md["pmv_band"])
        reasons.append(f"pmv:mode={ctx.mode}:center={pmv_center:+.2f},band={pmv_band:.2f}")

        # Optional nudges
        if ctx.season == OperativeSeason.SUMMER and ctx.mode == HVACOperatingProfile.ECO:
            pmv_center = min(0.20, pmv_center + 0.10)
            reasons.append(f"pmv:summer_eco:center_adj -> {pmv_center:+.2f}")

        return pmv_center, pmv_band

    def _v_air_policy(self, ctx: PolicyContext, vmc_speed: int, reasons: list[str]) -> Tuple[float, float, Optional[float]]:
        v_best_s = 1.0
        v_hi_s = 1.0
        v_lo_override: Optional[float] = None

        living = is_living(ctx.room)

        if vmc_speed >= 4 and not living:
            v_hi_s = float(self._cfg.non_living_high_speed_hi_scale)
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (speed>=4 non-living)")

        if living and vmc_speed >= 3:
            v_hi_s = max(v_hi_s, float(self._cfg.living_high_speed_hi_scale))
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (living speed>=3)")

        # Sleep/Winter: reduce draft conservatism
        if ctx.mode == HVACOperatingProfile.SLEEP and ctx.season.name.lower() == "winter":
            v_hi_s = min(v_hi_s, 1.00)
            reasons.append("v_air_hi:sleep<=1.00")

        return float(v_best_s), float(v_hi_s), v_lo_override

    def _compliance(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[Optional[bool], Optional[bool]]:
        from .....domain.models.season import OperativeSeason

        if self._cfg.compliance_mode == ComplianceMode.OFF:
            return None, None

        now_local: datetime = ctx.now
        if dt_util is not None:
            now_local = dt_util.as_local(ctx.now)
        elif ctx.now.tzinfo is not None:
            now_local = ctx.now.astimezone()

        h_ok = self._time_in_window_eval(now_local, self._cfg.heating_allowed_from, self._cfg.heating_allowed_to)
        c_ok = self._time_in_window_eval(now_local, self._cfg.cooling_allowed_from, self._cfg.cooling_allowed_to)

        heating_allowed: Optional[bool] = None
        cooling_allowed: Optional[bool] = None

        if ctx.season == OperativeSeason.WINTER:
            heating_allowed = h_ok
            if heating_allowed is not None:
                reasons.append(f"compliance:heating_allowed={heating_allowed}")
        elif ctx.season == OperativeSeason.SUMMER:
            cooling_allowed = c_ok
            if cooling_allowed is not None:
                reasons.append(f"compliance:cooling_allowed={cooling_allowed}")

        return heating_allowed, cooling_allowed

    @staticmethod
    def _time_in_window_eval(now: datetime, start: Optional[time], end: Optional[time]) -> Optional[bool]:
        """Return whether now.local_time is within [start, end). Supports overnight windows.

        Semantics preserved:
        - If start or end is None => returns None (not evaluable)
        - 'now' should already be in local timezone.
        """
        if start is None or end is None:
            return None

        t = now.timetz().replace(tzinfo=None)
        if start <= end:
            return start <= t < end
        return (t >= start) or (t < end)


# -----------------------------
# Builder
# -----------------------------


def build_policy_layer(
    *,
    climate_zone: Optional[ClimateZoneIT] = ClimateZoneIT.D,
    compliance_mode: ComplianceMode = ComplianceMode.OFF,
) -> ComfortPolicyLayer:
    """Convenience builder (no behavior change; no automatic validate)."""

    if climate_zone is None:
        cz = None
    elif isinstance(climate_zone, ClimateZoneIT):
        cz = climate_zone
    else:
        v = str(climate_zone).strip().upper()
        cz = ClimateZoneIT.from_str(v) if ClimateZoneIT.is_member(v) else None

    cfg = ConfortPolicyConfig(climate_zone=cz, compliance_mode=compliance_mode)
    return ComfortPolicyLayer(cfg)
