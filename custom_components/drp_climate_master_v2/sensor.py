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

from .helpers.formatter import fbool, fnum

from .helpers.builders.config_slave_sensors import build_slave_sensor_defs
from .plant.decision.contracts import PlantMode

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

        self._sensors = sensors

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the device."""
        def fnum(x, nd=1):
            return f"{x:.{nd}f}" if x is not None else "-"
        def fbool(b, on="on", off="off"):
            return on if b is True else (off if b is False else "-")
        
        data: dict[str, Any] = super().extra_state_attributes

        if self._area is not None and self._zone_snapshot is not None:
            cb = self._zone_snapshot.confort_band
            if cb is not None:
                data[ "air band" ] = (
                    f"in band={fbool(cb.v_air_best >= cb.v_air_lo and cb.v_air_best <= cb.v_air_hi, on='yes', off='no')}, "
                    f"speed={fnum(cb.speed, 0)}, "
                    f"best={fnum(cb.v_air_best, 2)}, "
                    f"low={fnum(cb.v_air_lo, 2)}, "
                    f"hi={fnum(cb.v_air_hi, 2)}"
                    )
                data[ "t band" ] = (
                    f"in band={fbool(cb.t_op is not None and cb.t_op >= cb.t_op_min and cb.t_op <= cb.t_op_max, on='yes', off='no')}, "
                    f"t_op={fnum(cb.t_op)}, "
                    f"t_min={fnum(cb.t_op_min)}, "
                    f"t_max={fnum(cb.t_op_max)}, "
                    f"[pmv={fnum(cb.pmv)}, ppd={fnum(cb.ppd)}%]"
                )
                data[ "pmv legend" ] = (
                    "-3=molto freddo, -2=freddo, -1=leggermente freddo, 0=neutro, +1=leggermente caldo, +2=caldo, +3=molto caldo"
                )

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

    # def _slave_update(self) -> bool:
    #     """
    #     Valore nativo del sensore (dew point).

    #     Ritorna:
    #         float | None: dew point nella stessa unità dichiarata dall'entità.
    #                       None se mancano i dati o non sono validi.
    #     """
    #     # Recupero valori grezzi dal registry interno dell’integrazione
    #     raw_t = self._entities_state.get(self._sensors.temperature)
    #     raw_rh = self._entities_state.get(self._sensors.humidity)

    #     t_c = as_float(raw_t)
    #     rh = as_float(raw_rh)

    #     if t_c is None or rh is None:
    #         self._attr_available = False
    #         return True

    #     # dew_point_celsius richiede T in °C e RH in percento
    #     try:
    #         dp_c = dew_point_celsius(t_c, rh)
    #     except Exception as ex:  # noqa: BLE001
    #         log_debug(_LOGGER, "Impossibile calcolare il dew point: %s", ex)
    #         self._attr_available = False
    #         return True

    #     # Conversione nell'unità richiesta dall'entità
    #     if self._target_temp_unit == UnitOfTemperature.FAHRENHEIT:
    #         dp_val = celsius_to_fahrenheit(dp_c)
    #     else:
    #         dp_val = dp_c

    #     # Aggiorna la rolling window (lista) e calcola media con stdlib
    #     self._data_series.append(dp_val)
    #     if len(self._data_series) > self._window_size:
    #         self._data_series = self._data_series[-self._window_size:]

    #     avg = fmean(self._data_series) if self._data_series else dp_val

    #     attr_native_old_value = self._attr_native_value
    #     self._attr_native_value = avg
    #     self._attr_available = True

    #     return attr_native_old_value != self._attr_native_value

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
            
            data["window"] = f"{window.start.isoformat()} - {window.end.isoformat()}"
            data["window days"] = f"{season.days} passed={season.passed} remaining={season.remaining}"

            data["weather season"] = (
                f"{weather.season} [anomaly={weather.anomaly}, score={fnum(weather.anomaly_score)}] "
            )
            data["weather regime"] = weather.regime_hint
            data["weather signals"] = (
                f"t_low={weather.weather_day_signals.t_low} °C ",
                f"t_mean={weather.weather_day_signals.t_mean} °C ",
                f"dewp={weather.weather_day_signals.dew} °C ",
                f"wind={weather.weather_day_signals.wind} km/h ",
                f"cloud={fnum(weather.weather_day_signals.cloud*100) if weather.weather_day_signals.cloud else '-'} %",        
            )
            data["weather signals eng."] = (
                f"smooth=[t={fnum(season.weather.weather_day_signals.t_smooth)} °C ",
                f"dewp={fnum(season.weather.weather_day_signals.dew_smooth)} °C] ",
                f"trend={fnum(season.weather.weather_day_signals.trend)}",
            )
        return data
    
    def _slave_update(self) -> bool:
        """

        """
        if not self._coordinator.ready:
            return False
    
        if self._coordinator.plant_snapshot is not None and self._coordinator.plant_snapshot.season is not None:
            season = self._coordinator.plant_snapshot.season

            self._attr_native_value = season.window.season.value
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

            data["hvac mode"] = f"{signals.user_hvac_mode}"
            data["hvac preset"] = f"{signals.user_profile}"

            data["mode"] = f"{pdc.mode}"

            if pdc.mode is not None and pdc.mode.lower() == PlantMode.HEATING.value.lower(): 
                data["heat-wot"] = f"{pdc.heat_wot_c} °C"
                data["heat-Δt"] = f"{pdc.heat_dt_c} °C"
            elif pdc.mode is not None and pdc.mode.lower() == PlantMode.COOLING.value.lower():
                data["cool-wot"] = f"{pdc.cool_wot_c} °C"
                data["cool-Δt"] = f"{pdc.cool_dt_c} °C"

            data["debug"] = f"{pdc.debug}"
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

        last_plant_decision = self._supervisor.last_plant_decision
        if last_plant_decision is not None and last_plant_decision.vmc is not None:
            vmc = last_plant_decision.vmc
            signals = last_plant_decision.signals

            data["hvac mode"] = f"{signals.user_hvac_mode}"
            data["hvac preset"] = f"{signals.user_profile}"

            data["mode"] = f"{vmc.mode}"
            data["air speed"] = f"{fnum(vmc.air_speed, nd=0)}"
            data["set t"] = f"{fnum(vmc.setpoint_t_c)} °C"
            data["set h"] = f"{fnum(vmc.setpoint_rh_pct)} %"
            data["set dp"] = f"{fnum(vmc.setpoint_dp_c)} °C"
            data["set Δdp"] = f"{fnum(vmc.setpoint_ddp_c)} °C"

            data["debug"] = f"{vmc.debug}"
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

            data["hvac mode"] = f"{signals.user_hvac_mode}"
            data["hvac preset"] = f"{signals.user_profile}"

            data["pump direct"] = f"{fbool(supply.direct_pump_on)}"
            data["pump adj"] = f"{fbool(supply.adj_pump_on)}"
            data["mix valve"] = f"{fnum(supply.mix_valve_pct)} %"
            data["radiant target"] = f"{fnum(supply.rad_supply_target_c)} °C"

            data["debug"] = f"{supply.debug}"
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
