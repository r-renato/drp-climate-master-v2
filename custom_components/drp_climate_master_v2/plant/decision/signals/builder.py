from __future__ import annotations

from dataclasses import fields as dc_fields, is_dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from ..contracts import MetricBasis, PlantDemandSignals
from ....domain.enums import HVACOperatingProfile
from ....domain.models.plant import PlantSnapshot
from ....helpers.psychrometric import dew_point_celsius
from ....helpers.utils import as_float

from .clusters import DewPointCluster, VmcCluster, ZoneComfortCluster, ZoneDemandMetrics
from .stats import percentile_sorted


class PlannerLike(Protocol):
    cfg: Any

    def _infer_operative_bucket(self, snapshot: PlantSnapshot) -> Any: ...

    def _get_indoor_reference_temp_c(self, snapshot: PlantSnapshot) -> float: ...

    def _resolve_vmc_rh_target_pct(self, operative: Any, profile: HVACOperatingProfile) -> float: ...

    def _compute_vmc_dp_setpoint_c_from(self, t_ref_c: float, rh_target_pct: float) -> float: ...

    def _vmc_need_dehumidification(
        self,
        dp_dehum_c: Optional[float],
        dp_on_thr_c: float,
        dp_off_thr_c: float,
    ) -> bool: ...

    def _vmc_allow_heat_boost(
        self,
        snapshot: PlantSnapshot,
        heat_def_max_c: float,
        heat_def_wmean_c: float,
        heat_cov: float,
    ) -> bool: ...

    def _vmc_allow_cool_boost(
        self,
        snapshot: PlantSnapshot,
        cool_sur_max_c: float,
        cool_sur_wmean_c: float,
        cool_cov: float,
    ) -> bool: ...


class DemandSignalsBuilder:
    """Compute PlantDemandSignals via clustered methods (zone comfort / DP / VMC)."""

    def __init__(
        self,
        planner: PlannerLike,
        *,
        zone_weight_fn: Callable[[Any], float],
        percentile_sorted_fn: Callable[[List[float], float], float] = percentile_sorted,
    ) -> None:
        self._planner = planner
        self.cfg = planner.cfg
        self._zone_weight_fn = zone_weight_fn
        self._percentile_sorted_fn = percentile_sorted_fn

    def build(self, *, snapshot: PlantSnapshot) -> PlantDemandSignals:
        zc = self._compute_zone_comfort_cluster(snapshot)
        zm = self._compute_zone_demand_metrics(zc)
        dp = self._compute_dew_point_cluster(snapshot, zc)
        vmc = self._compute_vmc_cluster(snapshot, zc, zm, dp)

        payload: Dict[str, Any] = dict(
            # zone comfort
            heat_def_max_c=zc.heat_def_max_c,
            cool_sur_max_c=zc.cool_sur_max_c,
            heat_def_by_zone_c=zc.heat_def_by_zone_c,
            cool_sur_by_zone_c=zc.cool_sur_by_zone_c,
            heat_headroom_min_c=zc.heat_headroom_min_c,
            cool_headroom_min_c=zc.cool_headroom_min_c,
            # zone metrics
            heat_def_wmean_c=zm.heat_def_wmean_c,
            cool_sur_wmean_c=zm.cool_sur_wmean_c,
            heat_cov=zm.heat_cov,
            cool_cov=zm.cool_cov,
            heat_metric_basis=zm.heat_metric_basis,
            cool_metric_basis=zm.cool_metric_basis,
            # dew point
            dp_max_c=dp.dp_max_c,
            dp_dehum_c=dp.dp_dehum_c,
            outdoor_dp_c=dp.outdoor_dp_c,
            # vmc
            vmc_dehum_feasible=vmc.vmc_dehum_feasible,
            vmc_req_heating=vmc.vmc_req_heating,
            vmc_req_cooling=vmc.vmc_req_cooling,
            vmc_req_dehumidif=vmc.vmc_req_dehumidif,
            vmc_req_water=vmc.vmc_req_water,
            vmc_dp_sp_c=vmc.vmc_dp_sp_c,
            vmc_dehum_on_thr_c=vmc.vmc_dehum_on_thr_c,
            vmc_dehum_off_thr_c=vmc.vmc_dehum_off_thr_c,
        )

        return self._safe_signals_init(payload)

    # -----------------
    # Cluster builders
    # -----------------

    def _compute_zone_comfort_cluster(self, snapshot: PlantSnapshot) -> ZoneComfortCluster:
        heat_def_max = 0.0
        cool_sur_max = 0.0
        heat_def_by_zone: Dict[str, float] = {}
        cool_sur_by_zone: Dict[str, float] = {}
        heat_headroom_min_c: Optional[float] = None
        cool_headroom_min_c: Optional[float] = None

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
        dp_values: List[float] = []

        for zone_key, z in (getattr(snapshot, "indoor_zones", None) or {}).items():
            t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
            if t_meas is None:
                t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))

            band = getattr(z, "confort_band", None)
            t_min = as_float(getattr(band, "t_op_min", None))
            t_max = as_float(getattr(band, "t_op_max", None))

            # headroom
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

                w = float(self._zone_weight_fn(z) or 0.0)

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

                w = float(self._zone_weight_fn(z) or 0.0)

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

        return ZoneComfortCluster(
            heat_def_max_c=heat_def_max,
            cool_sur_max_c=cool_sur_max,
            heat_def_by_zone_c=heat_def_by_zone,
            cool_sur_by_zone_c=cool_sur_by_zone,
            heat_headroom_min_c=heat_headroom_min_c,
            cool_headroom_min_c=cool_headroom_min_c,
            heat_den_w=heat_den_w,
            heat_out_w=heat_out_w,
            heat_sum_wdef=heat_sum_wdef,
            heat_den_n=heat_den_n,
            heat_out_n=heat_out_n,
            heat_sum_ndef=heat_sum_ndef,
            cool_den_w=cool_den_w,
            cool_out_w=cool_out_w,
            cool_sum_wsur=cool_sum_wsur,
            cool_den_n=cool_den_n,
            cool_out_n=cool_out_n,
            cool_sum_nsur=cool_sum_nsur,
            dp_max_c=dp_max,
            dp_values=dp_values,
        )

    def _compute_zone_demand_metrics(self, zc: ZoneComfortCluster) -> ZoneDemandMetrics:
        # heating
        if zc.heat_den_w > 0.0:
            heat_def_wmean = zc.heat_sum_wdef / zc.heat_den_w
            heat_cov = zc.heat_out_w / zc.heat_den_w
            heat_metric_basis = MetricBasis.WEIGHTED
        elif zc.heat_den_n > 0:
            heat_def_wmean = zc.heat_sum_ndef / float(zc.heat_den_n)
            heat_cov = float(zc.heat_out_n) / float(zc.heat_den_n)
            heat_metric_basis = MetricBasis.COUNT
        else:
            heat_def_wmean = 0.0
            heat_cov = 0.0
            heat_metric_basis = MetricBasis.NONE

        # cooling
        if zc.cool_den_w > 0.0:
            cool_sur_wmean = zc.cool_sum_wsur / zc.cool_den_w
            cool_cov = zc.cool_out_w / zc.cool_den_w
            cool_metric_basis = MetricBasis.WEIGHTED
        elif zc.cool_den_n > 0:
            cool_sur_wmean = zc.cool_sum_nsur / float(zc.cool_den_n)
            cool_cov = float(zc.cool_out_n) / float(zc.cool_den_n)
            cool_metric_basis = MetricBasis.COUNT
        else:
            cool_sur_wmean = 0.0
            cool_cov = 0.0
            cool_metric_basis = MetricBasis.NONE

        return ZoneDemandMetrics(
            heat_def_wmean_c=float(heat_def_wmean),
            heat_cov=float(heat_cov),
            heat_metric_basis=heat_metric_basis,
            cool_sur_wmean_c=float(cool_sur_wmean),
            cool_cov=float(cool_cov),
            cool_metric_basis=cool_metric_basis,
        )

    def _compute_dew_point_cluster(self, snapshot: PlantSnapshot, zc: ZoneComfortCluster) -> DewPointCluster:
        # Outdoor dew point (best effort)
        outdoor_dp_c = as_float(getattr(getattr(snapshot, "global_outdoor_dew_point", None), "value", None))
        if outdoor_dp_c is None:
            t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
            rh_out = as_float(getattr(getattr(snapshot, "global_outdoor_humidity", None), "value", None))
            if t_out is not None and rh_out is not None:
                try:
                    outdoor_dp_c = float(dew_point_celsius(float(t_out), float(rh_out)))
                except Exception:
                    outdoor_dp_c = None

        # Robust DP for dehumidification control
        dp_dehum_c: Optional[float] = None
        if zc.dp_values:
            xs = sorted(zc.dp_values)
            p = float(getattr(self.cfg, "vmc_dp_control_percentile", 1.0))
            try:
                dp_dehum_c = float(self._percentile_sorted_fn(xs, p))
            except Exception:
                dp_dehum_c = float(xs[-1])

        if dp_dehum_c is None:
            dp_dehum_c = zc.dp_max_c

        return DewPointCluster(
            dp_max_c=zc.dp_max_c,
            dp_dehum_c=dp_dehum_c,
            outdoor_dp_c=float(outdoor_dp_c) if outdoor_dp_c is not None else None,
        )

    def _compute_vmc_cluster(
        self,
        snapshot: PlantSnapshot,
        zc: ZoneComfortCluster,
        zm: ZoneDemandMetrics,
        dp: DewPointCluster,
    ) -> VmcCluster:
        vmc = getattr(snapshot, "vmc", None)
        vmc_raw_req_dehum = bool(getattr(vmc, "request_dehumidification", False)) if vmc else False

        # dp_sp coherent with profile/season
        preset_raw = getattr(snapshot, "climate_preset_mode", None)
        profile = HVACOperatingProfile.from_value(preset_raw, default=HVACOperatingProfile.COMFORT) or HVACOperatingProfile.COMFORT
        operative = self._planner._infer_operative_bucket(snapshot)

        t_ref_c = float(self._planner._get_indoor_reference_temp_c(snapshot))
        rh_target_pct = float(self._planner._resolve_vmc_rh_target_pct(operative, profile))
        vmc_dp_sp_c = float(self._planner._compute_vmc_dp_setpoint_c_from(t_ref_c, rh_target_pct))

        ddp = float(getattr(self.cfg, "vmc_setpoint_ddp_c", 0.0))
        hyst = float(getattr(self.cfg, "vmc_dehum_hysteresis_c", 0.0))
        vmc_dehum_on_thr_c = float(vmc_dp_sp_c) + ddp
        vmc_dehum_off_thr_c = float(vmc_dehum_on_thr_c) - max(0.0, hyst)

        # Feasibility
        vmc_dehum_feasible: Optional[bool] = None
        if bool(getattr(self.cfg, "vmc_water_on_for_dehumid", False)):
            vmc_dehum_feasible = True
        elif dp.outdoor_dp_c is not None and dp.dp_dehum_c is not None:
            headroom = float(getattr(self.cfg, "vmc_dehum_outdoor_dp_headroom_c", 0.0))
            vmc_dehum_feasible = float(dp.outdoor_dp_c) <= (float(dp.dp_dehum_c) - headroom)

        vmc_need_dehum = self._planner._vmc_need_dehumidification(dp.dp_dehum_c, vmc_dehum_on_thr_c, vmc_dehum_off_thr_c)

        vmc_boost_heat = self._planner._vmc_allow_heat_boost(
            snapshot,
            float(zc.heat_def_max_c),
            float(zm.heat_def_wmean_c),
            float(zm.heat_cov),
        )
        vmc_boost_cool = self._planner._vmc_allow_cool_boost(
            snapshot,
            float(zc.cool_sur_max_c),
            float(zm.cool_sur_wmean_c),
            float(zm.cool_cov),
        )

        vmc_req_heat = bool(vmc_boost_heat)
        vmc_req_cool = bool(vmc_boost_cool)
        vmc_req_dehum = bool(vmc_need_dehum or vmc_raw_req_dehum) and (vmc_dehum_feasible is not False)
        vmc_req_water = (
            vmc_req_heat
            or vmc_req_cool
            or (vmc_req_dehum and bool(getattr(self.cfg, "vmc_water_on_for_dehumid", False)))
        )

        return VmcCluster(
            vmc_dp_sp_c=float(vmc_dp_sp_c) if vmc_dp_sp_c is not None else None,
            vmc_dehum_on_thr_c=float(vmc_dehum_on_thr_c) if vmc_dehum_on_thr_c is not None else None,
            vmc_dehum_off_thr_c=float(vmc_dehum_off_thr_c) if vmc_dehum_off_thr_c is not None else None,
            vmc_dehum_feasible=vmc_dehum_feasible,
            vmc_req_heating=bool(vmc_req_heat),
            vmc_req_cooling=bool(vmc_req_cool),
            vmc_req_dehumidif=bool(vmc_req_dehum),
            vmc_req_water=bool(vmc_req_water),
        )

    # -----------------
    # Assembly helper
    # -----------------

    def _safe_signals_init(self, payload: Dict[str, Any]) -> PlantDemandSignals:
        kwargs = dict(payload)
        try:
            if is_dataclass(PlantDemandSignals):
                allowed = {f.name for f in dc_fields(PlantDemandSignals)}
                kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        except Exception:
            pass

        return PlantDemandSignals(**kwargs)  # type: ignore[arg-type]


# Backward-compat alias
PlantDemandSignalsBuilder = DemandSignalsBuilder
