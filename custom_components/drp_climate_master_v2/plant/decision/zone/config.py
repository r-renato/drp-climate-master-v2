from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


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

    # Comfort slack (°C)
    # Ammorbidisce la penalità comfort allargando la banda: [t_min - slack, t_max + slack].
    # Se "accetti discomfort", questa è la leva più efficace contro il comportamento "full-on" su micro-sforamenti.
    comfort_slack_c: float = 0.10

    # --- Degeneracy detection / retry (1 sola ripianificazione)
    # Trigger tipico: molte zone FULL-ON mentre tutte le zone sono già "in band".
    degenerate_full_on_pct_thr: float = 80.0
    degenerate_retry_enabled: bool = True
    degenerate_retry_only_if_all_in_band: bool = True

    # Retry config: più tolleranza comfort + più focus energia (discomfort accettato)
    degenerate_retry_comfort_slack_c: float = 0.25
    degenerate_retry_w_comfort_mult: float = 0.35
    degenerate_retry_w_energy_mult: float = 1.50
    degenerate_retry_w_switch_mult: float = 0.75

    # Practical constraints
    min_on_minutes: int = 10
    min_off_minutes: int = 10
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
