from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from homeassistant.core import HomeAssistant

from ...helpers.logger import log_debug
from ...domain.models.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ..rcmpc.contracts import ControlPlan
from .contracts import PlantDecision
from .planner import PlantDecisionPlanner
from .actuator import PlantActuator

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PlantControlEngine:
    """Plant-level control engine (decision + optional actuation)."""

    hass: HomeAssistant
    runtime: RuntimeConfig
    enabled: bool = False  # SAFE DEFAULT: decision-only until you flip it

    _planner: PlantDecisionPlanner = field(init=False, repr=False)
    _actuator: PlantActuator = field(init=False, repr=False)
    _last: Optional[PlantDecision] = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._planner = PlantDecisionPlanner()
        self._actuator = PlantActuator(hass=self.hass, runtime=self.runtime)

    @property
    def last_decision(self) -> Optional[PlantDecision]:
        return self._last

    async def async_run_once(
        self,
        *,
        snapshot: PlantSnapshot,
        zone_plan: Optional[ControlPlan] = None,
        reason: str,
    ) -> PlantDecision:
        # If disabled: return a safe, explicit "no-op" decision
        if not self.enabled:
            d = PlantDecision.disabled(reason=reason)
            self._last = d
            return d

        d = self._planner.plan(snapshot=snapshot, zone_plan=zone_plan, reason=reason)
        self._last = d

        log_debug(
            _LOGGER,
            "PlantControlEngine decision: mode=%s pdc=%s mix=%s dir=%s wot=%s warnings=%s",
            d.mode,
            d.pdc_power,
            d.pump_mix_on,
            d.pump_direct_on,
            d.pdc_heat_wot_c,
            d.warnings,
        )

        if self.enabled:
            await self._actuator.apply(d)
        return d
