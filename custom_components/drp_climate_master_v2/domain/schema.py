"""Schema for DRP Climate."""
import logging
import voluptuous as vol

import homeassistant.helpers.config_validation as cv

from homeassistant.const import (
    CONF_NAME,
    CONF_SENSORS,
    CONF_UNIQUE_ID,
    CONF_TEMPERATURE_UNIT,
)

from ..const import (
    CONF_ACTUATOR,
    CONF_ADJUSTABLE_SUPPLY_UNIT,
    CONF_ADJUSTABLE_TEMP_SYSTEM_RETURN,
    CONF_ADJUSTABLE_TEMP_SYSTEM_SUPPLY,
    CONF_ALARM,
    CONF_ALARMS,
    CONF_AREA,
    CONF_AREAS,
    CONF_AUTUMN,
    CONF_BOILER_TEMP_SYSTEM_RETURN,
    CONF_BOILER_TEMP_SYSTEM_SUPPLY,
    CONF_BUCKET,
    CONF_CEILING,
    CONF_CLIMATE,
    CONF_COMPRESSOR_MANAGEMENT,
    CONF_COMPRESSOR_ONLY,
    CONF_COOLING,
    CONF_COOLING_DT_SETPOINT,
    CONF_COOLING_MANAGEMENT,
    CONF_COOLING_ONLY,
    CONF_COOLING_T_SETPOINT,
    CONF_DEHUMIDIFICATION,
    CONF_DEHUMIDIFICATION_ONLY,
    CONF_DEHUMIDIFICATION_OR_COOLING,
    CONF_DELTA_DEW_POINT_SETPOINT,
    CONF_DEVICES,
    CONF_DEW_POINT,
    CONF_DEW_POINT_SETPOINT,
    CONF_DIRECT_SUPPLY_UNIT,
    CONF_DIRECT_TEMP_SYSTEM_RETURN,
    CONF_DIRECT_TEMP_SYSTEM_SUPPLY,
    CONF_FIRST_WATER_THEN_COMPRESSOR,
    CONF_FM_POWER,
    CONF_FORCE_COOLING,
    CONF_FORCE_FREE_COOLING,
    CONF_FORCE_HEATING,
    CONF_FORECAST_DATA,
    CONF_H_AMBIENT,
    CONF_H_SETPOINT,
    CONF_HEATING,
    CONF_HEATING_DT_SETPOINT,
    CONF_HEATING_T_SETPOINT,
    CONF_HIGH_PRESSURE,
    CONF_HIGH_WATER_TEMP,
    CONF_HISTORICAL_DATA,
    CONF_HUMIDITY,
    CONF_INDOOR,
    CONF_INFLUXDB,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_LOW_WATER_TEMP,
    CONF_MODE,
    CONF_NOBODYSIN,
    CONF_ORGANIZATION,
    CONF_PDC_COMPRESSOR_STATE,
    CONF_PDC_TEMP_OUTDOOR,
    CONF_PDC_TEMP_WATER_IN,
    CONF_PDC_TEMP_WATER_OUT,
    CONF_POWER,
    CONF_POWER_ON_NIGHT,
    CONF_POWER_ON_TODAY,
    CONF_PROVIDER,
    CONF_RADIANT,
    CONF_RADIANT_SURFACE,
    CONF_RADIANT_SURFACES,
    CONF_SURFACE_M2,
    CONF_VALVE_SWITCH,
    CONF_REQUESTS,
    CONF_SCENARIOS,
    CONF_WINDOWS,
    CONF_SEASON,
    CONF_SPARE_SETPOINT,
    CONF_SPRING,
    CONF_CLOSED_STATE,
    CONF_CONFORT_ZONES,
    CONF_SUMMER,
    CONF_SUPPLY_UNITS,
    CONF_T_AMBIENT,
    CONF_T_OUTDOOR,
    CONF_T_SETPOINT,
    CONF_T_WATER,
    CONF_TCOLLECTOR,
    CONF_TEMPERATURE,
    CONF_THREE_POINT_MIXING_VALVE,
    CONF_TOKEN,
    CONF_UNITS,
    CONF_TEMP_MIN,
    CONF_TEMP_MAX,
    CONF_HUMI_MIN,
    CONF_HUMI_MAX,
    CONF_DP_MIN,
    CONF_DP_MAX,
    CONF_SPRINT,
    CONF_URL,
    CONF_VACATION,
    CONF_VALUE,
    CONF_VENT_RECIRCULATION,
    CONF_VMC,
    CONF_WATER,
    CONF_WATER_ONLY,
    CONF_WEATHER,
    CONF_WINTER,
    DEFAULT_INFLUXDB_URL,
    DEFAULT_TEMP_UNIT,
    DEFAULT_UNITS,
    DOMAIN
)

_LOGGER = logging.getLogger(__name__)

# ########## # ########## # ########## # ########## #
# S C H E M A
# ########## # ########## # ########## # ########## #

AREAS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AREA): cv.string,
        vol.Optional(CONF_INDOOR, default=True): vol.In([True,False,]),
        vol.Optional(CONF_RADIANT, default=True): vol.In([True,False,]),
        vol.Required(CONF_SENSORS): vol.Schema(
            {
                vol.Required(CONF_TEMPERATURE): cv.entity_id,
                vol.Required(CONF_HUMIDITY): cv.entity_id,
            }
        ),
        # Nuovo formato: lista di superfici radianti indipendenti.
        vol.Optional(CONF_RADIANT_SURFACES): vol.All(
            cv.ensure_list,
            [vol.Schema({
                vol.Required(CONF_VALVE_SWITCH): cv.entity_id,
                vol.Optional(CONF_SURFACE_M2, default=0.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
            })]
        ),
        # Vecchio formato flat — mantenuto per retrocompatibilità YAML.
        # Il parser (config_entries.py) converte automaticamente in radiant_surfaces.
        vol.Optional(CONF_TCOLLECTOR): cv.entity_id,
        vol.Optional(CONF_RADIANT_SURFACE): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(CONF_CEILING): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

SUPPLY_UNITS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DIRECT_SUPPLY_UNIT): cv.entity_id,
        vol.Required(CONF_ADJUSTABLE_SUPPLY_UNIT): cv.entity_id,
        vol.Required(CONF_THREE_POINT_MIXING_VALVE): cv.entity_id,
        vol.Required(CONF_SENSORS): vol.Schema(
            {
                vol.Required(CONF_BOILER_TEMP_SYSTEM_SUPPLY): cv.entity_id,
                vol.Required(CONF_BOILER_TEMP_SYSTEM_RETURN): cv.entity_id,
                vol.Required(CONF_ADJUSTABLE_TEMP_SYSTEM_SUPPLY): cv.entity_id,
                vol.Required(CONF_ADJUSTABLE_TEMP_SYSTEM_RETURN): cv.entity_id,
                vol.Required(CONF_DIRECT_TEMP_SYSTEM_SUPPLY): cv.entity_id,
                vol.Required(CONF_DIRECT_TEMP_SYSTEM_RETURN): cv.entity_id,
            }
        ),
    }
)

RADIANT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FM_POWER): cv.entity_id,
        vol.Required(CONF_POWER): cv.entity_id,
        # vol.Required(CONF_POWER): cv.entity_id,
        vol.Required(CONF_MODE): vol.Schema(
            {
                vol.Required(CONF_ACTUATOR): cv.entity_id,
                vol.Required(CONF_HEATING): cv.positive_int,
                vol.Required(CONF_COOLING): cv.positive_int,
            }
        ),
        vol.Required(CONF_HEATING_T_SETPOINT): vol.Schema({
            vol.Required(CONF_ACTUATOR): cv.entity_id,
            vol.Required(CONF_VALUE): cv.positive_int,
        }),
        vol.Required(CONF_HEATING_DT_SETPOINT): vol.Schema({
            vol.Required(CONF_ACTUATOR): cv.entity_id,
            vol.Required(CONF_VALUE): cv.positive_int,
        }),
        vol.Required(CONF_COOLING_T_SETPOINT): vol.Schema({
            vol.Required(CONF_ACTUATOR): cv.entity_id,
            vol.Required(CONF_VALUE): cv.positive_int,
        }),
        vol.Required(CONF_COOLING_DT_SETPOINT): vol.Schema({
            vol.Required(CONF_ACTUATOR): cv.entity_id,
            vol.Required(CONF_VALUE): cv.positive_int,
        }),
        vol.Required(CONF_SENSORS): vol.Schema(
            {
                vol.Required(CONF_PDC_TEMP_WATER_IN): cv.entity_id,
                vol.Required(CONF_PDC_TEMP_WATER_OUT): cv.entity_id,
                vol.Optional(CONF_PDC_TEMP_OUTDOOR): cv.entity_id,
                vol.Optional(CONF_PDC_COMPRESSOR_STATE): cv.entity_id,
            }
        ),
    }
)

VMC_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POWER): cv.entity_id,
        vol.Required(CONF_T_SETPOINT): cv.entity_id,
        vol.Required(CONF_H_SETPOINT): cv.entity_id,
        vol.Required(CONF_DEW_POINT_SETPOINT): cv.entity_id,
        vol.Required(CONF_DELTA_DEW_POINT_SETPOINT): cv.entity_id,
        vol.Required(CONF_SPARE_SETPOINT): cv.entity_id,
        vol.Required(CONF_VENT_RECIRCULATION): cv.entity_id,
        vol.Required(CONF_FORCE_HEATING): cv.entity_id,
        vol.Required(CONF_FORCE_COOLING): cv.entity_id,
        vol.Required(CONF_FORCE_FREE_COOLING): cv.entity_id,

        vol.Required(CONF_SEASON): vol.Schema(
            {
                vol.Required(CONF_ACTUATOR): cv.entity_id,
                vol.Required(CONF_WINTER): cv.string,
                vol.Required(CONF_SUMMER): cv.string,
                vol.Required(CONF_AUTUMN): cv.string,
                vol.Required(CONF_SPRING): cv.string,
            }
         ),

        vol.Required(CONF_COMPRESSOR_MANAGEMENT): vol.Schema(
            {
                vol.Required(CONF_ACTUATOR): cv.entity_id,
                vol.Required(CONF_DEHUMIDIFICATION_OR_COOLING): cv.positive_int,
                vol.Required(CONF_DEHUMIDIFICATION_ONLY): cv.positive_int,
                vol.Required(CONF_COOLING_ONLY): cv.positive_int,
            }
         ),

        vol.Required(CONF_COOLING_MANAGEMENT): vol.Schema(
            {
                vol.Required(CONF_ACTUATOR): cv.entity_id,
                vol.Required(CONF_COMPRESSOR_ONLY): cv.positive_int,
                vol.Required(CONF_WATER_ONLY): cv.positive_int,
                vol.Required(CONF_FIRST_WATER_THEN_COMPRESSOR): cv.positive_int,
            }
         ),

        vol.Required(CONF_REQUESTS): vol.Schema(
            {
                vol.Required(CONF_WATER): cv.entity_id,
                vol.Required(CONF_DEHUMIDIFICATION): cv.entity_id,
                vol.Required(CONF_HEATING): cv.entity_id,
                vol.Required(CONF_COOLING): cv.entity_id,
            }
         ),

        vol.Required(CONF_SENSORS): vol.Schema(
            {
                vol.Required(CONF_T_AMBIENT): cv.entity_id,
                vol.Required(CONF_H_AMBIENT): cv.entity_id,
                vol.Required(CONF_T_WATER): cv.entity_id,
                vol.Required(CONF_T_OUTDOOR): cv.entity_id,
                vol.Required(CONF_POWER_ON_NIGHT): cv.entity_id,
                vol.Required(CONF_POWER_ON_TODAY): cv.entity_id,
            }
        ),

        vol.Required(CONF_ALARMS): vol.Schema(
            {
                vol.Required(CONF_HIGH_PRESSURE): cv.entity_id,
                vol.Required(CONF_DEW_POINT): cv.entity_id,
                vol.Required(CONF_LOW_WATER_TEMP): cv.entity_id,
                vol.Required(CONF_HIGH_WATER_TEMP): cv.entity_id,
                vol.Required(CONF_ALARM): cv.entity_id,
            }
        ),
    }
)

DEVICES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SUPPLY_UNITS): vol.All(SUPPLY_UNITS_SCHEMA),
        vol.Optional(CONF_RADIANT): vol.All(RADIANT_SCHEMA),
        vol.Optional(CONF_VMC): vol.All(VMC_SCHEMA),
    }
)

WINDOWS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOSED_STATE): cv.entity_id,
    },
    extra=vol.ALLOW_EXTRA,
)

CONFORT_RANGE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TEMP_MIN): vol.Coerce(float),
        vol.Optional(CONF_TEMP_MAX): vol.Coerce(float),
        vol.Optional(CONF_HUMI_MIN): vol.Coerce(float),
        vol.Optional(CONF_HUMI_MAX): vol.Coerce(float),
        vol.Optional(CONF_DP_MIN): vol.Coerce(float),
        vol.Optional(CONF_DP_MAX): vol.Coerce(float),
    },
    extra=vol.ALLOW_EXTRA,
)

CONFORT_ZONES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SUMMER): vol.All(CONFORT_RANGE_SCHEMA),
        vol.Optional(CONF_AUTUMN): vol.All(CONFORT_RANGE_SCHEMA),
        vol.Optional(CONF_WINTER): vol.All(CONFORT_RANGE_SCHEMA),
        vol.Optional(CONF_SPRING): vol.All(CONFORT_RANGE_SCHEMA),
        vol.Optional(CONF_SPRINT): vol.All(CONFORT_RANGE_SCHEMA),
    },
    extra=vol.ALLOW_EXTRA,
)

WEATHER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FORECAST_DATA) : vol.Schema(
            {
                vol.Required(CONF_PROVIDER): cv.string,
            }
        ),
        vol.Required(CONF_HISTORICAL_DATA) : vol.Schema(
            {
                vol.Required(CONF_PROVIDER): cv.string,
                vol.Optional(CONF_TOKEN): cv.string,
                vol.Optional(CONF_LATITUDE): cv.string,
                vol.Optional(CONF_LONGITUDE): cv.string,
            }
        )
    }
)

HISTORICAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_INFLUXDB) : vol.Schema(
            {
                vol.Required(CONF_ORGANIZATION): cv.string,
                vol.Required(CONF_BUCKET): cv.string,
                vol.Optional(CONF_URL, default=DEFAULT_INFLUXDB_URL): cv.string,
                vol.Required(CONF_TOKEN): cv.string,
            }
        )
    }
)

BASE_CLIMATE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        # vol.Optional(CONF_FRIENDLY_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,

        # vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
        # vol.Optional(CONF_MIN_TEMP, default=5): vol.Coerce(float),
        # vol.Optional(CONF_STEP, default=0.5): vol.Coerce(float),
        vol.Optional(CONF_TEMPERATURE_UNIT, default=DEFAULT_TEMP_UNIT): cv.string,
        vol.Optional(CONF_UNITS, default=DEFAULT_UNITS): cv.string,

        vol.Required(CONF_AREAS): vol.All(
            cv.ensure_list, [vol.All(AREAS_SCHEMA)]
        ),
        vol.Optional(CONF_DEVICES): vol.All(DEVICES_SCHEMA),

        vol.Optional(CONF_WINDOWS): vol.All(WINDOWS_SCHEMA),
        vol.Optional(CONF_CONFORT_ZONES): vol.All(CONFORT_ZONES_SCHEMA),

        # vol.Required(CONF_HOME_WINDOWS_STATE): cv.entity_id,
        vol.Required(CONF_WEATHER): vol.All(WEATHER_SCHEMA),
        vol.Required(CONF_HISTORICAL_DATA): vol.All(HISTORICAL_DATA_SCHEMA),
        vol.Required(CONF_SCENARIOS) : vol.Schema(
            {
                vol.Required(CONF_VACATION): cv.string,
                vol.Required(CONF_NOBODYSIN): cv.string,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

CLIMATE_SCHEMA = vol.Schema(
    {
        # vol.Required(CONF_NAME, default=DEFAULT_CLIMATE_NAME): cv.string,
        vol.Required(CONF_CLIMATE): vol.All(
            cv.ensure_list, [vol.All(BASE_CLIMATE_SCHEMA)]
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: CLIMATE_SCHEMA,   # <-- qui NON è una lista
    },
    extra=vol.ALLOW_EXTRA,
)

def area( areas_config : dict, name : str = "") -> dict | None:
    """Get area configuration by name."""
    for area_cfg in areas_config:
        area_name = area_cfg.get(CONF_AREA)
        # _LOGGER.debug( "%s %s", area_name, name )
        if area_name == name:
            return area_cfg

    return None
