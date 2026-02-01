from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(slots=True)
class PlantDecision:
    # Minimal contract (extend later)
    mode: Optional[str] = None               # "heating" | "cooling" | "off" | ...
    pdc_power: Optional[bool] = None
    pump_mix_on: Optional[bool] = None
    pump_direct_on: Optional[bool] = None
    pdc_heat_wot_c: Optional[float] = None

    warnings: List[str] = field(default_factory=list)
    reason: str = "tick"
    enabled: bool = True

    @classmethod
    def disabled(cls, *, reason: str = "disabled") -> "PlantDecision":
        # Explicit no-op decision (safe default)
        return cls(
            mode=None,
            pdc_power=None,
            pump_mix_on=None,
            pump_direct_on=None,
            pdc_heat_wot_c=None,
            warnings=["plant_control_disabled"],
            reason=reason,
            enabled=False,
        )
