from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional


@dataclass(slots=True)
class ZoneDecision:
    """Immediate and horizon decisions for a single zone."""

    zone: str
    valve_on: bool
    seq: list[int] = field(default_factory=list)  # horizon (0/1)
    cost: float = 0.0
    debug: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ControlPlan:
    """Output of the controller to be applied to the plant."""

    ts: datetime
    dt_minutes: int
    horizon_steps: int

    # Decisions
    zones: dict[str, ZoneDecision] = field(default_factory=dict)

    # Diagnostics
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def any_heat_demand(self) -> bool:
        return any(z.valve_on for z in self.zones.values())
