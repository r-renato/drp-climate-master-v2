from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(slots=True)
class RcZoneParams:
    """First-order RC parameters for a single zone.

    Model (continuous time):
        dT/dt = (T_out - T)/tau + k*u

    where:
      - tau_h: time constant (hours)
      - k_c_per_h: effective heating gain (°C/hour) when the zone valve is ON
        (it implicitly absorbs supply water temperature + emitter effectiveness).
    """

    tau_h: float = 6.0
    k_c_per_h: float = 0.8


@dataclass(slots=True)
class MpcConfig:
    """MPC-lite configuration.

    This version is a per-zone receding-horizon optimizer over a binary input u∈{0,1}
    (zone valve ON/OFF). It is intentionally small and deterministic.
    """

    dt_minutes: int = 10
    horizon_steps: int = 12

    # Cost weights
    w_comfort: float = 10.0
    w_energy: float = 0.3
    w_switch: float = 1.5

    # Practical constraints
    min_switch_minutes: int = 10


@dataclass(slots=True)
class ControlConfig:
    """Top-level control configuration.

    For the first iteration we keep this runtime-only (defaults + optional overrides).
    Later you can expose it through ConfigEntry options.
    """

    mpc: MpcConfig = field(default_factory=MpcConfig)

    # Global defaults for RC params
    rc_default: RcZoneParams = field(default_factory=RcZoneParams)

    # Optional per-zone overrides, keyed by zone slug (e.g., "living")
    rc_by_zone: Mapping[str, RcZoneParams] = field(default_factory=dict)
