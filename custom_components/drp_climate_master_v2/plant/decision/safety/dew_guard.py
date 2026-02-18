from __future__ import annotations

from dataclasses import dataclass

from ....helpers.utils import clamp
from ..config import PlantPlannerConfig
from ..contracts import PlantMode


@dataclass(slots=True, frozen=True)
class DewGuardResult:
    """Outcome of dew-point safety evaluation for radiant cooling.

    Fields
    - dp_max_c: max dew point observed across zones (°C), or None if unavailable.
    - safe_required_c: minimum safe radiant supply temperature (°C) to avoid condensation.
    - max_allowed_c: configured max radiant cooling supply (°C).
    - radiant_allowed: whether radiant cooling is permitted given dew-point constraints.
    - radiant_target_c: target radiant supply (°C) if allowed, else None.
    - suggested_mode: if current mode is incompatible with dew safety, a suggested fallback PlantMode.
    - reason: short machine-readable reason string (diagnostic).
    """

    dp_max_c: float | None
    safe_required_c: float | None
    max_allowed_c: float
    radiant_allowed: bool
    radiant_target_c: float | None
    suggested_mode: PlantMode | None
    reason: str | None = None


class DewGuardPolicy:
    """Single source of truth for dew-point safety constraints (radiant cooling).

    This policy is intentionally *pure* (no Home Assistant / IO).
    """

    def __init__(self, cfg: PlantPlannerConfig) -> None:
        self._cfg = cfg

    def evaluate(
        self,
        *,
        mode: PlantMode,
        dp_max_c: float | None,
        allow_dehum_assist: bool,
    ) -> DewGuardResult:
        cfg = self._cfg

        max_allowed = float(cfg.radiant.cool_supply_max_c)

        # Only meaningful for cooling-like modes.
        if mode not in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            return DewGuardResult(
                dp_max_c=dp_max_c,
                safe_required_c=None,
                max_allowed_c=max_allowed,
                radiant_allowed=True,
                radiant_target_c=None,
                suggested_mode=None,
                reason="not_applicable",
            )

        if dp_max_c is None:
            return DewGuardResult(
                dp_max_c=None,
                safe_required_c=None,
                max_allowed_c=max_allowed,
                radiant_allowed=False,
                radiant_target_c=None,
                suggested_mode=None,
                reason="missing_dp_max",
            )

        safe_required = float(dp_max_c) + float(cfg.dp_guard.dp_margin_c) + float(cfg.dp_guard.delta_surface_water_c)

        # Unachievable within configured bounds: radiant cooling must be disabled.
        if safe_required > max_allowed + 1e-6:
            suggested = None
            if mode == PlantMode.COOLING:
                suggested = PlantMode.DEHUM_ASSIST if allow_dehum_assist else PlantMode.VENT_ONLY
            return DewGuardResult(
                dp_max_c=float(dp_max_c),
                safe_required_c=float(safe_required),
                max_allowed_c=max_allowed,
                radiant_allowed=False,
                radiant_target_c=None,
                suggested_mode=suggested,
                reason="unachievable",
            )

        target = float(
            clamp(
                safe_required,
                float(cfg.radiant.cool_supply_min_c),
                float(cfg.radiant.cool_supply_max_c),
            )
        )
        return DewGuardResult(
            dp_max_c=float(dp_max_c),
            safe_required_c=float(safe_required),
            max_allowed_c=max_allowed,
            radiant_allowed=True,
            radiant_target_c=target,
            suggested_mode=None,
            reason="ok",
        )
