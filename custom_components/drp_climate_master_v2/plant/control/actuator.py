from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...devices.eneren_rer020i import EnerenRER020I

from ...devices.aermec_hmi080 import AermecHMI080

from ...domain.models.runtime_schema import RuntimeConfig

from ..decision.contracts import PdcCommand, PlantDecision, VmcCommand

from ...controller.coordinator import ClimateCoordinator
from ...domain.models.plant import PlantSnapshot
from ...helpers.utils import slugify

_LOGGER = logging.getLogger(__name__)

class PlantActuator:
    """Translate a ControlPlan into Home Assistant service calls.

    """
    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig):
        self._hass = hass
        self._runtime = runtime_cfg
        self._heatpump = AermecHMI080(hass=hass, runtime_cfg=runtime_cfg)
        self._vmc = EnerenRER020I(hass=hass, runtime_cfg=runtime_cfg)
        
    async def _async_pdc_actuator(self, pdc_command: PdcCommand):
        """Apply a PDC command to HA entities."""

        await self._heatpump.async_set_processing_mode(mode=pdc_command.mode)
        await self._heatpump.async_set_heat_setpoints(t=pdc_command.heat_wot_c, dt=pdc_command.heat_dt_c)
        await self._heatpump.async_set_cool_setpoints(t=pdc_command.cool_wot_c, dt=pdc_command.cool_dt_c)

    async def _async_vmc_actuator(self, vmc_command: VmcCommand):
        """Apply a VMC command to HA entities."""

        await self._vmc.async_set_power(power=vmc_command.power)
        await self._vmc.async_set_processing_mode(mode=vmc_command.mode)
        await self._vmc.async_set_spare(spare=vmc_command.air_speed)
        await self._vmc.async_set_temperature(target=vmc_command.setpoint_t_c)
        await self._vmc.async_set_humidity(target=vmc_command.setpoint_rh_pct)
        await self._vmc.async_set_dew_point(target=vmc_command.setpoint_dp_c)
        await self._vmc.async_set_delta_dew_point(target=vmc_command.setpoint_ddp_c)

    async def async_apply(self, decision: PlantDecision) -> None:
        """..."""

        pdc_command = decision.pdc
        vmc_command = decision.vmc

        await self._async_pdc_actuator(pdc_command)
        await self._async_vmc_actuator(vmc_command)


