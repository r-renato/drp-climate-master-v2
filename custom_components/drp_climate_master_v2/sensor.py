# sensor.py
from __future__ import annotations

import logging
from typing import Any, Optional, List, cast
from abc import ABC, abstractmethod

from statistics import fmean

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
    PERCENTAGE,
    UnitOfTemperature,
)

from .helpers.sensor_aggregator import AggregatedValue

from .controller.coordinator import ClimateCoordinator

from .domain.models.runtime_schema import SensorPair

from .helpers.logger import log_debug, log_exception, log_info, log_warning
from .helpers.utils import slugify, as_float

from .helpers.psychrometric import celsius_to_fahrenheit, dew_point_celsius, heat_index_celsius
from .const import (
    COORDINATOR,
    DOMAIN,
    ENTITIES_STATE,
    INTEGRATION_MANUFACTURER,
    INTEGRATION_NAME,
    INTEGRATION_VERSION,
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
        for cfg in coordinator.build_slave_sensor_defs():
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

            if cfg["type"] == "CurrentTemperatureSensor":
                sensor = CurrentTemperatureSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    temp_sensors=cfg["temp_sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                home_unique_ids_store["temperature"] = sensor
                entities.append(sensor)

            if cfg["type"] == "CurrentHumiditySensor":
                sensor = CurrentHumiditySensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    humi_sensors=cfg["humi_sensors"],
                    humidity_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                home_unique_ids_store["humidity"] = sensor
                entities.append(sensor)

            if cfg["type"] == "CurrentDewpointSensor":
                sensor = CurrentDewpointSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    temp_sensors=cfg["temp_sensors"],
                    humi_sensors=cfg["humi_sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                home_unique_ids_store["dew_point"] = sensor
                entities.append(sensor)

            if cfg["type"] == "CurrentHeatIndexSensor":
                sensor = CurrentHeatIndexSensor(
                    hass=hass,
                    coordinator=coordinator,
                    entry=entry,
                    name=cfg["name"],
                    temp_sensors=cfg["temp_sensors"],
                    humi_sensors=cfg["humi_sensors"],
                    temperature_unit=cfg["unit"],
                )
                # unique_id[sensor.unique_id] = None
                home_unique_ids_store["heat_index"] = sensor
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
        sensor_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._coordinator: ClimateCoordinator = cast(ClimateCoordinator, coordinator)
        self._entry = entry
        self._area = area
        self._attr_name = name
        # unique_id stabile e safe
        self._attr_unique_id: str = slugify(f"{entry.entry_id}_{unique_key}")

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
    
    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

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

class CurrentTemperatureSensor(BaseSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    T_NAME_POSTFIX = "Temperature"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        temp_sensors: List[str],
        temperature_unit: UnitOfTemperature | str = UnitOfTemperature.CELSIUS,
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.T_NAME_POSTFIX}",
            unique_key=f"{name} {self.T_NAME_POSTFIX} uid",
            sensor_unit=temperature_unit,
        )

        self._temp_sensors = temp_sensors

    def _slave_update(self) -> bool:
        """
        Ritorna la temperatura corrente media da tutti i sensori di temperatura
        configurati, in °C o °F a seconda dell'unità dell'entità.
        """
        if not self._temp_sensors:
            log_warning(_LOGGER, "Nessun sensore di temperatura configurato per %s", self.entity_id)
            self._attr_available = False
            return True

        # Leggi i valori dai sensori configurati
        temps = [as_float(self._entities_state.get(s)) for s in self._temp_sensors]
        valid_temps = [t for t in temps if t is not None]

        if len(temps) != len(valid_temps):
            log_warning(_LOGGER,
                "Alcuni sensori di temperatura non disponibili per %s: %d/%d validi",
                self.entity_id,
                len(valid_temps),
                len(temps),
            )
            self._attr_available = False
            return True

        avg_c = fmean(valid_temps)

        # Conversione nell'unità richiesta dall'entità
        if self._target_temp_unit == UnitOfTemperature.FAHRENHEIT:
            avg = celsius_to_fahrenheit(avg_c)
        else:
            avg = avg_c

        # Aggiorna la rolling window (lista) e calcola media con stdlib
        self._data_series.append(avg)
        if len(self._data_series) > self._window_size:
            self._data_series = self._data_series[-self._window_size:]

        attr_native_old_value = self._attr_native_value
        self._attr_native_value = fmean(self._data_series) if self._data_series else avg
        self._attr_available = True

        return attr_native_old_value != self._attr_native_value

class CurrentHumiditySensor(BaseSensor):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # è una misura "normale", non diagnostica

    H_NAME_POSTFIX = "Humidity"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        humi_sensors: List[str],
        humidity_unit: UnitOfTemperature | str = PERCENTAGE
    ) -> None:
        super().__init__(
            hass=hass,
            coordinator=coordinator,
            entry=entry,
            name=f"{name} {self.H_NAME_POSTFIX}",
            unique_key=f"{name} {self.H_NAME_POSTFIX} uid",
            sensor_unit=humidity_unit,
        )

        self._humi_sensors = humi_sensors

    def _slave_update(self) -> bool:
        """Ricalcola l'umidità media (con clamp 0..100) e applica smoothing nella finestra."""
        if not self._humi_sensors:
            _LOGGER.debug("Nessun sensore di umidità configurato per %s", self.entity_id)
            self._attr_available = False
            self._attr_native_value = None
            return True

        # Leggi i valori dai sensori configurati
        humis = [as_float(self._entities_state.get(s)) for s in self._humi_sensors]
        valid_humis = [t for t in humis if t is not None]

        if len(humis) != len(valid_humis):
            log_warning(_LOGGER,
                "Alcuni sensori di umidità non disponibili per %s: %d/%d validi",
                self.entity_id,
                len(valid_humis),
                len(humis),
            )
            self._attr_available = False
            return True

        vals: list[float] = []
        for eid in self._humi_sensors:
            v = as_float(self._entities_state.get(eid))
            if v is not None:
                # clamp 0..100
                vals.append(max(0.0, min(100.0, v)))

        if not vals:
            log_debug(_LOGGER, "Nessun valore valido dai sensori di umidità per %s", self.entity_id)
            self._attr_available = False
            self._attr_native_value = None
            return True

        avg = fmean(vals)

        # Smoothing con finestra mobile
        self._data_series.append(avg)
        if len(self._data_series) > self._window_size:
            self._data_series = self._data_series[-self._window_size:]

        out = fmean(self._data_series) if self._data_series else avg
        attr_native_old_value = self._attr_native_value
        self._attr_native_value = out
        self._attr_available = True

        return attr_native_old_value != self._attr_native_value

class CurrentDewpointSensor(BaseSensor):
    """
    Punto di rugiada medio corrente calcolato da più sensori di T e UR.

    - Legge una lista di sensori di temperatura (°C) e umidità (% o frazione 0..1)
    - Normalizza l'umidità (0..1 -> 0..100), applica clamp 0..100
    - Calcola il dew point in °C, quindi converte nell'unità dell'entità (°C/°F)
    - Applica una media mobile su una finestra calcolata dal BaseSensor
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # misura “normale”, non diagnostica

    DWP_NAME_POSTFIX = "Dew-Point"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        temp_sensors: List[str],
        humi_sensors: List[str],
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
        self._temp_sensors = temp_sensors or []
        self._humi_sensors = humi_sensors or []

    def _slave_update(self) -> bool:
        """
        Calcola e aggiorna `_attr_native_value` e `_attr_available`.
        Esegue smoothing su finestra mobile definita in BaseSensor.
        """
        # 1) Validazione base liste
        if not self._temp_sensors or not self._humi_sensors:
            log_debug(_LOGGER, "%s: liste sensori incomplete (temp=%d, humi=%d)",
                self.entity_id, len(self._temp_sensors), len(self._humi_sensors),
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 2) Lettura valori
        temps = [as_float(self._entities_state.get(eid)) for eid in self._temp_sensors]
        humis = [as_float(self._entities_state.get(eid)) for eid in self._humi_sensors]

        valid_temps = [t for t in temps if t is not None]
        valid_humis = [h for h in humis if h is not None]

        if len(temps) != len(valid_temps) or len(humis) != len(valid_humis):
            log_debug(_LOGGER, "%s: nessun dato valido (T:%d/%d, RH:%d/%d)",
                self.entity_id, len(valid_temps), len(temps), len(valid_humis), len(humis)
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 3) Medie sorgente (in °C e %)
        avg_temp_c = fmean(valid_temps)

        # Normalizza umidità: accetta 0..1 come frazione → %
        norm_humis: list[float] = []
        for h in valid_humis:
            if 0.0 <= h <= 1.0:
                h *= 100.0
            # clamp 0..100
            norm_humis.append(max(0.0, min(100.0, h)))
        avg_humi_pct = fmean(norm_humis)

        # 4) Calcolo dew point (in °C)
        try:
            dp_c = dew_point_celsius(avg_temp_c, avg_humi_pct)
        except Exception as ex:  # noqa: BLE001
            log_debug(_LOGGER,
                "%s: errore calc dewpoint T=%.2f°C RH=%.2f%% → %s",
                self.entity_id, avg_temp_c, avg_humi_pct, ex
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 5) Conversione nell'unità richiesta
        out_val = (
            celsius_to_fahrenheit(dp_c)
            if self._target_temp_unit == UnitOfTemperature.FAHRENHEIT
            else dp_c
        )

        self._data_series.append(out_val)
        if len(self._data_series) > self._window_size:
            self._data_series = self._data_series[-self._window_size:]

        smoothed = fmean(self._data_series) if self._data_series else out_val
        attr_native_old_value = self._attr_native_value
        self._attr_native_value = smoothed
        self._attr_available = True

        return attr_native_old_value != self._attr_native_value

class CurrentHeatIndexSensor(BaseSensor):
    """
    Heat Index medio corrente calcolato da più sensori di T e UR.

    - Legge una lista di sensori di temperatura (°C) e umidità (% o frazione 0..1)
    - Normalizza l'umidità (0..1 -> 0..100), clamp 0..100
    - Calcola l'Heat Index in °C (algoritmo NWS/Rothfusz) e converte in °F se richiesto
    - Applica una media mobile su una finestra calcolata nel BaseSensor
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # misura “normale”, non diagnostica

    HNX_NAME_POSTFIX = "Heat-Index"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry: ConfigEntry,
        name: str,
        temp_sensors: List[str],
        humi_sensors: List[str],
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
        self._temp_sensors = temp_sensors or []
        self._humi_sensors = humi_sensors or []

    def _slave_update(self) -> bool:
        """
        Calcola e aggiorna `_attr_native_value` e `_attr_available`.
        Esegue smoothing su finestra mobile definita in BaseSensor.
        """
        # 1) Validazione liste
        if not self._temp_sensors or not self._humi_sensors:
            log_debug(_LOGGER,
                "%s: liste sensori incomplete (temp=%d, humi=%d)",
                self.entity_id, len(self._temp_sensors), len(self._humi_sensors),
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 2) Lettura valori
        temps = [as_float(self._entities_state.get(eid)) for eid in self._temp_sensors]
        humis = [as_float(self._entities_state.get(eid)) for eid in self._humi_sensors]

        valid_temps = [t for t in temps if t is not None]
        valid_humis = [h for h in humis if h is not None]

        if not valid_temps or not valid_humis:
            log_debug(_LOGGER,
                "%s: nessun dato valido (T:%d/%d, RH:%d/%d)",
                self.entity_id, len(valid_temps), len(temps), len(valid_humis), len(humis)
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 3) Medie sorgente (°C e %)
        avg_temp_c = fmean(valid_temps)

        norm_humis: list[float] = []
        for h in valid_humis:
            if 0.0 <= h <= 1.0:
                h *= 100.0
            norm_humis.append(max(0.0, min(100.0, h)))
        avg_humi_pct = fmean(norm_humis)

        # 4) Calcolo Heat Index in °C
        try:
            hi_c = float(heat_index_celsius(avg_temp_c, avg_humi_pct))
        except Exception as ex:  # noqa: BLE001
            log_debug(_LOGGER,
                "%s: errore calc HI T=%.2f°C RH=%.2f%% → %s",
                self.entity_id, avg_temp_c, avg_humi_pct, ex
            )
            self._attr_available = False
            self._attr_native_value = None
            return True

        # 5) Conversione nell'unità richiesta (output °C/°F)
        out_val = (
            celsius_to_fahrenheit(hi_c)
            if self._target_temp_unit == UnitOfTemperature.FAHRENHEIT
            else hi_c
        )

        self._data_series.append(out_val)
        if len(self._data_series) > self._window_size:
            self._data_series = self._data_series[-self._window_size:]

        smoothed = fmean(self._data_series) if self._data_series else out_val
        attr_native_old_value = self._attr_native_value
        self._attr_native_value = smoothed
        self._attr_available = True

        return attr_native_old_value != self._attr_native_value
