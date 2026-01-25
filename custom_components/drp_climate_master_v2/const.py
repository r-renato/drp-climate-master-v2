"""Costanti per DRP Climate Master (Foundation)."""
from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Final
from importlib.resources import files  # Python 3.9+

import json
import logging

from homeassistant.const import Platform

_LOGGER = logging.getLogger(__name__)

# --- Metadati integrazione ----------------------------------------------------
class IntegrationMeta(NamedTuple):
    domain: str
    name: str
    version: str
    manufacturer: str
    issue_url: str

@lru_cache(maxsize=1)
def load_integration_meta(pkg: str = __package__ or "const") -> IntegrationMeta:
    """
    Legge manifest.json come risorsa del pacchetto (portabile anche con zipimport).
    Ritorna fallback sicuri se mancante/corrotto.
    """
    default = IntegrationMeta("N/A", "Unknown Integration", "N/A", "N/A", "N/A")
    try:
        text = (files(pkg) / "manifest.json").read_text(encoding="utf-8")
        data = json.loads(text)
        return IntegrationMeta(
            data.get("domain", default.domain),
            data.get("name", default.name),
            data.get("version", default.version),
            data.get("manufacturer", default.manufacturer),
            data.get("issue_tracker") or data.get("issue_url") or default.issue_url,
        )
    except Exception as e:
        _LOGGER.error("Manifest read error for %s: %s", pkg, e)
        return default

def make_startup_banner(meta: IntegrationMeta) -> str:
    return (
        "-------------------------------------------------------------------\n"
        f"{meta.name}\n"
        f"Version: {meta.version}\n"
        "This is a custom integration!\n"
        "If you have any issues with this you need to open an issue here:\n"
        f"{meta.issue_url}\n"
        "-------------------------------------------------------------------"
    )

_META = load_integration_meta()   # __package__ punta a custom_components.<domain>
DOMAIN, INTEGRATION_NAME, INTEGRATION_VERSION, INTEGRATION_MANUFACTURER, INTEGRATION_ISSUE_URL = _META
STARTUP_MESSAGE = make_startup_banner(_META)

# --- Identità integrazione ----------------------------------------------------

# DOMAIN: Final = "drp_climate_master_v2"

PLATFORMS = [Platform.SENSOR, Platform.CLIMATE]  # aggiungi Platform.SENSOR/NUMBER se in futuro esponi altre entità

COORDINATOR: Final = "coordinator"
SUPERVISOR: Final = "supervisor"
WEATHER_COORDINATOR: Final = "weather_coordinator"

# ======================================================
# Default
# ======================================================

DEFAULT_CLIMATE_NAME: Final[str] = "(DRP) Home Master"
DEFAULT_TEMP_UNIT: Final[str] = "°C"
DEFAULT_UNITS: Final[str] = "si"  # "metric" | "imperial"

OPT_UPDATE_INTERVAL_S: Final[int] = 90
OPT_UPDATE_MIN_INTERVAL_S: Final[int] = 60

ENTITIES_STATE: Final[str] = "entities_state"
ENTITIES_OBSERVED_TS: Final[str] = "entities_observed_ts"
SEASON_STATE: Final[str] = "season_state"

# ======================================================
# Coordinator
# ======================================================
NAME_AREA_HOME: Final[str] = "Home Current"

# ======================================================
# Schema
# ======================================================

# Sezioni top-level del blocco climate
CONF_UNITS : Final[str] = "units"
CONF_CLIMATE: Final[str] = "climate"
CONF_AREAS: Final[str] = "areas"
CONF_DEVICES: Final[str] = "devices"
CONF_WEATHER: Final[str] = "weather"
CONF_SCENARIOS: Final[str] = "scenarios"
CONF_HOME_WINDOWS_STATE: Final[str] = "home_windows_state"
CONF_WINDOWS: Final[str] = "windows"
CONF_CLOSED_STATE: Final[str] = "closed_state"
CONF_CONFORT_ZONES: Final[str] = "confort_zones"
CONF_TEMP_MIN: Final[str] = "temp_min"
CONF_TEMP_MAX: Final[str] = "temp_max"
CONF_HUMI_MIN: Final[str] = "humi_min"
CONF_HUMI_MAX: Final[str] = "humi_max"
CONF_DP_MIN: Final[str] = "dp_min"
CONF_DP_MAX: Final[str] = "dp_max"
CONF_SPRINT: Final[str] = "sprint"

# Limiti & step temperatura (opzionali, per UI/options)
CONF_MAX_TEMP: Final[str] = "max_temp"
CONF_MIN_TEMP: Final[str] = "min_temp"
CONF_STEP: Final[str] = "temp_step"

# ======================================================
# AREAS (Home Areas)
# ======================================================
CONF_AREA: Final[str] = "area"
CONF_INDOOR: Final[str] = "indoor"
CONF_TEMPERATURE: Final[str] = "temperature"
CONF_HUMIDITY: Final[str] = "humidity"
CONF_TCOLLECTOR: Final[str] = "thermal_collector_valve_switch"
CONF_CEILING: Final[str] = "ceiling"
CONF_RADIANT_SURFACE: Final[str] = "radiant_surface"

# ======================================================
# DEVICES
# ======================================================
# -- Supply Units
CONF_SUPPLY_UNITS: Final[str] = "supply_units"
CONF_DIRECT_SUPPLY_UNIT: Final[str] = "direct_supply_unit"
CONF_ADJUSTABLE_SUPPLY_UNIT: Final[str] = "adjustable_supply_unit"
CONF_THREE_POINT_MIXING_VALVE: Final[str] = "three_point_mixing_valve"

# Sensori supply units
CONF_BOILER_TEMP_SYSTEM_SUPPLY: Final[str] = "boiler_temp_system_supply"
CONF_BOILER_TEMP_SYSTEM_RETURN: Final[str] = "boiler_temp_system_return"
CONF_ADJUSTABLE_TEMP_SYSTEM_SUPPLY: Final[str] = "adjustable_temp_system_supply"
CONF_ADJUSTABLE_TEMP_SYSTEM_RETURN: Final[str] = "adjustable_temp_system_return"
CONF_DIRECT_TEMP_SYSTEM_SUPPLY: Final[str] = "direct_temp_system_supply"
CONF_DIRECT_TEMP_SYSTEM_RETURN: Final[str] = "direct_temp_system_return"

# -- Radiant
CONF_RADIANT: Final[str] = "radiant"
CONF_FM_POWER: Final[str] = "fm_power"
CONF_POWER: Final[str] = "power"
CONF_MODE: Final[str] = "mode"
CONF_ACTUATOR: Final[str] = "actuator"

# Setpoint/Delta-T radiant
CONF_HEATING_T_SETPOINT: Final[str] = "heating_t_setpoint"
CONF_HEATING_DT_SETPOINT: Final[str] = "heating_dt_setpoint"
CONF_COOLING_T_SETPOINT: Final[str] = "cooling_t_setpoint"
CONF_COOLING_DT_SETPOINT: Final[str] = "cooling_dt_setpoint"
CONF_VALUE: Final[str] = "value"

# Sensori radiant / PDC
CONF_PDC_TEMP_WATER_IN: Final[str] = "pdc_temp_water_in"
CONF_PDC_TEMP_WATER_OUT: Final[str] = "pdc_temp_water_out"
CONF_PDC_TEMP_OUTDOOR: Final[str] = "pdc_temp_outdoor"
CONF_PDC_COMPRESSOR_STATE: Final[str] = "pdc_compressor_state"

# -- VMC
CONF_VMC: Final[str] = "vmc"

# Comandi setpoint/forzature
CONF_T_SETPOINT: Final[str] = "t_setpoint"
CONF_H_SETPOINT: Final[str] = "h_setpoint"
CONF_DEW_POINT_SETPOINT: Final[str] = "t_dew_point_setpoint"
CONF_DELTA_DEW_POINT_SETPOINT: Final[str] = "delta_t_dew_point_setpoint"
CONF_SPARE_SETPOINT: Final[str] = "spare_setpoint"
CONF_VENT_RECIRCULATION: Final[str] = "vent_recirculation"
CONF_FORCE_HEATING: Final[str] = "force_heating"
CONF_FORCE_COOLING: Final[str] = "force_cooling"
CONF_FORCE_FREE_COOLING: Final[str] = "force_free_cooling"

# Stagioni
CONF_SEASON: Final[str] = "season"
CONF_WINTER: Final[str] = "winter"
CONF_SUMMER: Final[str] = "summer"
CONF_AUTUMN: Final[str] = "autumn"
CONF_SPRING: Final[str] = "spring"

# Gestione compressore / raffrescamento
CONF_COMPRESSOR_MANAGEMENT: Final[str] = "compressor_management"
CONF_DEHUMIDIFICATION_OR_COOLING: Final[str] = "dehumidification_or_cooling"
CONF_DEHUMIDIFICATION_ONLY: Final[str] = "dehumidification_only"
CONF_COOLING_ONLY: Final[str] = "cooling_only"

CONF_COOLING_MANAGEMENT: Final[str] = "cooling_management"
CONF_COMPRESSOR_ONLY: Final[str] = "compressor_only"
CONF_WATER_ONLY: Final[str] = "water_only"
CONF_FIRST_WATER_THEN_COMPRESSOR: Final[str] = "first_water_then_compressor"

# Richieste VMC
CONF_REQUESTS: Final[str] = "requests"
CONF_WATER: Final[str] = "water"
CONF_DEHUMIDIFICATION: Final[str] = "dehumidification"
CONF_HEATING: Final[str] = "heating"
CONF_COOLING: Final[str] = "cooling"

# Sensori VMC
CONF_T_AMBIENT: Final[str] = "t_ambient"
CONF_H_AMBIENT: Final[str] = "h_ambient"
CONF_T_WATER: Final[str] = "t_water"
CONF_T_OUTDOOR: Final[str] = "t_outdoor"
CONF_POWER_ON_NIGHT: Final[str] = "power_on_night"
CONF_POWER_ON_TODAY: Final[str] = "power_on_today"

# Allarmi VMC
CONF_ALARMS: Final[str] = "alarms"
CONF_HIGH_PRESSURE: Final[str] = "high_pressure"
CONF_DEW_POINT: Final[str] = "dew_point"
CONF_LOW_WATER_TEMP: Final[str] = "low_water_temp"
CONF_HIGH_WATER_TEMP: Final[str] = "high_water_temp"
CONF_ALARM: Final[str] = "alarm"

# Weather
CONF_PROVIDER: Final[str] = "provider"
CONF_FORECAST_DATA: Final[str] = "forecast_data"
CONF_HISTORICAL_DATA: Final[str] = "historical_data"
CONF_TOKEN: Final[str] = "token"
CONF_LATITUDE: Final[str] = "latitude"
CONF_LONGITUDE: Final[str] = "longitude"

# Historical data
CONF_INFLUXDB: Final[str] = "influxdb"
CONF_ORGANIZATION: Final[str] = "organization"
CONF_BUCKET: Final[str] = "bucket"
CONF_URL: Final[str] = "url"
DEFAULT_INFLUXDB_URL: Final[str] = "http://127.0.0.1:8086"

# ======================================================
# SCENARIOS
# ======================================================
CONF_VACATION: Final[str] = "vacation"
CONF_NOBODYSIN: Final[str] = "nobodysin"

# -------------------------
# Chiavi dati / opzioni (flow)
# -------------------------
CONF_HUB_NAME = "hub_name"
CONF_CLIMATE_NAME = "climate_name"
CONF_CLIMATE_UNIQUE_ID = "climate_unique_id"