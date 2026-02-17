from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant


from ..domain.models.season import Seasons
from ..domain.models.runtime_schema import VMCConfig
from ..helpers.builders.config_entries import RuntimeConfig
from ..helpers.formatter import fbool
from ..helpers.ha import set_entity_bool, set_entity_number, set_entity_select
from ..helpers.logger import log_info

from .vmc import ControlledMechanicalVentilationDevice

_LOGGER = logging.getLogger(__name__)

class EnerenRER020I(ControlledMechanicalVentilationDevice):
    """..."""

    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig) -> None:
        self._hass: HomeAssistant = hass
        self._runtime_cfg: RuntimeConfig = runtime_cfg
        self._vmc: VMCConfig | None = runtime_cfg.climate.devices.vmc

    async def async_set_power(self, *, power: bool | None = None) -> None:
        if power is not None and self._vmc is not None and self._vmc.power is not None:
            changed = await set_entity_bool(hass=self._hass, entity_id=self._vmc.power, value=power)
            if changed:
                log_info(_LOGGER, f"Power set to {fbool(power, on='On', off='Off')}") 

    async def async_set_processing_mode(self, mode: str | None = None) -> None:
        if mode is not None and self._vmc is not None and self._vmc.season is not None:
            selected_mode = self._vmc.season.autumn
            if mode == Seasons.WINTER:
                selected_mode = self._vmc.season.winter
            elif mode == Seasons.SUMMER:
                selected_mode = self._vmc.season.summer

            changed = await set_entity_select(hass=self._hass, entity_id=self._vmc.season.actuator, option=selected_mode)
            if changed:
                log_info(_LOGGER, "Processing mode set to %.1f", selected_mode)           

    async def async_set_spare(self, spare: int | None = None) -> None:
        if spare is not None and self._vmc is not None and self._vmc.spare_setpoint is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._vmc.spare_setpoint, value=spare)
            if changed:
                log_info(_LOGGER, "Spare set to %d", spare)

    async def async_set_temperature(self, target: float | None = None) -> None:
        if target is not None and self._vmc is not None and self._vmc.t_setpoint is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._vmc.t_setpoint, value=target, tol=3)
            if changed:
                log_info(_LOGGER, "Temperature set to %.1f", target)

    async def async_set_humidity(self, target: float | None = None) -> None:
        if target is not None and self._vmc is not None and self._vmc.h_setpoint is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._vmc.h_setpoint, value=target, tol=5)
            if changed:
                log_info(_LOGGER, "Humidity set to %.1f", target)

    async def async_set_dew_point(self, target: float | None = None) -> None:
        if target is not None and self._vmc is not None and self._vmc.t_dew_point_setpoint is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._vmc.t_dew_point_setpoint, value=target, tol=1)
            if changed:
                log_info(_LOGGER, "Dew Point set to %.1f", target)

    async def async_set_delta_dew_point(self, target: int | None = None) -> None:
        if target is not None and self._vmc is not None and self._vmc.delta_t_dew_point_setpoint is not None:
            changed = await set_entity_number(hass=self._hass, entity_id=self._vmc.delta_t_dew_point_setpoint, value=target)
            if changed:
                log_info(_LOGGER, "Delta Dew Point set to %d", target)

