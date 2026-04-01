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

from ...plant.monitor.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ..decision.contracts import PdcCommand, PlantDecision, VmcCommand

from .config import PlantActuatorConfig
from .signals import build_control_context
from .readiness import update_boiler_ready
from .fsm import PlantFsmConfig, PlantFsmInputs, fsm_step, fsm_valve_gate, fsm_pump_gate
from .plans import compute_supply_plan, compute_zone_valves_plan
from .observed_phase import estimate_observed_phase
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
            await self._electrovalve.async_set_circuit_open(valve_switch=cmd.valve_switch, area_name=cmd.area_name, state=cmd.state)

    async def _async_apply_supply(self, *, direct_on: bool, adj_on: bool, mv_applied: Optional[float]) -> None:
        """Esegue i comandi pompe/miscelatrice calcolati dalla logica."""
        await self._supply_pumps.async_set_direct_power(power=direct_on)
        await self._supply_pumps.async_set_adj_power(power=adj_on)
        if mv_applied is not None and adj_on:
            await self._supply_pumps.async_set_mix_adj_setpoints(value=float(mv_applied))

    # ------------------------- orchestrator -------------------------
    async def async_apply(self, snapshot: PlantSnapshot, decision: PlantDecision) -> None:
        """Applica una `PlantDecision` allo stato reale dell'impianto.

        Sequenza (leggibile, a blocchi)
        -------------------------------
        1) Deriva contesto tipizzato (requested vs observed)
        2) Comandi immediati PDC + VMC
        3) Aggiorna boiler readiness (isteresi)
        4) Query pura fsm_valve_gate -> gating valvole (no mutazione stato)
        5) Piano valvole + attuazione
        6) FSM (unica passata, valves_ready reale) -> avanza stato + gating pompe
        7) Piano pompe/miscelatrice + attuazione
        8) Log strutturato per commissioning
        """
        async with self._apply_lock:
            self._snapshot = snapshot
            now = self._now()

            # 1) Derivazione contesto (single source of truth per le variabili locali).
            ctx = build_control_context(now=now, snapshot=snapshot, decision=decision, cfg=self._cfg)

            # Stima *osservata* (solo diagnostica, non influenza la FSM).
            observed = estimate_observed_phase(snapshot, ctx)

            # 2) Step dispositivi immediati (non dipendono da staging).
            await self._async_pdc_actuator(decision.pdc)
            await self._async_vmc_actuator(decision.vmc)

            # 3) Boiler readiness (isteresi)
            prev_ready = self._stage.boiler_ready
            boiler_update = update_boiler_ready(
                self._stage,
                mode=ctx.mode,
                t_boiler_supply=ctx.boiler.t_supply_c,
                t_target=ctx.boiler.t_ready_ref_c,
                on_margin_c=self._cfg.boiler_ready_on_margin_c,
                off_margin_c=self._cfg.boiler_ready_off_margin_c,
            )
            boiler_ready = boiler_update.ready
            boiler_dbg = boiler_update.debug

            if prev_ready != boiler_ready:
                log_info(
                    _LOGGER,
                    "Boiler_ready changed: %s -> %s (mode=%s t=%s target_ready=%s)",
                    prev_ready,
                    boiler_ready,
                    ctx.mode,
                    f"{ctx.boiler.t_supply_c:.2f}" if ctx.boiler.t_supply_c is not None else "-",
                    f"{ctx.boiler.t_ready_ref_c:.2f}" if ctx.boiler.t_ready_ref_c is not None else "-",
                )

            # Energia "credibile": stesso criterio per FSM e pompe.
            energy_ok = bool(boiler_ready) if ctx.boiler.available else bool(ctx.pdc.effective_on)

            fsm_cfg = PlantFsmConfig(
                min_on_s=self._cfg.fsm_min_on_s,
                min_off_s=self._cfg.fsm_min_off_s,
                start_timeout_s=self._cfg.fsm_start_timeout_s,
                stop_timeout_s=self._cfg.fsm_stop_timeout_s,
            )

            # 4) Gating valvole: query pura sulla fase FSM corrente (no mutazione).
            # allow_valves=True in STARTING e RUNNING; force_close in tutti gli altri stati.
            allow_valves, force_close_valves = fsm_valve_gate(self._stage.fsm.phase)

            # 5) Piano valvole + apply
            valves_plan = compute_zone_valves_plan(
                self._stage,
                runtime_areas=self._runtime.climate.areas or [],
                desired=ctx.desired_valves,
                allow_valves=allow_valves,
                force_close_valves=force_close_valves,
                now=now,
                valve_open_delay_s=self._cfg.valve_open_delay_s,
            )
            await self._async_apply_zone_valves(valves_plan.commands)
            valves_ready = valves_plan.result.ready
            valves_stats = valves_plan.result.stats

            # 6) FSM: unica chiamata con valves_ready reale.
            # Può avanzare STARTING->RUNNING se energy_ok e valves_ready entrambi True.
            fsm_inp = PlantFsmInputs(
                now=now,
                request_on=ctx.request_on,
                pdc_effective_on=ctx.pdc.effective_on,
                compressor_on=ctx.pdc.compressor_on,
                boiler_ready=boiler_ready,
                boiler_signal_available=ctx.boiler.available,
                valves_ready=valves_ready,
                needs_valves=ctx.needs_valves,
            )
            fsm = fsm_step(self._stage, inp=fsm_inp, cfg=fsm_cfg)
            allow_pumps, force_pumps_off = fsm_pump_gate(fsm.phase)

            # 7) Piano pompe/miscelatrice + apply
            supply_plan = compute_supply_plan(
                decision,
                supply_configured=self._supply_cfg is not None,
                allow_pumps=allow_pumps,
                force_pumps_off=force_pumps_off,
                energy_ok=energy_ok,
                valves_ready=valves_ready,
            )
            if self._supply_cfg is not None:
                await self._async_apply_supply(
                    direct_on=supply_plan.direct_on,
                    adj_on=supply_plan.adj_on,
                    mv_applied=supply_plan.mix_valve_pct_applied,
                )

            # 8) Snapshot tipizzato (per log/commissioning).
            status = PlantActuatorStatus(
                timestamp=now,
                fsm_phase=getattr(getattr(fsm, "phase", None), "value", str(getattr(fsm, "phase", "-"))),
                observed_phase=getattr(getattr(observed, "phase", None), "value", str(getattr(observed, "phase", "-"))),
                mode=ctx.mode,
                request_on=bool(ctx.request_on),
                pdc_req_on=bool(ctx.pdc.requested_on),
                direct_desired=bool(ctx.supply.direct_desired),
                adj_desired=bool(ctx.supply.adjustable_desired),
                needs_valves=bool(ctx.needs_valves),
                compressor_on=ctx.pdc.compressor_on,
                pdc_effective_on=bool(ctx.pdc.effective_on),
                pdc_effective_known=bool(ctx.pdc.effective_known),
                boiler_signal_available=bool(ctx.boiler.available),
                t_boiler_supply_c=ctx.boiler.t_supply_c,
                target_ctrl_c=ctx.boiler.t_control_target_c,
                target_ready_c=ctx.boiler.t_ready_ref_c,
                boiler_ready=bool(boiler_ready),
                on_thr_c=boiler_dbg.on_thr_c,
                off_thr_c=boiler_dbg.off_thr_c,
                valves_zones_total=int(valves_stats.zones_total),
                valves_zones_on=int(valves_stats.zones_on),
                valves_ready=bool(valves_ready),
                valves_requested_at=valves_stats.requested_at,
                valves_elapsed_s=valves_stats.elapsed_s,
                valves_opening_transition=bool(valves_stats.opening_transition),
                desired_by_zone=dict(getattr(ctx.desired_valves, "by_zone", {}) or {}),
                allow_valves=bool(allow_valves),
                allow_pumps=bool(allow_pumps),
                force_close_valves=bool(force_close_valves),
                force_pumps_off=bool(force_pumps_off),
                direct_on=bool(supply_plan.direct_on),
                adj_on=bool(supply_plan.adj_on),
                mix_valve_pct_applied=supply_plan.mix_valve_pct_applied,
                reasons=[],
            )

            # --- Debug/reasons
            reasons: list[str] = []
            reasons.extend(getattr(fsm, "reasons", []) or [])

            if not ctx.request_on:
                reasons.append("req_off")

            if ctx.request_on and ctx.boiler.available and not boiler_ready:
                reasons.append("boiler_not_ready")

            if ctx.request_on and ctx.needs_valves and not valves_ready:
                reasons.append("valves_not_ready")

            if ctx.request_on and not ctx.pdc.effective_known:
                reasons.append("pdc_state_unavailable")

            status.reasons = reasons

            log_debug(_LOGGER, "Observed phase: %s (%s)", status.observed_phase, " | ".join(getattr(observed, "reasons", []) or []))
            log_debug(_LOGGER, "%s", status)
