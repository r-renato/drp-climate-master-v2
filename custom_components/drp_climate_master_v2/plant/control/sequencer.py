from __future__ import annotations

import logging
from typing import Any, Sequence

from homeassistant.components.climate.const import HVACMode
from homeassistant.util import dt as dt_util

from ...helpers.logger import log_debug, log_info

from ...plant.monitor.plant import PlantSnapshot
from ..decision.contracts import PlantDecision

from .config import PlantActuatorConfig
from .signals import build_control_context
from .readiness import update_boiler_ready
from .fsm import PlantFsmConfig, PlantFsmInputs, fsm_step
from .plans import compute_supply_plan, compute_zone_valves_plan
from .observed_phase import estimate_observed_phase
from .model import PlantActuatorStatus, PlantPhase, StagingState
from .device_io import PlantDeviceIO

_LOGGER = logging.getLogger(__name__)


def _valves_ready_for_fsm(stage: StagingState, *, now: Any, needs_valves: bool, valve_open_delay_s: float) -> bool:
    """Best-effort readiness prima del piano valvole del tick corrente."""
    if not needs_valves:
        return True
    ts = stage.valves_open_request_ts
    if ts is None:
        return False
    return (now - ts).total_seconds() >= float(valve_open_delay_s)


async def run_staging_cycle(
    *,
    snapshot: PlantSnapshot,
    decision: PlantDecision,
    stage: StagingState,
    cfg: PlantActuatorConfig,
    runtime_areas: Sequence[Any],
    supply_configured: bool,
    device_io: PlantDeviceIO,
) -> StagingState:
    """Esegue il ciclo di staging idraulico per un tick di controllo.

    Sequenza (8 passi)
    ------------------
    1) Derivazione contesto tipizzato (requested vs observed).
    2) VMC-only bypass: se modo=vent_only/iaq_only e FSM in OFF, applica solo
       VMC e ritorna (nessun comando a PDC/pompe/valvole).
    3) Comandi immediati PDC + VMC (non dipendono da staging).
    4) Aggiornamento boiler readiness (isteresi).
    5) FSM step (unica chiamata) → avanza stato + gating forte.
    6) Piano valvole + attuazione.
    7) Piano pompe/miscelatrice + attuazione.
    8) Log strutturato.

    Separazione da PlantActuator
    ----------------------------
    - PlantActuator: init driver, lock, delega qui.
    - PlantDeviceIO: I/O puro verso i driver HW.
    - Questo modulo: orchestrazione staging (logica pura + I/O via device_io).
    """
    now = dt_util.utcnow()

    # 1) Derivazione contesto
    ctx = build_control_context(now=now, snapshot=snapshot, decision=decision, cfg=cfg)
    observed = estimate_observed_phase(snapshot, ctx)

    # ── P-02: Gate OFF (§4.1) — secondo livello di sicurezza ───────────────────
    # §4.1: in modalità OFF nessun componente dell'impianto deve essere attuato.
    # Il gate primario è nel Supervisor (async_apply non viene chiamato in OFF).
    # Questo gate garantisce il contratto §4.1 indipendentemente dal chiamante:
    # se run_staging_cycle venisse invocato per errore con hvac_mode=OFF
    # (test, servizi futuri, bug nel supervisor), il sequencer non attua nulla.
    if decision.gating.user_hvac_mode == HVACMode.OFF.value:
        log_debug(
            _LOGGER,
            "staging_skip: hvac_mode=off fsm=%s mode=%s reason=%s",
            stage.fsm.phase.value,
            ctx.mode,
            getattr(decision, "reason", "-"),
        )
        return stage

    # 2) VMC-only bypass (VENT_ONLY / IAQ_ONLY in steady-state)
    if ctx.mode in ("vent_only", "iaq_only") and stage.fsm.phase == PlantPhase.OFF:
        await device_io.apply_vmc(decision.vmc)
        log_debug(
            _LOGGER,
            "staging_bypass: phase=%s mode=%s | vmc_power=%s speed=%s reason=%s",
            PlantPhase.VMC_ONLY.value,
            ctx.mode,
            getattr(decision.vmc, "power", None),
            getattr(decision.vmc, "air_speed", None),
            getattr(decision, "reason", "-"),
        )
        return stage

    # 3) Comandi immediati PDC + VMC
    await device_io.apply_pdc(decision.pdc)
    await device_io.apply_vmc(decision.vmc)

    # 4) Boiler readiness (isteresi)
    prev_ready = stage.boiler_ready
    _on_margin = (
        cfg.boiler_ready_cool_on_margin_c
        if ctx.mode in ("cooling", "dehum_assist")
        else cfg.boiler_ready_on_margin_c
    )
    _off_margin = (
        cfg.boiler_ready_cool_off_margin_c
        if ctx.mode in ("cooling", "dehum_assist")
        else cfg.boiler_ready_off_margin_c
    )
    boiler_update = update_boiler_ready(
        stage,
        mode=ctx.mode,
        t_boiler_supply=ctx.boiler.t_supply_c,
        t_target=ctx.boiler.t_ready_ref_c,
        on_margin_c=_on_margin,
        off_margin_c=_off_margin,
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

    energy_ok = bool(boiler_ready) if ctx.boiler.available else bool(ctx.pdc.effective_on)

    fsm_cfg = PlantFsmConfig(
        min_on_s=cfg.fsm_min_on_s,
        min_off_s=cfg.fsm_min_off_s,
        start_timeout_s=cfg.fsm_start_timeout_s,
        stop_timeout_s=cfg.fsm_stop_timeout_s,
        energy_stall_timeout_s=cfg.fsm_energy_stall_timeout_s,
    )

    # 5) FSM step (unica chiamata): produce gating forte per valvole e pompe.
    valves_ready_for_fsm = _valves_ready_for_fsm(
        stage,
        now=now,
        needs_valves=ctx.needs_valves,
        valve_open_delay_s=cfg.valve_open_delay_s,
    )
    fsm_inp = PlantFsmInputs(
        now=now,
        request_on=ctx.request_on,
        pdc_effective_on=ctx.pdc.effective_on,
        pdc_effective_known=bool(ctx.pdc.effective_known),
        compressor_on=ctx.pdc.compressor_on,
        boiler_ready=boiler_ready,
        boiler_signal_available=ctx.boiler.available,
        valves_ready=valves_ready_for_fsm,
        needs_valves=ctx.needs_valves,
    )
    fsm = fsm_step(stage, inp=fsm_inp, cfg=fsm_cfg)
    allow_valves = bool(fsm.allow_valves)
    force_close_valves = bool(fsm.force_close_valves)

    # 6) Piano valvole + apply
    valves_plan = compute_zone_valves_plan(
        stage,
        runtime_areas=runtime_areas,
        desired=ctx.desired_valves,
        allow_valves=allow_valves,
        force_close_valves=force_close_valves,
        now=now,
        valve_open_delay_s=cfg.valve_open_delay_s,
    )
    await device_io.apply_zone_valves(valves_plan.commands)
    valves_ready = valves_plan.result.ready
    valves_stats = valves_plan.result.stats

    # 7) Gating pompe dal risultato FSM.
    allow_pumps = bool(fsm.allow_pumps)
    force_pumps_off = bool(fsm.force_pumps_off)

    # 8) Piano pompe + apply + log
    supply_plan = compute_supply_plan(
        decision,
        supply_configured=supply_configured,
        allow_pumps=allow_pumps,
        force_pumps_off=force_pumps_off,
        energy_ok=energy_ok,
        valves_ready=valves_ready,
    )
    if supply_configured:
        await device_io.apply_supply(
            direct_on=supply_plan.direct_on,
            adj_on=supply_plan.adj_on,
            mv_applied=supply_plan.mix_valve_pct_applied,
        )

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

    # stage.clone(fsm=fsm.phase)  # aggiorna lo stato di staging con la nuova FSM
    return stage
