from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....helpers.utils import as_bool, as_float, clamp
from ....plant.monitor.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode


@dataclass(slots=True)
class VmcCommandBuilder:
    """Build VMC commands based on PlantMode and VMC policy outputs."""

    cfg: PlantPlannerConfig

    def fill(self, dec: PlantDecision, snapshot: PlantSnapshot, demand: PlantDemandSignals) -> None:
        """Populate VMC commands.

        Rational:
        - La VMC serve per ventilare e deumidificare (via DP setpoint).
        - Il contributo termico (heating/cooling) è solo boost quando fuori comfort in modo significativo.
        - Il setpoint T neutro evita di trascinare la PDC per inseguire 24°C in inverno.
        """
        cfg = self.cfg
        v = dec.vmc

        # OFF means plant idle, including VMC.
        # Solo hvac_mode=OFF esplicito o vacation+finestre aperte arriva qui.
        if dec.mode == PlantMode.OFF:
            v.power = False
            v.mode = "off"
            v.air_speed = 0
            v.setpoint_t_c = None
            v.setpoint_rh_pct = None
            v.setpoint_dp_c = None
            v.setpoint_ddp_c = None
            v.force_treatment_off = False
            v.enable_free_cooling = False
            v.force_free_cooling = False
            v.debug.update({"reason": "plant_mode_off"})
            return

        # IAQ_ONLY: ricambio aria minimo, nessun trattamento attivo, nessun bypass.
        # Tutti i setpoint (T, RH, DP, DeltaDP) vengono scritti con i valori calcolati
        # dall'algoritmo corrente perche':
        #   a) la VMC usa setpoint_t_c come soglia per decidere se intervenire
        #      termicamente: un valore stale altera il comportamento autonomo del device;
        #   b) la soglia Alarm Dew Point riflette le comfort band correnti;
        #   c) alla transizione verso un modo attivo non c'e' finestra di inconsistenza
        #      di un tick dovuta a setpoint stale sul device.
        if dec.mode == PlantMode.IAQ_ONLY:
            iaq_speed = int(
                clamp(
                    float(cfg.vmc.speed.speed_iaq_min),
                    float(cfg.vmc.speed.speed_min),
                    float(cfg.vmc.speed.speed_max),
                )
            )
            if dec.gating.operative_season == "winter":
                iaq_mode = cfg.vmc.mode_winter
            elif dec.gating.operative_season == "summer":
                iaq_mode = cfg.vmc.mode_summer
            else:
                iaq_mode = getattr(snapshot.vmc, "processing_mode", None) or cfg.vmc.mode_winter

            # Setpoint termico: t_ref_c con deadband applicata in base alla stagione,
            # stessa logica del path normale: e' la soglia di intervento termico del device.
            t_ref_c = float(dec.gating.vmc_t_ref_c)
            dead = float(cfg.vmc.temp_neutral_deadband_c)
            if iaq_mode == cfg.vmc.mode_winter:
                iaq_t_sp_c = t_ref_c - dead
            elif iaq_mode == cfg.vmc.mode_summer:
                iaq_t_sp_c = t_ref_c + dead
            else:
                iaq_t_sp_c = t_ref_c
            iaq_t_sp_c = float(clamp(iaq_t_sp_c, float(cfg.vmc.temp_min_c), float(cfg.vmc.temp_max_c)))

            # Setpoint igrometrici: usa valori calcolati dall'algoritmo corrente,
            # fallback a config se demand non ancora disponibile (degradazione).
            iaq_dp_sp_c = (
                demand.vmc_dehum_on_thr_c
                if demand.vmc_dehum_on_thr_c is not None
                else float(cfg.vmc.dehum.setpoint_dp_c)
            )
            iaq_ddp_c = (
                demand.vmc_ddp_cmd_c
                if demand.vmc_ddp_cmd_c is not None
                else float(cfg.vmc.dehum.setpoint_ddp_c)
            )
            iaq_rh_pct = float(dec.gating.vmc_rh_target_pct)

            v.power = True
            v.mode = iaq_mode
            v.air_speed = iaq_speed
            
            v.setpoint_t_c = round(iaq_t_sp_c, 1)
            v.setpoint_rh_pct = round(iaq_rh_pct, 0)

            v.setpoint_dp_c = round(iaq_dp_sp_c, 1)
            v.setpoint_ddp_c = int(round(iaq_ddp_c, 0))
            
            v.force_treatment_off = False
            v.enable_free_cooling = False
            v.force_free_cooling = False
            v.debug.update(
                {
                    "reason": "iaq_only",
                    "iaq_speed": iaq_speed,
                    "iaq_t_sp_c": round(iaq_t_sp_c, 1),
                    "iaq_dp_sp_c": round(iaq_dp_sp_c, 1),
                    "iaq_ddp_c": int(round(iaq_ddp_c, 0)),
                    "iaq_rh_pct": round(iaq_rh_pct, 0),
                }
            )
            return

        # ── FAN_ONLY / VENT_ONLY esplicito: ventilazione pura ────────────────
        # Condizione: modo VENT_ONLY con hvac_mode utente = "fan_only".
        # Deumidifica disabilitata per contratto: il soffitto radiante è spento,
        # non esistono superfici fredde → nessun rischio condensa da gestire.
        # Velocità: base/presence-driven, nessun boost DP (dp_current=None).
        # Setpoint DP: valore già calcolato dalla VmcPolicy con profilo passivo
        # (radiant_cooling_active=False → dp_sp_cmd = dp_sp_max_c): soglia
        # permissiva che non triggera deumidifica sul device.
        if dec.mode == PlantMode.VENT_ONLY and dec.gating.user_hvac_mode == "fan_only":
            _fo_op = dec.gating.operative_season
            if _fo_op == "winter":
                _fo_mode = cfg.vmc.mode_winter
            elif _fo_op == "summer":
                _fo_mode = cfg.vmc.mode_summer
            else:
                # Shoulder: usa mode_shoulder se configurato (può essere "Off" = solo
                # ventilazione, che è esattamente l'intento di fan_only); altrimenti
                # ricade sul modo corrente del device o sul default invernale.
                _fo_mode = (
                    getattr(cfg.vmc, "mode_shoulder", None)
                    or getattr(getattr(snapshot, "vmc", None), "processing_mode", None)
                    or cfg.vmc.mode_winter
                )

            _fo_t_ref_c = float(dec.gating.vmc_t_ref_c)
            _fo_dead = float(cfg.vmc.temp_neutral_deadband_c)
            if _fo_mode == cfg.vmc.mode_winter:
                _fo_t_sp = _fo_t_ref_c - _fo_dead
            elif _fo_mode == cfg.vmc.mode_summer:
                _fo_t_sp = _fo_t_ref_c + _fo_dead
            else:
                _fo_t_sp = _fo_t_ref_c
            _fo_t_sp = float(clamp(_fo_t_sp, float(cfg.vmc.temp_min_c), float(cfg.vmc.temp_max_c)))

            _fo_rh_pct = float(dec.gating.vmc_rh_target_pct)
            # Setpoint DP passivo (già calcolato da VmcPolicy senza cooling attivo).
            _fo_dp_sp_c = float(
                getattr(demand, "vmc_dp_sp_c", None)
                or getattr(cfg.vmc.dehum, "dp_sp_max_c", cfg.vmc.dehum.setpoint_dp_c)
            )
            _fo_ddp_c = float(getattr(demand, "vmc_ddp_cmd_c", None) or cfg.vmc.dehum.setpoint_ddp_c)
            # Velocità presence-driven, nessun boost DP (dp_current_c=None).
            _fo_speed = self._compute_air_speed(snapshot, None, _fo_dp_sp_c, boost=False)

            v.power = True
            v.mode = _fo_mode
            v.air_speed = int(_fo_speed)
            v.setpoint_t_c = round(_fo_t_sp, 1)
            v.setpoint_rh_pct = round(_fo_rh_pct, 0)
            v.setpoint_dp_c = round(_fo_dp_sp_c, 1)
            v.setpoint_ddp_c = int(round(_fo_ddp_c, 0))
            v.force_treatment_off = False
            v.enable_free_cooling = False
            v.force_free_cooling = False
            v.debug.update(
                {
                    "reason": "fan_only_vent",
                    "fo_mode": _fo_mode,
                    "fo_t_sp_c": round(_fo_t_sp, 1),
                    "fo_dp_sp_c": round(_fo_dp_sp_c, 1),
                    "fo_ddp_c": int(round(_fo_ddp_c, 0)),
                    "fo_speed": int(_fo_speed),
                    "dehum_suppressed": True,
                }
            )
            return

        # ── PATH FREE COOLING (bypass recuperatore) ───────────────────────────
        # Mutuamente esclusivo con il path normale: nessun setpoint T/RH/DP,
        # nessun trattamento idronico. Prerequisito: force_treatment_off = True.
        if demand.vmc_req_free_cooling:
            windows_closed = as_bool(getattr(snapshot, "windows_closed", None), default=True)
            air_speed = int(self.cfg.vmc.speed.speed_base) if windows_closed else int(self.cfg.vmc.speed.speed_windows_open)
            air_speed = int(clamp(float(air_speed), float(self.cfg.vmc.speed.speed_min), float(self.cfg.vmc.speed.speed_max)))
            v.power = True
            v.mode = None
            v.air_speed = air_speed
            v.setpoint_t_c = None
            v.setpoint_rh_pct = None
            v.setpoint_dp_c = None
            v.setpoint_ddp_c = None
            v.force_treatment_off = True
            v.enable_free_cooling = True
            v.force_free_cooling = True
            v.debug.update(
                {
                    "reason": "free_cooling_bypass",
                    "free_cool_delta_c": getattr(demand, "free_cool_delta_c", None),
                    "free_cool_dp_ok": getattr(demand, "free_cool_dp_ok", None),
                }
            )
            return

        # ── PATH NORMALE (recuperatore + eventuale batteria idraulica) ─────────
        # Riabilita il trattamento nel caso in cui fosse stato disabilitato
        # nel tick precedente per free cooling.
        v.force_treatment_off = False
        v.enable_free_cooling = False
        v.force_free_cooling = False

        if dec.gating.operative_season == "winter":
            mode = cfg.vmc.mode_winter
        elif dec.gating.operative_season == "summer":
            mode = cfg.vmc.mode_summer
        elif dec.gating.operative_season == "shoulder":
            mode = cfg.vmc.mode_shoulder
        else:
            mode = getattr(snapshot.vmc, "processing_mode", None) or cfg.vmc.mode_winter

        # Override stagione spalla con deumidifica richiesta.
        #
        # Problema: mode_shoulder mappa su "Off" nella config Eneren (autumn="Off"),
        # che disabilita il trattamento VMC (solo ventilazione). Con trattamento
        # disabilitato la VMC non può deumidificare né usare la batteria idraulica,
        # indipendentemente dai setpoint DP scritti: il device riporta sempre
        # request_dehumidification=False e l'umidità non viene controllata.
        #
        # Soluzione: quando la policy ha già deciso che la deumidifica è necessaria
        # (DP > soglia igrometrica), si forza mode_summer che abilita il trattamento.
        # Questo è corretto fisicamente: in mezza stagione con DP elevato il
        # comportamento termico desiderato è quello estivo (raffrescamento latente),
        # non lo spegnimento del trattamento.
        if demand.vmc_req_dehumidif and dec.gating.operative_season == "shoulder":
            mode = cfg.vmc.mode_summer

        t_ref_c = float(getattr(dec.gating, "vmc_t_ref_c", 22.0))
        rh_target_pct = float(getattr(dec.gating, "vmc_rh_target_pct", 50.0))

        dp_sp_c = float(
            getattr(demand, "vmc_dehum_on_thr_c", None)
            or cfg.vmc.dehum.setpoint_dp_c
        )
        ddp_sp_c = float(getattr(demand, "vmc_ddp_cmd_c", None) or cfg.vmc.dehum.setpoint_ddp_c)

        dp_current = getattr(demand, "dp_dehum_c", None) or demand.dp_max_c
        boost_active = bool(demand.vmc_req_heating or demand.vmc_req_cooling or demand.vmc_req_dehumidif)

        # Use the ON threshold as speed-boost reference (not the device-adjusted setpoint).
        # dp_sp_c is shifted down by quantization compensation (e.g. 10.4 → 9.7°C), which
        # artificially inflates the delta and causes extra speed steps to fire.
        # The ON threshold (dp_sp_cmd + ddp_cmd) is the semantically correct boundary:
        # it measures "how far above the dehumidification trigger we currently are".
        dp_speed_ref_c = float(getattr(demand, "vmc_dehum_on_thr_c", None) or dp_sp_c)
        air_speed = self._compute_air_speed(snapshot, dp_current, dp_speed_ref_c, boost_active)

        if demand.vmc_req_heating:
            t_sp = float(cfg.vmc.boost.setpoint_heat_c)
        elif demand.vmc_req_cooling:
            t_sp = float(cfg.vmc.boost.setpoint_cool_c)
        else:
            dead = float(cfg.vmc.temp_neutral_deadband_c)
            if mode == cfg.vmc.mode_winter:
                t_sp = t_ref_c - dead
            elif mode == cfg.vmc.mode_summer:
                t_sp = t_ref_c + dead
            else:
                t_sp = t_ref_c

        t_sp = clamp(float(t_sp), float(cfg.vmc.temp_min_c), float(cfg.vmc.temp_max_c))

        v.power = True
        v.mode = mode
        v.setpoint_t_c = round(t_sp, 1)
        v.setpoint_rh_pct = round(rh_target_pct, 0)
        v.setpoint_dp_c = round(dp_sp_c, 1)
        v.setpoint_ddp_c = int(round(ddp_sp_c, 0))
        v.air_speed = int(air_speed)

        # Diagnostics: report current vmc state
        if snapshot.vmc:
            vmc = snapshot.vmc
            v.debug.update(
                {
                    "recirculation": cfg.vmc.recirculation,
                    "device_power": getattr(vmc, "power_on", None),
                    "req_water": getattr(vmc, "request_water", None),
                    "req_heating": getattr(vmc, "request_heating", None),
                    "req_cooling": getattr(vmc, "request_cooling", None),
                    "req_dehumidif": getattr(vmc, "request_dehumidification", None),
                    "ambient_t_c": as_float(getattr(vmc, "sensor_t_ambient", None)),
                    "ambient_rh_pct": as_float(getattr(vmc, "sensor_h_ambient", None)),
                    "water_t_c": as_float(getattr(vmc, "sensor_t_water", None)),
                    "outdoor_t_c": as_float(getattr(vmc, "sensor_t_outdoor", None)),
                    "alarm_dew_point": getattr(vmc, "alarm_dew_point", None),
                    "alarm_general": getattr(vmc, "alarm_alarm", None),
                }
            )

    def _compute_air_speed(
        self,
        snapshot: PlantSnapshot,
        dp_current_c: Optional[float],
        dp_setpoint_c: float,
        boost: bool,
    ) -> int:
        windows_closed = as_bool(getattr(snapshot, "windows_closed", None), default=True)
        if windows_closed is False:
            sp = int(self.cfg.vmc.speed.speed_windows_open)
        elif bool(snapshot.presence_vacation) or bool(snapshot.presence_nobodysin):
            sp = int(self.cfg.vmc.speed.speed_vacation)
        else:
            sp = int(self.cfg.vmc.speed.speed_base)

        if dp_current_c is not None:
            delta = float(dp_current_c) - float(dp_setpoint_c)
            if delta >= float(self.cfg.vmc.speed.dp_boost_step1_c):
                sp += 1
            if delta >= float(self.cfg.vmc.speed.dp_boost_step2_c):
                sp += 1
            if delta >= float(self.cfg.vmc.speed.dp_boost_step3_c):
                sp += 1

        if boost:
            sp = max(sp, int(self.cfg.vmc.boost.min_air_speed))

        return int(clamp(float(sp), float(self.cfg.vmc.speed.speed_min), float(self.cfg.vmc.speed.speed_max)))
