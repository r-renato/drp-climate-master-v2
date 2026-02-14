# custom_components/drp_climate_master/ui_schemas.py
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import voluptuous as vol
from homeassistant.helpers import selector
from homeassistant.const import (
    CONF_TEMPERATURE_UNIT,
)
from ..const import (
    CONF_AREAS,
    CONF_DEVICES,
    CONF_SCENARIOS,
    CONF_HISTORICAL_DATA,
    CONF_WEATHER,
    CONF_UNITS,
    DEFAULT_TEMP_UNIT,
    DEFAULT_UNITS,
    CONF_HUB_NAME,
    CONF_CLIMATE_NAME,
    CONF_CLIMATE_UNIQUE_ID,
    # CONF_APT_WINDOWS,
    CONF_CONFORT_ZONES,
)
# Opzioni runtime
OPT_UPDATE_INTERVAL_S = "update_interval_s"

def schema_user() -> vol.Schema:
    """Form iniziale (user)."""
    return vol.Schema(
        {
            vol.Required(CONF_HUB_NAME): str,
            vol.Required(CONF_CLIMATE_NAME): str,
            vol.Required(CONF_CLIMATE_UNIQUE_ID): str,
            # vol.Required(CONF_APT_WINDOWS): selector.EntitySelector(
            #     selector.EntitySelectorConfig(domain="binary_sensor")
            # ),
            vol.Required(CONF_WEATHER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
        }
    )

def schema_dynamic(cur: Mapping[str, Any], entry_data: Mapping[str, Any]) -> vol.Schema:
    units_default = str(cur.get(CONF_UNITS, entry_data.get(CONF_UNITS, "")) or DEFAULT_UNITS)
    temp_unit_default = str(cur.get(CONF_TEMPERATURE_UNIT, entry_data.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMP_UNIT)) or DEFAULT_TEMP_UNIT)
    units_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["si", "metric", "imperial"],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    temp_unit_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["°C", "°F"],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    return vol.Schema(
        {
            vol.Required(OPT_UPDATE_INTERVAL_S, default=cur.get(OPT_UPDATE_INTERVAL_S, 30)): vol.All(int, vol.Range(min=5, max=3600)),
            vol.Required(CONF_UNITS, default=units_default): units_selector,
            vol.Required(CONF_TEMPERATURE_UNIT, default=temp_unit_default): temp_unit_selector,
        }
    )

def schema_areas(current: list[Any]) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_AREAS, default=current): selector.ObjectSelector()})

def schema_device(key: str, current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({vol.Required(key, default=deepcopy(dict(current))): selector.ObjectSelector()})

def schema_weather(current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_WEATHER, default=deepcopy(dict(current))): selector.ObjectSelector()})

def schema_historical(current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_HISTORICAL_DATA, default=deepcopy(dict(current))): selector.ObjectSelector()})

def schema_advanced(
    scenarios: Mapping[str, Any],
    extras: Mapping[str, Any],
    apt_windows: Mapping[str, Any],
    confort_zones: Mapping[str, Any],
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SCENARIOS, default=deepcopy(dict(scenarios))): selector.ObjectSelector(),
            # vol.Optional(CONF_APT_WINDOWS, default=deepcopy(dict(apt_windows))): selector.ObjectSelector(),
            vol.Optional(CONF_CONFORT_ZONES, default=deepcopy(dict(confort_zones))): selector.ObjectSelector(),
            vol.Required(CONF_DEVICES, default=deepcopy(dict(extras))): selector.ObjectSelector(),
        }
    )
