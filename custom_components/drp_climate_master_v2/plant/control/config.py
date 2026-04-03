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
    boiler_ready_cool_bias_c: float = -1.5
    fsm_min_on_s: float = 180.0
    fsm_min_off_s: float = 120.0
    fsm_start_timeout_s: float = 900.0
    fsm_stop_timeout_s: float = 30.0
    # Timeout stall energetico in RUNNING: se energy_ok=False persiste oltre questo
    # valore, la FSM forza la transizione a STOPPING per ripristinare il ciclo
    # di avvio. Deve essere inferiore a fsm_start_timeout_s per consentire
    # almeno un tentativo di ripartenza pulito.
    # Valore tipico: 600s (10 min). Abbassare a 300s per impianti con PDC
    # a risposta rapida; alzare a 900s se il boiler impiega molto a scaldarsi.
    fsm_energy_stall_timeout_s: float = 600.0

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
