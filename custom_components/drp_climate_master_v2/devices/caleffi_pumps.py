#
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from ..domain.models.runtime_schema import RuntimeConfig

from ..helpers.formatter import fbool
from ..helpers.ha import set_entity_bool, set_entity_number
from ..helpers.logger import log_info

from .supplypumps import SupplyPumpsDevice

_LOGGER = logging.getLogger(__name__)

class CaleffiSupplyPumps(SupplyPumpsDevice):
    """..."""
    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig) -> None:
        self._hass: HomeAssistant = hass
        self._runtime_cfg: RuntimeConfig = runtime_cfg
        self._supply_units = runtime_cfg.climate.devices.supply_units


    async def async_set_direct_power(self, *, power: Optional[bool] = None) -> None:
        if power is not None and self._supply_units is not None and self._supply_units.direct_supply_unit is not None:
            changed = await set_entity_bool(hass=self._hass, entity_id=self._supply_units.direct_supply_unit, value=power)
            if changed:
                log_info(_LOGGER, f"Direct Pump Power set to {fbool(power, on='On', off='Off')}") 

    async def async_set_adj_power(self, *, power: Optional[bool] = None) -> None:
        if power is not None and self._supply_units is not None and self._supply_units.adjustable_supply_unit is not None:
            changed = await set_entity_bool(hass=self._hass, entity_id=self._supply_units.adjustable_supply_unit, value=power)
            if changed:
                log_info(_LOGGER, f"Direct Pump Power set to {fbool(power, on='On', off='Off')}") 

    async def async_set_mix_adj_setpoints(self, *, value: Optional[float] = None) -> None:
        if value is not None and self._supply_units is not None and self._supply_units.three_point_mixing_valve is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._supply_units.three_point_mixing_valve, value=value)
            if changed:
                log_info(_LOGGER, "Three Point Mixing Valve set to %d", value)


