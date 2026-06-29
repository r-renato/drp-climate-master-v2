from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant

from ...helpers.logger import log_debug
from ...domain.models.runtime_schema import RuntimeConfig

from ...devices.aermec_hmi080 import AermecHMI080
from ...devices.caleffi_pumps import CaleffiSupplyPumps
from ...devices.eneren_rer020i import EnerenRER020I
from ...devices.eurotherm import EurothermElectrovalve

from ..decision.contracts import PdcCommand, VmcCommand
from .model import ValveCommand

_LOGGER = logging.getLogger(__name__)


@dataclass
class PlantDeviceIO:
    """Strato I/O puro: traduce comandi tipizzati in chiamate ai driver HW.

    Non contiene logica di controllo né stato persistente.
    Separato dall'orchestrazione (sequencer.py) per rendere entrambi
    testabili indipendentemente: il sequencer usa mock di PlantDeviceIO,
    i test di device_io usano mock dei singoli driver.
    """

    _heatpump: AermecHMI080
    _vmc: EnerenRER020I
    _electrovalve: EurothermElectrovalve
    _supply_pumps: CaleffiSupplyPumps

    @classmethod
    def build(cls, *, hass: HomeAssistant, runtime_cfg: RuntimeConfig) -> "PlantDeviceIO":
        """Istanzia PlantDeviceIO costruendo i driver dai parametri HA."""
        return cls(
            _heatpump=AermecHMI080(hass=hass, runtime_cfg=runtime_cfg),
            _vmc=EnerenRER020I(hass=hass, runtime_cfg=runtime_cfg),
            _electrovalve=EurothermElectrovalve(hass=hass, runtime_cfg=runtime_cfg),
            _supply_pumps=CaleffiSupplyPumps(hass=hass, runtime_cfg=runtime_cfg),
        )

    async def apply_pdc(self, pdc_command: PdcCommand) -> None:
        """Applica un comando PDC rispettando la sequenza del protocollo Aermec HMI080.

        Sequenza obbligatoria (vincolo Modbus Word 2):
          Spegnimento  (power=False): 1) power OFF  2) mode + setpoints
          Accensione   (power=True):  1) mode + setpoints  2) power ON
          Nessun cambio potenza (None): solo mode + setpoints
        """
        debug = (
            f"hwot={pdc_command.heat_wot_c} hdt={pdc_command.heat_dt_c} "
            f"cwot={pdc_command.cool_wot_c} cdt={pdc_command.cool_dt_c}"
        )
        if pdc_command.power is False:
            log_debug(_LOGGER, "PDC power → OFF (mode=%s %s)", pdc_command.mode, debug)
            await self._heatpump.async_set_power(fm_power=pdc_command.fm_power, power=False)

        await self._heatpump.async_set_processing_mode(mode=pdc_command.mode)
        await self._heatpump.async_set_heat_setpoints(t=pdc_command.heat_wot_c, dt=pdc_command.heat_dt_c)
        await self._heatpump.async_set_cool_setpoints(t=pdc_command.cool_wot_c, dt=pdc_command.cool_dt_c)

        if pdc_command.power is True:
            log_debug(_LOGGER, "PDC power → ON (mode=%s %s)", pdc_command.mode, debug)
            await self._heatpump.async_set_power(fm_power=pdc_command.fm_power, power=True)

    async def apply_vmc(self, vmc_command: VmcCommand) -> None:
        """Applica un comando VMC (setpoint + modalità + free cooling)."""
        log_debug(_LOGGER, "Applying VMC command: %s", vmc_command)
        await self._vmc.async_set_power(power=vmc_command.power)
        await self._vmc.async_set_processing_mode(mode=vmc_command.mode)
        await self._vmc.async_set_spare(spare=vmc_command.air_speed)
        await self._vmc.async_set_temperature(target=vmc_command.setpoint_t_c)
        await self._vmc.async_set_humidity(target=vmc_command.setpoint_rh_pct)
        await self._vmc.async_set_dew_point(target=vmc_command.setpoint_dp_c)
        await self._vmc.async_set_delta_dew_point(target=vmc_command.setpoint_ddp_c)
        await self._vmc.async_set_free_cooling(value=vmc_command.force_free_cooling)

    async def apply_zone_valves(self, commands: list[ValveCommand]) -> None:
        """Esegue i comandi alle elettrovalvole di zona."""
        for cmd in commands:
            await self._electrovalve.async_set_circuit_open(
                valve_switch=cmd.valve_switch,
                area_name=cmd.area_name,
                state=cmd.state,
            )

    async def apply_supply(
        self,
        *,
        direct_on: bool,
        adj_on: bool,
        mv_applied: Optional[float],
    ) -> None:
        """Esegue i comandi pompe (diretta e miscelatrice)."""
        await self._supply_pumps.async_set_direct_power(power=direct_on)
        await self._supply_pumps.async_set_adj_power(power=adj_on)
        if mv_applied is not None and adj_on:
            await self._supply_pumps.async_set_mix_adj_setpoints(value=float(mv_applied))
