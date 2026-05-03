# sensor.py
from __future__ import annotations

import logging
from typing import Any, Optional, List, cast
from abc import ABC, abstractmethod

from homeassistant.components.sensor import (
    SensorEntity,
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
)

from .helpers.formatter import fbool, fnum, fstr

from .helpers.builders.config_slave_sensors import build_slave_sensor_defs
from .plant.decision.contracts import PlantDecision, PlantMode

from .controller.coordinator import ClimateCoordinator
from .controller.supervisor import ClimateSupervisor

from .domain.models.runtime_schema import SensorPair

from .helpers.sensor_aggregator import AggregatedValue
from .helpers.logger import log_debug, log_exception, log_info, log_warning
from .helpers.utils import slugify, as_float

from .const import (
    COORDINATOR,
    DOMAIN,
    ENTITIES_STATE,
    INTEGRATION_MANUFACTURER,
    INTEGRATION_NAME,
    INTEGRATION_VERSION,
    SUPERVISOR,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Setup della piattaforma sensor per questa ConfigEntry.

    - Recupera il Coordinator dallo store di integrazione
    - Costruisce i DewpointSensor
    - Li registra con async_add_entities
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: ClimateCoordinator = store.get(COORDINATOR)
    supervisor: ClimateSupervisor = store.get(SUPERVISOR)
    # unique_id = store.setdefault("unique_id", {})
    area_unique_ids_store = store.setdefault("area_unique_ids", {})
    home_unique_ids_store = store.setdefault("home_unique_ids", {})

    def _set_area_id(area: str, data: dict[str, Any]) -> None:
        """..."""
        area_unique_ids_store.setdefault(area, data)

    if coordinator is None:
        log_warning(_LOGGER,
            "Coordinator non trovato per entry %s: nessuna entity sensor aggiunta",
            entry.entry_id,
        )
        return

    entities: List[SensorEntity] = []
    try:
        area_data: dict[str, Any] = {}
        for cfg in build_slave_sensor_defs(coordinator.runtime_config):
            log_info(_LOGGER, "Try to add %s", cfg)

            if cfg["type"] == "TemperatureSensor":
                sensor = TemperatureSensor(
                    hass=hass,
                    coordinator=coordinator,
                    supervisor=supervisor,
                    entry=entry,
                    name=cfg["name"],
                    area=cfg.get("area"),
                    sensors=cfg["sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            if cfg["type"] == "HumiditySensor":
                sensor = HumiditySensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    sensors=cfg["sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            if cfg["type"] == "DewpointSensor":
                sensor = DewpointSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    sensors=cfg["sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                area_data["dew_point"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            if cfg["type"] == "HeatIndexSensor":
                sensor = HeatIndexSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    sensors=cfg["sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                area_data["heat_index"] = sensor
                # _set_area_id(cfg.get("area", "default"), "heat_index", sensor)
                entities.append(sensor)

            if cfg["type"] == "SeasonSensor":
                sensor = SeasonSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            if cfg["type"] == "PDCSensor":
                sensor = PDCSensor(
                    hass=hass,
                    coordinator=coordinator,
                    supervisor=supervisor,
                    entry=entry,
                    name=cfg["name"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            if cfg["type"] == "VMCSensor":
                sensor = VMCSensor(
                    hass=hass,
                    coordinator=coordinator,
                    supervisor=supervisor,
                    entry=entry,
                    name=cfg["name"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)
            
            if cfg["type"] == "RadiantSensor":
                sensor = RadiantSensor(
                    hass=hass,
                    coordinator=coordinator,
                    supervisor=supervisor,
                    entry=entry,
                    name=cfg["name"],
                )
                # unique_id[sensor.unique_id] = None
                # area_data["temperature"] = sensor
                # _set_area_id(cfg.get("area", "default"), "dew_point", sensor)
                entities.append(sensor)

            _set_area_id(cfg.get("area", "default"), area_data)

        log_debug(_LOGGER, "area_unique_ids_store %s", area_unique_ids_store)
        log_debug(_LOGGER, "home_unique_ids_store %s", home_unique_ids_store)

        setup_unique_ids_store = store.setdefault("setup_unique_ids", True)
        log_debug(_LOGGER, "setup_unique_ids_store %s", setup_unique_ids_store)

    except Exception as ex:  # noqa: BLE001
        log_exception(_LOGGER, "Errore durante creazione sensori dew-point: %s", ex)

    if entities:
        async_add_entities(entities)  # update_before_add=False di default
        log_info(_LOGGER, "Aggiunte %d entità a %s.sensor", len(entities), DOMAIN)
    else:
        log_info(_LOGGER, "Nessuna entità sensor da aggiungere per %s", entry.entry_id)

class BaseSensor(
    CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]],
    RestoreSensor,
    SensorEntity,
    ABC,
):
    """
    Base class per sensori guidati da DataUpdateCoordinator.

    - Disabilita il polling (gli update arrivano dal coordinator).
    - Fornisce `device_info` coerente con l'entry dell'integrazione.
    - Espone un accesso comodo allo store degli stati delle entità esterne
      tenuto dall'integrazione (chiave ENTITIES_STATE).
    """

    _attr_should_poll = False
    _attr_has_entity_name = False  # ← il nome dell’entità sarà ESATTAMENTE self._attr_name
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # default: diagnostico
    
    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        unique_key: str,
        area: Optional[str] = None,
        sensor_unit: UnitOfTemperature | str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._coordinator: ClimateCoordinator = cast(ClimateCoordinator, coordinator)
        self._entry = entry
        self._area = area
        self._attr_name = name
        # unique_id stabile e safe
        self._attr_unique_id: str | None  = slugify(f"{entry.entry_id}_{unique_key}")

        # Normalizzazione *generica* dell’unità:
        self._target_temp_unit: UnitOfTemperature | None = None

        if isinstance(sensor_unit, UnitOfTemperature):
            # Unità di temperatura espresse come enum
            self._target_temp_unit = sensor_unit
            self._attr_native_unit_of_measurement = sensor_unit
        elif isinstance(sensor_unit, str):
            tu = sensor_unit.strip().upper().replace("°", "")
            if tu in ("C", "CELSIUS"):
                self._target_temp_unit = UnitOfTemperature.CELSIUS
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            elif tu in ("F", "FAHRENHEIT"):
                self._target_temp_unit = UnitOfTemperature.FAHRENHEIT
                self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
            else:
                # Esempio: "%", "ppm", ecc. → lascia invariato
                self._attr_native_unit_of_measurement = sensor_unit
                self._target_temp_unit = None
        else:
            # Fallback sicuro
            self._target_temp_unit = UnitOfTemperature.CELSIUS
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

        # Rolling window dimensionata sull’update_interval del coordinator
        interval_s = 60
        try:
            if getattr(coordinator, "update_interval", None):
                interval_s = max(1, int(coordinator.update_interval.total_seconds()))  # type: ignore
        except Exception:
            pass
        self._window_size = max(1, min(3 * 3600 // interval_s, 1000))
        self._data_series: list[float] = []

        self._zone_snapshot = None

    @property
    def _entities_state(self) -> dict[str, Any]:
        """
        Ritorna il dizionario degli stati delle entità esterne gestito
        dall'integrazione (popolato altrove).

        Returns:
            dict[str, Any]: mappa entity_id -> valore (numero/str/State).
        """
        return self._hass.data[DOMAIN][self._entry.entry_id][ENTITIES_STATE]

    @property
    def native_value(self) -> Optional[float]:
        value = as_float(self._attr_native_value)

        if value is None:
            return None
        
        return round(value, self._attr_suggested_display_precision)
    
    # @property
    # def unique_id(self) -> str:
    #     return self._attr_unique_id

    @property
    def device_info(self) -> DeviceInfo:
        """Metadati del dispositivo per raggruppare le entity nel pannello Dispositivi."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=INTEGRATION_NAME,
            manufacturer=INTEGRATION_MANUFACTURER,
            sw_version=str(INTEGRATION_VERSION),
        )

    @property
    def available(self) -> bool:
        """Disponibilità legata allo stato dell'ultimo aggiornamento del coordinator."""
        return bool(self.coordinator.last_update_success)

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        
        data: dict[str, Any] = {}
        data[ "integration" ] = INTEGRATION_NAME
        data[ "manufacturer" ] = INTEGRATION_MANUFACTURER

        return data

    async def async_added_to_hass(self) -> None:
        """
        Hook chiamato quando l'entità è aggiunta a Home Assistant.

        Qui ripristiniamo lo stato precedente (se presente) per ridurre
        l'effetto "Unknown" al riavvio.
        """
        await super().async_added_to_hass()

        # Assicurati che l'entity_id esista
        if not self.entity_id:
            return
        
        # (opzionale) prendi anche il unique_id dal registry
        reg = er.async_get(self.hass)
        entry = reg.async_get(self.entity_id)

        uid = (entry.unique_id if entry else None) or self.unique_id
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault("uid_map", {})
        store[uid] = self.entity_id
                
        # 
        # Qui ripristiniamo lo stato precedente (se presente) per ridurre
        # l'effetto "Unknown" al riavvio.
        #        
        last_state = await self.async_get_last_state()
        if last_state and self._attr_native_value is None:
            try:
                # Proviamo a ripristinare il valore numerico dal last_state
                self._attr_native_value = as_float(last_state.state)
                self.async_write_ha_state()
            except Exception as ex:  # noqa: BLE001
                log_debug(_LOGGER, "Restore skipped for %s: %s", self.entity_id, ex)


    def _slave_update_from_aggregator(self, entity_type: str, entity_id: str) -> bool:
        if not self._coordinator.ready:
            return False

        attr_native_old_value = self._attr_native_value

        if self._area is not None and self._coordinator.plant_snapshot is not None:
            area = slugify(self._area)
            indoor_zones = self._coordinator.plant_snapshot.indoor_zones
            self._zone_snapshot = indoor_zones.get(area) if indoor_zones is not None else None

        if self._coordinator.sensor_aggregator is not None:
            entity_aggr_value: AggregatedValue = self._coordinator.sensor_aggregator.get(entity_id)
            
            if entity_aggr_value.is_stale or entity_aggr_value.is_insufficient or entity_aggr_value.value is None:
                self._attr_available = False
                log_warning(_LOGGER, "%s: %s non disponibile (source=%s, stale=%s, insufficient=%s)", 
                    self.entity_id, entity_type, entity_id, entity_aggr_value.is_stale, entity_aggr_value.is_insufficient
                )
                return True
            
            self._attr_native_value = entity_aggr_value.value
            self._attr_available = True
        else:
            self._attr_available = False
            return True
        
        return attr_native_old_value != self._attr_native_value

    # --- QUI il metodo astratto che i figli DEVONO implementare ---
    @abstractmethod
    def _slave_update(self) -> bool:
        """..."""

    def _save_myself_state(self) -> None:
        """Salva lo State corrente dell'entità nello store condiviso."""
        if not self.entity_id:
            return
        st: State | None = self.hass.states.get(self.entity_id)
        if st is None:
            return
        self._entities_state[self.entity_id] = st

    @callback
    def _handle_coordinator_update(self) -> None:
        """Invocato ad ogni update del coordinator."""
        try:
            if self._slave_update():  # calcola/aggiorna _attr_native_value ecc.
                self._save_myself_state()  # pubblica lo stato aggiornato
        finally:
            # importantissimo: notifica HA che lo stato è cambiato
            super()._handle_coordinator_update()
        
class TemperatureSensor(BaseSensor):
    """
    Sensore di Temperatura.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    TEMP_NAME_POSTFIX = "Temperature"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        supervisor: ClimateSupervisor,
        entry: ConfigEntry,
        name: str,
        sensors: SensorPair,
        area: Optional[str] = None,
        temperature_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.TEMP_NAME_POSTFIX}",
            unique_key=f"{name} {self.TEMP_NAME_POSTFIX} uid",
            area=area,
            sensor_unit=temperature_unit,
        )

        self._supervisor = supervisor
        self._sensors = sensors

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        
        data: dict[str, Any] = super().extra_state_attributes

        last_pd = self._supervisor.last_plant_decision
        if last_pd is not None and last_pd.derived_input.comfort_bands_by_zone is not None and self._area is not None:
            comfort_bands = last_pd.derived_input.comfort_bands_by_zone
            
            cb = comfort_bands.get(slugify(self._area), None)
            if cb is not None:
                # data[ "air speed (m/s)" ] = (
                #     f"v_air best={fnum(cb.v_air_best, 3)}, "
                #     f"v_air lo={fnum(cb.v_air_lo, 3)}, "
                #     f"v_air hi={fnum(cb.v_air_hi, 3)}, "
                #     f"v_air draft={fnum(cb.v_air_draft, 3)}"
                # )
                data[ "air speed (m/s)" ] = {
                    "v_air_best": round(cb.v_air_best, 2),
                    "v_air_lo": round(cb.v_air_lo, 2),
                    "v_air_hi": round(cb.v_air_hi, 2),
                    "v_air_draft": round(cb.v_air_draft, 2) if cb.v_air_draft is not None else None
                }

                # data[ "comfort band (°C)" ] = (
                #     f"t_op min={fnum(cb.t_op_min, 2)}, "
                #     f"t_op max={fnum(cb.t_op_max, 2)}"
                # )
                data[ "comfort band (°C)" ] = {
                    "t_op_min": round(cb.t_op_min, 2),
                    "t_op_max": round(cb.t_op_max, 2)
                }
                # data[ "evaluation" ] = (
                #     f"t_op={fnum(cb.t_op, 2)}, "
                #     f"PMV={fnum(cb.pmv, 2)}, "
                #     f"PPD={fnum(cb.ppd, 2)}, "
                #     f"in band={fbool(cb.ok, "True", "False")}, "
                # )
                data[ "evaluation" ] = {
                    "t_op": round(cb.t_op, 2) if cb.t_op is not None else None,
                    "PMV": round(cb.pmv, 2) if cb.pmv is not None else None,
                    "PPD": round(cb.ppd, 2) if cb.ppd is not None else None,
                    "in_band": cb.ok
                }
                # data[ "knobs" ] = (
                #     f"Humidity solve={fstr(cb.humidity_solve_mode)}, "
                #     f"PMV target={fnum(cb.pmv_center, 2)} ± {fnum(cb.pmv_band, 2)}, "
                #     f"met used={fnum(cb.met_used, 2)}, "
                #     f"clo used={fbool(cb.clo_used, "True", "False")}, "
                # )
                data[ "knobs" ] = {
                    "humidity_solve_mode": fstr(cb.humidity_solve_mode),
                    "PMV_target": f"{fnum(cb.pmv_center, 2)} ± {fnum(cb.pmv_band, 2)}",
                    "met_used": round(cb.met_used, 2) if cb.met_used is not None else None,
                    "clo_used": cb.clo_used
                }
        return data
    
    def _slave_update(self) -> bool:
        """..."""
        return self._slave_update_from_aggregator(entity_type=self.TEMP_NAME_POSTFIX, entity_id=self._sensors.temperature)

class HumiditySensor(BaseSensor):
    """
    Sensore di Umidità.
    """

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    HUMI_NAME_POSTFIX = "Humidity"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        sensors: SensorPair,
        temperature_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.HUMI_NAME_POSTFIX}",
            unique_key=f"{name} {self.HUMI_NAME_POSTFIX} uid",
            sensor_unit=temperature_unit,
        )

        self._sensors = sensors

    def _slave_update(self) -> bool:
        """..."""
        return self._slave_update_from_aggregator(entity_type=self.HUMI_NAME_POSTFIX, entity_id=self._sensors.humidity)
    
class DewpointSensor(BaseSensor):
    """
    Sensore di Punto di Rugiada (Dew Point).

    Calcola il dew point a partire da:
    - temperatura aria (°C)
    - umidità relativa (%)

    Il calcolo sfrutta `dew_point_celsius(...)` che a sua volta usa PsychroLib.
    L'unità di misura esposta può essere °C o °F (conversione effettuata qui).
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    DWP_NAME_POSTFIX = "Dew-Point"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        sensors: SensorPair,
        temperature_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.DWP_NAME_POSTFIX}",
            unique_key=f"{name} {self.DWP_NAME_POSTFIX} uid",
            sensor_unit=temperature_unit,
        )

        self._sensors = sensors

    def _slave_update(self) -> bool:
        """..."""
        if self._sensors.dew_point is None:
            self._attr_available = False
            return True
        
        return self._slave_update_from_aggregator(entity_type=self.DWP_NAME_POSTFIX, entity_id=self._sensors.dew_point)

class HeatIndexSensor(BaseSensor):
    """
    Sensore di **Heat Index** (Indice di calore).

    Input:
      - Temperatura aria [°C]
      - Umidità relativa [%] (accetta anche frazione 0..1, viene normalizzata)

    Algoritmo:
      - Usa `heat_index_celsius(t_c, rh_pct)`:
        * converte T in °F
        * calcola HI “semplice” (Steadman/NWS) e media con T
        * se HI>=80°F usa la regressione di Rothfusz + aggiustamenti
        * ritorna HI in °C
      - L’entità esporta HI in °C o °F a seconda della configurazione.
    """
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        sensors: SensorPair,
        temperature_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.HNX_NAME_POSTFIX}",
            unique_key=f"{name} {self.HNX_NAME_POSTFIX} uid",
            sensor_unit=temperature_unit,
        )

        self._sensors = sensors
    
    def _slave_update(self) -> bool:
        """
        Ritorna l’Heat Index nella stessa unità dell’entità (°C o °F).
        """
        if self._sensors.heat_index is None:
            self._attr_available = False
            return True
        
        return self._slave_update_from_aggregator(entity_type=self.HNX_NAME_POSTFIX, entity_id=self._sensors.heat_index)

class SeasonSensor(BaseSensor):
    """
    Heat Index medio corrente calcolato da più sensori di T e UR.

    - Legge una lista di sensori di temperatura (°C) e umidità (% o frazione 0..1)
    - Normalizza l'umidità (0..1 -> 0..100), clamp 0..100
    - Calcola l'Heat Index in °C (algoritmo NWS/Rothfusz) e converte in °F se richiesto
    - Applica una media mobile su una finestra calcolata nel BaseSensor
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_state_class = None
    _attr_native_unit_of_measurement = None
    _attr_options = ["winter", "spring", "summer", "autumn"]  # adatta ai tuoi valori
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # misura “normale”, non diagnostica

    # HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
    ) -> None:
        full_name = f"{name}"

        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=full_name,
            unique_key=f"{full_name} uid",
            sensor_unit=None,
        )

        self._target_temp_unit = None
        self._attr_native_unit_of_measurement = None

    @property
    def native_value(self):        
        return self._attr_native_value
    
    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        def fnum(x, nd=1):
            return f"{x:.{nd}f}" if x is not None else "-"
        def fbool(b, on="on", off="off"):
            return on if b is True else (off if b is False else "-")
        
        data: dict[str, Any] = super().extra_state_attributes
        data["category"] = EntityCategory.DIAGNOSTIC

        if self._coordinator.plant_snapshot is not None and self._coordinator.plant_snapshot.season is not None:
            season = self._coordinator.plant_snapshot.season
            window = season.window
            weather = season.weather
            
            data["calendar window"] = f"{window.start.isoformat()} - {window.end.isoformat()}"
            # data["calendar window"] = [window.start.isoformat(), window.end.isoformat()]
            data["calendar days"] = f"{season.days}"
            data["calendar days passed"] = f"{season.passed}"
            data["calendar days remaining"] = f"{season.remaining}"

            # data["weather season"] = (
            #     f"{weather.season} [anomaly={weather.anomaly}, score={fnum(weather.anomaly_score)}] "
            # )
            data["weather reason"] = {
                "detect_model": season.weather_detect_model,
                "anomaly": weather.anomaly,
                "score": round(weather.anomaly_score, 2),
                "cold_snap": weather.cold_snap,
                "cold_snap_score": round(weather.cold_snap_score, 2),
            }
            data["weather regime"] = weather.regime_hint
            # data["weather signals"] = (
            #     f"t_low={weather.weather_day_signals.t_low} °C ",
            #     f"t_mean={weather.weather_day_signals.t_mean} °C ",
            #     f"dewp={weather.weather_day_signals.dew} °C ",
            #     f"wind={weather.weather_day_signals.wind} km/h ",
            #     f"cloud={fnum(weather.weather_day_signals.cloud*100) if weather.weather_day_signals.cloud else '-'} %",        
            # )
            data["weather signals"] = {
                "t_low": {weather.weather_day_signals.t_low},
                "t_mean": {weather.weather_day_signals.t_mean},
                "dewp": {weather.weather_day_signals.dew},
                "wind": {weather.weather_day_signals.wind},
                "cloud": {fnum(weather.weather_day_signals.cloud*100) if weather.weather_day_signals.cloud else '-'}
            }


            # data["weather signals eng."] = (
            #     f"smooth=[t={fnum(season.weather.weather_day_signals.t_smooth)} °C ",
            #     f"dewp={fnum(season.weather.weather_day_signals.dew_smooth)} °C] ",
            #     f"trend={fnum(season.weather.weather_day_signals.trend)}",
            # )
            data["weather signals eng."] = {
                "smooth": round(season.weather.weather_day_signals.t_smooth,2 ) if season.weather.weather_day_signals.t_smooth is not None else None,
                "dewp": round(season.weather.weather_day_signals.dew_smooth, 2) if season.weather.weather_day_signals.dew_smooth is not None else None,
                "trend": round(season.weather.weather_day_signals.trend, 2) if season.weather.weather_day_signals.trend is not None else None
            }
        return data
    
    def _slave_update(self) -> bool:
        """

        """
        if not self._coordinator.ready:
            return False
    
        if self._coordinator.plant_snapshot is not None and self._coordinator.plant_snapshot.season is not None:
            season = self._coordinator.plant_snapshot.season

            self._attr_native_value = season.weather.season.value
            self._attr_available = True
        else:
            self._attr_available = False
            
        self.async_write_ha_state()
        return True


class PDCSensor(BaseSensor):
    """

    """
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_state_class = None
    _attr_native_unit_of_measurement = None
    _attr_options = ["On", "Off"]  # adatta ai tuoi valori
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # misura “normale”, non diagnostica

    # HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        supervisor: ClimateSupervisor,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        full_name = f"{name}"

        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=full_name,
            unique_key=f"{full_name} uid",
            sensor_unit=None,
        )

        self._supervisor = supervisor

        self._target_temp_unit = None
        self._attr_native_unit_of_measurement = None

    @property
    def native_value(self):        
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""

        data: dict[str, Any] = super().extra_state_attributes
        data["category"] = EntityCategory.DIAGNOSTIC

        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.pdc is not None:
            pdc = last_plant_decision.pdc
            signals = last_plant_decision.signals
            gating = last_plant_decision.gating

            data["hvac mode"] = f"{gating.user_hvac_mode}"
            data["hvac preset"] = f"{gating.user_profile}"

            data["mode"] = f"{pdc.mode}"

            data["heat-wot"]   = f"{fnum(pdc.heat_wot_c, nd=1)} °C"
            data["heat-Δt"]   = f"{fnum(pdc.heat_dt_c, nd=1)} °C"
            data["cool-wot"]   = f"{fnum(pdc.cool_wot_c, nd=1)} °C"
            data["cool-Δt"]   = f"{fnum(pdc.cool_dt_c, nd=1)} °C"

            # Stagione e profilo (contesto decisione)
            data["operative season"] = f"{gating.operative_season}"
            data["runtime season"]   = f"{gating.runtime_season}"
            data["ctrl aggressiveness"] = f"{fnum(gating.ctrl_aggr, nd=2)}"

            # Domanda sensibile (il "perché" la PDC è accesa)
            data["heat def max"]   = f"{fnum(signals.heat_def_max_c, nd=1)} °C"
            data["heat def mean"]  = f"{fnum(signals.heat_def_wmean_c, nd=1)} °C"
            data["heat coverage"]  = f"{fnum(signals.heat_cov, nd=0)} %"
            data["heat on thr"]    = f"{fnum(gating.heat_on_thr_c, nd=1)} °C"
            data["cool sur max"]   = f"{fnum(signals.cool_sur_max_c, nd=1)} °C"
            data["cool coverage"]  = f"{fnum(signals.cool_cov, nd=0)} %"

            # Gating / quorum (il "se" la PDC parte)
            data["heat override"]  = gating.heat_override
            data["heat quorum ok"] = gating.heat_quorum_ok
            data["heat mean ok"]   = gating.heat_mean_ok
            data["quorum cov req"] = f"{fnum(gating.quorum_cov_req, nd=0)} %"
            data["any heat"]       = gating.any_heat
            data["any cool"]       = gating.any_cool

            # Curva climatica WOT (estratta dal debug — già calcolata)
            if isinstance(pdc.debug, dict):
                dbg = pdc.debug
                data["wot curve"]        = dbg.get("curve", "—")
                data["wot base"]         = f"{fnum(dbg.get('curve_base', 0), nd=2)} °C"
                data["wot profile off"]  = f"{fnum(dbg.get('profile_offset_c', 0), nd=1)} °C"
                data["wot feedback"]     = f"{fnum(dbg.get('feedback_c', 0), nd=2)} °C"
                data["wot kick"]         = f"{fnum(dbg.get('kick_c', 0), nd=2)} °C"
                data["wot pre rate"]     = f"{fnum(dbg.get('wot_target_pre_rate_c', 0), nd=2)} °C"
                data["t outdoor"]        = f"{fnum(dbg.get('t_out_c', 0), nd=1)} °C"
                data["regime hint"]      = dbg.get("regime_hint", "—")
                data["cold snap"]        = dbg.get("cold_snap", False)
                data["regime delta"]     = f"{fnum(dbg.get('regime_delta_c', 0), nd=2)} °C"
                data["activity scale"]   = f"{fnum(dbg.get('activity_scale', 1.0), nd=2)}"
                data["zones on now"]     = f"{fnum(dbg.get('zones_on_now_pct', 0), nd=1)} %"
                data["zones duty avg"]   = f"{fnum(dbg.get('zones_duty_avg_pct', 0), nd=1)} %"

            # MPC hints (attività zone — utile per leggere il carico richiesto)
            data["zones mpc heat"]     = gating.zones_any_heat_demand
            data["zones mpc on now"]   = f"{fnum(gating.zones_full_on_pct, nd=1)} %"
            data["zones mpc duty avg"] = f"{fnum(gating.zones_duty_avg_pct, nd=1)} %"
            data["zones mpc preheat"]  = gating.zones_mpc_heat_preheat_ok

        return data

    def _slave_update(self) -> bool:
        """

        """
        if not self._coordinator.ready:
            return False

        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.pdc is not None:
            self._attr_native_value = fbool(last_plant_decision.pdc.power, on='On', off='Off')
            self._attr_available = True
        else:
            self._attr_available = False
            
        self.async_write_ha_state()
        return True

class VMCSensor(BaseSensor):
    """

    """
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_state_class = None
    _attr_native_unit_of_measurement = None
    _attr_options = ["On", "Off"]  # adatta ai tuoi valori
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # misura “normale”, non diagnostica

    # HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        supervisor: ClimateSupervisor,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        full_name = f"{name}"

        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=full_name,
            unique_key=f"{full_name} uid",
            sensor_unit=None,
        )

        self._supervisor = supervisor

        self._target_temp_unit = None
        self._attr_native_unit_of_measurement = None

    @property
    def native_value(self):        
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""

        data: dict[str, Any] = super().extra_state_attributes
        data["category"] = EntityCategory.DIAGNOSTIC

        last_plant_decision: PlantDecision | None = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.vmc is not None:
            vmc = last_plant_decision.vmc
            signals = last_plant_decision.signals
            gating = last_plant_decision.gating

            # --- Contesto ---
            data["hvac mode"]        = f"{gating.user_hvac_mode}"
            data["hvac preset"]      = f"{gating.user_profile}"
            data["operative season"] = f"{gating.operative_season}"

            # --- Comando VMC ---
            data["mode"]    = f"{vmc.mode}"
            data["air speed"] = f"{fnum(vmc.air_speed, nd=0)}"
            data["set t"]   = f"{fnum(vmc.setpoint_t_c)} °C"
            data["set h"]   = f"{fnum(vmc.setpoint_rh_pct)} %"
            data["set dp"]  = f"{fnum(vmc.setpoint_dp_c)} °C"
            data["set Δdp"] = f"{fnum(vmc.setpoint_ddp_c)} °C"

            # --- Richieste calcolate dal planner ---
            data["req heating"]   = signals.vmc_req_heating
            data["req cooling"]   = signals.vmc_req_cooling
            data["req dehumidif"] = signals.vmc_req_dehumidif
            data["req water"]     = signals.vmc_req_water
            data["req free cool"] = signals.vmc_req_free_cooling
            data["req free heat"] = signals.vmc_req_free_heating

            # --- Soglie dew-point ---
            data["dp indoor max"]   = f"{fnum(signals.dp_max_c)} °C"
            data["dp dehum"]        = f"{fnum(signals.dp_dehum_c)} °C"
            data["dp outdoor"]      = f"{fnum(signals.outdoor_dp_c)} °C"
            data["dp sp raw"]       = f"{fnum(signals.vmc_dp_sp_raw_c)} °C"
            data["dehum on thr"]    = f"{fnum(signals.vmc_dehum_on_thr_c)} °C"
            data["dehum off thr"]   = f"{fnum(signals.vmc_dehum_off_thr_c)} °C"
            data["dehum feasible"]  = signals.vmc_dehum_feasible

            # --- Free conditioning ---
            data["free cool Δt"]       = f"{fnum(signals.free_cool_delta_c, nd=1)} °C"
            data["free heat Δt"]       = f"{fnum(signals.free_heat_delta_c, nd=1)} °C"
            data["free cool feasible"] = signals.free_cool_feasible
            data["free heat feasible"] = signals.free_heat_feasible

            data["debug"] = vmc.debug

        return data

    def _slave_update(self) -> bool:
        """

        """
        if not self._coordinator.ready:
            return False

        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.vmc is not None:
            self._attr_native_value = fbool(last_plant_decision.vmc.power, on='On', off='Off')
            self._attr_available = True
        else:
            self._attr_available = False
            
        self.async_write_ha_state()
        return True

class RadiantSensor(BaseSensor):
    """

    """
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_state_class = None
    _attr_native_unit_of_measurement = None
    _attr_options = ["On", "Off"]  # adatta ai tuoi valori
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # misura “normale”, non diagnostica

    # HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        supervisor: ClimateSupervisor,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        full_name = f"{name}"

        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=full_name,
            unique_key=f"{full_name} uid",
            sensor_unit=None,
        )

        self._supervisor = supervisor

        self._target_temp_unit = None
        self._attr_native_unit_of_measurement = None

    @property
    def native_value(self):        
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        data: dict[str, Any] = super().extra_state_attributes
        data["category"] = EntityCategory.DIAGNOSTIC
        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.supply is not None:
            supply = last_plant_decision.supply
            signals = last_plant_decision.signals
            gating = last_plant_decision.gating

            # --- Contesto ---
            data["hvac mode"]        = f"{gating.user_hvac_mode}"
            data["hvac preset"]      = f"{gating.user_profile}"
            data["operative season"] = f"{gating.operative_season}"

            # --- Comandi supply ---
            data["pump direct"]      = f"{fbool(supply.direct_pump_on)}"
            data["pump adj"]         = f"{fbool(supply.adj_pump_on)}"
            data["mix valve"]        = f"{fnum(supply.mix_valve_pct)} %"
            data["radiant target"]   = f"{fnum(supply.rad_supply_target_c)} °C"

            # --- Temperature circuiti (dal debug) ---
            if isinstance(supply.debug, dict):
                dbg = supply.debug
                data["adj supply flow"]    = f"{fnum(dbg.get('adj_supply_flow_c'))} °C"
                data["adj return flow"]    = f"{fnum(dbg.get('adj_return_flow_c'))} °C"
                data["direct supply flow"] = f"{fnum(dbg.get('direct_supply_flow_c'))} °C"
                data["direct return flow"] = f"{fnum(dbg.get('direct_return_flow_c'))} °C"
                data["boiler supply flow"] = f"{fnum(dbg.get('boiler_supply_flow_c'))} °C"
                data["boiler return flow"] = f"{fnum(dbg.get('boiler_return_flow_c'))} °C"
                # Calcolo valvola miscelatrice (t_target, t_primary, t_return)
                mvc = dbg.get("mix_valve_calc", {})
                if mvc:
                    data["mix t target"]   = f"{fnum(mvc.get('t_target_c'))} °C"
                    data["mix t primary"]  = f"{fnum(mvc.get('t_primary_c'))} °C"
                    data["mix t return"]   = f"{fnum(mvc.get('t_return_c'))} °C"

            # --- Domanda sensibile ---
            data["heat def max"]     = f"{fnum(signals.heat_def_max_c, nd=1)} °C"
            data["heat def mean"]    = f"{fnum(signals.heat_def_wmean_c, nd=1)} °C"
            data["heat coverage"]    = f"{fnum(signals.heat_cov * 100, nd=0)} %"
            data["heat headroom"]    = f"{fnum(signals.heat_headroom_min_c, nd=1)} °C"
            data["cool sur max"]     = f"{fnum(signals.cool_sur_max_c, nd=1)} °C"
            data["cool coverage"]    = f"{fnum(signals.cool_cov * 100, nd=0)} %"
            data["cool headroom"]    = f"{fnum(signals.cool_headroom_min_c, nd=1)} °C"

            # --- Dew point (safety gate radiante) ---
            data["dp indoor max"]    = f"{fnum(signals.dp_max_c, nd=1)} °C"
            data["dp dehum"]         = f"{fnum(signals.dp_dehum_c, nd=1)} °C"
            data["dp outdoor"]       = f"{fnum(signals.outdoor_dp_c, nd=1)} °C"

            # --- Flag finali ---
            data["any heat"]         = gating.any_heat
            data["any cool"]         = gating.any_cool
            data["heat sensible"]    = gating.heat_sensible
            data["cool sensible"]    = gating.cool_sensible
            data["heat override"]    = gating.heat_override

            # --- Zone MPC (guidano valvole e target supply) ---
            data["zones on now"]     = f"{fnum(gating.zones_on_now_pct, nd=1)} %"
            data["zones duty avg"]   = f"{fnum(gating.zones_duty_avg_pct, nd=1)} %"
            data["zones full on"]    = f"{fnum(gating.zones_full_on_pct, nd=1)} %"
            data["zones mpc heat"]   = gating.zones_any_heat_demand
            data["zones mpc preheat"]= gating.zones_mpc_heat_preheat_ok

            # --- VMC water request (determina pompa diretta) ---
            data["vmc req water"]    = signals.vmc_req_water
            data["vmc req heating"]  = signals.vmc_req_heating
            data["vmc req cooling"]  = signals.vmc_req_cooling

        return data

    def _slave_update(self) -> bool:
        """

        """
        if not self._coordinator.ready:
            return False

        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.supply is not None:
            supply = last_plant_decision.supply
            adj = supply.adj_pump_on
            direct = supply.direct_pump_on

            pump_any = (
                True if (adj is True or direct is True)
                else False if (adj is False and direct is False)
                else None
            )

            self._attr_native_value = fbool(pump_any, on='On', off='Off')
            self._attr_available = True
        else:
            self._attr_available = False
            
        self.async_write_ha_state()
        return True
