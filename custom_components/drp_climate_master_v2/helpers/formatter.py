
from __future__ import annotations
from typing import Any


def fnum(x, nd=1):
    return f"{x:.{nd}f}" if x is not None else "-"

def fbool(b, on="on", off="off"):
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

def fpadstr(text: Any, pad_before: int = 0, field_width: int = 0, pad_after: int = 0) -> str:
    """
    Restituisce una stringa formattata con spazi:
      - pad_before spazi prima
      - stringa dentro un campo di larghezza minima field_width (padding a destra)
      - pad_after spazi dopo

    Esempio:
      fpadstr("ciao", 2, 10, 3) -> "  ciao      "
    """
    if pad_before < 0 or field_width < 0 or pad_after < 0:
        raise ValueError("pad_before, field_width e pad_after devono essere >= 0")

    s = "" if text is None else str(text)
    field = s.ljust(field_width)  # se s è più lunga di field_width, non viene tagliata
    return (" " * pad_before) + field + (" " * pad_after)