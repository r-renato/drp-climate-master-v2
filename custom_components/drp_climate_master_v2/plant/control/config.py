from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PlantActuatorConfig:
    """Configurazione (tuning) dell'attuatore di plant.

    Scopo
    -----
    Centralizzare in un unico punto i parametri che governano:
    - staging idraulico (ritardi, readiness)
    - bias tra target di controllo e target di readiness (sensore pre-miscelatrice)
    - macchina a stati (FSM) per anti-chatter e timeouts
    """

    valve_open_delay_s: float = 95.0
    boiler_ready_on_margin_c: float = 0.5
    boiler_ready_off_margin_c: float = 1.5
    # Bias negativo: abbassa il target di readiness rispetto al target di controllo.
    # -12.0 permette RUNNING con boiler a 25°C (utile per cold-start).
    boiler_ready_heat_bias_c: float = -12.0
    # In raffrescamento il bias viene neutralizzato: la semantica è interamente
    # nei margini cooling-specifici (boiler_ready_cool_on/off_margin_c).
    # Con bias=0.0: t_ready_ref = t_target_ctrl, e i margini esprimono direttamente
    # lo scostamento dal target operativo.
    boiler_ready_cool_bias_c: float = 0.0
    # Margine ON cooling: la pompa parte quando t_boiler <= t_target + on_margin.
    # Valore 1.0°C: consente l'avvio con acqua ancora 1°C sopra il target
    # (fisicamente: il PDC raffredda attivamente, l'acqua scenderà ulteriormente
    # dopo l'avvio della pompa). Evita di attendere il raggiungimento esatto
    # del target prima di avviare la circolazione.
    boiler_ready_cool_on_margin_c: float = 1.0
    # Margine OFF cooling: la pompa si ferma solo quando t_boiler > t_target + off_margin.
    # Valore 3.5°C: con carico termico parziale (1-3 zone aperte su 6) e PDC attiva
    # il boiler non dovrebbe mai raggiungere t_target+3.5°C → boiler_ready resta True
    # per tutta la durata del ciclo → nessun energy_stall spurio.
    # Isteresi effettiva: off_margin - on_margin = 2.5°C (vs 0°C attuale).
    boiler_ready_cool_off_margin_c: float = 3.5
    # Tempo minimo di permanenza in RUNNING prima di poter transitare a STOPPING
    # per calo della domanda (request_on=False). Allineato alle specifiche §5.2
    # (MIN_ON_TIME_MINUTES=10-15 min). Con impianto radiante a soffitto e boiler
    # ad accumulo, 3 min (180s) erano insufficienti: il ciclo termico dell'impianto
    # richiede almeno 10 min di funzionamento stabile per trasferire calore/freddo
    # significativo ai pannelli. Valore conservativo: 600s (10 min).
    fsm_min_on_s: float = 600.0
    # Tempo minimo in OFF prima di poter ripartire. Allineato alle specifiche §5.2
    # (MIN_OFF_TIME_MINUTES=5-10 min). Protegge la PDC da short-cycling meccanico
    # e consente al boiler di stabilizzarsi prima del ciclo successivo.
    # Valore conservativo: 300s (5 min).
    fsm_min_off_s: float = 300.0
    fsm_start_timeout_s: float = 900.0
    fsm_stop_timeout_s: float = 30.0
    # Timeout stall energetico in RUNNING: se energy_ok=False persiste oltre questo
    # valore, la FSM forza la transizione a STOPPING per ripristinare il ciclo
    # di avvio. Deve essere inferiore a fsm_start_timeout_s per consentire
    # almeno un tentativo di ripartenza pulito.
    # Aermec HMI080 = PDC a risposta rapida: si usa il valore basso 300s.
    # Con BUG-3 attivo (comando PDC commentato) l'energy_stall è strutturale:
    # abbassare il timeout riduce il tempo sprecato in RUNNING senza pompa.
    # Invariante: energy_stall_timeout_s < fsm_start_timeout_s (900s). ✓
    fsm_energy_stall_timeout_s: float = 300.0

    def validate(self) -> None:
        """Valida i parametri di configurazione."""
        if self.valve_open_delay_s < 0:
            raise ValueError("valve_open_delay_s deve essere >= 0")

        if self.boiler_ready_on_margin_c < 0:
            raise ValueError("boiler_ready_on_margin_c deve essere >= 0")
        if self.boiler_ready_off_margin_c < 0:
            raise ValueError("boiler_ready_off_margin_c deve essere >= 0")
        if self.boiler_ready_off_margin_c < self.boiler_ready_on_margin_c:
            raise ValueError("boiler_ready_off_margin_c deve essere >= boiler_ready_on_margin_c")

        if self.boiler_ready_cool_on_margin_c < 0:
            raise ValueError("boiler_ready_cool_on_margin_c deve essere >= 0")
        if self.boiler_ready_cool_off_margin_c <= self.boiler_ready_cool_on_margin_c:
            raise ValueError(
                "boiler_ready_cool_off_margin_c deve essere > boiler_ready_cool_on_margin_c"
            )

        if self.fsm_min_on_s < 0:
            raise ValueError("fsm_min_on_s deve essere >= 0")
        if self.fsm_min_off_s < 0:
            raise ValueError("fsm_min_off_s deve essere >= 0")
        if self.fsm_start_timeout_s <= 0:
            raise ValueError("fsm_start_timeout_s deve essere > 0")
        if self.fsm_stop_timeout_s <= 0:
            raise ValueError("fsm_stop_timeout_s deve essere > 0")
        if self.fsm_energy_stall_timeout_s <= 0:
            raise ValueError("fsm_energy_stall_timeout_s deve essere > 0")
        if self.fsm_energy_stall_timeout_s >= self.fsm_start_timeout_s:
            raise ValueError(
                "fsm_energy_stall_timeout_s deve essere < fsm_start_timeout_s "
                "(altrimenti lo stall non verrebbe mai rilevato prima del timeout di avvio)"
            )
