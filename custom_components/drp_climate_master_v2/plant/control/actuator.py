from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.drp_climate_master_v2.plant.decision.zone.config import ControlConfig

from ...controller.coordinator import ClimateCoordinator
from ...domain.models.plant import PlantSnapshot
from ...helpers.utils import slugify

_LOGGER = logging.getLogger(__name__)


def _state_is_on(state: Any) -> Optional[bool]:
    """Best-effort conversion for HA entity state -> bool."""
    if state is None:
        return None
    s = getattr(state, "state", None)
    if s is None:
        return None
    if isinstance(s, str):
        v = s.strip().lower()
        if v in ("on", "open", "true", "1"):
            return True
        if v in ("off", "closed", "false", "0"):
            return False
    return None


@dataclass(slots=True)
class _ZoneCommandMemory:
    last_value: Optional[bool] = None
    last_ts: Optional[datetime] = None


class PlantActuator:
    """Translate a ControlPlan into Home Assistant service calls.

    v1 scope:
      - zone radiant valves as switch entities (turn_on/turn_off)
      - idempotent & rate-limited (basic min-switch interval)
    """

    def __init__(self, hass: HomeAssistant, coordinator: ClimateCoordinator, cfg: ControlConfig) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._cfg = cfg
        self._mem: dict[str, _ZoneCommandMemory] = {}

        # zone_key -> switch entity_id
        self._zone_valves: dict[str, str] = {}
        for area in coordinator.runtime_config.climate.areas:
            if not getattr(area, "indoor", False):
                continue
            if not getattr(area, "radiant", False):
                continue
            ent = getattr(area, "thermal_collector_valve_switch", None)
            if ent:
                self._zone_valves[slugify(area.name)] = str(ent)

        _LOGGER.debug("Actuator zone valves=%s", self._zone_valves)

    def _now(self) -> datetime:
        return dt_util.utcnow()

    async def apply_plan(self, *, plan: ControlPlan, snapshot: PlantSnapshot) -> None:
        """Apply a plan (receding horizon: apply only the first step)."""

        for zone_key, decision in plan.zones.items():
            ent = self._zone_valves.get(zone_key)
            if not ent:
                continue
            await self._async_set_switch(
                zone_key=zone_key,
                entity_id=ent,
                desired=bool(decision.valve_on),
                min_switch_minutes=self._cfg.mpc.min_switch_minutes,
                reason=f"mpc:{plan.reason}",
            )

    async def _async_set_switch(
        self,
        *,
        zone_key: str,
        entity_id: str,
        desired: bool,
        min_switch_minutes: int,
        reason: str,
    ) -> None:
        mem = self._mem.setdefault(zone_key, _ZoneCommandMemory())
        now = self._now()

        # Current state (best-effort)
        st = self._hass.states.get(entity_id)
        current = _state_is_on(st)

        # If current is unknown, we still allow a command but we dedup on mem.
        if current is not None and current == desired:
            mem.last_value = desired
            mem.last_ts = now
            return

        if mem.last_value is not None and mem.last_value == desired:
            # Already requested the same state recently.
            return

        if mem.last_ts is not None:
            if (now - mem.last_ts) < timedelta(minutes=min_switch_minutes):
                return

        service = "turn_on" if desired else "turn_off"
        _LOGGER.info("Valve %s -> %s (%s)", zone_key, service, reason)
        await self._hass.services.async_call(
            "switch",
            service,
            {"entity_id": entity_id},
            blocking=False,
        )
        mem.last_value = desired
        mem.last_ts = now
