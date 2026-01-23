# custom_components/drp_climate/coordinator.py
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import fields, replace
from typing import Any, Mapping
from datetime import datetime, timedelta, timezone

import psychrolib

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STARTED, PERCENTAGE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile

from ..helpers.confort.policy_layer import ComfortPolicyLayer, PolicyContext, PolicyDecision, build_policy_layer

from ..domain.models.season import OperativeSeason, SeasonState
from ..helpers.confort.confort_band import ComfortBandCalculator

from ..domain.influx import InfluxConfig
# from ..strategies.plant_regime_pipeline import InfluxSeriesReader, PlantEntities, PlantRegimePipeline, RegimeConfig, RegimeSearchResult, daily_local_mean

from ..helpers.config_sensors import build_sensor_mapping

from ..helpers.sensor_aggregator import SensorAggregator

from ..const import CONF_INDOOR, CONF_RADIANT, DOMAIN, ENTITIES_STATE, NAME_AREA_HOME, SEASON_STATE
from ..domain.models.runtime_schema import AreaConfig, RuntimeConfig, SensorPair, WeatherConfig
from ..helpers.config_entries import (
    build_runtime_config,
    collect_entity_ids_for_state_changes,
    subscribe_entity_state_changes,
)
from ..helpers.logger import log_debug, log_info, log_warning
from ..helpers.utils import slugify

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
        - Sottoscrive EVENT_HOMEASSISTANT_STARTED e ritarda di qualche secondo
          la finalizzazione runtime + subscribe state changes.
        - Espone async_stop() per unload/reload.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._name = entry.data.get(CONF_NAME, "default-name")

        # # Shared store per entry (idempotente)
        # domain_store = hass.data.setdefault(DOMAIN, {})
        # entry_store = domain_store.setdefault(entry.entry_id, {})
        # entry_store.setdefault(ENTITIES_STATE, {})

        # # Stato sensori (entity_id -> State)
        # self._entities_state_store: dict[str, State] = entry_store[ENTITIES_STATE]

        # Runtime config (può essere aggiornato a runtime dopo setup unique ids)
        self._runtime: RuntimeConfig = build_runtime_config(entry)

        self._sensor_aggregator = SensorAggregator(
            entities_state_store=self._entities_state, 
            mapping=build_sensor_mapping(self._runtime.climate)
        )

        self._climate_preset_mode: HVACOperatingProfile | None = None
        self._season_state = None

        self._policy_layer: ComfortPolicyLayer = build_policy_layer()
        self._confort_band = ComfortBandCalculator()

        # Psychrolib unit system: impostazione globale (attenzione: globale nel processo)
        # Se più entry con unit diverse coesistono, questa è una criticità.
        psychrolib.SetUnitSystem(psychrolib.SI if self._runtime.climate.units == "si" else psychrolib.IP)

        # Flags / subscriptions
        self._init_complete = False
        self._unsub_state_changes: CALLBACK_TYPE | None = None
        self._unsub_delayed: CALLBACK_TYPE | None = None
        self._unsub_hastarted_event: CALLBACK_TYPE | None = None

        # FAST loop
        self._fast_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        super().__init__(
            hass,
            _LOGGER,
            name=slugify(f"{DOMAIN}-{self._name}-coordinator"),
            update_interval=self._runtime.update_interval,
        )

        # HA started hook (one-shot)
        self._unsub_hastarted_event = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            self._on_ha_started,
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
    def entry_id(self) -> str:
        return self._entry.entry_id

    @property
    def unit_system(self) -> str:
        return self._runtime.climate.units

    @property
    def runtime_weather_config(self) -> WeatherConfig:
        return self._runtime.climate.weather

    @property
    def _entry_store(self) -> dict[str, Any]:
        domain_store = self._hass.data.setdefault(DOMAIN, {})
        return domain_store.setdefault(self._entry.entry_id, {})

    @property
    def _entities_state(self) -> dict[str, State]:
        return self._entry_store.setdefault(ENTITIES_STATE, {})

    # @property
    # def _store_entities_state(self) -> dict[str, State]:
    #     """Mappa entity_id -> State.

    #     Idempotente anche se hass.data viene ricreato (riusa il reference store iniziale).
    #     """
    #     # domain_store = self._hass.data.setdefault(DOMAIN, {})
    #     # entry_store = domain_store.setdefault(self._entry.entry_id, {})
    #     # return entry_store.setdefault(ENTITIES_STATE, self._entities_state_store)
    #     return self._entry_store.setdefault(ENTITIES_STATE, self._entities_state_store)

    # @property
    # def _season_state(self) -> SeasonState | None:
    #     season_state = self._entry_store.get(SEASON_STATE)
        
    #     if not isinstance(season_state, SeasonState):
    #         log_warning(_LOGGER, "No valid SeasonState in store for regime config test")
    #         return None

    #     return season_state

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

    # ----------------- HA started / runtime finalize ----------------- #

    @callback
    def _on_ha_started(self, event: Event) -> None:
        """Callback sync: schedula la finalizzazione dopo un breve delay."""

        @callback
        def _runner(_now) -> None:
            self._hass.async_create_task(self._async_post_start(event))

        # Delay (es. attendi che altre integrazioni espongano entity e store)
        self._unsub_delayed = async_call_later(self._hass, 10, _runner)

    async def _async_post_start(self, event: Event) -> None:
        """Post-start: completa runtime, subscribe state changes, bootstrap stati."""
        try:
            await self._async_complete_runtime_config(event)
        except Exception as exc:  # noqa: BLE001
            log_warning(_LOGGER, "Runtime config completion failed: %s", exc, exc_info=True)
            # anche se fallisce, non blocchiamo HA: proseguiamo con runtime attuale

        # Subscribe ai cambi di stato
        eids = collect_entity_ids_for_state_changes(self._runtime)
        self._unsub_state_changes = subscribe_entity_state_changes(
            self._hass,
            callback=self.entity_changed,
            entity_ids=eids,
        )

        # Bootstrap stato iniziale per evitare cache vuota fino al primo change
        for eid in eids:
            st = self._hass.states.get(eid)
            if st is not None:
                self._entities_state[eid] = st

        await self.async_start_fast_loop()

        self._init_complete = True
        log_debug(_LOGGER, "Post-start completed. Subscribed %d entities.", len(eids))

    async def _async_complete_runtime_config(self, _event: Event) -> None:
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

    # ----------------- Slave sensors factory ----------------- #

    def build_slave_sensor_defs(self) -> list[dict[str, Any]]:
        """Ritorna le definizioni per le entity "slave" (dewpoint, heat-index, ecc.)."""
        defs: list[dict[str, Any]] = []

        temps: list[str] = []
        humis: list[str] = []

        for area in getattr(self._runtime.climate, "areas", []) or []:
            # i tuoi AreaConfig potrebbero essere dataclass: manteniamo getattr flessibile
            if getattr(area, CONF_INDOOR, False) and getattr(area, CONF_RADIANT, False):
                sensors = getattr(area, "sensors", None)
                if not sensors:
                    continue

                temps.append(sensors.temperature)
                humis.append(sensors.humidity)

                defs.append(
                    {
                        "type": "DewpointSensor",
                        "area": area.name,
                        "name": f"Ambient {area.name}",
                        "sensors": sensors,
                        "unit": self._runtime.climate.unit_system.temperature,
                    }
                )
                defs.append(
                    {
                        "type": "HeatIndexSensor",
                        "area": area.name,
                        "name": f"Ambient {area.name}",
                        "sensors": sensors,
                        "unit": self._runtime.climate.unit_system.temperature,
                    }
                )

        defs.append(
            {
                "type": "CurrentTemperatureSensor",
                "name": f"Ambient {NAME_AREA_HOME}",
                "temp_sensors": temps,
                "unit": self._runtime.climate.unit_system.temperature,
            }
        )
        defs.append(
            {
                "type": "CurrentHumiditySensor",
                "name": f"Ambient {NAME_AREA_HOME}",
                "humi_sensors": humis,
                "unit": PERCENTAGE,
            }
        )
        defs.append(
            {
                "type": "CurrentDewpointSensor",
                "name": f"Ambient {NAME_AREA_HOME}",
                "temp_sensors": temps,
                "humi_sensors": humis,
                "unit": self._runtime.climate.unit_system.temperature,
            }
        )
        defs.append(
            {
                "type": "CurrentHeatIndexSensor",
                "name": f"Ambient {NAME_AREA_HOME}",
                "temp_sensors": temps,
                "humi_sensors": humis,
                "unit": self._runtime.climate.unit_system.temperature,
            }
        )

        log_debug(_LOGGER, "build_slave_sensor_defs: %d definitions", len(defs))
        return defs




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
            self._entities_state[entity_id] = new_state
        except Exception as ex:  # noqa: BLE001
            log_warning(_LOGGER, "Ignore state change for %s (%s)", entity_id, ex)
            return

        # NOTA: non usare async_set_updated_data qui per non resettare update_interval.
        # Avvisa le entity collegate.
        self.async_update_listeners()

    # ---------------------- Lifecycle hooks ------------------------- #

    async def async_config_entry_first_refresh(self) -> None:
        """Primo refresh: dopo il SLOW loop, avvia il FAST loop."""
        await super().async_config_entry_first_refresh()
        log_debug(_LOGGER, "First refresh completed")
        # await self.async_start_fast_loop()

    async def async_start_fast_loop(self) -> None:
        """Avvia il loop FAST (controlli frequenti)."""
        if self._fast_task is not None:
            return
        self._stop_event.clear()
        self._fast_task = self._hass.async_create_task(self._fast_loop(), name="drp_fast_loop")

    async def async_stop(self) -> None:
        """Stop coordinato: cancella FAST loop e unsubscribe eventi."""
        self._stop_event.set()

        if self._fast_task is not None:
            self._fast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._fast_task
            self._fast_task = None

        if self._unsub_state_changes is not None:
            with contextlib.suppress(Exception):
                self._unsub_state_changes()
            self._unsub_state_changes = None

        if self._unsub_hastarted_event is not None:
            with contextlib.suppress(Exception):
                self._unsub_hastarted_event()
            self._unsub_hastarted_event = None

        if self._unsub_delayed is not None:
            with contextlib.suppress(Exception):
                self._unsub_delayed()
            self._unsub_delayed = None

        self._init_complete = False

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
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    def _compute_confort_band(self):

        runtime_vmc = self._runtime.climate.devices.vmc

        season_state = self._season_state
        vmc_speed = self._entities_state.get(runtime_vmc.spare_setpoint) if runtime_vmc else None

        # log_debug(_LOGGER, "runtime_vmc.spare_setpoint %s", runtime_vmc.spare_setpoint if runtime_vmc else "***")
        # log_debug(_LOGGER, "self._entities_state.keys %s", self._entities_state.keys())

        if season_state and runtime_vmc and vmc_speed and self._climate_preset_mode:

            for area in self._runtime.climate.areas:
                name = slugify(area.name)

                rh_pct = self._sensor_aggregator.get(f"{name}.indoor_humidity")
                if rh_pct.value is None:
                    continue

                area_policy_ctx = PolicyContext(
                    now=datetime.now(timezone.utc),
                    room=name,
                    season=OperativeSeason.from_value(season_state.season),
                    vmc_speed=int(vmc_speed.state),
                    rh_pct=rh_pct.value,
                    t_op_current=self._sensor_aggregator.get( f"{name}.t_op" ).value,
                    outdoor_temp=self._sensor_aggregator.get( "global.outdoor_temperature" ).value,
                    mode=self._climate_preset_mode
                )

                # area_confort_band = self._confort_band.compute_comfort_band(
                #     room=name,
                #     speed=int(vmc_speed.state),
                #     rh_pct=rh_pct.value,
                #     season=OperativeSeason.from_value(season_state.season),
                #     t_op_current=self._sensor_aggregator.get( f"{name}.t_op" ).value
                # )

                decision: PolicyDecision = self._policy_layer.decide(area_policy_ctx)
                _LOGGER.debug("[comfort_policy] ctrl_aggressiveness=%.2f", decision.ctrl_aggressiveness)
                area_confort_band = self._confort_band.compute_comfort_band(
                    room=name,
                    speed=int(vmc_speed.state),
                    rh_pct=rh_pct.value,
                    season=OperativeSeason.from_value(season_state.season),
                    t_op_current=self._sensor_aggregator.get(f"{name}.t_op").value,
                    policy=decision,  # <-- QUI
                )

                log_debug(_LOGGER, "%s", area_policy_ctx)
                log_debug(_LOGGER, "%s", area_confort_band)
        else:
            log_warning(_LOGGER, "season_state=%s", season_state)
            log_warning(_LOGGER, "vmc_speed=%s", vmc_speed)


    # async def _test_regime_config(self) -> None:
    #     season_state = self._entry_store.get(SEASON_STATE)
    #     if not isinstance(season_state, SeasonState):
    #         log_warning(_LOGGER, "No valid SeasonState in store for regime config test")
    #         return

    #     influx_cfg = InfluxConfig(
    #         bucket=self._runtime.climate.historical_data.bucket,
    #         org=self._runtime.climate.historical_data.organization,
    #         token=self._runtime.climate.historical_data.token,
    #         url=self._runtime.climate.historical_data.url,
    #     )

    #     entities = PlantEntities(
    #         outdoor_temp="ambient_outdoor_temperature",
    #         compressor_state="hmi080_compressor_state",
    #         device_mode="hmi080_device_mode",
    #         vmc_pump_switch="hcs_direct_supply_unit",
    #         radiant_pump_switch="hcs_motorized_temperature_adjustable_supply_unit",
    #         active_power="emeter_clima_active_power",
    #     )

    #     def _fit_sync() -> tuple[str, RegimeConfig, RegimeSearchResult]:
    #         influx_reader = InfluxSeriesReader(cfg=influx_cfg)
    #         try:
    #             pipe = PlantRegimePipeline(
    #                 reader=influx_reader,
    #                 entities=entities,
    #                 season_state=season_state,
    #                 season_windows=season_state.weather.windows or [],
    #                 local_tz="Europe/Rome",
    #                 start="-730d",
    #                 stop="now()",
    #             )

    #             # Fit "vero" (globale + per-season) e scelta config per oggi
    #             fit = pipe.fit_seasonal()
    #             cfg_today = pipe.pick_runtime_config(fit)
    #             season = pipe.pick_runtime_season()

    #             # Se vuoi anche il dettaglio della grid search (coerente con la pipeline):
    #             raw = pipe.load_raw()
    #             norm = pipe.normalize(raw)
    #             obs_daily, _duty_daily, _frame = pipe.build_observed_daily_regime(norm)

    #             # IMPORTANTISSIMO: media giornaliera su confini giorno locali (Europe/Rome),
    #             # coerente con fit_seasonal()
    #             T_out_daily = daily_local_mean(norm["T_out"], tz=pipe.local_tz)

    #             search = pipe.grid_search_regime(
    #                 T_out_daily=T_out_daily,
    #                 obs_regime_daily=obs_daily,
    #             )

    #             return (str(season), cfg_today, search)

    #         finally:
    #             influx_reader.close()

    #     season_str, cfg_today, search_obj = await self._hass.async_add_executor_job(_fit_sync)

    #     log_debug(_LOGGER, "Runtime season: %s", season_str)
    #     log_debug(_LOGGER, "Chosen regime config: %s", cfg_today)

    #     for i, c in enumerate(search_obj.top10, start=1):
    #         log_debug(
    #             _LOGGER,
    #             "Top %02d)\n"
    #             "   loss=%.3f err=%.3f pen=%.3f\n"
    #             "   switches=%d/%d\n"
    #             "   tau=%.1f\n"
    #             "   hon=%.1f hoff=%.1f\n"
    #             "   con=%.1f coff=%.1f",
    #             i,
    #             c.loss, c.err, c.pen,
    #             c.switches, c.days,
    #             c.cfg.tau_days,
    #             c.cfg.heating_on, c.cfg.heating_off,
    #             c.cfg.cooling_on, c.cfg.cooling_off,
    #         )


    # --------------------- Debug helpers -------------------- #

    def _debug_dump_entities_state(self, *, max_attr_len: int = 400) -> None:
        """Logga l'istantanea di self._entities_state (entity -> State)."""
        try:
            items = sorted(self._entities_state.items(), key=lambda kv: kv[0])
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

        Importante: niente side-effect (niente comandi agli attuatori).

        Returns:
            dict[str, Any]: snapshot per gli consumers (entity/supervisor).
        """
        if not self._init_complete:
            return {}

        try:
            await self._sensor_aggregator.async_update()

            self._compute_confort_band()

            # rh_pct = self._sensor_aggregator.get( "kitchen.indoor_humidity" ).value
            # t_op_current = self._sensor_aggregator.get( "kitchen.t_op" ).value

            # if rh_pct is not None and t_op_current is not None:
            #     kitchen_cb = self._confort_band.compute_comfort_band(
            #         room="kitchen",
            #         speed=1,
            #         rh_pct=rh_pct,
            #         season="winter",
            #         t_op_current=t_op_current
            #     )
            #     log_debug(_LOGGER, "%s", kitchen_cb)
            #     kitchen_cb = self._confort_band.compute_comfort_band(
            #         room="kitchen",
            #         speed=5,
            #         rh_pct=rh_pct,
            #         season="winter",
            #         t_op_current=t_op_current
            #     )
            #     log_debug(_LOGGER, "%s", kitchen_cb)
            # await self._test_regime_config()

            log_debug(_LOGGER, "%s", self._sensor_aggregator.latest_all())

            # TODO: costruire snapshot reale (PlantSnapshot ecc.)
            # Esempio minimale: esporta solo timestamp e numero entity osservate
            snapshot = {
                "ts": self._hass.loop.time(),
                "observed": len(self._entities_state),
            }
            return snapshot
        except Exception as exc:  # noqa: BLE001
            log_warning(_LOGGER, "Update failed: %s", exc, exc_info=True)
            raise UpdateFailed(f"Update failed: {exc}") from exc


    def set_preset_mode(self, preset_mode: HVACOperatingProfile) -> None:
        self._climate_preset_mode = preset_mode

    def set_season_state(self, season_state: SeasonState) -> None:
        self._season_state = season_state
