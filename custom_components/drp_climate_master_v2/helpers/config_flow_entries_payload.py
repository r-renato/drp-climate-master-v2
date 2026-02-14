# custom_components/drp_climate_master/payload.py
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Tuple

import voluptuous as vol

from homeassistant.const import (
    CONF_TEMPERATURE_UNIT,
)

from ..const import (
    CONF_AREAS,
    CONF_CLIMATE_NAME,
    CONF_CLIMATE_UNIQUE_ID,
    CONF_DEVICES,
    CONF_HUB_NAME,
    CONF_SCENARIOS,
    CONF_HISTORICAL_DATA,
    CONF_WEATHER,
    CONF_UNITS,
    DEFAULT_TEMP_UNIT,
    DEFAULT_UNITS,
    # CONF_APT_WINDOWS,
    CONF_CONFORT_ZONES,
    # CONF_STATE,
    CONF_HOME_WINDOWS_STATE,
)
from ..domain.schema import BASE_CLIMATE_SCHEMA
from .config_flow import (
    normalize_weather_block,
    validate_areas,
    validate_devices,
    validate_scenarios,
    validate_historical_data,
    # validate_apt_windows,
    validate_confort_zones,
)

# Opzioni runtime (allineate al runtime_config)
OPT_UPDATE_INTERVAL_S = "update_interval_s"

def yaml_climate_to_entry_payload(hub_name: str, climate: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Converte un blocco 'climate' YAML in (entry.data, entry.options).
    """
    try:
        normalized_climate = BASE_CLIMATE_SCHEMA(climate)
    except vol.Invalid as exc:
        raise ValueError(f"Blocco climate non valido: {exc}") from exc

    normalized_climate = deepcopy(normalized_climate)

    climate_name = normalized_climate.get("name")
    uid = normalized_climate.get("unique_id")

    areas = normalized_climate.get(CONF_AREAS, [])
    devices = normalized_climate.get(CONF_DEVICES, {}) or {}
    scenarios = normalized_climate.get(CONF_SCENARIOS, {})
    historical_data_cfg = normalized_climate.get(CONF_HISTORICAL_DATA, {})
    weather = normalized_climate.get(CONF_WEATHER)
    # apt_windows = normalized_climate.get(CONF_APT_WINDOWS)
    confort_zones = normalized_climate.get(CONF_CONFORT_ZONES, {})

    # Parametri climatici (con default come nello schema)
    temp_unit = normalized_climate.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMP_UNIT)
    units = normalized_climate.get(CONF_UNITS, DEFAULT_UNITS)

    # Validazioni minime
    # if not apt_windows:
    #     legacy_windows = normalized_climate.get(CONF_HOME_WINDOWS_STATE)
    #     if legacy_windows:
    #         apt_windows = {CONF_STATE: legacy_windows}

    # if not apt_windows:
    #     raise ValueError("Manca 'apt_windows.state' (obbligatorio).")

    # if not isinstance(apt_windows, dict):
    #     raise ValueError("Il blocco 'apt_windows' deve essere un oggetto.")
    if weather is None:
        raise ValueError("Manca 'weather' (obbligatorio).")

    for fn, payload in (
        (validate_areas, areas),
        (validate_devices, devices),
        (validate_scenarios, scenarios),
        (validate_historical_data, historical_data_cfg),
        # (validate_apt_windows, apt_windows),
        (validate_confort_zones, confort_zones),
    ):
        err = fn(payload)  # type: ignore[arg-type]
        if err:
            raise ValueError(err)

    # if not isinstance(apt_windows.get(CONF_STATE), str) or not apt_windows.get(CONF_STATE):
    #     raise ValueError("Manca 'apt_windows.state' (obbligatorio).")

    # Valida lo shape di weather e normalizza lat/lon
    weather_error, normalized_weather = normalize_weather_block(weather)
    if weather_error:
        raise ValueError(weather_error)
    weather = normalized_weather

    data: Dict[str, Any] = {
        CONF_HUB_NAME: hub_name,
        CONF_CLIMATE_NAME: climate_name,
        CONF_CLIMATE_UNIQUE_ID: uid,
        CONF_WEATHER: weather,
        CONF_UNITS: str(units),
    }

    options: Dict[str, Any] = {
        CONF_AREAS: deepcopy(areas),
        CONF_DEVICES: deepcopy(devices),
        CONF_SCENARIOS: deepcopy(scenarios),
        CONF_HISTORICAL_DATA: deepcopy(historical_data_cfg),
        # CONF_APT_WINDOWS: deepcopy(apt_windows),
        CONF_CONFORT_ZONES: deepcopy(confort_zones),
        # runtime defaults
        OPT_UPDATE_INTERVAL_S: 30,
        # parametri climatici
        CONF_TEMPERATURE_UNIT: str(temp_unit),
        CONF_UNITS: str(units),
    }
    return data, options
