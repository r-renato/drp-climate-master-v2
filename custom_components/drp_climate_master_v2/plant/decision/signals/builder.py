from __future__ import annotations

from dataclasses import fields as dc_fields, is_dataclass
from typing import Any, Callable, Dict, List, Optional

from ..contracts import MetricBasis, PlantDemandSignals
from ....domain.models.plant import PlantSnapshot
from ....helpers.psychrometric import dew_point_celsius
from ....helpers.utils import as_float

from ..config import PlantPlannerConfig
from .clusters import DewPointCluster, ZoneComfortCluster, ZoneDemandMetrics
from .stats import percentile_sorted


class DemandSignalsBuilder:
    """Compute PlantDemandSignals via clustered methods (zone comfort / DP).

    NOTE
    ----
    This builder is intentionally **observation-only**:
    - It aggregates zone comfort deltas and dew-point signals.
    - It does NOT compute VMC policy (requests, DP setpoints, hysteresis), which lives
      in the dedicated VMC domain module.
    """

    def __init__(
        self,
        cfg: PlantPlannerConfig,
        *,
        zone_weight_fn: Callable[[Any], float],
        percentile_sorted_fn: Callable[[List[float], float], float] = percentile_sorted,
    ) -> None:
        self.cfg = cfg
        self._zone_weight_fn = zone_weight_fn
        self._percentile_sorted_fn = percentile_sorted_fn

    def build(self, *, snapshot: PlantSnapshot) -> PlantDemandSignals:
        zc = self._compute_zone_comfort_cluster(snapshot)
        zm = self._compute_zone_demand_metrics(zc)
        dp = self._compute_dew_point_cluster(snapshot, zc)

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
            p = float(getattr(self.cfg.vmc.dehum, "dp_control_percentile", 1.0))
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
