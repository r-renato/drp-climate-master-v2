from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from .config import RcZoneParams


@dataclass(slots=True)
class RcZoneModel:
    """Discrete 1st-order RC model for a single zone."""

    params: RcZoneParams

    def simulate(
        self,
        *,
        t0_c: float,
        t_out_c: Iterable[float],
        u: Iterable[int],
        dt_minutes: int,
    ) -> List[float]:
        """Simulate temperatures over the horizon.

        Returns a list of length N (same length as `u`), each being T(k+1).
        """

        tau_h = max(0.25, float(self.params.tau_h))
        k_c_per_h = float(self.params.k_c_per_h)
        dt_h = float(dt_minutes) / 60.0

        out: List[float] = []
        t = float(t0_c)
        for to, uk in zip(t_out_c, u):
            # Euler forward discretization
            dt_term = (float(to) - t) / tau_h
            heat_term = k_c_per_h * (1.0 if int(uk) else 0.0)
            t = t + dt_h * (dt_term + heat_term)
            out.append(t)

        return out
