from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional

from homeassistant.components.climate.const import HVACMode

from .....domain.models.plant import PlantSnapshot

from ...config import ZonesMpcConfig
from ..model import ZonesDecision
from ..planner import ZoneDecisionPlanner
from ..confort_band.model import ComfortBandResult

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ZonesMpcProvider:
    """Provide (optionally) a zones MPC-lite plan to the plant-level planner.

    Design intent
    ------------
    The plant planner needs an optional `ZonesDecision` to:
      - avoid starting the plant when all valves are planned OFF,
      - compute pump intents (adjacent/mixing loop),
      - enable controlled *preheat* without short-cycling,
      - expose MPC KPIs for commissioning and tuning.

    This provider keeps the zones MPC logic **isolated** from `plant/decision/planner.py`.
    It encapsulates:
      - lightweight eligibility checks (season/user OFF),
      - the actual `ZoneDecisionPlanner` invocation.

    Notes
    -----
    v1 uses a *heating-only* MPC-lite. We therefore skip computation in summer
    to save CPU and avoid misleading signals.
    """

    cfg: ZonesMpcConfig = field(default_factory=ZonesMpcConfig)
    planner: ZoneDecisionPlanner = field(default_factory=ZoneDecisionPlanner)

    def maybe_plan(self, *, snapshot: PlantSnapshot, reason: str, comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None) -> Optional[ZonesDecision]:
        """Return a `ZonesDecision` when eligible, otherwise None."""

        if not bool(getattr(self.cfg, "enabled", True)):
            return None

        # No zones -> no plan
        if not getattr(snapshot, "indoor_zones", None):
            return None

        # User OFF -> do not spend CPU computing a plan that cannot be applied.
        hvac_mode_raw = getattr(snapshot, "climate_hvac_mode", None)
        hvac_mode_val = getattr(hvac_mode_raw, "value", hvac_mode_raw)
        hvac_mode_s = str(hvac_mode_val).strip().lower() if hvac_mode_val is not None else "auto"
        if bool(getattr(self.cfg, "skip_if_user_off", True)) and hvac_mode_s == HVACMode.OFF.value:
            return None

        # Season gating (operative bucket): winter/summer/shoulder
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", season)
        season_s = str(season_val).strip().lower() if season_val is not None else "unknown"
        operative = "winter" if season_s == "winter" else ("summer" if season_s == "summer" else "shoulder")

        if operative == "winter" and not bool(getattr(self.cfg, "run_in_winter", True)):
            return None
        if operative == "shoulder" and not bool(getattr(self.cfg, "run_in_shoulder", True)):
            return None
        if operative == "summer" and not bool(getattr(self.cfg, "run_in_summer", False)):
            return None

        try:
            return self.planner.plan(snapshot=snapshot, reason=reason, comfort_bands_by_zone=comfort_bands_by_zone)
        except Exception:
            # Keep plant planner robust: a failure in zone MPC must not kill the whole tick.
            _LOGGER.exception("Zones MPC planning failed")
            if bool(getattr(self.cfg, "swallow_exceptions", True)):
                return None
            raise
