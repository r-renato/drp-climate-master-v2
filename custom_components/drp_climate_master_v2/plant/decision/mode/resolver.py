from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from homeassistant.components.climate.const import HVACMode

from ....helpers.utils import as_float
from ....domain.enums import HVACOperatingProfile
from ....domain.models.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDemandSignals, PlantMode
from ..zone.model import ZonesDecision

from .gating import compute_gating


@dataclass(slots=True)
class ModeResolver:
    """Resolve the global PlantMode (regime) from demand + user intent + seasons.

    Responsibility
    - Decide the high-level regime ONLY (no actuator commands).
    - Populate diagnostic fields in PlantDemandSignals for observability.

    Notes
    - This module deliberately remains HA-aware (HVACMode mapping), because it is
      a thin translation of user intent.
    """

    cfg: PlantPlannerConfig

    def decide(
        self,
        *,
        snapshot: PlantSnapshot,
        demand: PlantDemandSignals,
        zones_decision: Optional[ZonesDecision] = None,
    ) -> PlantMode:
        cfg = self.cfg

        heat_def = float(demand.heat_def_max_c)
        cool_sur = float(demand.cool_sur_max_c)

        # --------------------
        # 0) User intent (HA Climate)
        # --------------------
        hvac_mode_raw = getattr(snapshot, "climate_hvac_mode", None)
        hvac_mode_val = getattr(hvac_mode_raw, "value", hvac_mode_raw)
        hvac_mode_s = str(hvac_mode_val).strip().lower() if hvac_mode_val is not None else "auto"

        preset_raw = getattr(snapshot, "climate_preset_mode", None)
        profile = (
            HVACOperatingProfile.from_value(preset_raw, default=HVACOperatingProfile.COMFORT)
            or HVACOperatingProfile.COMFORT
        )

        # Expose to signals for observability (ends up in PlantDecision.signals)
        demand.user_hvac_mode = hvac_mode_s
        demand.user_profile = profile.value

        # Absolute override
        if hvac_mode_s == HVACMode.OFF.value:
            demand.user_forced_off = True
            return PlantMode.OFF
        demand.user_forced_off = False

        # --------------------
        # 1) Profile-aware gating (thresholds + booleans)
        # --------------------
        g = compute_gating(cfg=cfg, demand=demand, profile=profile, zones_decision=zones_decision)

        demand.ctrl_aggr = g.ctrl_aggr
        demand.heat_on_thr_c = g.heat_thr_c
        demand.cool_on_thr_c = g.cool_thr_c
        demand.quorum_cov_req = g.quorum_cov_req

        demand.heat_override = g.heat_override
        demand.heat_quorum_ok = g.heat_quorum_ok
        demand.heat_mean_ok = g.heat_mean_ok

        demand.cool_override = g.cool_override
        demand.cool_quorum_ok = g.cool_quorum_ok
        demand.cool_mean_ok = g.cool_mean_ok

        demand.zones_any_heat_demand = g.zones_any_heat
        demand.zones_full_on_pct = g.zones_full_on_pct
        demand.zones_mpc_heat_preheat_ok = g.zones_preheat_ok

        # Extra MPC-lite KPIs (for observability and plant-side tuning)
        demand.zones_duty_avg_pct = g.zones_duty_avg_pct
        demand.zones_on_now_pct = g.zones_on_now_pct
        demand.zones_first_on_step = g.zones_first_on_step

        demand.any_heat = g.any_heat
        demand.any_cool = g.any_cool
        demand.any_dehum = g.vmc_req_dehum
        demand.heat_sensible = g.heat_sensible
        demand.cool_sensible = g.cool_sensible

        # --------------------
        # 2) Season gating / conflict resolution
        # --------------------
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)
        demand.runtime_season = season_val or "unknown"

        # Map Seasons -> operative buckets
        if season_val == "winter":
            operative = "winter"
        elif season_val == "summer":
            operative = "summer"
        else:
            operative = "shoulder"
        demand.operative_season = operative

        any_cool_or_dehum = g.any_cool_or_dehum

        # --------------------
        # 2.a) VACATION override
        # --------------------
        if bool(snapshot.presence_vacation) and bool(cfg.vacation.allows_vmc_off):
            dp_cur = as_float(demand.dp_max_c)
            dp_sp = (
                float(demand.vmc_dp_sp_c)
                if getattr(demand, "vmc_dp_sp_c", None) is not None
                else float(cfg.vmc.dehum.setpoint_dp_c)
            )
            ddp_vac = float(cfg.vacation.ddp_on_c)
            dew_risk = (dp_cur is not None) and (float(dp_cur) > (dp_sp + ddp_vac))

            if dew_risk:
                # Winter: avoid active cooling; keep ventilation only.
                if operative == "winter":
                    return PlantMode.VENT_ONLY
                # Summer/shoulder: allow latent assist if configured and dehumidification is requested.
                if bool(cfg.vacation.allow_dehum_assist) and bool(demand.vmc_req_dehumidif):
                    return PlantMode.DEHUM_ASSIST
                return PlantMode.VENT_ONLY

            # No dew risk: fully OFF (including VMC)
            return PlantMode.OFF

        # --------------------
        # 2.b) Main season gating
        # --------------------
        if operative == "winter":
            if g.any_heat:
                return PlantMode.HEATING
            if any_cool_or_dehum:
                # Avoid active cooling in winter: prefer ventilation only.
                return PlantMode.VENT_ONLY
            # Idle: in AWAY/VACATION save energy by not forcing ventilation at plant level
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return PlantMode.OFF
            return PlantMode.VENT_ONLY

        if operative == "summer":
            if any_cool_or_dehum:
                return PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING
            if g.any_heat:
                # Avoid active heating in summer: ventilation only.
                return PlantMode.VENT_ONLY
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return PlantMode.OFF
            return PlantMode.VENT_ONLY

        # SHOULDER: allow both, resolve conflicts by dominant error
        if g.any_heat and not any_cool_or_dehum:
            return PlantMode.HEATING

        if any_cool_or_dehum and not g.any_heat:
            return PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING

        if g.any_heat and any_cool_or_dehum:
            # If latent is requested and we are also warm, prioritize dehumidification.
            if g.vmc_req_dehum and cool_sur >= 0.1:
                return PlantMode.DEHUM_ASSIST
            return (
                PlantMode.HEATING
                if heat_def >= cool_sur
                else (PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING)
            )

        # Idle shoulder
        return PlantMode.OFF
