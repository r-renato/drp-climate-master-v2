from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant

from ..coordinator import ClimateCoordinator

from .actuator import PlantActuator
from .config import ControlConfig
from .contracts import ControlPlan
from .mpc_planner import MpcLitePlanner

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EngineState:
    last_plan: Optional[ControlPlan] = None


class ControlEngine:
    """Compute and apply a control plan.

    The engine is invoked by the Supervisor (and optionally by WeatherCoordinator via
    coordinator.async_decide_and_act). It is designed to be idempotent and safe.
    """

    def __init__(self, hass: HomeAssistant, coordinator: ClimateCoordinator, cfg: Optional[ControlConfig] = None) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._cfg = cfg or ControlConfig()

        self._planner = MpcLitePlanner(cfg=self._cfg)
        self._actuator = PlantActuator(hass=hass, coordinator=coordinator, cfg=self._cfg)
        self._lock = asyncio.Lock()
        self.state = EngineState()

    async def async_run_once(self, *, reason: str) -> Optional[ControlPlan]:
        """Compute a plan from the latest snapshot and apply its first action."""

        async with self._lock:
            snap = self._coordinator.plant_snapshot
            if snap is None:
                _LOGGER.debug("No PlantSnapshot yet: skip control (%s)", reason)
                return None

            plan = self._planner.plan(snapshot=snap, reason=reason)

            # Apply (receding horizon)
            await self._actuator.apply_plan(plan=plan, snapshot=snap)

            self.state.last_plan = plan
            return plan
