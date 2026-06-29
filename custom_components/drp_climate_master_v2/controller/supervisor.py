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

INVARIANTI & LINEE GUIDA
------------------------
- Le decisioni sono **idempotenti**: ripetere lo stesso stato non deve generare flood di comandi.
- Non blocca mai il thread di evento; tutte le operazioni sono `async`.
- La logica di sicurezza (anticondensa, interlock) ha **priorità** su comfort/risparmio.
- Nessuna manipolazione diretta di entità: gli Adapters gestiscono backoff, retry e clamp.

NOTE IMPLEMENTATIVE (scheduler)
-------------------------------
Questo Supervisor estende `DailyGatedSchedulerBase` per:
- schedulare un "tick" alla prossima scadenza (point-in-time)
- tentare l'esecuzione *al più* una volta per intervallo (persistito su storage)
- gestire correttamente unsubscribe/reschedule e dedup di run concorrenti

L'intervallo può essere 24h, 8 minuti, ecc. (qualunque `timedelta`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, Event, callback

from ..plant.control.actuator import PlantActuator
from ..plant.decision.zone.model import ZonesDecision

from ..plant.decision.contracts import PlantDecision, PlantMode
from ..plant.decision.planner import PlantDecisionPlanner
from ..plant.decision.context import DecisionDerivedInputs

from ..const import DOMAIN
from ..helpers.logger import log_debug, log_exception, log_info, log_warning
from ..helpers.diagnostics.dashboard import build_dashboard, render_dashboard_text
from ..helpers.scheduler import IntervalGatedSchedulerBase
from ..helpers.utils import as_float, as_int

from ..domain.enums import HVACOperatingProfile
from .coordinator import ClimateCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _State:
    """
    Stato corrente esposto a ClimateEntity.
    I setter centralizzano validazione e rendono espliciti i contratti
    sui valori ammessi per ciascun campo.
    """
    _hvac_mode: HVACMode = field(default=HVACMode.OFF)
    _hvac_action: HVACAction = field(default=HVACAction.IDLE)
    _hvac_profile: HVACOperatingProfile | None = field(default=None)

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @hvac_mode.setter
    def hvac_mode(self, value: HVACMode) -> None:
        if not isinstance(value, HVACMode):
            raise TypeError(f"hvac_mode deve essere HVACMode, ricevuto {type(value)}")
        self._hvac_mode = value

    @property
    def hvac_action(self) -> HVACAction:
        return self._hvac_action

    @hvac_action.setter
    def hvac_action(self, value: HVACAction) -> None:
        if not isinstance(value, HVACAction):
            raise TypeError(f"hvac_action deve essere HVACAction, ricevuto {type(value)}")
        self._hvac_action = value

    @property
    def hvac_profile(self) -> HVACOperatingProfile | None:
        return self._hvac_profile

    @hvac_profile.setter
    def hvac_profile(self, value: HVACOperatingProfile | None) -> None:
        self._hvac_profile = value


class ClimateSupervisor(IntervalGatedSchedulerBase):
    """Supervisore: decide la modalità dell'impianto e invia comandi agli adapters.

    Scheduling
    - Usa `IntervalGatedSchedulerBase` come scheduler persistito (gating su intervallo).
    - Tutti i trigger (coordinator updates, bootstrap, ecc.) chiamano `async_run_if_due()`.
    - Il lavoro vero è implementato in `_async_on_due()`.

    Nota
    - Non accedere direttamente a membri protetti del base (es. `_run_task`).
      Usa le API (`async_run_if_due`, `async_start`, `async_stop`).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ClimateCoordinator,
        *,
        decision_interval: timedelta = timedelta(minutes=5),
    ) -> None:
        super().__init__(
            hass,
            store_key=f"{DOMAIN}.supervisor.{coordinator.entry_id}",
            daily_interval=decision_interval,
            logger=_LOGGER,
        )

        self._hass = hass
        self._coordinator = coordinator

        self._entry_id = coordinator.entry_id
        self._unit_system = coordinator.unit_system
        self._instance_id: str = f"{id(self):x}"

        self._state = _State()

        self._plant_decision_planner: PlantDecisionPlanner = PlantDecisionPlanner()
        
        # Comfort-band engine (policy + layer) cached at supervisor level.
        # Computed bands are *decision-time derived inputs* (not stored in PlantSnapshot).
        # self._comfort_policy_cfg, self._comfort_policy_layer = build_comfort_engine()
        self._plant_actuator = PlantActuator(hass=self._hass, runtime_cfg=coordinator.runtime_config)
        
        # self._engine = ControlEngine(hass=hass, coordinator=coordinator)
        # self._plant_engine = PlantControlEngine(
        #     hass=self._hass,
        #     runtime=self._coordinator.runtime_config,
        #     enabled=False,
        # )

        self._last_plant_decision: PlantDecision | None = None
        self._last_zones_decision: ZonesDecision | None = None

        self._unsub_coordinator: Optional[Callable[[], None]] = None

        # Decision concurrency guard (se il job parte da tick e da evento, l'engine non gira in parallelo)
        self._decider_lock = asyncio.Lock()

        # Stop flag
        self._stop_event = asyncio.Event()

        # Start when HA is ready
        self._unsub_hastarted_event: Optional[Callable[[], None]] = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
        )

        log_info(
            _LOGGER,
            "Initialized (id=%s) entry=%s unit=%s interval=%s",
            hex(id(self)),
            self._entry_id,
            self._unit_system,
            decision_interval,
        )

    # -----------------------------
    # Exposed properties
    # -----------------------------

    @property
    def current_hvac_mode(self) -> HVACMode:
        return self._state.hvac_mode

    @property
    def current_hvac_action(self) -> HVACAction:
        return self._state.hvac_action

    @property
    def current_profile(self) -> Optional[HVACOperatingProfile]:
        return self._state.hvac_profile

    @property
    def last_plant_decision(self) -> PlantDecision | None:
        return self._last_plant_decision

    def set_preset_mode(self, preset_mode: HVACOperatingProfile) -> None:
        self._state.hvac_profile = preset_mode

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._state.hvac_mode = hvac_mode

    # -----------------------------
    # HA lifecycle
    # -----------------------------

    @callback
    def _on_ha_started(self, event: Event) -> None:
        """Boot hook: start supervisor and arm scheduler only after HA STARTED."""
        self._hass.async_create_task(self.async_start(), name=f"drp_supervisor_start:{self._instance_id}")

    async def async_start(self, *, run_immediately: bool = True) -> None:
        """Start supervisor.

        - subscribe coordinator listener
        - start persisted scheduler (meta load + next tick)
        - optionally trigger a first decision run if due
        """
        if self._stop_event.is_set():
            return

        # Allinea _state con il coordinator all'avvio
        if self._coordinator.current_hvac_mode is not None:
            self._state.hvac_mode = self._coordinator.current_hvac_mode
        if self._coordinator.current_profile is not None:
            self._state.hvac_profile = self._coordinator.current_profile

        if self._unsub_coordinator is None:
            self._unsub_coordinator = self._coordinator.async_add_listener(self._on_coordinator_update)

        # Start the persisted tick scheduler
        await super().async_start()

        # Let the scheduler tick drive the very first run (it will be due immediately if no meta).
        # If you prefer an explicit immediate run, uncomment:
        # await self.async_run_if_due(reason="startup")

    async def async_stop(self) -> None:
        """Stop supervisor: unsubscribe listeners and stop background tasks."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()

        if self._unsub_coordinator is not None:
            with contextlib.suppress(Exception):
                self._unsub_coordinator()
            self._unsub_coordinator = None

        # Stop scheduler (cancels tick + in-flight due job)
        await super().async_stop()

        log_info(_LOGGER, "Supervisor stopped. instance=%s", self._instance_id)

    # -----------------------------
    # Coordinator trigger
    # -----------------------------

    def _on_coordinator_update(self) -> None:
        """Called when ClimateCoordinator publishes an update (every ~5 minutes)."""

        if not self._coordinator.ready:
            return
        
        self._request_run(reason="coordinator")

    def _request_run(self, *, reason: str) -> None:
        """Thread-safe scheduling of an interval-gated run."""
        if self._stop_event.is_set():
            return

        # Be defensive: coordinator callbacks *should* be on-loop, but keep a guard.
        try:
            if asyncio.get_running_loop() is not self._hass.loop:
                raise RuntimeError
        except RuntimeError:
            self._hass.loop.call_soon_threadsafe(functools.partial(self._request_run, reason=reason))
            return

        self._hass.async_create_task(
            self.async_run_if_due(reason=reason),
            name=f"drp_supervisor_due:{self._instance_id}:{reason}",
        )

    # -----------------------------
    # Scheduler hook (DailyGatedSchedulerBase)
    # -----------------------------

    async def _async_on_due(self, reason: str) -> None:
        """Job executed when the interval gate allows a run."""
        await self.async_decide_and_act(reason=reason)

    # -----------------------------
    # Core decision
    # -----------------------------

    async def async_decide_and_act(self, *, reason: str) -> None:
        """Compute a control plan and apply it.

        Single entry-point for decision making.

        Notes
        - This method is called by `_async_on_due()` (gated scheduler).
        - It can also be called directly (e.g., future services) if needed.
        """

        if not self._coordinator.ready:
            return 
        
        plan = None
        if self._stop_event.is_set():
            return

        # Avoid heavy IO / side effects while the coordinator isn't ready yet.
        if getattr(self._coordinator, "data", None) is None or getattr(self._coordinator, "last_update_success", True) is False:
            log_debug(_LOGGER, "Skip decision: coordinator not ready (reason=%s)", reason)
            return

        async with self._decider_lock:
            try:
                # Plant control decision (separate from zone MPC-lite)
                snap = self._coordinator.plant_snapshot
                if snap is not None:
                    try:
                        log_debug(_LOGGER, "PlantSnapshot %s", snap)

                        self._last_plant_decision = self._plant_decision_planner.plan(
                            snapshot=snap,
                            reason="tick",
                            # derived=derived,
                        )
                        log_debug(_LOGGER, "PlantDecision %s", self._last_plant_decision)

                        # fan_only: solo VMC attuata (bypass staging idraulico gestito in
                        # PlantActuator.async_apply tramite PlantPhase.VMC_ONLY).
                        # L'attuatore viene chiamato anche per fan_only oltre che per auto.
                        if self.current_hvac_mode in (HVACMode.AUTO, HVACMode.FAN_ONLY):
                            await self._plant_actuator.async_apply(snapshot=snap, decision=self._last_plant_decision)

                            if self._last_plant_decision.mode in (PlantMode.IAQ_ONLY, PlantMode.VENT_ONLY):
                                self._state.hvac_action = HVACAction.FAN
                            elif self._last_plant_decision.mode == PlantMode.HEATING:
                                self._state.hvac_action = HVACAction.HEATING
                            elif self._last_plant_decision.mode == PlantMode.COOLING:
                                self._state.hvac_action = HVACAction.COOLING
                            elif self._last_plant_decision.mode == PlantMode.DEHUM_ASSIST:
                                self._state.hvac_action = HVACAction.DRYING
                            elif self._last_plant_decision.mode == PlantMode.OFF:
                                self._state.hvac_action = HVACAction.OFF
                            else:
                                self._state.hvac_action = HVACAction.IDLE
                        else:
                            self._state.hvac_action = HVACAction.OFF

                        # dash = build_dashboard(snap, self._last_zones_decision, self._last_plant_decision)
                        # log_debug(_LOGGER, "\n%s", render_dashboard_text(dash))
                        # self._last_plant_decision = await self._plant_engine.async_run_once(
                        #     snapshot=snap,
                        #     reason="tick",
                        # )
                    except Exception as e:
                        log_exception(_LOGGER, "Plant control decision failed: %s", e)
                        self._last_plant_decision = None

                else:
                    log_warning(_LOGGER, "No PlantSnapshot available, skipping plant decision (reason=%s)", reason)
                
            except asyncio.CancelledError:
                return

            # log_debug(_LOGGER, "ControlPlan %s", plan)
            
            if plan is None:
                return

            # Expose to UI
            if getattr(plan, "any_heat_demand", False):
                self._state.hvac_action = HVACAction.HEATING

            # TODO: qui puoi aggiornare hvac_mode / profile quando li colleghi a plan/state machine
