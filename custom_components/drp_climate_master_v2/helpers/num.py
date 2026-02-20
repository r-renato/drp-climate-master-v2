from __future__ import annotations

from typing import Iterable, Union, List
import math

Number = Union[int, float]

def quantile_linear(
    values: Iterable[Number],
    p: float,
    *,
    assume_sorted: bool = False,
    clamp_p: bool = False,
    nan_policy: str = "raise",  # "raise" | "omit" | "propagate"
) -> float:
    """
    Quantile empirico con interpolazione lineare (indice r = p*(n-1)).

    - values: iterabile di numeri
    - p: quantile in [0,1]
    - assume_sorted: se True, non ordina (più veloce, ma richiede input già ordinato crescente)
    - clamp_p: se True, clampa p in [0,1]; altrimenti alza ValueError fuori range
    - nan_policy:
        - "raise": errore se trova NaN
        - "omit": rimuove i NaN
        - "propagate": se trova NaN, ritorna NaN
    """
    xs: List[float] = [float(x) for x in values]

    n = len(xs)
    if n == 0:
        raise ValueError("empty values")

    # NaN handling
    has_nan = any(math.isnan(x) for x in xs)
    if has_nan:
        if nan_policy == "raise":
            raise ValueError("values contain NaN")
        elif nan_policy == "omit":
            xs = [x for x in xs if not math.isnan(x)]
            n = len(xs)
            if n == 0:
                raise ValueError("all values are NaN")
        elif nan_policy == "propagate":
            return float("nan")
        else:
            raise ValueError(f"invalid nan_policy: {nan_policy!r}")

    # Sort if needed
    if not assume_sorted:
        xs.sort()

    # p handling
    p = float(p)
    if clamp_p:
        if p < 0.0:
            p = 0.0
        elif p > 1.0:
            p = 1.0
    else:
        if not (0.0 <= p <= 1.0):
            raise ValueError("p must be in [0, 1]")

    if n == 1:
        return xs[0]

    r = p * (n - 1)
    lo = int(r)  # floor perché r >= 0
    hi = lo + 1
    if hi >= n:
        return xs[n - 1]

    frac = r - lo
    a = xs[lo]
    b = xs[hi]
    return a + (b - a) * frac
