from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ...helpers.utils import as_float
from ...helpers.psychrometric import dew_point_celsius
from ...domain.models.plant import PlantSnapshot
from ...domain.enums import HVACOperatingProfile

from .config import PlantPlannerConfig
from .contracts import MetricBasis, PlantDemandSignals


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _percentile_sorted(xs: list[float], q: float) -> float | None:
    """Percentile robusto senza numpy. xs deve essere NON vuota e già ordinata."""
    if not xs:
        return None
    q = max(0.0, min(1.0, float(q)))
    if len(xs) == 1:
        return float(xs[0])
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


@dataclass(slots=True)
class _PlantDemandSignalsBuilderCore:
    """Costruisce PlantDemandSignals da PlantSnapshot.

    Obiettivo: isolare la logica di aggregazione/derivazione segnali (zone + DP + VMC)
    lasciando al Planner la sola orchestrazione e le decisioni di regime.

    Nota: alcune policy/strategie (es. boost VMC, hysteresis) rimangono esterne e vengono
    iniettate come callable per non accoppiare il builder a stato e/o scelte di controllo.
    """

    cfg: PlantPlannerConfig

    # --- injection points (kept in planner for state/policy boundaries)
    zone_weight_fn: Callable[[Any], float]
    infer_operative_bucket_fn: Callable[[PlantSnapshot], str]
    get_indoor_reference_temp_fn: Callable[[PlantSnapshot], float]
    resolve_vmc_rh_target_fn: Callable[[str, HVACOperatingProfile], float]
    compute_vmc_dp_setpoint_from_fn: Callable[[float, float], float]

    vmc_need_dehumidification_fn: Callable[[Optional[float], float, float], bool]
    vmc_allow_heat_boost_fn: Callable[[PlantSnapshot, float, float, float], bool]
    vmc_allow_cool_boost_fn: Callable[[PlantSnapshot, float, float, float], bool]

    def build(self, *, snapshot: PlantSnapshot) -> PlantDemandSignals:
        # -------------
        # 1) ZONE CLUSTER
        # -------------
        heat_def_max = 0.0
        cool_sur_max = 0.0
        heat_def_by_zone: Dict[str, float] = {}
        cool_sur_by_zone: Dict[str, float] = {}
        heat_headroom_min_c: Optional[float] = None
        cool_headroom_min_c: Optional[float] = None

        # Weighted/quorum metrics
        heat_den_w = 0.0
        heat_out_w = 0.0
        heat_sum_wdef = 0.0
        heat_den_n = 0
        heat_out_n = 0
        heat_sum_ndef = 0.0

        cool_den_w = 0.0
        cool_out_w = 0.0
        cool_sum_wsur = 0.0
        cool_den_n = 0
        cool_out_n = 0
        cool_sum_nsur = 0.0

        dp_max: Optional[float] = None
        dp_values: list[float] = []

        for zone_key, z in (snapshot.indoor_zones or {}).items():
            t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
            if t_meas is None:
                t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))

            band = getattr(z, "confort_band", None)
            t_min = as_float(getattr(band, "t_op_min", None))
            t_max = as_float(getattr(band, "t_op_max", None))

            # headroom (for MPC preheat gating)
            if t_meas is not None and t_min is not None:
                hh = float(t_meas) - float(t_min)
                heat_headroom_min_c = hh if heat_headroom_min_c is None else min(heat_headroom_min_c, hh)
            if t_meas is not None and t_max is not None:
                ch = float(t_max) - float(t_meas)
                cool_headroom_min_c = ch if cool_headroom_min_c is None else min(cool_headroom_min_c, ch)

            # heating deficit
            if t_meas is not None and t_min is not None:
                d = max(0.0, float(t_min) - float(t_meas))
                heat_def_by_zone[zone_key] = d
                heat_def_max = max(heat_def_max, d)

                w = float(self.zone_weight_fn(z) or 0.0)

                heat_den_n += 1
                heat_sum_ndef += d
                if d > 0.0:
                    heat_out_n += 1

                if w > 0.0:
                    heat_den_w += w
                    heat_sum_wdef += w * d
                    if d > 0.0:
                        heat_out_w += w

            # cooling surplus
            if t_meas is not None and t_max is not None:
                d = max(0.0, float(t_meas) - float(t_max))
                cool_sur_by_zone[zone_key] = d
                cool_sur_max = max(cool_sur_max, d)

                w = float(self.zone_weight_fn(z) or 0.0)

                cool_den_n += 1
                cool_sum_nsur += d
                if d > 0.0:
                    cool_out_n += 1

                if w > 0.0:
                    cool_den_w += w
                    cool_sum_wsur += w * d
                    if d > 0.0:
                        cool_out_w += w

            # dew point
            dp = as_float(getattr(getattr(z, "dew_point", None), "value", None))
            if dp is not None:
                dp_f = float(dp)
                dp_values.append(dp_f)
                dp_max = dp_f if dp_max is None else max(dp_max, dp_f)

        # --------------------------------------
        # 2) MULTI-ZONE METRICS (weighted/cnt)
        # --------------------------------------
        if heat_den_w > 0.0:
            heat_def_wmean = heat_sum_wdef / heat_den_w
            heat_cov = heat_out_w / heat_den_w
            heat_metric_basis = MetricBasis.WEIGHTED
        elif heat_den_n > 0:
            heat_def_wmean = heat_sum_ndef / float(heat_den_n)
            heat_cov = float(heat_out_n) / float(heat_den_n)
            heat_metric_basis = MetricBasis.COUNT
        else:
            heat_def_wmean = 0.0
            heat_cov = 0.0
            heat_metric_basis = MetricBasis.NONE

        if cool_den_w > 0.0:
            cool_sur_wmean = cool_sum_wsur / cool_den_w
            cool_cov = cool_out_w / cool_den_w
            cool_metric_basis = MetricBasis.WEIGHTED
        elif cool_den_n > 0:
            cool_sur_wmean = cool_sum_nsur / float(cool_den_n)
            cool_cov = float(cool_out_n) / float(cool_den_n)
            cool_metric_basis = MetricBasis.COUNT
        else:
            cool_sur_wmean = 0.0
            cool_cov = 0.0
            cool_metric_basis = MetricBasis.NONE

        # --------------------------------------
        # 3) OUTDOOR DP (best effort)
        # --------------------------------------
        outdoor_dp_c = as_float(getattr(getattr(snapshot, "global_outdoor_dew_point", None), "value", None))
        if outdoor_dp_c is None:
            t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
            rh_out = as_float(getattr(getattr(snapshot, "global_outdoor_humidity", None), "value", None))
            if t_out is not None and rh_out is not None:
                try:
                    outdoor_dp_c = float(dew_point_celsius(t_out, rh_out))
                except Exception:
                    outdoor_dp_c = None

        # --------------------------------------
        # 4) DP CLUSTER for dehum control
        # --------------------------------------
        dp_dehum_c: Optional[float] = None
        if dp_values:
            dp_values.sort()
            q = float(getattr(self.cfg, "vmc_dp_control_percentile", 1.0))
            dp_dehum_c = _percentile_sorted(dp_values, q)
        if dp_dehum_c is None:
            dp_dehum_c = dp_max

        # --------------------------------------
        # 5) VMC CLUSTER (thresholds + requests)
        # --------------------------------------
        vmc = snapshot.vmc
        vmc_raw_req_dehum = bool(getattr(vmc, "request_dehumidification", False)) if vmc else False

        preset_raw = getattr(snapshot, "climate_preset_mode", None)
        profile = HVACOperatingProfile.from_value(preset_raw, default=HVACOperatingProfile.COMFORT) or HVACOperatingProfile.COMFORT
        operative = self.infer_operative_bucket_fn(snapshot)

        t_ref_c = float(self.get_indoor_reference_temp_fn(snapshot))
        rh_target_pct = float(self.resolve_vmc_rh_target_fn(operative, profile))

        vmc_dp_sp_c = float(self.compute_vmc_dp_setpoint_from_fn(t_ref_c, rh_target_pct))
        ddp = float(self.cfg.vmc_setpoint_ddp_c)
        hyst = float(getattr(self.cfg, "vmc_dehum_hysteresis_c", 0.0))

        vmc_dehum_on_thr_c = float(vmc_dp_sp_c) + ddp
        vmc_dehum_off_thr_c = float(vmc_dehum_on_thr_c) - max(0.0, hyst)

        # Feasibility
        vmc_dehum_feasible: Optional[bool] = None
        if bool(self.cfg.vmc_water_on_for_dehumid):
            vmc_dehum_feasible = True
        elif outdoor_dp_c is not None and dp_dehum_c is not None:
            headroom = float(getattr(self.cfg, "vmc_dehum_outdoor_dp_headroom_c", 0.0))
            vmc_dehum_feasible = float(outdoor_dp_c) <= (float(dp_dehum_c) - headroom)

        # Hysteresis (stateful) and boosts (policy)
        vmc_need_dehum = self.vmc_need_dehumidification_fn(dp_dehum_c, vmc_dehum_on_thr_c, vmc_dehum_off_thr_c)

        vmc_boost_heat = self.vmc_allow_heat_boost_fn(snapshot, float(heat_def_max), float(heat_def_wmean), float(heat_cov))
        vmc_boost_cool = self.vmc_allow_cool_boost_fn(snapshot, float(cool_sur_max), float(cool_sur_wmean), float(cool_cov))

        vmc_req_heat = bool(vmc_boost_heat)
        vmc_req_cool = bool(vmc_boost_cool)
        vmc_req_dehum = bool(vmc_need_dehum or vmc_raw_req_dehum) and (vmc_dehum_feasible is not False)

        vmc_req_water = (
            vmc_req_heat
            or vmc_req_cool
            or (vmc_req_dehum and bool(self.cfg.vmc_water_on_for_dehumid))
        )

        return PlantDemandSignals(
            heat_def_max_c=float(heat_def_max),
            cool_sur_max_c=float(cool_sur_max),
            heat_def_by_zone_c=heat_def_by_zone,
            cool_sur_by_zone_c=cool_sur_by_zone,
            heat_headroom_min_c=heat_headroom_min_c,
            cool_headroom_min_c=cool_headroom_min_c,
            outdoor_dp_c=float(outdoor_dp_c) if outdoor_dp_c is not None else None,
            vmc_dehum_feasible=vmc_dehum_feasible,
            heat_def_wmean_c=float(heat_def_wmean),
            cool_sur_wmean_c=float(cool_sur_wmean),
            heat_cov=float(heat_cov),
            cool_cov=float(cool_cov),
            heat_metric_basis=heat_metric_basis,
            cool_metric_basis=cool_metric_basis,
            dp_max_c=float(dp_max) if dp_max is not None else None,
            dp_dehum_c=float(dp_dehum_c) if dp_dehum_c is not None else None,
            vmc_req_heating=bool(vmc_req_heat),
            vmc_req_cooling=bool(vmc_req_cool),
            vmc_req_dehumidif=bool(vmc_req_dehum),
            vmc_req_water=bool(vmc_req_water),
            vmc_dp_sp_c=float(vmc_dp_sp_c),
            vmc_dehum_on_thr_c=float(vmc_dehum_on_thr_c),
            vmc_dehum_off_thr_c=float(vmc_dehum_off_thr_c),
        )


class DemandSignalsBuilder:
    """Compute PlantDemandSignals via dedicated cluster methods."""

    def __init__(self, planner: Any, *, zone_weight_fn: Callable[[Any], float]) -> None:
        self._core = _PlantDemandSignalsBuilderCore(
            cfg=planner.cfg,
            zone_weight_fn=zone_weight_fn,
            infer_operative_bucket_fn=planner._infer_operative_bucket,
            get_indoor_reference_temp_fn=planner._get_indoor_reference_temp_c,
            resolve_vmc_rh_target_fn=planner._resolve_vmc_rh_target_pct,
            compute_vmc_dp_setpoint_from_fn=planner._compute_vmc_dp_setpoint_c_from,
            vmc_need_dehumidification_fn=planner._vmc_need_dehumidification,
            vmc_allow_heat_boost_fn=planner._vmc_allow_heat_boost,
            vmc_allow_cool_boost_fn=planner._vmc_allow_cool_boost,
        )

    def build(self, *, snapshot: PlantSnapshot) -> PlantDemandSignals:
        return self._core.build(snapshot=snapshot)


# Backward-compat alias (in case other modules still import the old name)
PlantDemandSignalsBuilder = DemandSignalsBuilder
