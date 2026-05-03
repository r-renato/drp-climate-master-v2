from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from homeassistant.components.climate.const import HVACMode

from ....helpers.utils import as_float
from ....domain.enums import HVACOperatingProfile
from ....plant.monitor.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import GatingDiagnostics, PlantDemandSignals, PlantMode
from ..zone.model import ZonesDecision

from .gating import compute_gating


@dataclass(slots=True)
class ModeResolver:
    """Resolve the global PlantMode (regime) from demand + user intent + seasons.

    Responsibility
    - Decide the high-level regime ONLY (no actuator commands).
    - Return immutable `GatingDiagnostics` for observability and command builders.

    Notes
    - This module deliberately remains HA-aware (HVACMode mapping), because it is
      a thin translation of user intent.
    """

    cfg: PlantPlannerConfig

    def _iaq_mode(
        self,
        snapshot: PlantSnapshot,
        gating: GatingDiagnostics,
    ) -> tuple[PlantMode, GatingDiagnostics]:
        """Restituisce IAQ_ONLY o OFF in base a occupazione e stato finestre.

        Logica:
        - Vacation -> OFF (nessuno in casa per periodo esteso)
        - Finestre aperte da piu di windows_open_off_minutes -> OFF
          (nessun senso climatizzare/ventilare con dispersione attiva)
        - Altrimenti -> IAQ_ONLY (ricambio aria minimo garantito)
        """
        if bool(snapshot.presence_vacation):
            return (PlantMode.OFF, gating)

        windows_open_min = as_float(
            getattr(snapshot, "windows_close_minutes_off", None)
        )
        threshold = float(self.cfg.windows_open_off_minutes)
        if windows_open_min is not None and float(windows_open_min) >= threshold:
            return (PlantMode.OFF, gating)

        return (PlantMode.IAQ_ONLY, gating)

    def decide(
        self,
        *,
        snapshot: PlantSnapshot,
        demand: PlantDemandSignals,
        zones_decision: Optional[ZonesDecision] = None,
    ) -> tuple[PlantMode, GatingDiagnostics]:
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

        # Absolute override: hvac_mode OFF spegne tutto inclusa VMC
        # Off non deve spegnere nulla
        # if hvac_mode_s == HVACMode.OFF.value:
        #     gating = GatingDiagnostics(
        #         user_hvac_mode=hvac_mode_s,
        #         user_profile=profile.value,
        #         user_forced_off=True,
        #         vmc_t_ref_c=float(getattr(demand, "vmc_t_ref_c", 22.0)),
        #         vmc_rh_target_pct=float(getattr(demand, "vmc_rh_target_pct", 50.0)),
        #     )
        #     return (PlantMode.OFF, gating)

        # --------------------
        # 1) Profile-aware gating (thresholds + booleans)
        # --------------------
        # Legge regime_hint da snapshot.season.weather (stesso pattern di PdcCommandBuilder).
        # Fail-safe: "mild" se il campo non è disponibile.
        _rh_weather = getattr(getattr(snapshot, "season", None), "weather", None)
        _regime_hint: str = str(getattr(_rh_weather, "regime_hint", "mild")) if _rh_weather else "mild"

        g = compute_gating(
            cfg=cfg,
            demand=demand,
            profile=profile,
            zones_decision=zones_decision,
            t_ext=as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None)),
            regime_hint=_regime_hint,
        )

        # --------------------
        # 2) Season gating / conflict resolution
        # --------------------
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)
        runtime_season = season_val or "unknown"

        # Map Seasons -> operative buckets
        if season_val == "winter":
            operative_season = "winter"
        elif season_val == "summer":
            operative_season = "summer"
        else:
            operative_season = "shoulder"

        gating = GatingDiagnostics(
            user_hvac_mode=hvac_mode_s,
            user_profile=profile.value,
            user_forced_off=False,
            runtime_season=runtime_season,
            operative_season=operative_season,
            ctrl_aggr=g.ctrl_aggr,
            heat_on_thr_c=g.heat_thr_c,
            cool_on_thr_c=g.cool_thr_c,
            quorum_cov_req=g.quorum_cov_req,
            heat_override=g.heat_override,
            heat_quorum_ok=g.heat_quorum_ok,
            heat_mean_ok=g.heat_mean_ok,
            cool_override=g.cool_override,
            cool_quorum_ok=g.cool_quorum_ok,
            cool_mean_ok=g.cool_mean_ok,
            any_heat=g.any_heat,
            any_cool=g.any_cool,
            any_dehum=g.vmc_req_dehum,
            heat_sensible=g.heat_sensible,
            cool_sensible=g.cool_sensible,
            zones_any_heat_demand=g.zones_any_heat,
            zones_full_on_pct=g.zones_full_on_pct,
            zones_mpc_heat_preheat_ok=g.zones_preheat_ok,
            zones_duty_avg_pct=g.zones_duty_avg_pct,
            zones_on_now_pct=g.zones_on_now_pct,
            zones_first_on_step=g.zones_first_on_step,
            vmc_t_ref_c=float(getattr(demand, "vmc_t_ref_c", 22.0)),
            vmc_rh_target_pct=float(getattr(demand, "vmc_rh_target_pct", 50.0)),
        )

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
                if operative_season == "winter":
                    return (PlantMode.VENT_ONLY, gating)
                # Summer/shoulder: allow latent assist if configured and dehumidification is requested.
                if bool(cfg.vacation.allow_dehum_assist) and bool(demand.vmc_req_dehumidif):
                    return (PlantMode.DEHUM_ASSIST, gating)
                return (PlantMode.VENT_ONLY, gating)

            # No dew risk in vacation: fully OFF (including VMC)
            return (PlantMode.OFF, gating)

        # --------------------
        # 2.b) Main season gating
        # --------------------
        if operative_season == "winter":
            if g.any_heat:
                return (PlantMode.HEATING, gating)
            if any_cool_or_dehum:
                # Avoid active cooling in winter: prefer ventilation only.
                return (PlantMode.VENT_ONLY, gating)
            # Profili AWAY: rispetta la scelta utente ma garantisce IAQ se occupato
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return self._iaq_mode(snapshot, gating)
            # Free cooling/heating intenzionale (bypass recuperatore VMC)
            if demand.vmc_req_free_cooling:
                return (PlantMode.VENT_ONLY, gating)
            if demand.vmc_req_free_heating:
                return (PlantMode.VENT_ONLY, gating)
            # Idle inverno occupato: IAQ minimo garantito
            return self._iaq_mode(snapshot, gating)

        if operative_season == "summer":
            if any_cool_or_dehum:
                return (PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING, gating)
            if g.any_heat:
                # Avoid active heating in summer: ventilation only.
                return (PlantMode.VENT_ONLY, gating)
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return self._iaq_mode(snapshot, gating)
            # Free cooling intenzionale in estate (free heating non applicabile)
            if demand.vmc_req_free_cooling:
                return (PlantMode.VENT_ONLY, gating)
            # Idle estate occupata: IAQ minimo garantito
            return self._iaq_mode(snapshot, gating)

        # SHOULDER: allow both, resolve conflicts by dominant error
        if g.any_heat and not any_cool_or_dehum:
            return (PlantMode.HEATING, gating)

        if any_cool_or_dehum and not g.any_heat:
            return (PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING, gating)

        if g.any_heat and any_cool_or_dehum:
            # If latent is requested and we are also warm, prioritize dehumidification.
            if g.vmc_req_dehum and cool_sur >= 0.1:
                return (PlantMode.DEHUM_ASSIST, gating)
            mode = (
                PlantMode.HEATING
                if heat_def >= cool_sur
                else (PlantMode.DEHUM_ASSIST if g.vmc_req_dehum else PlantMode.COOLING)
            )
            return (mode, gating)

        # Idle shoulder: free cooling/heating se fattibile, altrimenti IAQ
        if demand.vmc_req_free_cooling:
            return (PlantMode.VENT_ONLY, gating)
        if demand.vmc_req_free_heating:
            return (PlantMode.VENT_ONLY, gating)
        return self._iaq_mode(snapshot, gating)
