from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional

from ....monitor.plant import PlantSnapshot

from ...config import ZonesMpcConfig
from ...zone.model import ZonesDecision
from ...zone.planner import ZoneDecisionPlanner
from ..model import ComfortBandResult

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ZonesMpcProvider:
    """Produce piani MPC-lite di zona (heating e cooling) per il plant planner."""

    cfg: ZonesMpcConfig = field(default_factory=ZonesMpcConfig)
    planner: ZoneDecisionPlanner = field(default_factory=ZoneDecisionPlanner)

    def _operative_season(self, snapshot: PlantSnapshot) -> str:
        """Restituisce 'winter', 'summer' o 'shoulder'."""
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", season)
        season_s = str(season_val).strip().lower() if season_val is not None else "unknown"
        if season_s == "winter":
            return "winter"
        if season_s == "summer":
            return "summer"
        return "shoulder"

    def maybe_plan(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
        comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None,
    ) -> Optional[ZonesDecision]:
        """Restituisce il piano MPC heating quando eligibile, altrimenti None."""

        if not bool(getattr(self.cfg, "enabled", True)):
            return None

        if not getattr(snapshot, "indoor_zones", None):
            return None

        operative = self._operative_season(snapshot)

        if operative == "winter" and not bool(getattr(self.cfg, "run_in_winter", True)):
            return None
        if operative == "shoulder" and not bool(getattr(self.cfg, "run_in_shoulder", True)):
            return None
        if operative == "summer" and not bool(getattr(self.cfg, "run_in_summer", False)):
            return None

        try:
            return self.planner.plan(
                snapshot=snapshot,
                reason=reason,
                comfort_bands_by_zone=comfort_bands_by_zone,
                cooling=False,
            )
        except Exception:
            _LOGGER.exception("Zones MPC heating planning failed")
            if bool(getattr(self.cfg, "swallow_exceptions", True)):
                return None
            raise

    def maybe_plan_cooling(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
        comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None,
        free_cool_feasible: bool = False,
    ) -> Optional[ZonesDecision]:
        """Restituisce il piano MPC cooling quando eligibile, altrimenti None."""
        if not bool(getattr(self.cfg, "enabled", True)):
            return None

        if not getattr(snapshot, "indoor_zones", None):
            return None

        operative = self._operative_season(snapshot)

        if operative == "winter" and not bool(getattr(self.cfg, "run_cooling_in_winter", False)):
            return None
        if operative == "shoulder" and not bool(getattr(self.cfg, "run_cooling_in_shoulder", True)):
            return None
        if operative == "summer" and not bool(getattr(self.cfg, "run_cooling_in_summer", True)):
            return None

        try:
            return self.planner.plan(
                snapshot=snapshot,
                reason=reason,
                comfort_bands_by_zone=comfort_bands_by_zone,
                cooling=True,
                free_cool_feasible=free_cool_feasible,
            )
        except Exception:
            _LOGGER.exception("Zones MPC cooling planning failed")
            if bool(getattr(self.cfg, "swallow_exceptions", True)):
                return None
            raise
