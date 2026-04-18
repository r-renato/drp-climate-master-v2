"""Comfort band calculator (ISO 7730 PMV/PPD) - policy-aware.

Refactor goals (no logic changes)
--------------------------------
- Keep the numerical model intact.
- Remove duplicate room classification by using domain.is_living().
- Keep public signatures stable.

NOTE
----
Despite the filename, this module contains the calculator/engine.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from math import exp, sqrt
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence

from ....plant.monitor.plant import SeasonState, ZoneSnapshot
from ....domain.models.season import OperativeSeason, Seasons
from ....domain.models.runtime_schema import RuntimeConfig

from .model import ComfortBandResult, PolicyContext, PolicyDecision, HumiditySolveMode, is_living
from .policy_layer import ComfortPolicyLayer

from ....helpers.logger import log_debug, log_info, log_warning
from ....helpers.utils import slugify

_LOGGER = logging.getLogger(__name__)

class ComfortBandCalculator:
    """ISO 7730 PMV/PPD comfort-band calculator (Category-like band), policy-aware."""

    # speed 0..5 tables
    _V_AIR_BEST_LIVING = (0.05, 0.07, 0.09, 0.11, 0.13, 0.15)
    _V_AIR_BEST_OTHER = (0.05, 0.06, 0.07, 0.08, 0.09, 0.10)

    _V_AIR_HI_LIVING = (0.05, 0.092, 0.134, 0.176, 0.218, 0.260)
    _V_AIR_HI_OTHER = (0.05, 0.074, 0.098, 0.122, 0.146, 0.170)

    _V_AIR_LO = 0.05

    def __init__(
        self,
        *,
        met: float = 1.1,
        clo_winter: float = 1.0,
        clo_summer: float = 0.5,
        clo_shoulder: float = 0.7,
        work_met: float = 0.0,
        pressure_pa: float = 101325.0,
    ) -> None:
        self._met = float(met)
        self._wme = float(work_met)
        self._pressure_pa = float(pressure_pa)  # reserved for future/advanced models

        self._clo_map: Dict[OperativeSeason, float] = {
            OperativeSeason.WINTER: float(clo_winter),
            OperativeSeason.SUMMER: float(clo_summer),
            OperativeSeason.SHOULDER: float(clo_shoulder),
        }

    # ------------------------- v_air model ---------------------------------

    def estimate_v_air(
        self,
        *,
        speed: int,
        room: str,
        season: OperativeSeason,
        v_air_best_scale: float = 1.0,
        v_air_hi_scale: float = 1.0,
        v_air_lo_override: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Return (v_air_best, v_air_lo, v_air_hi) in m/s.

        Scaling/override parameters are typically provided by the policy layer.
        The 'season' parameter is currently unused (kept for signature compatibility).
        """
        _ = season

        if not isinstance(speed, int) or speed < 0 or speed > 5:
            raise ValueError(f"speed must be int in [0..5], got {speed}")

        if is_living(room):
            v_best = float(self._V_AIR_BEST_LIVING[speed])
            v_hi = float(self._V_AIR_HI_LIVING[speed])
        else:
            v_best = float(self._V_AIR_BEST_OTHER[speed])
            v_hi = float(self._V_AIR_HI_OTHER[speed])

        v_lo = float(v_air_lo_override) if v_air_lo_override is not None else float(self._V_AIR_LO)

        # apply policy scaling
        v_best = max(0.0, v_best * float(v_air_best_scale))
        v_hi = max(0.0, v_hi * float(v_air_hi_scale))
        v_lo = max(0.0, v_lo)

        # reasonable indoor clamps (defensive)
        v_best = min(v_best, 0.6)
        v_hi = min(v_hi, 0.8)
        v_lo = min(v_lo, 0.3)

        v_hi = max(v_hi, v_best, v_lo)  # ensure hi >= best >= lo
        return v_best, v_lo, v_hi

    # ------------------------- ISO 7730 PMV/PPD ----------------------------

    @staticmethod
    def _svp_pa(t_c: float) -> float:
        """Saturation vapor pressure (Pa) - Tetens-like approximation."""
        return 610.5 * exp(17.2694 * float(t_c) / (float(t_c) + 237.29))

    def pmv_ppd(
        self,
        *,
        ta_c: float,
        tr_c: float,
        rh_pct: float,
        v_air: float,
        met: float,
        clo: float,
        wme: float = 0.0,
        pa_override_pa: Optional[float] = None,
    ) -> Tuple[float, float]:
        """Compute PMV/PPD (Fanger) per ISO 7730 (classic implementation)."""
        rh = max(0.0, min(100.0, float(rh_pct)))
        v = max(0.0, float(v_air))

        m = float(met) * 58.15
        w = float(wme) * 58.15
        mw = m - w

        icl = float(clo) * 0.155
        ta = float(ta_c)
        tr = float(tr_c)

        # vapor pressure (Pa)
        # By default derive pa from RH at current Ta.
        # If pa_override_pa is provided, keep absolute humidity (vapor pressure) constant
        # across Ta variations (useful when solving comfort bands by changing temperature).
        if pa_override_pa is None:
            pa = rh / 100.0 * self._svp_pa(ta)
        else:
            pa = max(0.0, float(pa_override_pa))

        # clothing area factor
        if icl <= 0.078:
            fcl = 1.0 + 1.29 * icl
        else:
            fcl = 1.05 + 0.645 * icl

        hcf = 12.1 * sqrt(v)

        taa = ta + 273.0
        tra = tr + 273.0

        tcla = taa + (35.5 - ta) / (3.5 * (6.45 * icl + 0.1))

        p1 = icl * fcl
        p2 = p1 * 3.96
        p3 = p1 * 100.0
        p4 = p1 * taa
        p5 = 308.7 - 0.028 * mw + p2 * (tra / 100.0) ** 4

        xn = tcla / 100.0
        xf = xn
        eps = 0.00015
        n = 0

        while True:
            xf = xn
            hcn = 2.38 * abs(100.0 * xf - taa) ** 0.25
            hc = max(hcf, hcn)
            xn = (p5 + p4 * hc - p2 * (xf**4)) / (100.0 + p3 * hc)
            n += 1
            if n > 150 or abs(xn - xf) <= eps:
                break

        tcl = 100.0 * xn - 273.0

        hl1 = 3.05 * 0.001 * (5733.0 - 6.99 * mw - pa)
        if mw > 58.15:
            hl2 = 0.42 * (mw - 58.15)
        else:
            hl2 = 0.0
        hl3 = 1.7e-5 * m * (5867.0 - pa)
        hl4 = 0.0014 * m * (34.0 - ta)
        hl5 = 3.96 * fcl * ((tcl + 273.0) / 100.0) ** 4 - 3.96 * fcl * (tra / 100.0) ** 4
        hl6 = fcl * hc * (tcl - ta)

        ts = 0.303 * exp(-0.036 * m) + 0.028
        pmv = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)

        ppd = 100.0 - 95.0 * exp(-0.03353 * pmv**4 - 0.2179 * pmv**2)
        return float(pmv), float(ppd)

    # ------------------------- comfort band --------------------------------

    def _clo_for_season(self, season: OperativeSeason) -> float:
        return self._clo_map.get(season, self._clo_map[OperativeSeason.SHOULDER])

    def _default_draft_robustness(self, room: str, season: OperativeSeason) -> float:
        """Default alpha for blending v_best -> v_hi on the lower bound."""
        _ = season
        return 0.55 if is_living(room) else 0.35

    def _resolve_band_v_air_bounds(
        self,
        *,
        season: OperativeSeason,
        v_best: float,
        v_lo: float,
        v_hi: float,
        v_draft: float,
    ) -> tuple[float, float]:
        """Pick air-speed assumptions for band bounds (t_op_min, t_op_max).

        Rationale (thermo/comfort):
        - In WINTER, draft risk dominates the *cold-side* bound: use v_draft for t_op_min,
          and keep t_op_max conservative with v_lo.
        - In SUMMER, elevated air speed *extends* the warm-side comfort: keep t_op_min
          conservative (v_lo), and compute t_op_max with higher air speed (v_hi).
        - In SHOULDER, use a balanced assumption: protect the cold-side (v_draft) while
          not ignoring typical air movement on the warm-side (v_best).
        """
        if season == OperativeSeason.SUMMER:
            return float(v_lo), float(v_hi)
        if season == OperativeSeason.WINTER:
            return float(v_draft), float(v_lo)
        # SHOULDER
        return float(v_draft), float(v_best)

    def _resolve_pa_ref_for_band(
        self,
        *,
        rh_pct: float,
        anchor_t_c: Optional[float],
        mode: HumiditySolveMode,
        reasons: Optional[list[str]] = None,
    ) -> tuple[Optional[float], HumiditySolveMode]:
        """Return (pa_ref, mode_used) for band solving."""
        r: list[str] = reasons if reasons is not None else []

        mode_used = mode
        if mode == HumiditySolveMode.AUTO:
            mode_used = HumiditySolveMode.PA_CONST if anchor_t_c is not None else HumiditySolveMode.RH_CONST

        if mode_used == HumiditySolveMode.PA_CONST:
            if anchor_t_c is None:
                # Can't keep vapor pressure constant without a reference temperature.
                r.append("humidity:pa_const:no_anchor->rh_const")
                return None, HumiditySolveMode.RH_CONST
            rh_clamped = max(0.0, min(100.0, float(rh_pct)))
            pa_ref = (rh_clamped / 100.0) * self._svp_pa(float(anchor_t_c))
            r.append(f"humidity:pa_const:anchor={float(anchor_t_c):.2f}C")
            return float(pa_ref), HumiditySolveMode.PA_CONST

        # RH_CONST
        r.append("humidity:rh_const")
        return None, HumiditySolveMode.RH_CONST

    def _resolve_eval_temperatures(
        self,
        *,
        t_op_current: Optional[float],
        ta_current: Optional[float],
        tr_current: Optional[float],
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if t_op_current is not None:
            t_op_eval = float(t_op_current)
        elif ta_current is not None and tr_current is not None:
            t_op_eval = 0.5 * (float(ta_current) + float(tr_current))
        else:
            t_op_eval = None

        ta_eval = float(ta_current) if ta_current is not None else t_op_eval
        tr_eval = float(tr_current) if tr_current is not None else t_op_eval
        return t_op_eval, ta_eval, tr_eval

    def _pmv_at_top(
        self,
        *,
        t_op_c: float,
        rh_pct: float,
        v_air: float,
        season: OperativeSeason,
        met_override: Optional[float] = None,
        clo_override: Optional[float] = None,
        pa_ref: Optional[float] = None,
    ) -> float:
        clo = float(clo_override) if clo_override is not None else self._clo_for_season(season)
        met = float(met_override) if met_override is not None else self._met

        pmv, _ppd = self.pmv_ppd(
            ta_c=float(t_op_c),
            tr_c=float(t_op_c),
            rh_pct=float(rh_pct),
            v_air=float(v_air),
            met=float(met),
            clo=float(clo),
            wme=float(self._wme),
            pa_override_pa=pa_ref,
        )
        return float(pmv)

    def _bisect_temperature_for_pmv(
        self,
        *,
        target_pmv: float,
        rh_pct: float,
        v_air: float,
        season: OperativeSeason,
        met_override: Optional[float] = None,
        clo_override: Optional[float] = None,
        pa_ref: Optional[float] = None,
        t_min: float = 10.0,
        t_max: float = 35.0,
        max_iter: int = 60,
    ) -> float:
        lo = float(t_min)
        hi = float(t_max)

        f_lo = self._pmv_at_top(
            t_op_c=lo,
            rh_pct=rh_pct,
            v_air=v_air,
            season=season,
            met_override=met_override,
            clo_override=clo_override,
            pa_ref=pa_ref,
        ) - float(target_pmv)
        f_hi = self._pmv_at_top(
            t_op_c=hi,
            rh_pct=rh_pct,
            v_air=v_air,
            season=season,
            met_override=met_override,
            clo_override=clo_override,
            pa_ref=pa_ref,
        ) - float(target_pmv)

        expand_steps = 0
        while f_lo * f_hi > 0 and expand_steps < 8:
            lo = max(-10.0, lo - 5.0)
            hi = min(60.0, hi + 5.0)
            f_lo = self._pmv_at_top(
                t_op_c=lo,
                rh_pct=rh_pct,
                v_air=v_air,
                season=season,
                met_override=met_override,
                clo_override=clo_override,
                pa_ref=pa_ref,
            ) - float(target_pmv)
            f_hi = self._pmv_at_top(
                t_op_c=hi,
                rh_pct=rh_pct,
                v_air=v_air,
                season=season,
                met_override=met_override,
                clo_override=clo_override,
                pa_ref=pa_ref,
            ) - float(target_pmv)
            expand_steps += 1

        if f_lo * f_hi > 0:
            return lo if abs(f_lo) < abs(f_hi) else hi

        for _ in range(int(max_iter)):
            mid = (lo + hi) / 2.0
            f_mid = self._pmv_at_top(
                t_op_c=mid,
                rh_pct=rh_pct,
                v_air=v_air,
                season=season,
                met_override=met_override,
                clo_override=clo_override,
                pa_ref=pa_ref,
            ) - float(target_pmv)

            if abs(f_mid) < 1e-4:
                return float(mid)
            if f_lo * f_mid <= 0:
                hi = mid
                f_hi = f_mid
            else:
                lo = mid
                f_lo = f_mid

        return float((lo + hi) / 2.0)

    def compute_single(
        self,
        *,
        vmc_air_speed: int,
        room: str,
        season: OperativeSeason | str,
        rh_pct: float,
        t_op_current: Optional[float] = None,
        ta_current: Optional[float] = None,
        tr_current: Optional[float] = None,
        # policy-aware knobs (all optional)
        policy: Optional[PolicyDecision] = None,
        met_override: Optional[float] = None,
        clo_override: Optional[float] = None,
        pmv_center: Optional[float] = None,
        pmv_band: Optional[float] = None,
        draft_robustness: Optional[float] = None,
        humidity_solve_mode: HumiditySolveMode | str | None = None,
    ) -> ComfortBandResult:
        season_value = OperativeSeason.from_value(season)

        # Resolve inputs precedence: explicit overrides > policy > legacy defaults
        met_used = float(met_override) if met_override is not None else float(policy.met) if policy else float(self._met)
        clo_used = float(clo_override) if clo_override is not None else float(policy.clo) if policy else float(self._clo_for_season(season_value))

        pmv_center_used = float(pmv_center) if pmv_center is not None else float(policy.pmv_center) if policy else 0.0
        pmv_band_used = float(pmv_band) if pmv_band is not None else float(policy.pmv_band) if policy else 0.5
        pmv_band_used = max(0.0, float(pmv_band_used))

        v_best_scale = float(policy.v_air_best_scale) if policy else 1.0
        v_hi_scale = float(policy.v_air_hi_scale) if policy else 1.0
        v_lo_ovr = policy.v_air_lo_override if policy else None

        v_best, v_lo, v_hi = self.estimate_v_air(
            speed=int(vmc_air_speed),
            room=str(room),
            season=season_value,
            v_air_best_scale=v_best_scale,
            v_air_hi_scale=v_hi_scale,
            v_air_lo_override=v_lo_ovr,
        )

        # --- Draft robustness for cold-side bound (t_op_min) ---------------
        policy_alpha = getattr(policy, "draft_robustness", None) if policy else None
        alpha = (
            float(draft_robustness)
            if draft_robustness is not None
            else float(policy_alpha)
            if policy_alpha is not None
            else float(self._default_draft_robustness(str(room), season_value))
        )
        alpha = max(0.0, min(1.0, alpha))
        v_draft = float(v_best) + alpha * (float(v_hi) - float(v_best))

        # Select which v_air to use for each comfort bound (season-aware)
        v_for_min, v_for_max = self._resolve_band_v_air_bounds(
            season=season_value, v_best=float(v_best), v_lo=float(v_lo), v_hi=float(v_hi), v_draft=float(v_draft)
        )

        pmv_lo = pmv_center_used - pmv_band_used
        pmv_hi = pmv_center_used + pmv_band_used

        # --- Humidity handling for band solve --------------------------------
        # When solving for t_op_min/t_op_max we vary temperature.
        # Keeping RH constant is often non-physical for short time horizons: in most
        # homes absolute humidity (vapor pressure) changes slower than temperature.
        # If we have an "anchor" temperature, solve with constant vapor pressure (pa_ref).
        anchor_t: Optional[float] = None
        if t_op_current is not None:
            anchor_t = float(t_op_current)
        elif ta_current is not None and tr_current is not None:
            anchor_t = 0.5 * (float(ta_current) + float(tr_current))

        # Resolve precedence: explicit override > policy > default(AUTO)
        policy_h_mode = getattr(policy, "humidity_solve_mode", None) if policy else None
        raw_h_mode = (
            humidity_solve_mode
            if humidity_solve_mode is not None
            else policy_h_mode
            if policy_h_mode is not None
            else HumiditySolveMode.AUTO
        )

        try:
            h_mode = raw_h_mode if isinstance(raw_h_mode, HumiditySolveMode) else HumiditySolveMode(str(raw_h_mode))
        except Exception:
            h_mode = HumiditySolveMode.AUTO

        # collect trace reasons locally (surface in result for commissioning/debug)
        humidity_reasons: list[str] = []
        pa_ref, h_mode_used = self._resolve_pa_ref_for_band(
            rh_pct=float(rh_pct),
            anchor_t_c=anchor_t,
            mode=h_mode,
            reasons=humidity_reasons,
        )

        t_op_min = self._bisect_temperature_for_pmv(
            target_pmv=float(pmv_lo),
            rh_pct=float(rh_pct),
            v_air=float(v_for_min),
            season=season_value,
            met_override=met_used,
            clo_override=clo_used,
            pa_ref=pa_ref,
        )
        t_op_max = self._bisect_temperature_for_pmv(
            target_pmv=float(pmv_hi),
            rh_pct=float(rh_pct),
            v_air=float(v_for_max),
            season=season_value,
            met_override=met_used,
            clo_override=clo_used,
            pa_ref=pa_ref,
        )

        if t_op_min > t_op_max:
            t_op_min, t_op_max = t_op_max, t_op_min

        res = ComfortBandResult(
            room=str(room),
            season=season_value,
            speed=int(vmc_air_speed),
            v_air_best=float(v_best),
            v_air_lo=float(v_lo),
            v_air_hi=float(v_hi),
            v_air_draft=float(v_draft),
            t_op_min=float(t_op_min),
            t_op_max=float(t_op_max),
            t_op=None,
            pmv=None,
            ppd=None,
            ok=None,
            humidity_solve_mode=str(h_mode_used),
            pmv_center=float(pmv_center_used),
            pmv_band=float(pmv_band_used),
            met_used=float(met_used),
            clo_used=float(clo_used),
        )

        t_op_eval, ta_eval, tr_eval = self._resolve_eval_temperatures(
            t_op_current=t_op_current,
            ta_current=ta_current,
            tr_current=tr_current,
        )

        if t_op_eval is not None and ta_eval is not None and tr_eval is not None:
            pmv, ppd = self.pmv_ppd(
                ta_c=float(ta_eval),
                tr_c=float(tr_eval),
                rh_pct=float(rh_pct),
                v_air=float(v_best),
                met=float(met_used),
                clo=float(clo_used),
                wme=float(self._wme),
            )
            ok = float(t_op_min) <= float(t_op_eval) <= float(t_op_max)
            res = replace(
                res,
                t_op=float(t_op_eval),
                pmv=float(pmv),
                ppd=float(ppd),
                ok=bool(ok),
            )

        return res

    def compute_many(
        self,
        *,
        now: datetime,
        season: OperativeSeason | str,
        vmc_air_speed: int,
        indoor_zones: Mapping[str, ZoneSnapshot],
        outdoor_temp: float | None,
        mode,  # HVACOperatingProfile (kept unimported here to avoid circulars)
        policy_layer: ComfortPolicyLayer,
        room_names: Sequence[str] | None = None,
        include_global: bool = True,
        humidity_solve_mode: HumiditySolveMode | str | None = None,
        cold_snap: bool = False,
    ) -> Dict[str, ComfortBandResult]:
        """Compute comfort band for multiple rooms.

        - If room_names is None, uses all keys in indoor_zones (except optional global handling).
        - If include_global=True and indoor_zones contains "global", also returns "global_indoor".
        - humidity_solve_mode precedence is handled by compute_single():
            explicit override > policy.humidity_solve_mode > default(AUTO)
        """
        season_value = OperativeSeason.from_value(season)

        def _val(x: object) -> Optional[float]:
            # supports your common "ValueWithTs" shape: obj.value
            try:
                v = getattr(x, "value")
            except Exception:
                v = None
            return float(v) if v is not None else None

        def _extract_zone_inputs(z: ZoneSnapshot) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
            """Return (rh, t_op, ta, tr) if present."""
            rh = _val(getattr(z, "humidity", None))
            t_op = _val(getattr(z, "t_op", None))

            # Optional fallbacks if your ZoneSnapshot exposes them (best effort)
            ta = _val(getattr(z, "temperature", None)) or _val(getattr(z, "t_air", None)) or _val(getattr(z, "ta", None))
            tr = _val(getattr(z, "mrt", None)) or _val(getattr(z, "t_mrt", None)) or _val(getattr(z, "tr", None))
            return rh, t_op, ta, tr

        def _compute_one(room_id: str) -> Optional[ComfortBandResult]:
            z = indoor_zones.get(room_id)
            if z is None:
                log_warning(_LOGGER, "comfort_band:skip zone=%s reason=zone_not_in_snapshot", room_id)
                return None

            rh, t_op, ta, tr = _extract_zone_inputs(z)

            # Need at least humidity and one temperature input path
            if rh is None:
                log_warning(_LOGGER, "comfort_band:skip zone=%s reason=missing_humidity", room_id)
                return None
            if t_op is None and (ta is None or tr is None):
                log_warning(
                    _LOGGER,
                    "comfort_band:skip zone=%s reason=missing_temperature t_op=%s ta=%s tr=%s",
                    room_id, t_op, ta, tr,
                )
                return None

            ctx = PolicyContext(
                now=now,
                room=room_id,
                season=season_value,
                vmc_speed=int(vmc_air_speed),
                rh_pct=float(rh),
                t_op_current=float(t_op) if t_op is not None else None,
                outdoor_temp=outdoor_temp,
                mode=mode,
                cold_snap=bool(cold_snap),
            )
            decision: PolicyDecision = policy_layer.decide(ctx)

            return self.compute_single(
                vmc_air_speed=int(vmc_air_speed),
                room=room_id,
                season=season_value,
                rh_pct=float(rh),
                t_op_current=float(t_op) if t_op is not None else None,
                ta_current=float(ta) if ta is not None else None,
                tr_current=float(tr) if tr is not None else None,
                policy=decision,
                humidity_solve_mode=humidity_solve_mode,
            )

        out: Dict[str, ComfortBandResult] = {}

        # Determine rooms list
        rooms: Sequence[str]
        if room_names is None:
            rooms = [k for k in indoor_zones.keys() if k != "global"]
        else:
            rooms = list(room_names)

        for room_id in rooms:
            res = _compute_one(str(room_id))
            if res is not None:
                out[str(room_id)] = res

        if include_global:
            # Convention used in your current code: "global" zone -> "global_indoor"
            if "global" in indoor_zones:
                res_g = _compute_one("global")
                if res_g is not None:
                    out["global_indoor"] = res_g

        return out
