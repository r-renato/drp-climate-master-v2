from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .model import PlantFsmOutput, PlantFsmState, PlantPhase, StagingState


def fsm_valve_gate(
    fsm_state: "PlantFsmState",
    *,
    pdc_effective_on: Optional[bool] = None,
) -> tuple[bool, bool]:
    """Query pura: restituisce (allow_valves, force_close_valves) per la fase corrente.

    Non muta alcuno stato.

    Regola base: le valvole sono consentite in STARTING e RUNNING.
    In STOPPING, OFF e FAULT → chiusura forzata (fail-safe).

    Eccezioni in STARTING — valvole soppresse se:
      1) stall_triggered_restart=True: restart dopo stall energetico,
         valvole chiuse finché energy_ok (e quindi pdc_effective_on) non torna.
      2) pdc_effective_on=False (PDC nota spenta): non ha senso aprire valvole
         se non c'è energia disponibile. Protegge dal primo tick di STARTING
         in cui stall_triggered_restart potrebbe non essere ancora impostato.

    L'argomento pdc_effective_on deve essere None se lo stato PDC non è noto
    (sensore assente), in quel caso non viene applicato nessun gate aggiuntivo.
    """
    phase = fsm_state.phase
    allow = phase in (PlantPhase.STARTING, PlantPhase.RUNNING)

    if phase == PlantPhase.STARTING:
        # Caso 1: restart dopo stall energetico persistente.
        if fsm_state.stall_triggered_restart:
            allow = False
        # Caso 2: PDC nota spenta (stato noto = pdc_effective_on non è None).
        # Non aprire valvole senza fonte di energia: non si fa circolare
        # acqua fredda nei pannelli radianti se la PDC non sta erogando.
        elif pdc_effective_on is False:
            allow = False

    force_close = not allow
    return allow, force_close


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
        st.energy_stall_since = None  # reset ad ogni cambio di fase
        # stall_triggered_restart NON viene azzerato qui: persiste
        # attraverso STOPPING→OFF→STARTING finché energy_ok non torna.
        if new_phase == PlantPhase.STARTING:
            st.start_deadline = now + timedelta(seconds=float(cfg.start_timeout_s))
        if new_phase == PlantPhase.STOPPING:
            st.stop_deadline = now + timedelta(seconds=float(cfg.stop_timeout_s))

    # Energia "credibile" disponibile (gating per RUNNING/STARTING).
    #
    # Prerequisito PDC (quando nota):
    #   Se la PDC è nota spenta (pdc_effective_known=True e pdc_effective_on=False),
    #   energy_ok è SEMPRE False indipendentemente dal boiler.
    #   Rationale: senza PDC attiva non c'è fonte di calore/freddo; il boiler
    #   non si scalderà mai da solo. Questo impedisce che RUNNING venga raggiunto
    #   con PDC spenta, anche quando boiler_ready=True per effetto dell'isteresi
    #   o di una soglia di readiness bassa (come boiler_ready_heat_bias_c=-12.0).
    #
    # Se PDC non nota (pdc_effective_known=False): nessun gate PDC.
    #   Utile per sistemi senza sensore di stato PDC.
    #
    # Se PDC nota accesa: usa boiler_ready (o pdc_effective_on come fallback).
    pdc_known_off = inp.pdc_effective_known and not inp.pdc_effective_on
    if pdc_known_off:
        energy_ok = False
    elif inp.boiler_signal_available:
        energy_ok = bool(inp.boiler_ready)
    else:
        energy_ok = bool(inp.pdc_effective_on)

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
                if st.stall_triggered_restart:
                    if energy_ok:
                        # Energia tornata disponibile: rimuovi la soppressione
                        # valvole e riprendi il normale sequenziamento di avvio.
                        st.stall_triggered_restart = False
                        reasons.append("stall_restart_energy_ok")
                    else:
                        reasons.append("stall_restart_wait_energy")
                ready_to_run = bool(energy_ok and (not inp.needs_valves or inp.valves_ready))
                if ready_to_run:
                    transition(PlantPhase.RUNNING)

    elif st.phase == PlantPhase.RUNNING:
        # Ordine di priorità:
        # 1) request_on=False → STOPPING (sempre prioritario, anche senza energia)
        # 2) energy_ok=False  → stall tracking → STOPPING dopo timeout
        # 3) tutto ok          → RUNNING stabile
        #
        # IMPORTANTE: request_on va controllato PER PRIMO.
        # Se energy_ok venisse controllato prima, un utente che spegne l'HVAC
        # mentre il boiler è freddo non otterrebbe lo spegnimento immediato —
        # il sistema entrerebbe nello stall tracking e aspetterebbe fino a 600s.
        if not inp.request_on:
            # Spegnimento volontario o perdita di domanda: STOPPING normale.
            # min_on_s protegge da short-cycling delle pompe; con energy_ok=False
            # le pompe sono già bloccate, ma il timer resta per coerenza FSM.
            if elapsed_s() >= float(cfg.min_on_s):
                transition(PlantPhase.STOPPING)
            else:
                reasons.append("min_on_hold")
        elif not energy_ok:
            # Domanda attiva ma energia non disponibile (boiler freddo o PDC spenta).
            # Tracciamo da quando per rilevare uno stall prolungato.
            if st.energy_stall_since is None:
                st.energy_stall_since = now
            stall_s = (now - st.energy_stall_since).total_seconds()
            if stall_s >= float(cfg.energy_stall_timeout_s):
                # Stall energetico persistente: forza STOPPING per ripristinare
                # il ciclo di avvio. Quando BUG-3 sarà risolto (comando PDC
                # attivo), il ciclo si stabilizzerà normalmente in RUNNING.
                # Marca il restart come stall-triggered: in STARTING successivo
                # le valvole resteranno chiuse finché energy_ok non torna.
                transition(PlantPhase.STOPPING)
                st.stall_triggered_restart = True
                reasons.append(f"energy_stall:{stall_s:.0f}s")
            else:
                reasons.append(f"energy_stall_wait:{stall_s:.0f}s/{cfg.energy_stall_timeout_s:.0f}s")
        else:
            # Energia ok e domanda attiva: azzera eventuale stall pregresso.
            if st.energy_stall_since is not None:
                st.energy_stall_since = None

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
        # Recupero automatico dopo dwell minimo: il FAULT da timeout di avvio
        # non è un guasto hardware — è un cold-start lento o una condizione
        # transitoria. Si tenta il riavvio a prescindere da request_on.
        # Un FAULT da allarme hardware esplicito deve essere gestito a livello
        # superiore (faults_present nel snapshot) PRIMA di chiamare fsm_step.
        if elapsed_s() >= float(cfg.min_off_s):
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
