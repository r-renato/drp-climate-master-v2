#
from __future__ import annotations

from typing import Any

from homeassistant.const import PERCENTAGE, EntityCategory

from ...const import CONF_CEILING, CONF_INDOOR, CONF_RADIANT, NAME_AREA_HOME
from ...domain.models.runtime_schema import SensorPair, RuntimeConfig

from ...helpers.builders.config_aggregate_sensors import GLOBAL, FieldSuffix
from ...helpers.utils import slugify

def build_slave_sensor_defs(runtime_config: RuntimeConfig) -> list[dict[str, Any]]:
    """Ritorna le definizioni per le entity "slave" (dewpoint, heat-index, ecc.)."""
    defs: list[dict[str, Any]] = []

    temps: list[str] = []
    humis: list[str] = []

    sensor_prefix = "Climate"

    for area in getattr(runtime_config.climate, "areas", []) or []:
        # i tuoi AreaConfig potrebbero essere dataclass: manteniamo getattr flessibile
        if getattr(area, CONF_INDOOR, False) and getattr(area, CONF_RADIANT, False) and getattr(area, CONF_CEILING, False):
            sensors = getattr(area, "sensors", None)
            if not sensors:
                continue

            temps.append(sensors.temperature)
            humis.append(sensors.humidity)

            defs.append(
                {
                    "type": "TemperatureSensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.INDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.INDOOR_HUMIDITY(slugify(area.name)),
                    ),
                    "unit": runtime_config.climate.unit_system.temperature,
                }
            )
            defs.append(
                {
                    "type": "HumiditySensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.INDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.INDOOR_HUMIDITY(slugify(area.name)),
                    ),
                    "unit": PERCENTAGE,
                }
            )
            defs.append(
                {
                    "type": "DewpointSensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.INDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.INDOOR_HUMIDITY(slugify(area.name)),
                        dew_point=FieldSuffix.INDOOR_DEW_POINT(slugify(area.name)),
                    ),
                    "unit": runtime_config.climate.unit_system.temperature,
                }
            )
            defs.append(
                {
                    "type": "HeatIndexSensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.INDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.INDOOR_HUMIDITY(slugify(area.name)),
                        heat_index=FieldSuffix.INDOOR_HEAT_INDEX(slugify(area.name)),
                    ),
                    "unit": runtime_config.climate.unit_system.temperature,
                }
            )
        elif getattr(area, CONF_INDOOR, False) is False and getattr(area, CONF_RADIANT, False) is False:
            defs.append(
                {
                    "type": "TemperatureSensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.OUTDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.OUTDOOR_HUMIDITY(slugify(area.name)),
                    ),
                    "unit": runtime_config.climate.unit_system.temperature,
                }
            )
            defs.append(
                {
                    "type": "HumiditySensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.OUTDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.OUTDOOR_HUMIDITY(slugify(area.name)),
                    ),
                    "unit": PERCENTAGE,
                }
            )
            defs.append(
                {
                    "type": "DewpointSensor",
                    "area": area.name,
                    "name": f"{sensor_prefix} Zone {area.name}",
                    "sensors": SensorPair(
                        temperature=FieldSuffix.OUTDOOR_TEMPERATURE(slugify(area.name)),
                        humidity=FieldSuffix.OUTDOOR_HUMIDITY(slugify(area.name)),
                        dew_point=FieldSuffix.OUTDOOR_DEW_POINT(slugify(area.name)),
                    ),
                    "unit": runtime_config.climate.unit_system.temperature,
                }
            )


    defs.append(
        {
            "type": "TemperatureSensor",
            "name": f"{sensor_prefix} {NAME_AREA_HOME}",
            "sensors": SensorPair(
                temperature=FieldSuffix.INDOOR_TEMPERATURE(GLOBAL),
                humidity=FieldSuffix.INDOOR_HUMIDITY(GLOBAL),
            ),
            "unit": runtime_config.climate.unit_system.temperature,
        }
    )
    defs.append(
        {
            "type": "HumiditySensor",
            "name": f"{sensor_prefix} {NAME_AREA_HOME}",
            "sensors": SensorPair(
                temperature=FieldSuffix.INDOOR_TEMPERATURE(GLOBAL),
                humidity=FieldSuffix.INDOOR_HUMIDITY(GLOBAL),
            ),
            "unit": PERCENTAGE,
        }
    )
    defs.append(
        {
            "type": "DewpointSensor",
            "name": f"{sensor_prefix} {NAME_AREA_HOME}",
            "sensors": SensorPair(
                temperature=FieldSuffix.INDOOR_TEMPERATURE(GLOBAL),
                humidity=FieldSuffix.INDOOR_HUMIDITY(GLOBAL),
                dew_point=FieldSuffix.INDOOR_DEW_POINT(GLOBAL),
            ),
            "unit": runtime_config.climate.unit_system.temperature,
        }
    )
    defs.append(
        {
            "type": "HeatIndexSensor",
            "name": f"{sensor_prefix} {NAME_AREA_HOME}",
            "sensors": SensorPair(
                temperature=FieldSuffix.INDOOR_TEMPERATURE(GLOBAL),
                humidity=FieldSuffix.INDOOR_HUMIDITY(GLOBAL),
                heat_index=FieldSuffix.INDOOR_HEAT_INDEX(GLOBAL),
            ),
            "unit": runtime_config.climate.unit_system.temperature,
        }
    )
    defs.append(
        {
            "type": "SeasonSensor",
            "name": f"{sensor_prefix} Weather Season",
            "category": EntityCategory.DIAGNOSTIC,
        }
    )
    defs.append(
        {
            "type": "PDCSensor",
            "name": f"{sensor_prefix} PDC Decision",
            "category": EntityCategory.DIAGNOSTIC,
        }
    )
    defs.append(
        {
            "type": "VMCSensor",
            "name": f"{sensor_prefix} VMC Decision",
            "category": EntityCategory.DIAGNOSTIC,
        }
    )
    defs.append(
        {
            "type": "RadiantSensor",
            "name": f"{sensor_prefix} Radiant Decision",
            "category": EntityCategory.DIAGNOSTIC,
        }
    )    

    # log_debug(_LOGGER, "build_slave_sensor_defs: %d definitions", len(defs))
    return defs

