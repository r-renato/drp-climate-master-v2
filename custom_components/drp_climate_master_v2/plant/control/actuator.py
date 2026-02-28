from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...helpers.logger import log_debug, log_info
from ...helpers.utils import as_float

from ...devices.aermec_hmi080 import AermecHMI080
from ...devices.caleffi_pumps import CaleffiSupplyPumps
from ...devices.eneren_rer020i import EnerenRER020I
from ...devices.eurotherm import EurothermElectrovalve

from ...domain.models.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ..decision.contracts import PdcCommand, PlantDecision, VmcCommand

from .config import PlantActuatorConfig
from .logic import (
    compute_current_plant_phase,
    compute_supply_plan,
    compute_operation_plant_t_ready_ref_c,
    compute_best_plant_t_target_c,
    compute_zone_valves_plan,
    desired_zone_valves,
    fsm_step,
    mode_value,
    pdc_requested_on,
    update_boiler_ready,
)
from .model import PlantActuatorStatus, StagingState

_LOGGER = logging.getLogger(__name__)


class PlantActuator:
    """Attuatore di plant: traduce `PlantDecision` in comandi ai dispositivi.

    Architettura
    ------------
    - `model.py`: value-objects e stato persistente (staging + FSM)
    - `logic.py`: logica pura (calcoli, piano valvole/pompe, FSM)
    - `actuator.py` (questo file): I/O e orchestrazione

    Obiettivo principale
    --------------------
    Usare una macchina a stati (FSM) sopra lo staging per evitare stati anomali
    di accensione/spegnimento dovuti a flap sensori e dinamiche lente.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        runtime_cfg: Optional[RuntimeConfig] = None,
        *,
        runtime: Optional[RuntimeConfig] = None,
        cfg: Optional[PlantActuatorConfig] = None,
    ) -> None:
        """Crea l'attuatore di impianto.

        Parametri
        ---------
        hass:
            Istanza Home Assistant.
        runtime_cfg / runtime:
            Configurazione runtime dell'integrazione.

        Nota
        ----
        `runtime` è un alias per compatibilità con chiamanti legacy.
        """

        if runtime_cfg is None:
            runtime_cfg = runtime
        if runtime_cfg is None:
            raise ValueError("runtime_cfg (o runtime) è obbligatorio")

        self._cfg = cfg or PlantActuatorConfig()
        self._cfg.validate()

        self._snapshot: Optional[PlantSnapshot] = None
        self._apply_lock = asyncio.Lock()

        self._runtime = runtime_cfg

        # Device drivers
        self._heatpump = AermecHMI080(hass=hass, runtime_cfg=runtime_cfg)
        self._vmc = EnerenRER020I(hass=hass, runtime_cfg=runtime_cfg)
        self._electrovalve = EurothermElectrovalve(hass=hass, runtime_cfg=runtime_cfg)
        self._supply_pumps = CaleffiSupplyPumps(hass=hass, runtime_cfg=runtime_cfg)

        self._radiant_cfg = getattr(runtime_cfg.climate.devices, "radiant", None)
        self._supply_cfg = getattr(runtime_cfg.climate.devices, "supply_units", None)

        # Sensors (optional)
        self._pdc_compressor_state_ent: Optional[str] = None
        if self._radiant_cfg and getattr(self._radiant_cfg, "sensors", None):
            self._pdc_compressor_state_ent = getattr(self._radiant_cfg.sensors, "pdc_compressor_state", None)

        self._boiler_supply_ent: Optional[str] = None
        if self._supply_cfg and getattr(self._supply_cfg, "sensors", None):
            self._boiler_supply_ent = getattr(self._supply_cfg.sensors, "boiler_temp_system_supply", None)

        # Stato persistente
        self._stage = StagingState()

    # ------------------------- time helper -------------------------

    @staticmethod
    def _now() -> datetime:
        """Timestamp corrente (UTC) coerente con Home Assistant."""
        return dt_util.utcnow()

    # ------------------------- low-level device actuation -------------------------

    async def _async_pdc_actuator(self, pdc_command: PdcCommand) -> None:
        """Applica un comando PDC alle entità Home Assistant."""

        # NON MODIFICARE: la potenza viene gestita altrove o da altri vincoli.
        # await self._heatpump.async_set_power(fm_power=pdc_command.fm_power, power=pdc_command.power)
        await self._heatpump.async_set_processing_mode(mode=pdc_command.mode)
        await self._heatpump.async_set_heat_setpoints(t=pdc_command.heat_wot_c, dt=pdc_command.heat_dt_c)
        await self._heatpump.async_set_cool_setpoints(t=pdc_command.cool_wot_c, dt=pdc_command.cool_dt_c)

    async def _async_vmc_actuator(self, vmc_command: VmcCommand) -> None:
        """Applica un comando VMC alle entità Home Assistant."""

        await self._vmc.async_set_power(power=vmc_command.power)
        await self._vmc.async_set_processing_mode(mode=vmc_command.mode)
        await self._vmc.async_set_spare(spare=vmc_command.air_speed)
        await self._vmc.async_set_temperature(target=vmc_command.setpoint_t_c)
        await self._vmc.async_set_humidity(target=vmc_command.setpoint_rh_pct)
        await self._vmc.async_set_dew_point(target=vmc_command.setpoint_dp_c)
        await self._vmc.async_set_delta_dew_point(target=vmc_command.setpoint_ddp_c)

    async def _async_apply_zone_valves(self, commands) -> None:
        """Esegue i comandi valvole calcolati dalla logica."""
        for cmd in commands:
            await self._electrovalve.async_set_circuit_open(area_name=cmd.area_name, state=cmd.state)

    async def _async_apply_supply(self, *, direct_on: bool, adj_on: bool, mv_applied: Optional[float]) -> None:
        """Esegue i comandi pompe/miscelatrice calcolati dalla logica."""
        await self._supply_pumps.async_set_direct_power(power=direct_on)
        await self._supply_pumps.async_set_adj_power(power=adj_on)
        if mv_applied is not None and adj_on:
            await self._supply_pumps.async_set_mix_adj_setpoints(value=float(mv_applied))

    # ------------------------- orchestrator -------------------------

    async def async_apply(self, snapshot: PlantSnapshot, decision: PlantDecision) -> None:
        """Applica una `PlantDecision` allo stato reale dell'impianto.

        Sequenza:
        1) Comandi immediati PDC + VMC
        2) Calcoli puri (readiness, desired valves, FSM)
        3) Attuazione valvole/pompe secondo piani calcolati
        """

        async with self._apply_lock:
            self._snapshot = snapshot
            current_phase = self._stage.fsm.phase

            current_plant_phase = compute_current_plant_phase(snapshot=snapshot, decision=decision)

            pdc_command = decision.pdc
            vmc_command = decision.vmc

            # --- Step 1: immediate devices
            await self._async_pdc_actuator(pdc_command)
            await self._async_vmc_actuator(vmc_command)

            # --- Input signals
            pdc_req_power_on = pdc_requested_on(pdc_command)
            compressor_sta_on: Optional[bool] = (
                self._snapshot.pdc.sensor_compressor_state
                if (self._snapshot and self._snapshot.pdc)
                else None
            )
            t_boiler_supply_sta: Optional[float] = (
                as_float(self._snapshot.supply_unit.sensor_boiler_temp_system_supply)
                if (self._snapshot and self._snapshot.supply_unit)
                else None
            )

            mode_req = mode_value(decision)
            t_control_target_req = compute_best_plant_t_target_c(decision)
            t_plant_ready_ref = compute_operation_plant_t_ready_ref_c(
                mode=mode_req,
                t_control_target_c=t_control_target_req,
                heat_bias_c=self._cfg.boiler_ready_heat_bias_c,
                cool_bias_c=self._cfg.boiler_ready_cool_bias_c,
            )
            boiler_signal_available = bool(t_boiler_supply_sta is not None and t_plant_ready_ref is not None)

            # --- Boiler readiness
            prev_ready = self._stage.boiler_ready
            boiler_update = update_boiler_ready(
                self._stage,
                mode=mode_req,
                t_boiler_supply=t_boiler_supply_sta,
                t_target=t_plant_ready_ref,
                on_margin_c=self._cfg.boiler_ready_on_margin_c,
                off_margin_c=self._cfg.boiler_ready_off_margin_c,
            )
            boiler_ready = boiler_update.ready
            boiler_dbg = boiler_update.debug

            if prev_ready != boiler_ready:
                log_info(
                    _LOGGER,
                    "Boiler_ready changed: %s -> %s (mode=%s t=%.2f target=%s)",
                    prev_ready,
                    boiler_ready,
                    mode_req,
                    t_boiler_supply_sta if t_boiler_supply_sta is not None else float("nan"),
                    f"{t_control_target_req:.2f}" if t_control_target_req is not None else "-",
                )

            # --- Desired valves + request derivation
            desired_valves = desired_zone_valves(decision)
            supply_cmd = getattr(decision, "supply", None)
            direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False)) if supply_cmd is not None else False
            adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False)) if supply_cmd is not None else False

            request_on = bool(pdc_req_power_on or direct_desired or adj_desired or desired_valves.any_open)

            # PDC considerata ON se richiesta o compressore ON
            pdc_on = bool(pdc_req_power_on or compressor_sta_on is True)

            now = self._now()
            needs_valves = bool(adj_desired and desired_valves.any_open)

            # --- FSM (prima passata)
            fsm = fsm_step(
                self._stage,
                now=now,
                request_on=request_on,
                pdc_on=pdc_on,
                compressor_on=compressor_sta_on,
                boiler_ready=boiler_ready,
                boiler_signal_available=boiler_signal_available,
                valves_ready=False,
                needs_valves=needs_valves,
                min_on_s=self._cfg.fsm_min_on_s,
                min_off_s=self._cfg.fsm_min_off_s,
                start_timeout_s=self._cfg.fsm_start_timeout_s,
                stop_timeout_s=self._cfg.fsm_stop_timeout_s,
            )

            # --- Zone valves plan + apply
            valves_plan = compute_zone_valves_plan(
                self._stage,
                runtime_areas=self._runtime.climate.areas or [],
                desired=desired_valves,
                allow_valves=fsm.allow_valves,
                force_close_valves=fsm.force_close_valves,
                now=now,
                valve_open_delay_s=self._cfg.valve_open_delay_s,
            )
            await self._async_apply_zone_valves(valves_plan.commands)
            valves_ready = valves_plan.result.ready
            valves_stats = valves_plan.result.stats

            # --- FSM (seconda passata con valves_ready)
            fsm = fsm_step(
                self._stage,
                now=now,
                request_on=request_on,
                pdc_on=pdc_on,
                compressor_on=compressor_sta_on,
                boiler_ready=boiler_ready,
                boiler_signal_available=boiler_signal_available,
                valves_ready=valves_ready,
                needs_valves=needs_valves,
                min_on_s=self._cfg.fsm_min_on_s,
                min_off_s=self._cfg.fsm_min_off_s,
                start_timeout_s=self._cfg.fsm_start_timeout_s,
                stop_timeout_s=self._cfg.fsm_stop_timeout_s,
            )

            # --- Supply plan + apply
            supply_plan = compute_supply_plan(
                decision,
                supply_configured=self._supply_cfg is not None,
                allow_pumps=fsm.allow_pumps,
                force_pumps_off=fsm.force_pumps_off,
                pdc_on=pdc_on,
                compressor_on=compressor_sta_on,
                boiler_ready=boiler_ready,
                boiler_signal_available=boiler_signal_available,
                valves_ready=valves_ready,
            )
            if self._supply_cfg is not None:
                await self._async_apply_supply(
                    direct_on=supply_plan.direct_on,
                    adj_on=supply_plan.adj_on,
                    mv_applied=supply_plan.mix_valve_pct_applied,
                )

            # Costruisce uno snapshot tipizzato dello staging (per log/commissioning).
            status = PlantActuatorStatus(
                timestamp=now,
                phase=getattr(getattr(fsm, "phase", None), "value", str(getattr(fsm, "phase", "-"))),
                mode=mode_req,
                request_on=bool(request_on),
                pdc_req_on=bool(pdc_req_power_on),
                direct_desired=bool(direct_desired),
                adj_desired=bool(adj_desired),
                needs_valves=bool(needs_valves),
                compressor_on=compressor_sta_on,
                pdc_on=bool(pdc_on),
                boiler_signal_available=bool(boiler_signal_available),
                t_boiler_supply_c=t_boiler_supply_sta,
                target_ctrl_c=t_control_target_req,
                target_ready_c=t_plant_ready_ref,
                boiler_ready=bool(boiler_ready),
                on_thr_c=boiler_dbg.on_thr_c,
                off_thr_c=boiler_dbg.off_thr_c,
                valves_zones_total=int(valves_stats.zones_total),
                valves_zones_on=int(valves_stats.zones_on),
                valves_ready=bool(valves_ready),
                valves_requested_at=valves_stats.requested_at,
                valves_elapsed_s=valves_stats.elapsed_s,
                valves_opening_transition=bool(valves_stats.opening_transition),
                desired_by_zone=dict(getattr(desired_valves, "by_zone", {}) or {}),
                allow_valves=bool(getattr(fsm, "allow_valves", False)),
                allow_pumps=bool(getattr(fsm, "allow_pumps", False)),
                force_close_valves=bool(getattr(fsm, "force_close_valves", False)),
                force_pumps_off=bool(getattr(fsm, "force_pumps_off", False)),
                direct_on=bool(supply_plan.direct_on),
                adj_on=bool(supply_plan.adj_on),
                mix_valve_pct_applied=supply_plan.mix_valve_pct_applied,
                reasons=[],
            )

            # --- Debug log
            reasons: list[str] = []
            reasons.extend(fsm.reasons)
            if not request_on:
                reasons.append("req_off")
            if request_on and not pdc_on:
                reasons.append("pdc_not_on")
            if request_on and pdc_on and boiler_signal_available and not boiler_ready:
                reasons.append("boiler_not_ready")
            if request_on and pdc_on and needs_valves and not valves_ready:
                reasons.append("valves_not_ready")
            if compressor_sta_on is None and self._pdc_compressor_state_ent:
                reasons.append("compressor_state_unavailable")

            status.reasons = reasons
            log_debug(_LOGGER, "%s", current_plant_phase)
            log_debug(_LOGGER, "%s", status)
