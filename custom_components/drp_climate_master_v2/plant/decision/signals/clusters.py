from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..contracts import MetricBasis


@dataclass(slots=True)
class ZoneComfortCluster:
    heat_def_max_c: float
    cool_sur_max_c: float
    heat_def_by_zone_c: Dict[str, float]
    cool_sur_by_zone_c: Dict[str, float]

    # headroom (MPC preheat gating)
    heat_headroom_min_c: Optional[float]
    cool_headroom_min_c: Optional[float]

    # Weighted/count accumulators
    heat_den_w: float
    heat_out_w: float
    heat_sum_wdef: float
    heat_den_n: int
    heat_out_n: int
    heat_sum_ndef: float

    cool_den_w: float
    cool_out_w: float
    cool_sum_wsur: float
    cool_den_n: int
    cool_out_n: int
    cool_sum_nsur: float

    # Dew-point raw
    dp_max_c: Optional[float]
    dp_values: List[float]


@dataclass(slots=True)
class ZoneDemandMetrics:
    heat_def_wmean_c: float
    heat_cov: float
    heat_metric_basis: MetricBasis

    cool_sur_wmean_c: float
    cool_cov: float
    cool_metric_basis: MetricBasis


@dataclass(slots=True)
class DewPointCluster:
    dp_max_c: Optional[float]
    dp_dehum_c: Optional[float]
    outdoor_dp_c: Optional[float]


@dataclass(slots=True)
class VmcCluster:
    vmc_dp_sp_c: Optional[float]
    vmc_dehum_on_thr_c: Optional[float]
    vmc_dehum_off_thr_c: Optional[float]
    vmc_dehum_feasible: Optional[bool]

    vmc_req_heating: bool
    vmc_req_cooling: bool
    vmc_req_dehumidif: bool
    vmc_req_water: bool
