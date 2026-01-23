from __future__ import annotations

from dataclasses import dataclass, replace
from math import exp, sqrt
from typing import Dict, Tuple, Optional

from .policy_layer import PolicyDecision

from ...domain.models.season import OperativeSeason

@dataclass(frozen=True, slots=True)
class ComfortBandResult:
    """Result of comfort band computation for a room/zone."""

    room: str
    season: OperativeSeason
    speed: int

    v_air_best: float
    v_air_lo: float
    v_air_hi: float

    t_op_min: float
    t_op_max: float

    # current point evaluation
    t_op: Optional[float] = None
    pmv: Optional[float] = None
    ppd: Optional[float] = None
    ok: Optional[bool] = None

    # policy diagnostics (optional but very useful)
    pmv_center: Optional[float] = None
    pmv_band: Optional[float] = None
    met_used: Optional[float] = None
    clo_used: Optional[float] = None


class ComfortBandCalculator:
    """ISO 7730 PMV/PPD comfort-band calculator (Category-like band), policy-aware.

    Backward compatible:
      - If no policy/overrides are provided, behavior matches the previous implementation:
        met=self._met, clo=seasonal, band = PMV in [-0.5, +0.5], robust bounds using v_air_hi/v_air_lo.

    Policy-aware:
      - policy (ComfortPolicyDecision) can override:
          met, clo, pmv_center, pmv_band,
          v_air scaling/override.
    """

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
        self._pressure_pa = float(pressure_pa)

        self._clo_map: Dict[OperativeSeason, float] = {
            OperativeSeason.WINTER: float(clo_winter),
            OperativeSeason.SUMMER: float(clo_summer),
            OperativeSeason.SHOULDER: float(clo_shoulder),
        }

    # ------------------------- v_air model ---------------------------------

    @staticmethod
    def _norm_room(room: str) -> str:
        return (room or "").strip().lower()

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
        """
        if not isinstance(speed, int) or speed < 0 or speed > 5:
            raise ValueError(f"speed must be int in [0..5], got {speed}")

        r = self._norm_room(room)
        is_living = (r == "living") or ("soggiorno" in r) or ("salotto" in r)

        if is_living:
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

        v_hi = max(v_hi, v_best, v_lo)   # garantisce "hi >= best >= lo"
        
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
        pa = rh / 100.0 * self._svp_pa(ta)

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
            xn = (p5 + p4 * hc - p2 * (xf ** 4)) / (100.0 + p3 * hc)
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

    def _pmv_at_top(
        self,
        *,
        t_op_c: float,
        rh_pct: float,
        v_air: float,
        season: OperativeSeason,
        met_override: Optional[float] = None,
        clo_override: Optional[float] = None,
    ) -> float:
        """For band over T_op, use Ta=Tr=T_op (control-oriented approximation)."""
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
        t_min: float = 10.0,
        t_max: float = 35.0,
        max_iter: int = 60,
    ) -> float:
        """Find T where PMV(T)=target via bisection, expanding if needed."""
        lo = float(t_min)
        hi = float(t_max)

        f_lo = (
            self._pmv_at_top(
                t_op_c=lo,
                rh_pct=rh_pct,
                v_air=v_air,
                season=season,
                met_override=met_override,
                clo_override=clo_override,
            )
            - float(target_pmv)
        )
        f_hi = (
            self._pmv_at_top(
                t_op_c=hi,
                rh_pct=rh_pct,
                v_air=v_air,
                season=season,
                met_override=met_override,
                clo_override=clo_override,
            )
            - float(target_pmv)
        )

        expand_steps = 0
        while f_lo * f_hi > 0 and expand_steps < 8:
            lo = max(-10.0, lo - 5.0)
            hi = min(60.0, hi + 5.0)
            f_lo = (
                self._pmv_at_top(
                    t_op_c=lo,
                    rh_pct=rh_pct,
                    v_air=v_air,
                    season=season,
                    met_override=met_override,
                    clo_override=clo_override,
                )
                - float(target_pmv)
            )
            f_hi = (
                self._pmv_at_top(
                    t_op_c=hi,
                    rh_pct=rh_pct,
                    v_air=v_air,
                    season=season,
                    met_override=met_override,
                    clo_override=clo_override,
                )
                - float(target_pmv)
            )
            expand_steps += 1

        if f_lo * f_hi > 0:
            return lo if abs(f_lo) < abs(f_hi) else hi

        for _ in range(int(max_iter)):
            mid = (lo + hi) / 2.0
            f_mid = (
                self._pmv_at_top(
                    t_op_c=mid,
                    rh_pct=rh_pct,
                    v_air=v_air,
                    season=season,
                    met_override=met_override,
                    clo_override=clo_override,
                )
                - float(target_pmv)
            )
            if abs(f_mid) < 1e-4:
                return float(mid)
            if f_lo * f_mid <= 0:
                hi = mid
                f_hi = f_mid
            else:
                lo = mid
                f_lo = f_mid

        return float((lo + hi) / 2.0)

    def compute_comfort_band(
        self,
        *,
        speed: int,
        room: str,
        season: OperativeSeason | str,
        rh_pct: float,
        t_op_current: Optional[float] = None,
        # policy-aware knobs (all optional)
        policy: Optional[PolicyDecision] = None,
        met_override: Optional[float] = None,
        clo_override: Optional[float] = None,
        pmv_center: Optional[float] = None,
        pmv_band: Optional[float] = None,
    ) -> ComfortBandResult:
        """Return Cat-like comfort band over T_op plus current evaluation if provided.

        Backward compatible: if you pass no policy/overrides, defaults match legacy behavior.
        """
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
            speed=int(speed),
            room=str(room),
            season=season_value,
            v_air_best_scale=v_best_scale,
            v_air_hi_scale=v_hi_scale,
            v_air_lo_override=v_lo_ovr,
        )

        # target band around pmv_center
        pmv_lo = pmv_center_used - pmv_band_used
        pmv_hi = pmv_center_used + pmv_band_used

        # Robust band:
        # - lower bound uses v_hi (draft worst-case)
        # - upper bound uses v_lo (still air worst-case)
        t_op_min = self._bisect_temperature_for_pmv(
            target_pmv=float(pmv_lo),
            rh_pct=float(rh_pct),
            v_air=float(v_hi),
            season=season_value,
            met_override=met_used,
            clo_override=clo_used,
        )
        t_op_max = self._bisect_temperature_for_pmv(
            target_pmv=float(pmv_hi),
            rh_pct=float(rh_pct),
            v_air=float(v_lo),
            season=season_value,
            met_override=met_used,
            clo_override=clo_used,
        )

        if t_op_min > t_op_max:
            # caso patologico (input estremi / no bracket / policy errata): swap difensivo
            t_op_min, t_op_max = t_op_max, t_op_min

        res = ComfortBandResult(
            room=str(room),
            season=season_value,
            speed=int(speed),
            v_air_best=float(v_best),
            v_air_lo=float(v_lo),
            v_air_hi=float(v_hi),
            t_op_min=float(t_op_min),
            t_op_max=float(t_op_max),
            t_op=None,
            pmv=None,
            ppd=None,
            ok=None,
            pmv_center=float(pmv_center_used),
            pmv_band=float(pmv_band_used),
            met_used=float(met_used),
            clo_used=float(clo_used),
        )

        if t_op_current is not None:
            pmv, ppd = self.pmv_ppd(
                ta_c=float(t_op_current),
                tr_c=float(t_op_current),
                rh_pct=float(rh_pct),
                v_air=float(v_best),
                met=float(met_used),
                clo=float(clo_used),
                wme=float(self._wme),
            )
            ok = (float(t_op_min) <= float(t_op_current) <= float(t_op_max))
            res = replace(
                res,
                t_op=float(t_op_current),
                pmv=float(pmv),
                ppd=float(ppd),
                ok=bool(ok),
            )

        return res
