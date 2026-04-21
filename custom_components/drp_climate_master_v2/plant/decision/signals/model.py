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

    def __str__(self) -> str:
        return (
            "ZoneComfortCluster("
            f"heat_def_max_c={self.heat_def_max_c}, "
            f"cool_sur_max_c={self.cool_sur_max_c}, "
            f"heat_def_by_zone_c={self.heat_def_by_zone_c}, "
            f"cool_sur_by_zone_c={self.cool_sur_by_zone_c}, "
            f"heat_headroom_min_c={self.heat_headroom_min_c}, "
            f"cool_headroom_min_c={self.cool_headroom_min_c}, "
            f"heat_den_w={self.heat_den_w}, "
            f"heat_out_w={self.heat_out_w}, "
            f"heat_sum_wdef={self.heat_sum_wdef}, "
            f"heat_den_n={self.heat_den_n}, "
            f"heat_out_n={self.heat_out_n}, "
            f"heat_sum_ndef={self.heat_sum_ndef}, "
            f"cool_den_w={self.cool_den_w}, "
            f"cool_out_w={self.cool_out_w}, "
            f"cool_sum_wsur={self.cool_sum_wsur}, "
            f"cool_den_n={self.cool_den_n}, "
            f"cool_out_n={self.cool_out_n}, "
            f"cool_sum_nsur={self.cool_sum_nsur}, "
            f"dp_max_c={self.dp_max_c}, "
            f"dp_values={self.dp_values}"
            ")"
        )


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
class FreeVentCluster:
    """Segnali di fattibilità per la ventilazione free cooling/heating.

    Calcolati da DemandSignalsBuilder.FreeVentCluster — solo osservazione,
    nessuna logica decisionale.
    """

    free_cool_delta_c: Optional[float]
    """Differenza T_indoor_mean − T_outdoor [°C]. Positivo = esterno più freddo."""

    free_heat_delta_c: Optional[float]
    """Differenza T_outdoor − T_indoor_mean [°C]. Positivo = esterno più caldo."""

    free_cool_dp_ok: Optional[bool]
    """True se DP_esterno < DP_indoor_max − margine (aria esterna non aggiunge umidità)."""

    free_cool_feasible: bool
    """True se delta_T sufficiente AND dp_ok AND finestre chiuse."""

    free_heat_feasible: bool
    """True se delta_T sufficiente AND stagione != summer."""
