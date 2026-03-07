from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .model import PlantFsmOutput, PlantPhase, StagingState


@dataclass(slots=True, frozen=True)
class PlantFsmConfig:
    """Parametri della FSM (anti-chatter e timeout)."""

    min_on_s: float
    min_off_s: float
    start_timeout_s: float
    stop_timeout_s: float


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
    compressor_on: Optional[bool]

    boiler_ready: bool
    boiler_signal_available: bool

    valves_ready: bool
    needs_valves: bool


def fsm_step(stage: StagingState, *, inp: PlantFsmInputs, cfg: PlantFsmConfig) -> PlantFsmOutput:
    """Esegue uno step della FSM di plant e restituisce un gating "forte".

    Filosofia
    ---------
    - La FSM NON decide setpoint o strategia: decide cosa e *lecito attuare*
      (valvole/pompe) in base a readiness e timer.
    - Separare `requested` da `observed` riduce ambiguita e debug time.
    """
    st = stage.fsm
    reasons: list[str] = []

    now = inp.now

    if st.entered_at is None:
        st.entered_at = now

    def elapsed_s() -> float:
        return (now - (st.entered_at or now)).total_seconds()

    def transition(new_phase: PlantPhase) -> None:
        nonlocal reasons
        if st.phase != new_phase:
            reasons.append(f"phase:{st.phase.value}->{new_phase.value}")
        st.phase = new_phase
        st.entered_at = now
        st.start_deadline = None
        st.stop_deadline = None
        if new_phase == PlantPhase.STARTING:
            st.start_deadline = now + timedelta(seconds=float(cfg.start_timeout_s))
        if new_phase == PlantPhase.STOPPING:
            st.stop_deadline = now + timedelta(seconds=float(cfg.stop_timeout_s))

    # Energia "credibile" disponibile (gating per RUNNING):
    # - se sensori boiler disponibili: usa boiler_ready
    # - altrimenti: usa lo stato osservato della PDC
    energy_ok = bool(inp.boiler_ready) if inp.boiler_signal_available else bool(inp.pdc_effective_on)

    # --- Transizioni ---
    if st.phase == PlantPhase.OFF:
        if inp.request_on:
            if elapsed_s() >= float(cfg.min_off_s):
                transition(PlantPhase.STARTING)
            else:
                reasons.append("min_off_hold")

    elif st.phase == PlantPhase.STARTING:
        if not inp.request_on:
            transition(PlantPhase.STOPPING)
        else:
            if st.start_deadline is not None and now > st.start_deadline:
                transition(PlantPhase.FAULT)
                reasons.append("start_timeout")
            else:
                ready_to_run = bool(energy_ok and (not inp.needs_valves or inp.valves_ready))
                if ready_to_run:
                    transition(PlantPhase.RUNNING)

    elif st.phase == PlantPhase.RUNNING:
        if not inp.request_on:
            if elapsed_s() >= float(cfg.min_on_s):
                transition(PlantPhase.STOPPING)
            else:
                reasons.append("min_on_hold")

    elif st.phase == PlantPhase.STOPPING:
        # Attendi una finestra breve prima di dichiararti OFF.
        if st.stop_deadline is None:
            st.stop_deadline = now + timedelta(seconds=float(cfg.stop_timeout_s))
        if inp.request_on and elapsed_s() >= float(cfg.stop_timeout_s):
            # ripartenza dopo una breve finestra di stop
            transition(PlantPhase.STARTING)
        elif st.stop_deadline is not None and now >= st.stop_deadline:
            transition(PlantPhase.OFF)

    elif st.phase == PlantPhase.FAULT:
        # Recupero solo quando richiesta OFF e dwell minimo.
        if not inp.request_on and elapsed_s() >= float(cfg.min_off_s):
            transition(PlantPhase.OFF)

    st.last_request_on = bool(inp.request_on)

    # --- Output / gating forte ---
    phase = st.phase

    if phase == PlantPhase.OFF:
        return PlantFsmOutput(
            phase=phase,
            plant_on=False,
            allow_valves=False,
            allow_pumps=False,
            force_close_valves=True,
            force_pumps_off=True,
            reasons=reasons or ["off"],
        )

    if phase == PlantPhase.STARTING:
        # Scelta progettuale:
        # - in STARTING apriamo/gestiamo le valvole verso il desiderato il prima possibile
        #   (le elettrovalvole sono lente e fanno parte del tempo di avvio).
        # - evitiamo di far partire le pompe finche non siamo in RUNNING (gating forte).
        allow_valves = True
        return PlantFsmOutput(
            phase=phase,
            plant_on=True,
            allow_valves=allow_valves,
            allow_pumps=False,
            force_close_valves=False,
            force_pumps_off=True,
            reasons=reasons or ["starting"],
        )

    if phase == PlantPhase.RUNNING:
        return PlantFsmOutput(
            phase=phase,
            plant_on=True,
            allow_valves=True,
            allow_pumps=True,
            force_close_valves=False,
            force_pumps_off=False,
            reasons=reasons or ["running"],
        )

    if phase == PlantPhase.STOPPING:
        return PlantFsmOutput(
            phase=phase,
            plant_on=False,
            allow_valves=False,
            allow_pumps=False,
            force_close_valves=True,
            force_pumps_off=True,
            reasons=reasons or ["stopping"],
        )

    # FAULT
    return PlantFsmOutput(
        phase=phase,
        plant_on=False,
        allow_valves=False,
        allow_pumps=False,
        force_close_valves=True,
        force_pumps_off=True,
        reasons=reasons or ["fault"],
    )
