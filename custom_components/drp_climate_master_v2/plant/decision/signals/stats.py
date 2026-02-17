from __future__ import annotations

from typing import List


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def percentile_sorted(values_sorted: List[float], p01: float) -> float:
    """Percentile on a *sorted* list with linear interpolation.

    p01 in [0..1].
    """
    if not values_sorted:
        raise ValueError("empty values")
    p = clamp(float(p01), 0.0, 1.0)

    if len(values_sorted) == 1:
        return float(values_sorted[0])

    n = len(values_sorted)
    r = p * (n - 1)
    lo = int(r)
    hi = min(lo + 1, n - 1)
    frac = r - lo
    return float(values_sorted[lo]) * (1.0 - frac) + float(values_sorted[hi]) * frac
