from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class VmcState:
    """Minimal state for the VMC domain.

    Currently only used for DP hysteresis (anti-flapping) memory.
    """

    dehum_on: Optional[bool] = None
