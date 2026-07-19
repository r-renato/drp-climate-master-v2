from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
            getattr(snapshot, "windows_open_minutes", None)
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
        zones_decision_cool: Optional[ZonesDecision] = None,
        hvac_mode: Optional[str] = None,
    ) -> tuple[PlantMode, GatingDiagnostics]:
        cfg = self.cfg

        heat_def = float(demand.heat_def_max_c)
        cool_sur = float(demand.cool_sur_max_c)

        # --------------------
        # 0) User intent (HA Climate)
        # --------------------
        # P-05: usa hvac_mode parametro esplicito (fonte autorevole dal Supervisor);
        # fallback a snapshot.climate_hvac_mode per retrocompatibilità con
        # chiamanti che non lo passano ancora (es. test, integrazioni parziali).
        if hvac_mode is not None:
            hvac_mode_s = str(hvac_mode).strip().lower()
        else:
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
        # 0.b) fan_only: nessun early return — calcolo domanda termica completo (§4.1)
        # --------------------
        # §4.1: in FAN_ONLY la componente di decision continua a funzionare come
        # in OFF o AUTO: bande di comfort, quorum, surplus/deficit vengono calcolati
        # normalmente. Le restrizioni VMC (no dehum, processo OFF) sono applicate
        # in VmcCommandBuilder leggendo dec.gating.user_hvac_mode == "fan_only".
        # L'attuazione idraulica (PDC/pompe/valvole) è bloccata nel Supervisor.
        # user_hvac_mode="fan_only" è propagato in GatingDiagnostics (sezione 2).

        # --------------------
        # 1) Profile-aware gating (thresholds + booleans)
        # --------------------
        # Legge regime_hint da snapshot.season.weather (stesso pattern di PdcCommandBuilder).
        # Fail-safe: "mild" se il campo non è disponibile.
        _rh_weather = getattr(getattr(snapshot, "season", None), "weather", None)
        _regime_hint: str = str(getattr(_rh_weather, "regime_hint", "mild")) if _rh_weather else "mild"

        # Legge stato PDC per l'isteresi on/off (Patch B + Patch 0016).
        # power_on può essere None se il sensore non è disponibile: in quel caso
        # pdc_currently_on = False (conservativo: non assume PDC accesa).
        # device_mode: int letto dal registro Modbus Aermec (1=heating, altro=cooling).
        # Fail-safe: None se il sensore non è disponibile → compute_gating usa solo
        # il ramo "start" (no isteresi keep-running) per entrambe le direzioni.
        _pdc_snap = getattr(snapshot, "pdc", None)
        _pdc_on = bool(getattr(_pdc_snap, "power_on", False) or False) if _pdc_snap is not None else False
        _pdc_device_mode: int | None = getattr(_pdc_snap, "device_mode", None) if _pdc_snap is not None else None
        _pdc_mode_str: str | None = (
            "heating" if _pdc_device_mode == 1
            else "cooling" if _pdc_device_mode is not None
            else None
        )

        # Legge t_smooth (RMOT proxy) dal modello meteo per la modulazione
        # della soglia PDC in funzione del regime termico stagionale.
        # Fail-safe: None se non disponibile (compute_gating usa fallback base).
        _wds = getattr(_rh_weather, "weather_day_signals", None) if _rh_weather else None
        _t_smooth: float | None = as_float(getattr(_wds, "t_smooth", None)) if _wds else None

        g = compute_gating(
            cfg=cfg,
            demand=demand,
            profile=profile,
            zones_decision=zones_decision,
            zones_decision_cool=zones_decision_cool,
            t_ext=as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None)),
            t_smooth=_t_smooth,
            regime_hint=_regime_hint,
            pdc_currently_on=_pdc_on,
            pdc_current_mode=_pdc_mode_str,
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
            heat_def_global_c=g.heat_def_global_c,
            heat_pdc_on_thr_eff_c=g.heat_pdc_on_thr_eff_c,
            heat_global_pdc_active=g.heat_global_pdc_active,
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
            zones_any_cool_demand=g.zones_any_cool,
            zones_cool_duty_avg_pct=g.zones_cool_duty_avg_pct,
            zones_cool_on_now_pct=g.zones_cool_on_now_pct,
            zones_cool_first_on_step=g.zones_cool_first_on_step,
            zones_mpc_cool_preheat_ok=g.zones_cool_preheat_ok,
            zones_mpc_cool_preheat_skipped_reason=g.zones_cool_preheat_skipped_reason,
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
