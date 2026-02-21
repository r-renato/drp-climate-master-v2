from __future__ import annotations
import asyncio
from typing import Optional

from homeassistant.core import HomeAssistant

from ..const import CONF_COOLING, CONF_HEATING

from ..helpers.ha import set_entity_bool, set_entity_number

from ..domain.models.runtime_schema import RuntimeConfig

from .heatpump import HeatPumpDevice


class AermecHMI080(HeatPumpDevice):
    """..."""

    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig) -> None:
        self._hass = hass
        self._runtime_cfg = runtime_cfg
        self._heat_pump = runtime_cfg.climate.devices.radiant

    async def async_set_power(self, *, fm_power: Optional[bool] = None, power: Optional[bool] = None) -> None:
        changed_fm = False
        if fm_power is not None and self._heat_pump is not None and self._heat_pump.fm_power is not None:
            # set_entity_bool returns True only if a service call was actually made (state change)
            changed_fm = await set_entity_bool(hass=self._hass, entity_id=self._heat_pump.fm_power, value=fm_power)

        # If we had to toggle FM power, give the controller some time before toggling main power.
        if changed_fm and power is not None and self._heat_pump is not None and self._heat_pump.power is not None:
            await asyncio.sleep(10)

        if power is not None and self._heat_pump is not None and self._heat_pump.power is not None:
            await set_entity_bool(hass=self._hass, entity_id=self._heat_pump.power, value=power)

    async def async_set_processing_mode(self, mode: Optional[str] = None) -> None:

        if mode is not None and self._heat_pump is not None and self._heat_pump.mode is not None:
            if mode == CONF_HEATING:
                await set_entity_number(
                    hass=self._hass, 
                    entity_id=self._heat_pump.mode.actuator, 
                    value=self._heat_pump.mode.heating
                )
            elif mode == CONF_COOLING:
                await set_entity_number(
                    hass=self._hass, 
                    entity_id=self._heat_pump.mode.actuator, 
                    value=self._heat_pump.mode.cooling
                )
            
    async def async_set_heat_setpoints(self, *, t: Optional[float] = None, dt: Optional[float] = None) -> None:
        """..."""

        if t is not None and self._heat_pump is not None and self._heat_pump.heating_t_setpoint is not None:
            await set_entity_number(hass=self._hass, entity_id=self._heat_pump.heating_t_setpoint.actuator, value=t)

        if dt is not None and self._heat_pump is not None and self._heat_pump.heating_dt_setpoint is not None:
            await set_entity_number(hass=self._hass, entity_id=self._heat_pump.heating_dt_setpoint.actuator, value=dt)

    async def async_set_cool_setpoints(self, *, t: Optional[float] = None, dt: Optional[float] = None) -> None:
        if t is not None and self._heat_pump is not None and self._heat_pump.cooling_t_setpoint is not None:
            await set_entity_number(hass=self._hass, entity_id=self._heat_pump.cooling_t_setpoint.actuator, value=t)

        if dt is not None and self._heat_pump is not None and self._heat_pump.cooling_dt_setpoint is not None:
            await set_entity_number(hass=self._hass, entity_id=self._heat_pump.cooling_dt_setpoint.actuator, value=dt)



