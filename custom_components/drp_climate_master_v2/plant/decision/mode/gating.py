from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....domain.enums import HVACOperatingProfile

from ..config import PlantPlannerConfig
from ..contracts import PlantDemandSignals
from ..zone.contracts import ZonesDecision


@dataclass(slots=True, frozen=True)
class GatingResult:
    """Computed profile-aware gating flags used by mode resolution."""

    ctrl_aggr: float
    heat_thr_c: float
    cool_thr_c: float
    quorum_cov_req: float

    heat_override: Optional[bool]
    heat_quorum_ok: Optional[bool]
    heat_mean_ok: Optional[bool]

    cool_override: Optional[bool]
    cool_quorum_ok: Optional[bool]
    cool_mean_ok: Optional[bool]

    heat_sensible: bool
    cool_sensible: bool

    zones_any_heat: bool
    zones_full_on_pct: Optional[float]
    zones_preheat_ok: bool

    vmc_req_heat: bool
    vmc_req_cool: bool
    vmc_req_dehum: bool

    any_heat: bool
    any_cool: bool
    any_cool_or_dehum: bool


def compute_gating(
    *,
    cfg: PlantPlannerConfig,
    demand: PlantDemandSignals,
    profile: HVACOperatingProfile,
    zones_decision: Optional[ZonesDecision],
) -> GatingResult:
    """Compute all profile-aware thresholds and gating flags.

    This function is intentionally *side-effect free*.
    """

    heat_def = float(demand.heat_def_max_c)
    cool_sur = float(demand.cool_sur_max_c)
    heat_cov = float(demand.heat_cov)
    cool_cov = float(demand.cool_cov)
    heat_def_wmean = float(demand.heat_def_wmean_c)
    cool_sur_wmean = float(demand.cool_sur_wmean_c)

    ctrl_aggr = float(
        {
            HVACOperatingProfile.COMFORT: 1.00,
            HVACOperatingProfile.BOOST: 1.35,
            HVACOperatingProfile.ECO: 0.85,
            HVACOperatingProfile.SLEEP: 0.75,
            HVACOperatingProfile.AWAY: 0.50,
            HVACOperatingProfile.VACATION: 0.50,
        }.get(profile, 1.0)
    )
    ctrl_eff = max(0.2, ctrl_aggr)

    heat_thr = float(cfg.comfort.heat_on_deficit_c) / ctrl_eff
    cool_thr = float(cfg.comfort.cool_on_surplus_c) / ctrl_eff

    quorum = float(cfg.gating.quorum_cov(profile))

    vmc_req_heat = bool(demand.vmc_req_heating)
    vmc_req_cool = bool(demand.vmc_req_cooling)
    vmc_req_dehum = bool(demand.vmc_req_dehumidif)

    # Sensible gating (profile-aware)
    heat_override = heat_quorum_ok = heat_mean_ok = None
    cool_override = cool_quorum_ok = cool_mean_ok = None

    if profile in (HVACOperatingProfile.COMFORT, HVACOperatingProfile.BOOST):
        heat_sensible = heat_def >= heat_thr
        cool_sensible = cool_sur >= cool_thr
    else:
        heat_override = heat_def >= heat_thr * float(cfg.gating.demand_override_factor)
        heat_quorum_ok = heat_cov >= quorum
        heat_mean_ok = heat_def_wmean >= heat_thr * float(cfg.gating.demand_mean_factor)
        heat_sensible = bool(heat_override) or ((heat_def >= heat_thr) and (bool(heat_quorum_ok) or bool(heat_mean_ok)))

        cool_override = cool_sur >= cool_thr * float(cfg.gating.demand_override_factor)
        cool_quorum_ok = cool_cov >= quorum
        cool_mean_ok = cool_sur_wmean >= cool_thr * float(cfg.gating.demand_mean_factor)
        cool_sensible = bool(cool_override) or ((cool_sur >= cool_thr) and (bool(cool_quorum_ok) or bool(cool_mean_ok)))

    zones_any_heat = bool(zones_decision.any_heat_demand) if zones_decision else False
    zones_full_on_pct = zones_decision.meta.get("mpc_full_on_pct") if zones_decision else None

    zones_preheat_ok = False
    if zones_any_heat and getattr(demand, "heat_headroom_min_c", None) is not None:
        zones_preheat_ok = demand.heat_headroom_min_c <= float(getattr(cfg.zones_mpc, "preheat_headroom_c", 0.4))
    if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
        zones_preheat_ok = False

    any_heat = bool(heat_sensible) or vmc_req_heat or (zones_any_heat and zones_preheat_ok)
    any_cool = bool(cool_sensible) or vmc_req_cool

    any_cool_or_dehum = any_cool or vmc_req_dehum

    return GatingResult(
        ctrl_aggr=ctrl_aggr,
        heat_thr_c=heat_thr,
        cool_thr_c=cool_thr,
        quorum_cov_req=quorum,
        heat_override=heat_override,
        heat_quorum_ok=heat_quorum_ok,
        heat_mean_ok=heat_mean_ok,
        cool_override=cool_override,
        cool_quorum_ok=cool_quorum_ok,
        cool_mean_ok=cool_mean_ok,
        heat_sensible=bool(heat_sensible),
        cool_sensible=bool(cool_sensible),
        zones_any_heat=zones_any_heat,
        zones_full_on_pct=float(zones_full_on_pct) if zones_full_on_pct is not None else None,
        zones_preheat_ok=bool(zones_preheat_ok),
        vmc_req_heat=vmc_req_heat,
        vmc_req_cool=vmc_req_cool,
        vmc_req_dehum=vmc_req_dehum,
        any_heat=bool(any_heat),
        any_cool=bool(any_cool),
        any_cool_or_dehum=bool(any_cool_or_dehum),
    )
