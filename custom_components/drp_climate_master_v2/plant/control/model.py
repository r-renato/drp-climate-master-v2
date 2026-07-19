from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


@dataclass(slots=True)
class BoilerReadinessDebug:
    """Dati diagnostici della logica di *boiler readiness*.

    Questa struttura sostituisce il precedente `dict[str, Optional[float]]` per:
    - evitare chiavi stringa fragili
    - rendere espliciti i significati termotecnici (soglie ON/OFF)

    Attributi
    ---------
    t_boiler_supply_c:
        Temperatura attuale di mandata (lato impianto / buffer) usata come proxy di prontezza.
    t_target_c:
        Target di temperatura acqua (WOT o target mandata) ricavato dalla decisione.
    on_thr_c / off_thr_c:
        Soglie di isteresi per l'aggiornamento di `boiler_ready`.
    """

    t_boiler_supply_c: Optional[float]
    t_target_c: Optional[float]
    on_thr_c: Optional[float]
    off_thr_c: Optional[float]


@dataclass(slots=True)
class BoilerReadinessUpdate:
    """Risultato dell'aggiornamento dello stato di prontezza dell'acqua (boiler/buffer)."""

    ready: bool
    debug: BoilerReadinessDebug


@dataclass(slots=True)
class ZoneValvesDesired:
    """Stato desiderato delle valvole di zona (zona -> ON/OFF).

    Incapsula la mappa per evitare di passare `dict` grezzi tra funzioni e per aggiungere
    utilità (conteggi, proprietà) in modo tipizzato.
    """

    by_zone: dict[str, bool] = field(default_factory=dict)

    @property
    def on_count(self) -> int:
        """Numero di zone richieste ON."""
        return sum(1 for v in self.by_zone.values() if v)

    @property
    def any_open(self) -> bool:
        """True se almeno una zona è richiesta ON."""
        return self.on_count > 0


@dataclass(slots=True)
class ZoneValvesStats:
    """Statistiche/diagnostica per lo staging delle elettrovalvole."""

    zones_total: int
    zones_on: int
    requested_at: Optional[datetime]
    elapsed_s: Optional[float]
    opening_transition: bool


@dataclass(slots=True)
class ZoneValvesActuationResult:
    """Risultato dello step valvole: stato desiderato + valutazione 'ready'."""

    ready: bool
    desired: ZoneValvesDesired
    stats: ZoneValvesStats


@dataclass(slots=True)
class ValveCommand:
    """Comando di attuazione per una singola elettrovalvola."""

    area_name: str
    zone_key: str
    valve_switch: str
    state: bool


@dataclass(slots=True)
class ZoneValvesPlan:
    """Piano di attuazione per le elettrovalvole (calcolo puro + comandi necessari)."""

    result: ZoneValvesActuationResult
    commands: list[ValveCommand] = field(default_factory=list)


@dataclass(slots=True)
class SupplyActuationResult:
    """Risultato dello step (pompe/miscelatrice)."""

    direct_on: bool
    adj_on: bool
    mix_valve_pct_applied: Optional[float]


class PlantPhase(str, Enum):
    """Fase logica della plant (FSM sopra lo staging idraulico).

    I valori OFF..FAULT guidano la FSM (fsm_step).
    VMC_ONLY è esclusivamente diagnostico: indica che il ciclo di staging
    idraulico è stato bypassato (impianto fermo, solo VMC attiva).
    Non è mai prodotto da fsm_step, ma compare nei log dell'attuatore.
    """

    # L'impianto è spento (staging idraulico fermo, valvole chiuse, pompe spente, VMC spenta).
    # Tutti i comandi di attuazione sono soppressi.
    OFF = "off"

    # L'impianto sta partendo (staging idraulico in transizione, valvole e pompe in apertura).
    # In caso di sola ventilazione (vent_only / iaq_only) la fase viene saltata e si passa direttamente a RUNNING.
    # Tutti i comandi di attuazione sono permessi. 
    STARTING = "starting"

    # L'impianto è in funzione (staging idraulico attivo, valvole e pompe aperte oppure è attiva solo la VMC).
    RUNNING = "running"

    # L'impianto sta fermarsi (staging idraulico in transizione, valvole e pompe in chiusura).
    STOPPING = "stopping"

    UNDEFINED = "undefined"
    FAULT = "fault"
    LOCKOUT = "lockout"  # Blocco temporizzato (anti-cycling, dew-guard §5.2 / §6)
    DEGRADED = "degraded"  # Operazione parziale (§6): VMC KO, pompa KO, sensore mancante
    MAINTENANCE = "maintenance"  # Blocco volontario per intervento tecnico (§6)

    VMC_ONLY = "vmc_only"  # Diagnostico: bypass staging, solo VMC attuata

@dataclass(slots=True)
class PlantFsmState:
    """Stato persistente della FSM di plant."""

    phase: PlantPhase = PlantPhase.OFF
    entered_at: Optional[datetime] = None
    start_deadline: Optional[datetime] = None
    stop_deadline: Optional[datetime] = None
    last_request_on: bool = False

    # Timestamp da cui energy_ok è diventato False in fase RUNNING.
    # Usato per rilevare lo stall energetico prolungato (PDC/boiler non disponibili)
    # e forzare la transizione a STOPPING dopo energy_stall_timeout_s.
    # None se energy_ok=True o se siamo fuori da RUNNING.
    energy_stall_since: Optional[datetime] = None

    # Flag impostato quando la transizione RUNNING→STOPPING è causata da energy_stall
    # (non da una richiesta utente). Indica che il restart è dovuto a mancanza di
    # energia (tipicamente BUG-3: PDC non comandata) e non a una domanda soddisfatta.
    # Effetto in STARTING: sopprime l'apertura valvole finché energy_ok non torna True.
    # Questo previene il ciclo apri/chiudi valvole con PDC spenta.
    # Si azzera quando energy_ok diventa True in STARTING, o al raggiungimento di RUNNING.
    stall_triggered_restart: bool = False

    # Scadenza blocco LOCKOUT (§6). None = non in LOCKOUT o lockout già scaduto.
    # Impostato dal handler che richiede il lockout; azzerato da _transition
    # quando si esce da LOCKOUT verso OFF.
    lockout_until: Optional[datetime] = None

    # Motivo del degrado corrente (§6 DEGRADED). None = nessun degrado attivo.
    # Aggiornato ad ogni tick da _handle_degraded; azzerato da _transition.
    degraded_reason: Optional[str] = None


@dataclass(slots=True)
class PlantFsmOutput:
    """Output della FSM usato come "gating forte" sull'attuazione."""

    phase: PlantPhase
    plant_on: bool
    allow_valves: bool
    allow_pumps: bool
    force_close_valves: bool
    force_pumps_off: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StagingState:
    """Stato interno (staging) per attuazione idronica + FSM."""

    boiler_ready: bool = False
    valves_open_request_ts: Optional[datetime] = None
    last_valves_desired: dict[str, bool] = field(default_factory=dict)
    fsm: PlantFsmState = field(default_factory=PlantFsmState)
    
    def clone(self, fsm: PlantFsmState | None = None) -> "StagingState":
        """Crea una copia dello stato di staging, con eventuale override della FSM.
        """
        if fsm is not None:
            return StagingState(
                boiler_ready=self.boiler_ready,
                valves_open_request_ts=self.valves_open_request_ts,
                last_valves_desired=deepcopy(self.last_valves_desired),
                fsm=fsm,
            )
        return deepcopy(self)

@dataclass(slots=True)
class PlantActuatorStatus:
    """Snapshot strutturato dello stato di avvio/staging (debug-friendly).

    Obiettivo
    ---------
    - Evitare log assemblati a mano dentro `actuator.py`
    - Rendere stabile e tipizzato il contenuto diagnostico
    - Centralizzare la formattazione in `__str__` per log multi-linea leggibili

    Nota concettuale
    ----------------
    - `fsm_phase`: fase *decisa* dalla FSM (fonte di verità per l'attuazione)
    - `observed_phase`: fase *stimata* da stati osservati (solo diagnostica)
    """

    timestamp: datetime
    fsm_phase: str
    observed_phase: str
    mode: str

    request_on: bool
    pdc_req_on: bool
    direct_desired: bool
    adj_desired: bool
    needs_valves: bool

    compressor_on: Optional[bool]
    pdc_effective_on: bool
    pdc_effective_known: bool

    boiler_signal_available: bool
    t_boiler_supply_c: Optional[float]
    target_ctrl_c: Optional[float]
    target_ready_c: Optional[float]
    boiler_ready: bool
    on_thr_c: Optional[float]
    off_thr_c: Optional[float]

    valves_zones_total: int
    valves_zones_on: int
    valves_ready: bool
    valves_requested_at: Optional[datetime]
    valves_elapsed_s: Optional[float]
    valves_opening_transition: bool
    desired_by_zone: dict[str, bool] = field(default_factory=dict)

    allow_valves: bool = False
    allow_pumps: bool = False
    force_close_valves: bool = False
    force_pumps_off: bool = False

    direct_on: bool = False
    adj_on: bool = False
    mix_valve_pct_applied: Optional[float] = None

    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        """Rappresentazione multi-linea per log/commissioning."""

        def fnum(x: Optional[float], nd: int = 2) -> str:
            return f"{float(x):.{nd}f}" if x is not None else "-"

        def fint(x: Optional[int | float]) -> str:
            return str(int(x)) if x is not None else "-"

        def fbool(b: object, on: str = "True", off: str = "False") -> str:
            return on if b is True else (off if b is False else "-")

        def fstr(s: Optional[str]) -> str:
            return s if s else "-"

        def flist(xs: list[str]) -> str:
            return " | ".join(xs) if xs else "-"

        def fdict_compact_any(d: dict[str, Any] | None, max_items: int = 10, max_val_chars: int = 48) -> str:
            """dict compatto tipo: k=v, ... con limite item e truncation valori."""
            if not d:
                return "-"
            items = sorted(d.items(), key=lambda kv: str(kv[0]))
            more = ""
            if len(items) > max_items:
                items = items[:max_items]
                more = f" (+{len(d) - max_items})"

            def sval(v: object) -> str:
                s = repr(v)
                return (s[: max_val_chars - 1] + "...") if len(s) > max_val_chars else s

            s = ", ".join(f"{k}={sval(v)}" for k, v in items)
            return s + more

        ts = self.timestamp.isoformat()
        lines: list[str] = [
            "",
            "Plant staging",
            f"  Timestamp          :: {ts}",
            f"  FSM phase          :: {fstr(self.fsm_phase)}",
            f"  Observed phase     :: {fstr(self.observed_phase)}",
            f"  Mode               :: {fstr(self.mode)}",
            "------------------------------------------------------------------",
            "Requests / inputs",
            f"  request_on         :: {fbool(self.request_on)}",
            f"  pdc_req_on         :: {fbool(self.pdc_req_on)}",
            f"  direct_desired     :: {fbool(self.direct_desired)}",
            f"  adj_desired        :: {fbool(self.adj_desired)}",
            f"  needs_valves       :: {fbool(self.needs_valves)}",
            f"  compressor_on      :: {fbool(self.compressor_on)}",
            f"  pdc_effective_on   :: {fbool(self.pdc_effective_on)}",
            f"  pdc_effective_known:: {fbool(self.pdc_effective_known)}",
            "------------------------------------------------------------------",
            "Boiler readiness",
            f"  signal_available   :: {fbool(self.boiler_signal_available)}",
            f"  t_boiler_supply    :: {fnum(self.t_boiler_supply_c, 2)} degC",
            f"  target_ctrl        :: {fnum(self.target_ctrl_c, 2)} degC",
            f"  target_ready       :: {fnum(self.target_ready_c, 2)} degC",
            f"  ready              :: {fbool(self.boiler_ready)}",
            f"  on_thr             :: {fnum(self.on_thr_c, 2)} degC",
            f"  off_thr            :: {fnum(self.off_thr_c, 2)} degC",
            "------------------------------------------------------------------",
            "Valves",
            f"  zones_total        :: {fint(self.valves_zones_total)}",
            f"  zones_on           :: {fint(self.valves_zones_on)}",
            f"  valves_ready       :: {fbool(self.valves_ready)}",
            f"  requested_at       :: {fstr(self.valves_requested_at.isoformat() if self.valves_requested_at else None)}",
            f"  elapsed_s          :: {fnum(self.valves_elapsed_s, 1)}",
            f"  opening_transition :: {fbool(self.valves_opening_transition)}",
            f"  desired_by_zone    :: {fdict_compact_any(self.desired_by_zone)}",
            "------------------------------------------------------------------",
            "FSM gating",
            f"  allow_valves       :: {fbool(self.allow_valves)}",
            f"  allow_pumps        :: {fbool(self.allow_pumps)}",
            f"  force_close_valves :: {fbool(self.force_close_valves)}",
            f"  force_pumps_off    :: {fbool(self.force_pumps_off)}",
            "------------------------------------------------------------------",
            "Supply (applied plan)",
            f"  direct_on          :: {fbool(self.direct_on)}",
            f"  adj_on             :: {fbool(self.adj_on)}",
            f"  mix_valve_pct      :: {fnum(self.mix_valve_pct_applied, 1)}",
            "------------------------------------------------------------------",
            f"Reasons             :: {flist(self.reasons)}",
        ]
        return "\n".join(lines)
