"""Simulazione end-to-end della FSM di plant (fsm.py).

A differenza dei test unitari puntuali (test_fsm.py), qui si simula un
ciclo di vita realistico avanzando un clock manuale, per verificare che
sequenze di stati, timer e side-effect restino coerenti su un arco esteso
e non solo su singoli tick isolati.

Scenari coperti:
  1. Avvio pulito: OFF -> STARTING -> RUNNING (valvole pronte, energia ok).
  2. Stall energetico in RUNNING: dopo energy_stall_timeout_s senza energia,
     transizione forzata a STOPPING con stall_triggered_restart=True.
  3. Restart dopo stall: in STARTING successivo, valvole soppresse finché
     energy_ok non torna (bugfix gating unificato, vedi model.py / fsm.py).
  4. Spegnimento volontario prioritario su stall (request_on=False vince
     anche con energia assente, niente attesa fino a 600s).
  5. Timeout di avvio: STARTING -> FAULT se start_deadline scade senza
     ready_to_run, poi recupero automatico FAULT -> OFF dopo min_off_s.

Nota: usa direttamente fsm_step + StagingState reali, nessun mock di
componenti esterni — è un test di logica pura, non di integrazione HA.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from custom_components.drp_climate_master_v2.plant.control.fsm import (
    PlantFsmConfig,
    PlantFsmInputs,
    fsm_step,
)
from custom_components.drp_climate_master_v2.plant.control.model import PlantPhase, StagingState


START_T = datetime(2026, 1, 15, 8, 0, 0)


def _cfg(**overrides) -> PlantFsmConfig:
    base = PlantFsmConfig(
        min_on_s=600.0,
        min_off_s=300.0,
        start_timeout_s=120.0,
        stop_timeout_s=90.0,
        energy_stall_timeout_s=600.0,
    )
    return replace(base, **overrides) if overrides else base


def _inputs(
    now: datetime,
    *,
    request_on: bool,
    pdc_effective_on: bool = True,
    pdc_effective_known: bool = True,
    compressor_on: bool | None = None,
    boiler_ready: bool = True,
    boiler_signal_available: bool = True,
    valves_ready: bool = True,
    needs_valves: bool = True,
    fault_present: bool = False,
    lockout_requested: bool = False,
    maintenance: bool = False,
    vmc_request_on: bool = False,
    mode: str | None = None,
) -> PlantFsmInputs:
    return PlantFsmInputs(
        now=now,
        request_on=request_on,
        pdc_effective_on=pdc_effective_on,
        pdc_effective_known=pdc_effective_known,
        compressor_on=compressor_on,
        boiler_ready=boiler_ready,
        boiler_signal_available=boiler_signal_available,
        valves_ready=valves_ready,
        needs_valves=needs_valves,
        fault_present=fault_present,
        lockout_requested=lockout_requested,
        maintenance=maintenance,
        vmc_request_on=vmc_request_on,
        mode=mode,
    )


def _log_step(label: str, now: datetime, out) -> None:
    """Log compatto per ispezione in caso di fallimento test."""
    print(
        f"[{now.isoformat()}] {label}: phase={out.phase.value} "
        f"allow_valves={out.allow_valves} allow_pumps={out.allow_pumps} "
        f"reasons={out.reasons}"
    )


class TestFsmSimulationCleanStartup:
    """Scenario 1: avvio pulito senza intoppi energetici."""

    def test_off_to_starting_to_running(self):
        cfg = _cfg()
        stage = StagingState()
        now = START_T

        # Tick 0: OFF, nessuna richiesta -> resta OFF.
        out = fsm_step(stage, inp=_inputs(now, request_on=False), cfg=cfg)
        _log_step("idle", now, out)
        assert out.phase == PlantPhase.OFF
        assert out.force_close_valves is True

        # Tick 1: richiesta ON, ma min_off_s non ancora trascorso dall'ingresso
        # in OFF (entered_at viene fissato al primo tick) -> resta OFF.
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        _log_step("request_on early", now, out)
        assert out.phase == PlantPhase.OFF
        assert "min_off_hold" in out.reasons

        # Avanza oltre min_off_s -> transizione a STARTING consentita.
        now += timedelta(seconds=cfg.min_off_s)
        out = fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        _log_step("after min_off elapsed", now, out)
        assert out.phase == PlantPhase.STARTING
        assert out.allow_valves is True
        assert out.allow_pumps is False
        assert "phase:off->starting" in out.reasons

        # Energia e valvole pronte fin da subito -> RUNNING al tick successivo.
        now += timedelta(seconds=5)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=True, valves_ready=True, boiler_ready=True),
            cfg=cfg,
        )
        _log_step("ready_to_run", now, out)
        assert out.phase == PlantPhase.RUNNING
        assert out.allow_valves is True
        assert out.allow_pumps is True
        assert "phase:starting->running" in out.reasons


class TestFsmSimulationEnergyStallAndRestart:
    """Scenario 2+3: stall energetico in RUNNING, restart con valvole soppresse."""

    def _drive_to_running(self, stage: StagingState, cfg: PlantFsmConfig, now: datetime) -> datetime:
        fsm_step(stage, inp=_inputs(now, request_on=False), cfg=cfg)
        now += timedelta(seconds=cfg.min_off_s + 1)
        fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        assert out.phase == PlantPhase.RUNNING
        return now

    def test_stall_forces_stopping_and_suppresses_valves_on_restart(self):
        cfg = _cfg(energy_stall_timeout_s=600.0)
        stage = StagingState()
        now = self._drive_to_running(stage, cfg, START_T)

        # PDC diventa nota spenta: energy_ok=False da questo punto.
        now += timedelta(seconds=1)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=True, pdc_effective_on=False, pdc_effective_known=True),
            cfg=cfg,
        )
        _log_step("energy lost", now, out)
        assert out.phase == PlantPhase.RUNNING  # ancora in tolleranza, stall tracking iniziato
        assert any(r.startswith("energy_stall_wait:") for r in out.reasons)

        # Avanza oltre energy_stall_timeout_s: deve forzare STOPPING.
        now += timedelta(seconds=cfg.energy_stall_timeout_s + 1)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=True, pdc_effective_on=False, pdc_effective_known=True),
            cfg=cfg,
        )
        _log_step("stall timeout", now, out)
        assert out.phase == PlantPhase.STOPPING
        assert any(r.startswith("energy_stall:") for r in out.reasons)
        assert stage.fsm.stall_triggered_restart is True

        # Completa la finestra STOPPING con richiesta ancora attiva: riparte
        # direttamente in STARTING. Le valvole devono essere SOPPRESSE (bugfix
        # gating unificato) perché stall_triggered_restart è ancora True.
        now += timedelta(seconds=cfg.stop_timeout_s + 1)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=True, pdc_effective_on=False, pdc_effective_known=True),
            cfg=cfg,
        )
        _log_step("stopping->restart starting, energy still off", now, out)
        assert out.phase == PlantPhase.STARTING
        assert out.allow_valves is False, "valvole devono restare chiuse: stall restart + PDC nota spenta"
        assert out.force_close_valves is True
        assert "valves_suppressed:stall_restart" in out.reasons or "valves_suppressed:pdc_off" in out.reasons

        # Energia torna disponibile: soppressione rimossa, valvole consentite.
        now += timedelta(seconds=5)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=True, pdc_effective_on=True, pdc_effective_known=True, boiler_ready=True),
            cfg=cfg,
        )
        _log_step("energy restored", now, out)
        assert stage.fsm.stall_triggered_restart is False
        assert "stall_restart_energy_ok" in out.reasons
        assert out.allow_valves is True


class TestFsmSimulationVoluntaryStopPriority:
    """Scenario 4: spegnimento volontario prioritario su stall energetico."""

    def test_request_off_during_stall_does_not_wait_for_timeout(self):
        cfg = _cfg(energy_stall_timeout_s=600.0)
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False), cfg=cfg)
        now += timedelta(seconds=cfg.min_off_s + 1)
        fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=True), cfg=cfg)
        assert out.phase == PlantPhase.RUNNING

        # Energia assente, ma utente spegne subito: niente attesa fino a 600s.
        now += timedelta(seconds=2)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=False, pdc_effective_on=False),
            cfg=cfg,
        )
        _log_step("voluntary off during stall", now, out)
        # min_on_s non ancora trascorso dall'ingresso in RUNNING -> hold,
        # ma NON deve esserci alcun riferimento a stall tracking: la
        # richiesta off è valutata per prima, indipendentemente da energy_ok.
        assert out.phase == PlantPhase.RUNNING
        assert "min_on_hold" in out.reasons
        assert not any("stall" in r for r in out.reasons)

        now += timedelta(seconds=cfg.min_on_s + 1)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=False, pdc_effective_on=False),
            cfg=cfg,
        )
        _log_step("stopping after min_on elapsed", now, out)
        assert out.phase == PlantPhase.STOPPING
        assert stage.fsm.stall_triggered_restart is False, "stop volontario non deve marcare stall restart"


class TestFsmSimulationStartTimeoutAndRecovery:
    """Scenario 5: timeout di avvio -> FAULT -> recupero automatico a OFF."""

    def test_starting_times_out_to_fault_then_recovers(self):
        cfg = _cfg(start_timeout_s=120.0, min_off_s=300.0)
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False), cfg=cfg)
        now += timedelta(seconds=cfg.min_off_s + 1)
        out = fsm_step(stage, inp=_inputs(now, request_on=True, valves_ready=False), cfg=cfg)
        assert out.phase == PlantPhase.STARTING

        # Valvole mai pronte: avanza oltre start_timeout_s -> FAULT.
        now += timedelta(seconds=cfg.start_timeout_s + 1)
        out = fsm_step(stage, inp=_inputs(now, request_on=True, valves_ready=False), cfg=cfg)
        _log_step("start timeout", now, out)
        assert out.phase == PlantPhase.FAULT
        assert "start_timeout" in out.reasons
        assert out.force_close_valves is True
        assert out.force_pumps_off is True

        # Recupero automatico indipendente da request_on, dopo min_off_s
        # dall'ingresso in FAULT.
        now += timedelta(seconds=cfg.min_off_s + 1)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, valves_ready=False), cfg=cfg)
        _log_step("fault recovery", now, out)
        assert out.phase == PlantPhase.OFF


class TestFsmSimulationVmcOnly:
    """Scenario 6 (P-07b): ventilazione-sola come fase FSM reale.

    Copre:
    - OFF -> VMC_ONLY quando compare domanda VMC senza domanda idronica.
    - VMC_ONLY -> OFF quando cessa la domanda VMC.
    - VMC_ONLY -> FAULT/LOCKOUT/MAINTENANCE (gating che prima del bypass
      rimosso non si applicava mai alla VMC).
    - request_on idronico ignorato in VMC_ONLY (nessuna transizione a
      STARTING), con warning esplicito in log.
    """

    def test_off_to_vmc_only_and_back(self):
        cfg = _cfg()
        stage = StagingState()
        now = START_T

        # Tick 0: nessuna domanda di alcun tipo -> resta OFF.
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=False), cfg=cfg)
        assert out.phase == PlantPhase.OFF
        assert out.allow_vmc is False
        assert out.force_vmc_off is True

        # Compare domanda VMC (vent_only/iaq_only), nessuna domanda idronica:
        # transizione immediata (nessun anti-cycling per la VMC, per scelta
        # esplicita — vedi discussione P-07b).
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True, mode="vent_only"), cfg=cfg)
        _log_step("off->vmc_only", now, out)
        assert out.phase == PlantPhase.VMC_ONLY
        assert "phase:off->vmc_only" in out.reasons
        assert out.allow_vmc is True
        assert out.force_vmc_off is False
        # Valvole/pompe restano bloccate: VMC_ONLY non è idronico.
        assert out.allow_valves is False
        assert out.allow_pumps is False

        # Persiste in VMC_ONLY finché la domanda VMC resta attiva.
        now += timedelta(seconds=30)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True, mode="vent_only"), cfg=cfg)
        assert out.phase == PlantPhase.VMC_ONLY

        # Cessata domanda VMC -> OFF, immediato (nessun timer).
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=False), cfg=cfg)
        _log_step("vmc_only->off", now, out)
        assert out.phase == PlantPhase.OFF
        assert "phase:vmc_only->off" in out.reasons

    def test_vmc_only_gated_by_fault_lockout_maintenance(self):
        """Prima di P-07b, FAULT/LOCKOUT/MAINTENANCE non avevano alcun
        effetto sulla VMC (bypass nel sequencer, mai passava da fsm_step).
        Ora la VMC è soggetta allo stesso gating forte del resto."""

        # --- FAULT ---
        cfg = _cfg()
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True), cfg=cfg)
        assert stage.fsm.phase == PlantPhase.VMC_ONLY
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True, fault_present=True), cfg=cfg)
        _log_step("vmc_only->fault", now, out)
        assert out.phase == PlantPhase.FAULT
        assert out.allow_vmc is False
        assert out.force_vmc_off is True

        # --- LOCKOUT ---
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True), cfg=cfg)
        now += timedelta(seconds=1)
        out = fsm_step(
            stage,
            inp=_inputs(now, request_on=False, vmc_request_on=True, lockout_requested=True),
            cfg=cfg,
        )
        _log_step("vmc_only->lockout", now, out)
        assert out.phase == PlantPhase.LOCKOUT
        assert out.allow_vmc is False

        # --- MAINTENANCE ---
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True), cfg=cfg)
        now += timedelta(seconds=1)
        out = fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True, maintenance=True), cfg=cfg)
        _log_step("vmc_only->maintenance", now, out)
        assert out.phase == PlantPhase.MAINTENANCE
        assert out.allow_vmc is False

    def test_vmc_only_ignores_hydronic_request_and_logs_warning(self, caplog):
        """Domanda idronica in VMC_ONLY è un segnale di contratto violato a
        monte (PlantMode.VENT_ONLY/IAQ_ONLY dovrebbero implicare PDC OFF e
        valvole chiuse): va ignorata come trigger, solo loggata come warning.
        """
        cfg = _cfg()
        stage = StagingState()
        now = START_T
        fsm_step(stage, inp=_inputs(now, request_on=False, vmc_request_on=True), cfg=cfg)
        assert stage.fsm.phase == PlantPhase.VMC_ONLY

        now += timedelta(seconds=1)
        with caplog.at_level("WARNING"):
            out = fsm_step(
                stage,
                inp=_inputs(now, request_on=True, vmc_request_on=True, mode="vent_only"),
                cfg=cfg,
            )
        _log_step("vmc_only + spurious request_on", now, out)

        # Nessuna transizione: request_on idronico ignorato per contratto.
        assert out.phase == PlantPhase.VMC_ONLY
        assert out.allow_vmc is True

        # Warning esplicito emesso (non un semplice debug silenzioso).
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("VMC_ONLY" in r.message for r in warnings)
        assert any("vent_only" in r.message for r in warnings)
