# custom_components/drp_climate/coordinator.py
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import fields, replace
from typing import Any, Dict, Optional
from datetime import datetime, timezone

# psychrolib non viene importato qui: SetUnitSystem(SI) è gestito a livello
# di modulo in helpers/psychrometric.py all'import time. Vedere la docstring
# di quel modulo per la motivazione (problema global state multi-entry).

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE, Event, EventStateChangedData,
    HomeAssistant, State, callback,
)
from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STARTED

from ..plant.monitor.plant import PlantSnapshot

from ..domain.enums import HVACOperatingProfile
from ..plant.monitor.builder import async_build_plant_states_snapshot
from ..helpers.timeutils import now_utc

from ..domain.models.season import OperativeSeason, SeasonState, Seasons

from ..domain.influx import InfluxConfig
# from ..strategies.plant_regime_pipeline import InfluxSeriesReader, PlantEntities, PlantRegimePipeline, RegimeConfig, RegimeSearchResult, daily_local_mean

from ..helpers.builders.config_aggregate_sensors import GLOBAL, FieldSuffix, build_sensor_mapping

from ..helpers.sensor_aggregator import SensorAggregator

from ..const import CONF_CEILING, CONF_INDOOR, CONF_RADIANT, DOMAIN, ENTITIES_OBSERVED_TS, ENTITIES_STATE, NAME_AREA_HOME, SEASON_STATE
from ..domain.models.runtime_schema import AreaConfig, RuntimeConfig, SensorPair, WeatherConfig
from ..helpers.builders.config_entries import (
    build_runtime_config,
    collect_entity_ids_for_state_changes,
    subscribe_entity_state_changes,
)
from ..helpers.logger import log_debug, log_info, log_warning
from ..helpers.utils import as_int, slugify

_LOGGER = logging.getLogger(__name__)


_STORE_SETUP_UNIQUE_IDS = "setup_unique_ids"
_STORE_SETUP_UNIQUE_IDS_EVT = "setup_unique_ids_evt"
_STORE_AREA_UIDS = "area_unique_ids"
_STORE_HOME_UIDS = "home_unique_ids"


class ClimateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator per la raccolta/stato sensori e calcolo dati derivati.

    Responsabilità:
        - Mantiene uno snapshot degli State Home Assistant per gli entity_id di interesse.
        - Esegue un loop SLOW (DataUpdateCoordinator) per calcoli/aggregazioni periodiche.
        - Esegue un loop FAST (task dedicato) per controlli locali frequenti (future TODO).

    Non responsabilità:
        - Decisione strategica HVAC (delegata a un Supervisor/Strategy layer).

    Note lifecycle:
        - _async_setup() (chiamato da async_config_entry_first_refresh) fa solo
          il minimo: costruisce SensorAggregator e schedula il post-start hook.
          Non blocca → HA non resta in ConfigEntryNotReady.
        - _async_post_ha_start() esegue l'inizializzazione pesante dopo che HA
          è completamente avviato (EVENT_HOMEASSISTANT_STARTED + delay opzionale).
          Qui avvengono: attesa unique_ids, subscribe state changes, bootstrap
          snapshot sensori, avvio fast loop.
        - _async_update_data() ha un guard su _init_complete: il primo tick SLOW
          può arrivare prima del post-start.
        - Espone async_stop() per unload/reload.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._name = entry.data.get(CONF_NAME, "default-name")

        # Runtime config (può essere aggiornato a runtime dopo setup unique ids)
        self._runtime: RuntimeConfig = build_runtime_config(entry)

        self._climate_preset_mode: HVACOperatingProfile | None = None
        self._climate_hvac_mode: HVACMode | None = None
        self._season_state = None

        # self._policy_layer: ComfortPolicyLayer = build_policy_layer()
        # self._confort_bands = ComfortBandCalculator()

        self._plant_snapshot: PlantSnapshot | None = None

        # SensorAggregator: None fino a _async_setup(); guard in _async_update_data
        self._sensor_aggregator: SensorAggregator | None = None

        # Flag: True solo dopo _async_post_ha_start() completato
        self._init_complete: bool = False

        # Subscriptions lifecycle
        self._unsub_state_changes: CALLBACK_TYPE | None = None
        self._unsub_ha_started: CALLBACK_TYPE | None = None
        self._unsub_delayed: CALLBACK_TYPE | None = None

        # FAST loop
        self._fast_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        super().__init__(
            hass,  # self.hass disponibile dopo super().__init__
            _LOGGER,
            name=slugify(f"{DOMAIN}-{self._name}-coordinator"),
            update_interval=self._runtime.update_interval,
        )

        log_info(
            _LOGGER,
            "Initialized (id=%s) entry=%s source=%s update_interval=%s",
            hex(id(self)),
            entry.entry_id,
            entry.source,
            self._runtime.update_interval,
        )

    # ----------------- Shared store helpers ----------------- #

    @property
    def ready(self) -> bool:
        """True solo dopo _async_post_ha_start() completato."""
        return self._init_complete

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    @property
    def unit_system(self) -> str:
        return self._runtime.climate.units

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._runtime

    @property
    def runtime_weather_config(self) -> WeatherConfig:
        return self._runtime.climate.weather

    @property
    def plant_snapshot(self) -> PlantSnapshot | None:
        return self._plant_snapshot

    @property
    def sensor_aggregator(self) -> SensorAggregator | None:
        return self._sensor_aggregator

    @property
    def _entry_store(self) -> dict[str, Any]:
        domain_store = self.hass.data.setdefault(DOMAIN, {})
        return domain_store.setdefault(self._entry.entry_id, {})

    @property
    def _entities_state_store(self) -> dict[str, State]:
        return self._entry_store.setdefault(ENTITIES_STATE, {})

    @property
    def _entities_observed_ts_store(self) -> dict[str, datetime]:
        return self._entry_store.setdefault(ENTITIES_OBSERVED_TS, {})

    def _setup_unique_ids_event(self) -> asyncio.Event:
        store = self._entry_store
        evt = store.get(_STORE_SETUP_UNIQUE_IDS_EVT)
        if isinstance(evt, asyncio.Event):
            return evt
        evt = asyncio.Event()
        store[_STORE_SETUP_UNIQUE_IDS_EVT] = evt
        return evt

    def mark_unique_ids_ready(self) -> None:
        """Segnala che la fase di setup dei unique_ids è completata.

        Chiamabile da altri componenti della stessa integrazione.
        """
        store = self._entry_store
        store[_STORE_SETUP_UNIQUE_IDS] = True
        self._setup_unique_ids_event().set()

    # ----------------- _async_setup (HA ≥ 2024.8) ----------------- #

    async def _async_setup(self) -> None:
        """Inizializzazione minima: costruisce SensorAggregator e schedula post-start.

        Chiamato da async_config_entry_first_refresh(): deve completare rapidamente
        per non tenere HA in stato ConfigEntryNotReady.
        L'inizializzazione pesante (unique_ids, subscribe, bootstrap, fast loop)
        è demandata a _async_post_ha_start(), schedulata su EVENT_HOMEASSISTANT_STARTED.
        """
        # SensorAggregator costruito qui: self.hass è garantito da super().__init__()
        self._sensor_aggregator = SensorAggregator(
            entities_state=self._entities_state_store,
            entities_observed_ts=self._entities_observed_ts_store,
            mapping=build_sensor_mapping(self._runtime.climate),
        )

        # Se HA è già avviato (es. reload dopo boot), esegui subito il post-start.
        # Altrimenti aspetta EVENT_HOMEASSISTANT_STARTED.
        if self.hass.is_running:
            self.hass.async_create_task(
                self._async_post_ha_start(),
                name="drp_post_ha_start",
            )
        else:
            self._unsub_ha_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED,
                self._on_ha_started,
            )

        log_debug(_LOGGER, "_async_setup completed (post-start deferred).")

    # ----------------- Post-HA-start ----------------- #

    @callback
    def _on_ha_started(self, _event: Event) -> None:
        """Callback sync su EVENT_HOMEASSISTANT_STARTED: schedula post-start con delay."""
        self._unsub_ha_started = None

        @callback
        def _runner(_now: object) -> None:
            self.hass.async_create_task(
                self._async_post_ha_start(),
                name="drp_post_ha_start",
            )

        # Delay: lascia che altre integrazioni espongano entity e unique_ids store
        self._unsub_delayed = async_call_later(self.hass, 10, _runner)

    async def _async_post_ha_start(self) -> None:
        """Inizializzazione pesante: completata quando HA è pienamente avviato.

        Esegue: attesa unique_ids, aggiornamento RuntimeConfig, subscribe state
        changes, bootstrap snapshot sensori, avvio fast loop.
        """
        try:
            await self._async_complete_runtime_config()
        except Exception as exc:  # noqa: BLE001
            log_warning(_LOGGER, "Runtime config completion failed: %s", exc, exc_info=True)

        eids = collect_entity_ids_for_state_changes(self._runtime)
        self._unsub_state_changes = subscribe_entity_state_changes(
            self.hass,
            callback=self.entity_changed,
            entity_ids=eids,
        )

        # Bootstrap stato iniziale: evita cache vuota fino al primo state change
        for eid in eids:
            st = self.hass.states.get(eid)
            if st is not None:
                self._entities_state_store[eid] = st

        await self.async_start_fast_loop()

        self._init_complete = True
        log_debug(_LOGGER, "_async_post_ha_start completed. Subscribed %d entities.", len(eids))

    async def _async_complete_runtime_config(self) -> None:
        """Aggiorna il RuntimeConfig sostituendo gli entity_id dai unique_ids store.

        Meccanismo:
            - Attende che un'altra parte dell'integrazione abbia completato la
              popolazione dello store `setup_unique_ids` / `area_unique_ids`.
            - Applica le sostituzioni in modo atomico (swap di dataclass).

        Timeout:
            - 60 secondi.
            - Se scade, mantiene runtime originale.
        """
        store = self._entry_store
        evt = self._setup_unique_ids_event()

        # Compatibilità: se qualcuno setta solo il bool, onoralo.
        if store.get(_STORE_SETUP_UNIQUE_IDS) is True:
            evt.set()

        try:
            await asyncio.wait_for(evt.wait(), timeout=60)
        except TimeoutError:
            log_warning(_LOGGER, "Timeout waiting for setup_unique_ids (RuntimeConfig)")
            return

        area_unique_ids_store: dict[str, Any] = store.get(_STORE_AREA_UIDS, {}) or {}
        home_unique_ids_store: dict[str, Any] = store.get(_STORE_HOME_UIDS, {}) or {}

        sensorpair_fields = {f.name for f in fields(SensorPair)}

        old_areas = list(self._runtime.climate.areas)
        new_areas: list[AreaConfig] = []
        changed = False

        for area_cfg in old_areas:
            data = area_unique_ids_store.get(area_cfg.name) or {}
            sp = area_cfg.sensors
            updates: dict[str, str] = {}

            # data: attr -> entity_id (o oggetto con .entity_id)
            for attr, sensordata in (data or {}).items():
                if attr not in sensorpair_fields:
                    log_warning(_LOGGER, "Ignore unknown SensorPair.%s for area '%s'", attr, area_cfg.name)
                    continue

                eid = getattr(sensordata, "entity_id", None) or str(sensordata)
                if eid and getattr(sp, attr, None) != eid:
                    updates[attr] = eid

            if updates:
                new_sp = replace(sp, **updates)
                area_cfg = replace(area_cfg, sensors=new_sp)
                changed = True
                log_info(_LOGGER, "Area '%s' sensors updated: %s", area_cfg.name, new_sp)

            new_areas.append(area_cfg)

        # mean_apt (home sensors)
        mean_sp = self._runtime.climate.mean_apt
        mean_updates: dict[str, str] = {}
        for attr, sensor in (home_unique_ids_store or {}).items():
            if attr not in sensorpair_fields:
                log_warning(_LOGGER, "Ignore unknown SensorPair.%s for mean_apt", attr)
                continue
            eid = getattr(sensor, "entity_id", None) or str(sensor)
            if eid and getattr(mean_sp, attr, None) != eid:
                mean_updates[attr] = eid

        new_mean = replace(mean_sp, **mean_updates) if mean_updates else mean_sp
        if mean_updates:
            changed = True
            log_info(_LOGGER, "mean_apt updated: %s", new_mean)

        if changed:
            new_climate = replace(self._runtime.climate, areas=new_areas, mean_apt=new_mean)
            self._runtime = replace(self._runtime, climate=new_climate)

            # allinea l'update_interval del coordinator al runtime aggiornato
            self.update_interval = self._runtime.update_interval

        log_info(_LOGGER, "RuntimeConfig completion done (changed=%s)", changed)

    # ---------------------- Event handling -------------------------- #

    @callback
    def entity_changed(self, event: Event[EventStateChangedData]) -> None:
        """Gestisce variazioni di stato sensori/attuatori sottoscritti."""
        if self._stop_event.is_set():
            return

        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if not entity_id or new_state is None:
            return

        try:
            self._entities_state_store[entity_id] = new_state
            self._entities_observed_ts_store[entity_id] = event.time_fired or dt_util.utcnow()
        except Exception as ex:  # noqa: BLE001
            log_warning(_LOGGER, "Ignore state change for %s (%s)", entity_id, ex)
            return

        # NOTA: non usare async_set_updated_data qui per non resettare update_interval.
        # Avvisa le entity collegate.
        self.async_update_listeners()

    async def async_start_fast_loop(self) -> None:
        """Avvia il loop FAST (controlli frequenti)."""
        if self._fast_task is not None:
            return
        self._stop_event.clear()
        self._fast_task = self.hass.async_create_task(self._fast_loop(), name="drp_fast_loop")

    async def async_stop(self) -> None:
        """Stop coordinato: cancella FAST loop e unsubscribe tutti gli eventi."""
        self._stop_event.set()
        self._init_complete = False

        if self._fast_task is not None:
            self._fast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._fast_task
            self._fast_task = None

        if self._unsub_state_changes is not None:
            with contextlib.suppress(Exception):
                self._unsub_state_changes()
            self._unsub_state_changes = None

        if self._unsub_ha_started is not None:
            with contextlib.suppress(Exception):
                self._unsub_ha_started()
            self._unsub_ha_started = None

        if self._unsub_delayed is not None:
            with contextlib.suppress(Exception):
                self._unsub_delayed()
            self._unsub_delayed = None

    async def _fast_loop(self) -> None:
        """Ciclo FAST: controlli locali con cadenza breve.

        Deve essere idempotente e tollerante a snapshot parziali.
        """
        interval = getattr(self._runtime, "fast_interval", 90)
        try:
            while not self._stop_event.is_set():
                # TODO: PID miscelatrice verso T_supply_target
                # TODO: PID deumidifica VMC verso target_rh_pct
                # TODO: rate-limit, min_on/min_off, guardie runtime
                # Usa wait_for su stop_event per rispondere immediatamente allo stop
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            return

    # --------------------- Debug helpers -------------------- #

    def _debug_dump_entities_state(self, *, max_attr_len: int = 400) -> None:
        """Logga l'istantanea di self._entities_state_store (entity -> State)."""
        try:
            items = sorted(self._entities_state_store.items(), key=lambda kv: kv[0])
            lines: list[str] = []
            for entity_id, st in items:
                if st is None:
                    lines.append(f"- {entity_id}: <None>")
                    continue

                try:
                    attrs_json = json.dumps(st.attributes, ensure_ascii=False, default=str)
                except Exception:
                    attrs_json = str(st.attributes)

                if len(attrs_json) > max_attr_len:
                    attrs_json = attrs_json[:max_attr_len] + f"...(+{len(attrs_json)-max_attr_len} chars)"

                friendly = st.attributes.get("friendly_name")
                lines.append(
                    "--------------------------------------------\n"
                    f"id: {entity_id} - state={st.state!r}\n"
                    f"{f'({friendly})' if friendly else ''}: \n"
                    f"last change={getattr(st, 'last_changed', None)} - last update={getattr(st, 'last_updated', None)}\n"
                    f"attrs={attrs_json}\n"
                )

            log_debug(_LOGGER, "Entities state snapshot (%d items):\n%s", len(items), "\n".join(lines))
        except Exception as ex:  # noqa: BLE001
            log_warning(_LOGGER, "Failed dumping entities state: %s", ex)

    # --------------------- DataUpdateCoordinator -------------------- #

    async def _async_update_data(self) -> dict[str, Any]:
        """Loop SLOW: raccoglie sensori, calcola grandezze derivate e aggiorna snapshot.

        Guard su _init_complete perché il primo tick può precedere il completamento
        di _async_post_ha_start().

        Importante: niente side-effect (niente comandi agli attuatori).

        Returns:
            dict[str, Any]: snapshot per gli consumers (entity/supervisor).
        """
        if not self._init_complete:
            return {}

        if self._sensor_aggregator is None:
            # Paranoia: non dovrebbe mai accadere se _async_setup è passato
            return {}
    
        try:
            await self._sensor_aggregator.async_update()

            if self._season_state and self._climate_hvac_mode and self._climate_preset_mode:
                self._plant_snapshot = await async_build_plant_states_snapshot(
                    hass=self.hass,
                    timestamp=now_utc(),
                    runtime_config=self._runtime,
                    season=self._season_state,
                    entities_state=self._entities_state_store,
                    sensor_aggr=self._sensor_aggregator,
                    # confort_bands=self._compute_confort_band(),
                    climate_hvac_mode=self._climate_hvac_mode,
                    climate_preset_mode=self._climate_preset_mode,
                )
            else:
                log_warning(_LOGGER, "Invalid season state %s", self._season_state)

            return {
                "ts": self.hass.loop.time(),
                "observed": len(self._entities_state_store),
            }
        except Exception as exc:  # noqa: BLE001
            log_warning(_LOGGER, "Update failed: %s", exc, exc_info=True)
            raise UpdateFailed(f"Update failed: {exc}") from exc


    def set_preset_mode(self, preset_mode: HVACOperatingProfile) -> None:
        self._climate_preset_mode = preset_mode

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._climate_hvac_mode = hvac_mode

    def set_season_state(self, season_state: SeasonState) -> None:
        self._season_state = season_state

    @property
    def current_hvac_mode(self) -> Optional[HVACMode]:
        return self._climate_hvac_mode

    @property
    def current_profile(self) -> Optional[HVACOperatingProfile]:
        return self._climate_preset_mode