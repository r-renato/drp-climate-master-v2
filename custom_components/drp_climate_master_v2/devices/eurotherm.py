#
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from ..domain.models.runtime_schema import RuntimeConfig

from ..helpers.formatter import fbool
from ..helpers.ha import set_entity_bool
from ..helpers.logger import log_info
from ..helpers.utils import slugify

from .electrovalve import ElectrovalveDevice

_LOGGER = logging.getLogger(__name__)

class EurothermElectrovalve(ElectrovalveDevice):
    """..."""
    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig) -> None:
        self._hass: HomeAssistant = hass
        self._runtime_cfg: RuntimeConfig = runtime_cfg
        self._areas = runtime_cfg.climate.areas

    async def async_set_circuit_open(
        self,
        *,
        valve_switch: Optional[str] = None,
        area_name: Optional[str] = None,
        state: Optional[bool] = None,
    ) -> None:
        if state is None:
            return
        # Preferisce valve_switch diretto (nuovo schema multi-superficie).
        # Fallback: cerca la prima valvola dell'area per retrocompatibilità.
        entity_id: Optional[str] = valve_switch or None
        if not entity_id and area_name is not None and self._areas is not None:
            area = next((a for a in (self._areas or []) if slugify(a.name) == slugify(area_name)), None)
            if area:
                switches = getattr(area, "valve_switches", lambda: ())()
                entity_id = switches[0] if switches else None
        if entity_id:
            changed = await set_entity_bool(hass=self._hass, entity_id=entity_id, value=state)
            if changed:
                log_info(_LOGGER, f"Circuit {entity_id} (area={area_name}) set to {fbool(state, on='True', off='False')}")
    