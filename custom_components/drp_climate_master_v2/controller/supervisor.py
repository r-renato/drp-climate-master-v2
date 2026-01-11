# custom_components/drp_climate_master_v2/controller/supervisor.py
"""
DRP Climate Master v2 — SUPERVISOR
==================================

RUOLO (in breve)
----------------
Il Supervisor è il **cervello decisionale**: implementa la **state machine** dell’impianto
(OFF, IDLE, HEAT, COOL_DRY, COOL_VMC_ONLY, FREE_COOLING, FAULT), determina la modalità
HVAC effettiva e coordina gli attuatori tramite gli Adapters. Fornisce a `climate.py`
gli stati esposti in UI (`hvac_mode`, `hvac_action`, `preset/profile`).

RESPONSABILITÀ PRINCIPALI
-------------------------
1) **Decisione high-level**:
   - Valuta domanda di riscaldamento/raffrescamento, rischio condensa, possibilità di free-cooling.
   - Seleziona la modalità operativa: HEAT, COOL_DRY (radianti + deumidifica), COOL_VMC_ONLY,
     FREE_COOLING, o IDLE/FAULT, in base a target, profilo operativo e vincoli di sicurezza.

2) **Orchestrazione attuatori**:
   - Abilita/disabilita PDC, comanda valvole di zona secondo `zone_demand`, avvia/ferma pompe.
   - Imposta target di supply (caldo/freddo) e strategia VMC (compressore/acqua/ibrido).
   - Garantisce coerenza tra forzature (es. free-cooling → niente compressore).

3) **Profili & servizi**:
   - Gestisce i **preset** (OperatingProfile: comfort/eco/boost/away/vacation) e i servizi
     di dominio (es. `set_profile`, `set_cooling_strategy`, `calibrate_mixing_valve`).
   - Applica offset/strategie in funzione del profilo (estendibile).

4) **Sicurezza & fail-safe**:
   - Entra in **FAULT** quando necessario: esclude raffrescamento radiante e usa VMC in
     modalità “dry safe” se l’umidità è alta; rientra con rampa controllata.
   - Rispetta `min_on/min_off` delegando l’enforcement agli Adapters/Coordinator.

5) **Reattività agli eventi**:
   - Si sottoscrive agli update del Coordinator (SLOW loop) e decide non appena cambia lo
     snapshot; sincronizza l’UI chiamando `async_set_updated_data`.

INTERAZIONI
-----------
- Legge lo **snapshot** dal Coordinator e la **configurazione** (PlantConfig).
- Comanda tutto **solo** tramite Adapters (mai servizi diretti HA).
- Espone a `climate.py` le proprietà correnti (`current_hvac_mode`, `current_hvac_action`,
  `current_profile`) e metodi per cambiare modalità/target/strategie.

CICLO DI VITA
-------------
- `async_start()` → registra listener al Coordinator e avvia il primo ciclo decisionale.
- `async_stop()` → rimuove listener e chiude eventuali task interni.
- I metodi `async_set_*` (mode/profile/target/strategy) schedulano un nuovo ciclo decisionale.

INVARIANTI & LINEE GUIDA
------------------------
- Le decisioni sono **idempotenti**: ripetere lo stesso stato non deve generare flood di comandi.
- Non blocca mai il thread di evento; tutte le operazioni sono `async`.
- La logica di sicurezza (anticondensa, interlock) ha **priorità** su comfort/risparmio.
- Nessuna manipolazione diretta di entità: gli Adapters gestiscono backoff, retry e clamp.

ANTI-PATTERN (da evitare)
-------------------------
- Inserire algoritmi numerici “di regolazione” (PID) nel Supervisor → restano nel Coordinator.
- Spostare qui calcoli di psicrometria o stima domanda → li fornisce il Coordinator/snapshot.
- Aggirare gli Adapters per inviare comandi.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.core import HomeAssistant

from ..domain.enums import HVACOperatingProfile

from ..controller.coordinator import ClimateCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _State:
    """Stato corrente (esposto a ClimateEntity)."""
    hvac_mode: HVACMode = HVACMode.AUTO
    hvac_action: HVACAction = HVACAction.IDLE
    hvac_profile: Optional[HVACOperatingProfile] = None
   #  target_temp_c: float = 22.0
   #  cooling_strategy: CoolingStrategy = CoolingStrategy.FIRST_WATER_THEN_COMPRESSOR


class ClimateSupervisor:
    """
    Supervisore: decide la modalità dell'impianto e invia comandi agli adapters.
    - Reagisce agli update del DataUpdateCoordinator (SLOW loop)
    - Avvia il FAST loop del coordinator (PID miscelatrice, PID umidità VMC)
    - Mantiene coerenza di sicurezza (anticondensa, short-cycle demandato agli adapters)
    """

    def __init__(self, hass: HomeAssistant, coordinator: ClimateCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.state = _State(
            hvac_mode=HVACMode.AUTO,
            hvac_action=HVACAction.IDLE,
            # profile=self.coordinator.adapters.get_operating_profile() or OperatingProfile.COMFORT,
            # target_temp_c=22.0,
            # cooling_strategy=CoolingStrategy.FIRST_WATER_THEN_COMPRESSOR,
        )

        self._unsub_coordinator: Optional[Callable[[], None]] = None
        self._task_decider: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        _LOGGER.info("component initialized.")

    @property
    def current_hvac_mode(self) -> HVACMode:
        return self.state.hvac_mode

    @property
    def current_hvac_action(self) -> HVACAction:
        return self.state.hvac_action
    
    async def async_start(self) -> None:
        if self._unsub_coordinator is None:
            self._unsub_coordinator = self.coordinator.async_add_listener(self._on_coordinator_update)
        # await self.coordinator.async_start_fast_loop()
        self._schedule_decider()
      #   _LOGGER.info("Supervisor started with hvac_mode=%s, profile=%s, target=%.1f°C",
      #                self.state.hvac_mode, self.state.profile, self.state.target_temp_c)

    async def async_stop(self):
        """..."""

    def _schedule_decider(self) -> None:
      """..."""
      #   if self._task_decider and not self._task_decider.done():
      #       return
      #   self._task_decider = self.hass.async_create_task(self._decide_and_act(), name="drp_decider")

    def _on_coordinator_update(self) -> None:
      """..."""
      #   self._schedule_decider()

