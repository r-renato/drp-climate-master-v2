from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .model import PlantFsmOutput, PlantFsmState, PlantPhase, StagingState


def fsm_pump_gate(phase: PlantPhase) -> tuple[bool, bool]:
    """Query pura: restituisce (allow_pumps, force_pumps_off) per la fase corrente.

    Non muta alcuno stato. Sostituisce la seconda chiamata a fsm_step nel ciclo
    di attuazione: il gating pompe dipende unicamente dalla fase FSM già aggiornata,
    non da una seconda esecuzione della macchina a stati.

    Regola: le pompe sono consentite esclusivamente in RUNNING.
    In qualsiasi altra fase (STARTING, STOPPING, FAULT, OFF) → pompe bloccate.
    """
    allow = phase == PlantPhase.RUNNING
    return allow, not allow


@dataclass(slots=True, frozen=True)
class PlantFsmConfig:
    """Parametri della FSM (anti-chatter e timeout).

    energy_stall_timeout_s:
        Timeout stall energetico in RUNNING (secondi).
        Se energy_ok=False persiste oltre questo valore, la FSM forza la
        transizione a STOPPING per ripristinare il ciclo di avvio.
        Deve essere < start_timeout_s (validato in PlantActuatorConfig).
    """

    min_on_s: float
    min_off_s: float
    start_timeout_s: float
    stop_timeout_s: float
    energy_stall_timeout_s: float = 600.0


@dataclass(slots=True, frozen=True)
class PlantFsmInputs:
    """Ingressi della FSM.

    Separare gli ingressi in una dataclass:
    - riduce la lista parametri (leggibilita)
    - previene errori di ordering
    - rende piu semplice la serializzazione per test/diagnostica
    """

    now: datetime
    request_on: bool

    # Stato energia osservata (preferibile a 'requested_on' per evitare ottimismo)
    pdc_effective_on: bool
    # True se almeno un sensore di stato PDC (power o compressor) è disponibile.
    # False = stato PDC ignoto: non applicare gating PDC su energy_ok.
    pdc_effective_known: bool
    compressor_on: Optional[bool]

    boiler_ready: bool
    boiler_signal_available: bool

    valves_ready: bool
    needs_valves: bool


def _pdc_known_off(inp: PlantFsmInputs) -> bool:
    """True se la PDC è nota (sensore disponibile) e nota spenta."""
    return inp.pdc_effective_known and not inp.pdc_effective_on


def _energy_ok(inp: PlantFsmInputs) -> bool:
    """Energia credibile disponibile (gating per RUNNING/STARTING).

    Prerequisito PDC (quando nota): se nota spenta, energy_ok è SEMPRE False
    indipendentemente dal boiler — senza PDC attiva non c'è fonte di
    calore/freddo, il boiler non si scalderà mai da solo. Impedisce che
    RUNNING venga raggiunto con PDC spenta anche se boiler_ready=True per
    effetto di isteresi o soglia bassa.

    Se PDC non nota (pdc_effective_known=False): nessun gate PDC aggiuntivo,
    utile per sistemi senza sensore di stato PDC.
    """
    if _pdc_known_off(inp):
        return False
    if inp.boiler_signal_available:
        return bool(inp.boiler_ready)
    return bool(inp.pdc_effective_on)


def _ready_to_run(inp: PlantFsmInputs) -> bool:
    """True se energia disponibile e (valvole non richieste oppure pronte)."""
    return _energy_ok(inp) and (not inp.needs_valves or inp.valves_ready)


def _transition(st: PlantFsmState, new_phase: PlantPhase, now: datetime, cfg: PlantFsmConfig) -> None:
    """Applica side-effect di cambio fase: reset timer/deadline.

    stall_triggered_restart NON viene azzerato qui: persiste attraverso
    STOPPING→OFF→STARTING finché energy_ok non torna (vedi _handle_starting).
    """
    st.phase = new_phase
    st.entered_at = now
    st.start_deadline = None
    st.stop_deadline = None
    st.energy_stall_since = None
    if new_phase == PlantPhase.STARTING:
        st.start_deadline = now + timedelta(seconds=float(cfg.start_timeout_s))
    if new_phase == PlantPhase.STOPPING:
        st.stop_deadline = now + timedelta(seconds=float(cfg.stop_timeout_s))


_HandlerResult = tuple[Optional[PlantPhase], list[str]]


def _handle_off(st: PlantFsmState, inp: PlantFsmInputs, cfg: PlantFsmConfig, elapsed: float) -> _HandlerResult:
    if not inp.request_on:
        return None, []
    if elapsed >= float(cfg.min_off_s):
        return PlantPhase.STARTING, []
    return None, ["min_off_hold"]


def _handle_starting(st: PlantFsmState, inp: PlantFsmInputs, cfg: PlantFsmConfig, elapsed: float) -> _HandlerResult:
    if not inp.request_on:
        return PlantPhase.STOPPING, []

    reasons: list[str] = []

    # Risoluzione stall_triggered_restart: indipendente dal timeout, deve
    # sempre resettarsi quando l'energia torna disponibile.
    if st.stall_triggered_restart:
        if _energy_ok(inp):
            st.stall_triggered_restart = False
            reasons.append("stall_restart_energy_ok")
        else:
            reasons.append("stall_restart_wait_energy")

    # PRIORITÀ: ready_to_run valutato PRIMA del timeout (vedi rationale
    # originale su race condition tick/deadline).
    if _ready_to_run(inp):
        return PlantPhase.RUNNING, reasons
    if st.start_deadline is not None and inp.now > st.start_deadline:
        reasons.append("start_timeout")
        return PlantPhase.FAULT, reasons
    return None, reasons


def _handle_running(st: PlantFsmState, inp: PlantFsmInputs, cfg: PlantFsmConfig, elapsed: float) -> _HandlerResult:
    # Ordine di priorità: request_on PRIMA di energy_ok (vedi rationale
    # originale — spegnimento volontario non deve attendere stall timeout).
    if not inp.request_on:
        if elapsed >= float(cfg.min_on_s):
            return PlantPhase.STOPPING, []
        return None, ["min_on_hold"]

    if not _energy_ok(inp):
        if st.energy_stall_since is None:
            st.energy_stall_since = inp.now
        stall_s = (inp.now - st.energy_stall_since).total_seconds()
        if stall_s >= float(cfg.energy_stall_timeout_s):
            st.stall_triggered_restart = True
            return PlantPhase.STOPPING, [f"energy_stall:{stall_s:.0f}s"]
        return None, [f"energy_stall_wait:{stall_s:.0f}s/{cfg.energy_stall_timeout_s:.0f}s"]

    if st.energy_stall_since is not None:
        st.energy_stall_since = None
    return None, []


def _handle_stopping(st: PlantFsmState, inp: PlantFsmInputs, cfg: PlantFsmConfig, elapsed: float) -> _HandlerResult:
    if st.stop_deadline is None:
        st.stop_deadline = inp.now + timedelta(seconds=float(cfg.stop_timeout_s))
    if inp.request_on and elapsed >= float(cfg.stop_timeout_s):
        return PlantPhase.STARTING, []
    if st.stop_deadline is not None and inp.now >= st.stop_deadline:
        return PlantPhase.OFF, []
    return None, []


def _handle_fault(st: PlantFsmState, inp: PlantFsmInputs, cfg: PlantFsmConfig, elapsed: float) -> _HandlerResult:
    # Recupero automatico dopo dwell minimo: il FAULT da timeout di avvio
    # non è un guasto hardware — è un cold-start lento o una condizione
    # transitoria. Un FAULT da allarme hardware esplicito deve essere gestito
    # a livello superiore (faults_present nello snapshot) PRIMA di fsm_step.
    if elapsed >= float(cfg.min_off_s):
        return PlantPhase.OFF, []
    return None, []


_TRANSITION_HANDLERS: dict[PlantPhase, Callable[[PlantFsmState, PlantFsmInputs, PlantFsmConfig, float], _HandlerResult]] = {
    PlantPhase.OFF: _handle_off,
    PlantPhase.STARTING: _handle_starting,
    PlantPhase.RUNNING: _handle_running,
    PlantPhase.STOPPING: _handle_stopping,
    PlantPhase.FAULT: _handle_fault,
}


@dataclass(slots=True, frozen=True)
class _PhaseGating:
    allow_valves: bool
    allow_pumps: bool


# Gating base per fase. STARTING ha eccezioni applicate in _build_output
# (soppressione valvole su stall restart / PDC nota spenta) — vedi quel punto
# per il rationale di sicurezza (niente acqua fredda in circolo senza energia).
_BASE_GATING: dict[PlantPhase, _PhaseGating] = {
    PlantPhase.OFF: _PhaseGating(allow_valves=False, allow_pumps=False),
    PlantPhase.STARTING: _PhaseGating(allow_valves=True, allow_pumps=False),
    PlantPhase.RUNNING: _PhaseGating(allow_valves=True, allow_pumps=True),
    PlantPhase.STOPPING: _PhaseGating(allow_valves=False, allow_pumps=False),
    PlantPhase.FAULT: _PhaseGating(allow_valves=False, allow_pumps=False),
}


def _build_output(st: PlantFsmState, inp: PlantFsmInputs, reasons: list[str]) -> PlantFsmOutput:
    phase = st.phase
    gating = _BASE_GATING[phase]
    allow_valves = gating.allow_valves

    if phase == PlantPhase.STARTING:
        # Eccezione 1: restart dopo stall energetico persistente — valvole
        # chiuse finché energy_ok non torna.
        if st.stall_triggered_restart:
            allow_valves = False
            reasons = reasons + ["valves_suppressed:stall_restart"]
        # Eccezione 2: PDC nota spenta — non aprire valvole senza fonte di
        # energia, protegge anche il primo tick di STARTING prima che
        # stall_triggered_restart sia eventualmente impostato.
        elif _pdc_known_off(inp):
            allow_valves = False
            reasons = reasons + ["valves_suppressed:pdc_off"]

    return PlantFsmOutput(
        phase=phase,
        plant_on=phase in (PlantPhase.STARTING, PlantPhase.RUNNING),
        allow_valves=allow_valves,
        allow_pumps=gating.allow_pumps,
        force_close_valves=not allow_valves,
        force_pumps_off=not gating.allow_pumps,
        reasons=reasons or [phase.value],
    )


def fsm_step(stage: StagingState, *, inp: PlantFsmInputs, cfg: PlantFsmConfig) -> PlantFsmOutput:
    """Esegue uno step della FSM di plant e restituisce un gating "forte".

    Filosofia
    ---------
    - La FSM NON decide setpoint o strategia: decide cosa e *lecito attuare*
      (valvole/pompe) in base a readiness e timer.
    - Separare `requested` da `observed` riduce ambiguita e debug time.
    - Dispatch per fase: ogni `_handle_X` è una funzione pura testabile in
      isolamento; questa funzione orchestra dispatch + applicazione del
      cambio di fase (side-effect centralizzato in `_transition`).
    """
    st = stage.fsm
    now = inp.now

    if st.entered_at is None:
        st.entered_at = now

    elapsed = (now - st.entered_at).total_seconds()

    handler = _TRANSITION_HANDLERS[st.phase]
    new_phase, reasons = handler(st, inp, cfg, elapsed)

    if new_phase is not None and new_phase != st.phase:
        reasons = [f"phase:{st.phase.value}->{new_phase.value}"] + reasons
        _transition(st, new_phase, now, cfg)

    st.last_request_on = bool(inp.request_on)

    return _build_output(st, inp, reasons)
