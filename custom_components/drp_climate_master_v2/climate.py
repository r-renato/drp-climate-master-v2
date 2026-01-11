
from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode, HVACAction, ClimateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import (
    CONF_NAME,
    CONF_UNIQUE_ID,
    UnitOfTemperature,
)

from .helpers.logger import log_debug, log_info

from .helpers.utils import slugify

from .controller.coordinator import ClimateCoordinator
from .controller.supervisor import ClimateSupervisor
from .const import (
    COORDINATOR,
    DEFAULT_CLIMATE_NAME,
    DOMAIN,
    INTEGRATION_MANUFACTURER,
    INTEGRATION_NAME,
    INTEGRATION_VERSION,
    SUPERVISOR,
)
from .domain.enums import HVACOperatingProfile

# from .const import DOMAIN, NAME, MANUFACTURER
# from .controller.coordinator import DrpCoordinator, PlantSnapshot
# from .controller.supervisor import Supervisor

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: ClimateCoordinator = store[COORDINATOR]
    supervisor: ClimateSupervisor = store[SUPERVISOR]

    # slave_entities = await coordinator.async_setup_slave_entities()
    # if slave_entities:
    #     await async_platform_add_entities(hass, DOMAIN, Platform.SENSOR, slave_entities)
    #     _LOGGER.info("Registered %d slave sensor(s)", len(slave_entities))
    # else:
    #     _LOGGER.info("No indoor areas found; no slave sensors registered")
    
    async_add_entities([ClimateMasterEntity(coordinator, supervisor, entry)], update_before_add=True)
    _LOGGER.info("%s: setup entry '%s' completato.", DOMAIN, entry.entry_id)

class ClimateMasterEntity(CoordinatorEntity[ClimateCoordinator], ClimateEntity):
    # _attr_name = "Home Climate Master"
    # _attr_unique_id = "home_climate_master"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
    _attr_supported_features = ClimateEntityFeature.PRESET_MODE
    _attr_preset_modes = HVACOperatingProfile.values()

    _attr_preset_mode: str = HVACOperatingProfile.COMFORT.value
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: ClimateCoordinator, supervisor: ClimateSupervisor, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._supervisor = supervisor

        self._attr_name = entry.options.get(CONF_NAME, DEFAULT_CLIMATE_NAME)
        self._attr_unique_id = slugify(entry.options.get( CONF_UNIQUE_ID, f"""{self._attr_name}-uid""" ))

        self._preset_mode = HVACOperatingProfile.COMFORT
        self.map_on_hvac_mode = self._attr_hvac_mode = HVACMode.AUTO

        self._target_temp = 22.0
        self._attr_min_temp = 16.0
        self._attr_max_temp = 26.0
        # self._attr_temperature_unit = self.hass.config.units.temperature_unit
        # log_debug(_LOGGER, "xyz %s", entry.data)
        # log_debug(_LOGGER, "xyz %s", entry.options)
        log_info(
            _LOGGER,
            "Initialized (id=%s) entry=%s source=%s",
            hex(id(self)),
            entry.entry_id,
            entry.source,
        )

    @property
    def hvac_mode(self) -> HVACMode:
        return self._supervisor.current_hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        return self._supervisor.current_hvac_action

    @property
    def temperature_unit(self) -> str:
        return self._attr_temperature_unit

    # @property
    # def current_temperature(self) -> float | None:
    #     snap: PlantSnapshot = self.coordinator.snapshot
    #     if not snap.t_rooms:
    #         return None
    #     return sum(snap.t_rooms.values()) / len(snap.t_rooms)

    @property
    def target_temperature(self) -> float | None:
        return self._target_temp

    # @property
    # def preset_mode(self) -> str | None:
    #     return self._supervisor.current_profile.value if self._supervisor.current_profile else None

    # @property
    # def extra_state_attributes(self) -> dict[str, Any]:
    #     snap = self.coordinator.snapshot
    #     return {
    #         "dewpoint_guard": snap.dew_guard_active,
    #         "free_cooling_possible": snap.free_cooling_possible,
    #         "faults": snap.faults,
    #         "vmc_on": snap.vmc_on,
    #         "pdc_on": snap.pdc_on,
    #     }

    # async def async_set_temperature(self, **kwargs: Any) -> None:
    #     temp = kwargs.get("temperature")
    #     if temp is None:
    #         return
    #     self._target_temp = float(temp)
    #     await self._supervisor.async_set_target_temperature(self._target_temp)
    #     self.async_write_ha_state()

    # async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
    #     await self._supervisor.async_set_hvac_mode(hvac_mode)
    #     self.async_write_ha_state()

    # async def async_set_preset_mode(self, preset_mode: str) -> None:
    #     await self._supervisor.async_set_profile(preset_mode)
    #     self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": INTEGRATION_MANUFACTURER,
            "sw_version" : INTEGRATION_VERSION,
            # "config_version": str(getattr(self._entry, "version", "v2")),
        }


















# -------------------------------------------------------------------------------------------------------------------
# custom_components/drp_climate_master/climate.py
# from __future__ import annotations

# from typing import Any, Dict, List, Optional

# from homeassistant.core import HomeAssistant
# # from homeassistant.const import TEMP_CELSIUS
# from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID, UnitOfTemperature
# from homeassistant.config_entries import ConfigEntry
# from homeassistant.helpers.entity_platform import AddEntitiesCallback
# from homeassistant.helpers.device_registry import DeviceInfo
# from homeassistant.helpers.update_coordinator import CoordinatorEntity
# from homeassistant.components.climate import (
#     ClimateEntity,
#     # ClimateEntityFeature,
#     # HVACMode,
#     # HVACAction,
# )
# from homeassistant.components.climate.const import (
#     ClimateEntityFeature,
#     HVACMode,
#     HVACAction,
# )

# from .helpers.utils import slugify
# from .controller.coordinator import ClimateCoordinator

# from .const import (
#     DOMAIN,
#     DEFAULT_CLIMATE_NAME,
#     COORDINATORS,
#     # DEFAULT_TEMP_UNIT,
#     CONF_AREAS,
#     CONF_AREA,
# )

# # ---------- Setup tramite ConfigEntry (HA 2025.4.4) ----------
# # ------------------------------ Setup platform ------------------------------ #

# async def async_setup_entry(
#     hass: HomeAssistant, entry: ConfigEntry, async_add_entities
# ) -> None:
#     """Crea le entity Climate a partire dal coordinator."""
#     data = hass.data[DOMAIN][entry.entry_id]
#     coordinator: ClimateCoordinator = data[COORDINATORS]

#     # Se hai più zone, costruiscile qui; per ora una singola entity
#     entity = ClimateMasterEntity(
#         coordinator=coordinator,
#         entry=entry,
#     )
#     async_add_entities([entity])


# # async def async_setup_entry(
# #     hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
# # ) -> None:
# #     """Crea entità Climate per questo ConfigEntry."""
# #     domain_data = hass.data.get(DOMAIN, {})
# #     coordinator = domain_data.get(COORDINATORS, {}).get(entry.entry_id)

# #     # Estrarre aree dall'entry (options). Se mancano, creiamo una sola entity aggregata.
# #     areas: List[dict] = list(entry.options.get(CONF_AREAS, [])) if entry.options else []
# #     entities: List[DrpClimateEntity] = []

# #     if not areas:
# #         # Fallback: un'unica entità "aggregata"
# #         entities.append(
# #             DrpClimateEntity(
# #                 coordinator=coordinator,
# #                 entry=entry,
# #                 zone_name=entry.data.get("climate_name", DEFAULT_CLIMATE_NAME),
# #                 unique_suffix="aggregate",
# #             )
# #         )
# #     else:
# #         for area in areas:
# #             name = area.get(CONF_AREA)
# #             if not isinstance(name, str) or not name:
# #                 # Salta voci malformate
# #                 continue
# #             entities.append(
# #                 DrpClimateEntity(
# #                     coordinator=coordinator,
# #                     entry=entry,
# #                     zone_name=name,
# #                     unique_suffix=_slugify(name),
# #                 )
# #             )

# #     if entities:
# #         async_add_entities(entities)


# # ---------- Entity ----------

# class ClimateMasterEntity(CoordinatorEntity[ClimateCoordinator], ClimateEntity):
#     """Entity Climate per una singola zona/area (o aggregata)."""

#     # Feature minime sicure per 2025.4.4: bersaglio temperatura
#     # _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
#     _attr_temperature_unit = UnitOfTemperature.CELSIUS  # coerente con DEFAULT_TEMP_UNIT "°C"
#     _attr_hvac_modes = [
#         HVACMode.OFF,
#         # HVACMode.HEAT,
#         # HVACMode.COOL,
#         # HVACMode.DRY,
#         HVACMode.AUTO,
#     ]

#     def __init__(self, coordinator, entry: ConfigEntry) -> None:
#         super().__init__(coordinator)
#         self._entry = entry
#         # self._zone = zone_name
#         self._attr_name = entry.options.get(CONF_NAME, DEFAULT_CLIMATE_NAME)
#         self._unique_id = self._attr_unique_id = slugify(entry.options.get( CONF_UNIQUE_ID, f"""{self._attr_name}-uid""" ))
#         # cache locale come fallback se l'engine non fornisce dati
#         self._fallback_target: Optional[float] = None
#         self._fallback_mode: HVACMode = HVACMode.AUTO

#     # ---- Device info per raggruppare le entità sotto l'integrazione
#     @property
#     def device_info(self) -> DeviceInfo:
#         title = self._entry.data.get("climate_name") or DEFAULT_CLIMATE_NAME
#         return DeviceInfo(
#             identifiers={(DOMAIN, self._entry.entry_id)},
#             name=title,
#             manufacturer="Domotic Residential Platform",
#             model="Climate Master (DRP)",
#             configuration_url=None,
#         )

#     # ---- Proprietà richieste

#     @property
#     def hvac_mode(self) -> HVACMode:
#         snap = self._get_snapshot()
#         mode = snap.get("hvac_mode")
#         if isinstance(mode, HVACMode):
#             return mode
#         # consentire anche stringhe ("heat","cool",...) da engine non ancora tipizzato
#         if isinstance(mode, str):
#             try:
#                 return HVACMode(mode)
#             except Exception:
#                 pass
#         return self._fallback_mode

#     @property
#     def hvac_action(self) -> HVACAction | None:
#         snap = self._get_snapshot()
#         action = snap.get("action")
#         if isinstance(action, HVACAction):
#             return action
#         if isinstance(action, str):
#             try:
#                 return HVACAction(action)
#             except Exception:
#                 return None
#         return None

#     @property
#     def current_temperature(self) -> float | None:
#         snap = self._get_snapshot()
#         val = snap.get("current_temp")
#         try:
#             return float(val) if val is not None else None
#         except Exception:
#             return None

#     @property
#     def target_temperature(self) -> float | None:
#         snap = self._get_snapshot()
#         val = snap.get("target_temp")
#         if val is None:
#             return self._fallback_target
#         try:
#             return float(val)
#         except Exception:
#             return self._fallback_target

#     # ---- Comandi utente

#     async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
#         engine = getattr(self.coordinator, "engine", None)
#         if engine and hasattr(engine, "async_set_zone_hvac_mode"):
#             await engine.async_set_zone_hvac_mode(self._zone, hvac_mode)
#         else:
#             # fallback silenzioso: memorizza in cache locale
#             self._fallback_mode = hvac_mode
#         # Notifica aggiornamento stato
#         self.async_write_ha_state()

#     async def async_set_temperature(self, **kwargs: Any) -> None:
#         temperature = kwargs.get("temperature")
#         if temperature is None:
#             return
#         engine = getattr(self.coordinator, "engine", None)
#         if engine and hasattr(engine, "async_set_zone_target"):
#             await engine.async_set_zone_target(self._zone, float(temperature))
#         else:
#             self._fallback_target = float(temperature)
#         self.async_write_ha_state()

#     # ---- Helper

#     def _get_snapshot(self) -> Dict[str, Any]:
#         """Recupera lo snapshot della zona dal coordinator/engine, con fallback robusto."""
#         # 1) Se il coordinator non c'è (fase very-early), restituisci fallback
#         if self.coordinator is None:
#             return self._fallback_snapshot()

#         # 2) Prova a leggere dall'engine (preferito)
#         engine = getattr(self.coordinator, "engine", None)
#         if engine and hasattr(engine, "async_get_zone_snapshot"):
#             # L'engine potrebbe essere solo async; ma qui siamo in proprietà sync.
#             # Offriamo una cache lato coordinator: `coordinator.data` deve essere aggiornato.
#             pass

#         # 3) Prova `coordinator.data` (es. una struttura aggregata aggiornata dal Coordinator)
#         data = getattr(self.coordinator, "data", None)
#         if isinstance(data, dict):
#             zones = data.get("zones", {})
#             snap = zones.get(self._zone)
#             if isinstance(snap, dict):
#                 # Normalizza chiavi attese
#                 return {
#                     "current_temp": snap.get("current_temp"),
#                     "target_temp": snap.get("target_temp"),
#                     "hvac_mode": snap.get("hvac_mode"),
#                     "action": snap.get("action"),
#                 }

#         # 4) Fallback: snapshot locale
#         return self._fallback_snapshot()

#     def _fallback_snapshot(self) -> Dict[str, Any]:
#         return {
#             "current_temp": None,
#             "target_temp": self._fallback_target,
#             "hvac_mode": self._fallback_mode,
#             "action": None,
#         }
        

