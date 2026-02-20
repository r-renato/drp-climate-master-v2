"""Domain types for comfort-band and policy decisions.

Refactor goals (no logic changes)
--------------------------------
- Centralize shared types used by both policy and calculator.
- Provide a single room classification helper (is_living).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional, Tuple

from .....domain.enums import HVACOperatingProfile
from .....domain.models.season import OperativeSeason


def is_living(room: str) -> bool:
    """Heuristic: classify room as living area.

    Logic preserved from duplicated implementations.
    """
    r = (room or "").strip().lower()
    return (r == "living") or ("soggiorno" in r) or ("salotto" in r)


class HumiditySolveMode(StrEnum):
    """How to handle humidity while solving comfort-band bounds.

    - RH_CONST: keep relative humidity constant as temperature varies.
    - PA_CONST: keep vapor pressure (approx absolute humidity / dew point) constant.
    - AUTO: use PA_CONST if an anchor temperature is available, else RH_CONST.
    """

    AUTO = "auto"
    RH_CONST = "rh_const"
    PA_CONST = "pa_const"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Runtime context used by the policy layer."""

    now: datetime
    room: str
    season: OperativeSeason
    vmc_speed: int
    rh_pct: float
    t_op_current: Optional[float]
    mode: HVACOperatingProfile
    outdoor_temp: Optional[float] = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Policy decision passed to ComfortBandCalculator."""

    met: float
    clo: float
    pmv_center: float
    pmv_band: float

    # Controller-side knob (physics does not change)
    ctrl_aggressiveness: float = 1.0

    # Naming aligned to ComfortBandCalculator
    v_air_best_scale: float = 1.0
    v_air_hi_scale: float = 1.0
    v_air_lo_override: Optional[float] = None
    draft_robustness: Optional[float] = None
    humidity_solve_mode: HumiditySolveMode = HumiditySolveMode.AUTO

    # Optional compliance flags (None = not evaluated)
    heating_allowed: Optional[bool] = None
    cooling_allowed: Optional[bool] = None

    # Human-readable reasons
    reasons: Tuple[str, ...] = ()


@dataclass(slots=True)
class ComfortBandResult:
    """Output of comfort-band computation for a given zone."""

    room: str
    season: OperativeSeason
    speed: int

    v_air_best: float
    v_air_lo: float
    v_air_hi: float

    t_op_min: float
    t_op_max: float

    v_air_draft: Optional[float] = None

    t_op: Optional[float] = None
    pmv: Optional[float] = None
    ppd: Optional[float] = None
    ok: Optional[bool] = None

    # Debug/traceability: how humidity was handled while solving the band
    humidity_solve_mode: Optional[str] = None

    pmv_center: Optional[float] = None
    pmv_band: Optional[float] = None

    met_used: Optional[float] = None
    clo_used: Optional[float] = None

    def __str__(self) -> str:
        # --- helper di formattazione compatti e robusti (coerenti con PlantDecision.__str__) ---
        def fnum(x, nd=2):
            if x is None:
                return "-"
            try:
                return f"{float(x):.{nd}f}"
            except (TypeError, ValueError):
                return str(x)

        def fbool(b, on="True", off="False"):
            return on if b is True else (off if b is False else "-")

        def fint(x) -> str:
            if x is None:
                return "-"
            try:
                return str(int(x))
            except (TypeError, ValueError):
                return str(x)

        def fstr(s):
            return s if s else "-"

        def fseason(s):
            # OperativeSeason o str
            try:
                return s.value  # type: ignore[attr-defined]
            except Exception:
                return fstr(str(s) if s is not None else None)

        def emit(lines: list[str], label: str, value: str) -> None:
            # usa lo stesso separatore " :: " e un campo label largo 30 come nel tuo esempio
            lines.append(f"  {fstr(label):<30} :: {value}")

        # NB: header "finto" per compatibilità con parsing splitlines()[1:]
        lines: list[str] = [
            "ComfortBandResult",
            "------------------------------------------------------------------",
            "Comfort band",
        ]

        # -------------------------
        # Cluster: Context
        # -------------------------
        lines += ["Context"]
        emit(lines, "Room", fstr(self.room))
        emit(lines, "Season", fseason(self.season))
        emit(lines, "VMC speed", fint(self.speed))

        # -------------------------
        # Cluster: Air speed model
        # -------------------------
        lines += ["Air speed (m/s)"]
        emit(lines, "v_air best", fnum(self.v_air_best, 3))
        emit(lines, "v_air lo", fnum(self.v_air_lo, 3))
        emit(lines, "v_air hi", fnum(self.v_air_hi, 3))
        emit(lines, "v_air draft", fnum(self.v_air_draft, 3))

        # -------------------------
        # Cluster: Comfort band
        # -------------------------
        lines += ["Comfort band (°C)"]
        emit(lines, "t_op min", fnum(self.t_op_min, 2))
        emit(lines, "t_op max", fnum(self.t_op_max, 2))

        # -------------------------
        # Cluster: Evaluation (current)
        # -------------------------
        lines += ["Evaluation"]
        emit(lines, "t_op", fnum(self.t_op, 2))
        emit(lines, "PMV", fnum(self.pmv, 2))
        emit(lines, "PPD", fnum(self.ppd, 1))
        emit(lines, "OK", fbool(self.ok, "True", "False"))

        # -------------------------
        # Cluster: Knobs / traceability
        # -------------------------
        lines += ["Knobs"]
        emit(lines, "Humidity solve", fstr(self.humidity_solve_mode))
        emit(lines, "PMV target", f"{fnum(self.pmv_center, 2)} ± {fnum(self.pmv_band, 2)}")
        emit(lines, "met used", fnum(self.met_used, 2))
        emit(lines, "clo used", fnum(self.clo_used, 2))

        return "\n".join(lines)

