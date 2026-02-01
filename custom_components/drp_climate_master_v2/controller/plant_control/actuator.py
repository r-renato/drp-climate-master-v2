from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant

from ...helpers.logger import log_debug, log_warning
from ...domain.models.runtime_schema import RuntimeConfig

from .contracts import PlantDecision


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if entity_id else ""


async def _set_bool(hass: HomeAssistant, *, entity_id: str, value: bool) -> None:
    dom = _domain(entity_id)
    if dom in ("switch", "input_boolean"):
        await hass.services.async_call(dom, "turn_on" if value else "turn_off", {"entity_id": entity_id}, blocking=False)
        return
    log_warning("PlantActuator: unsupported bool entity domain: %s (%s)", dom, entity_id)


async def _set_number(hass: HomeAssistant, *, entity_id: str, value: float) -> None:
    dom = _domain(entity_id)
    if dom == "number":
        await hass.services.async_call(dom, "set_value", {"entity_id": entity_id, "value": value}, blocking=False)
        return
    log_warning("PlantActuator: unsupported number entity domain: %s (%s)", dom, entity_id)


async def _set_select(hass: HomeAssistant, *, entity_id: str, option: str) -> None:
    dom = _domain(entity_id)
    if dom in ("select", "input_select"):
        await hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": option}, blocking=False)
        return
    log_warning("PlantActuator: unsupported select entity domain: %s (%s)", dom, entity_id)


@dataclass(slots=True)
class PlantActuator:
    """Apply PlantDecision to HA entities described in RuntimeConfig.

    IMPORTANT: this is safe-by-default:
      - engine decides whether to call apply() (feature flag)
      - we keep v1 limited to obvious ON/OFF + PDC heating setpoints
    """

    hass: HomeAssistant
    runtime: RuntimeConfig

    async def apply(self, decision: PlantDecision) -> None:
        devices = self.runtime.climate.devices

        # --- PDC (Radiant/PDC block in runtime schema)
        pdc = devices.radiant

        if decision.pdc_power is not None and pdc.power:
            log_debug("PlantActuator: PDC power -> %s", decision.pdc_power)
            await _set_bool(self.hass, entity_id=pdc.power, value=bool(decision.pdc_power))

        # Mode is numeric-mapped in schema (heating/cooling)
        if decision.pdc_mode and pdc.mode and pdc.mode.actuator:
            if decision.pdc_mode == "heating":
                v = float(pdc.mode.heating)
            elif decision.pdc_mode == "cooling":
                v = float(pdc.mode.cooling)
            else:
                v = None
            if v is not None:
                log_debug("PlantActuator: PDC mode -> %s (%s)", decision.pdc_mode, v)
                await _set_number(self.hass, entity_id=pdc.mode.actuator, value=v)

        # Heating setpoints (v1)
        if decision.pdc_heat_wot_c is not None and pdc.heating_t_setpoint and pdc.heating_t_setpoint.actuator:
            await _set_number(self.hass, entity_id=pdc.heating_t_setpoint.actuator, value=float(decision.pdc_heat_wot_c))
        if decision.pdc_heat_dt_k is not None and pdc.heating_delta_t_setpoint and pdc.heating_delta_t_setpoint.actuator:
            await _set_number(self.hass, entity_id=pdc.heating_delta_t_setpoint.actuator, value=float(decision.pdc_heat_dt_k))

        # --- Supply units
        su = devices.supply_units
        if decision.pump_mix_on is not None and su.adjustable_supply_unit:
            await _set_bool(self.hass, entity_id=su.adjustable_supply_unit, value=bool(decision.pump_mix_on))
        if decision.pump_direct_on is not None and su.direct_supply_unit:
            await _set_bool(self.hass, entity_id=su.direct_supply_unit, value=bool(decision.pump_direct_on))

        # Mixing valve control intentionally NOT implemented yet (needs loop/PI + anti-windup + constraints)
        # if decision.mix_valve_pct is not None and su.mixing_valve:
        #     await _set_number(self.hass, entity_id=su.mixing_valve, value=float(decision.mix_valve_pct))

        # --- VMC season (optional; keep off for now)
        vmc = devices.vmc
        if decision.vmc_season and vmc.season and vmc.season.actuator:
            # map string to configured options
            opt: Optional[str] = None
            if decision.vmc_season.lower() == "winter":
                opt = vmc.season.winter
            elif decision.vmc_season.lower() == "summer":
                opt = vmc.season.summer
            elif decision.vmc_season.lower() == "off":
                opt = vmc.season.off
            if opt:
                await _set_select(self.hass, entity_id=vmc.season.actuator, option=opt)
