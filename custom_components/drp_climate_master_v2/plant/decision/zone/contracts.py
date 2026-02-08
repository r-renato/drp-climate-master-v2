from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional
from collections.abc import MutableMapping


@dataclass(slots=True)
class ZoneCommand:
    """Immediate and horizon decisions for a single zone."""

    zone: str
    valve_on: bool
    seq: list[int] = field(default_factory=list)  # horizon (0/1)
    cost: float = 0.0
    debug: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ZonesDecision:
    """Output of the controller to be applied to the plant."""

    ts: datetime
    dt_minutes: int
    horizon_steps: int

    # Decisions
    zones: dict[str, ZoneCommand] = field(default_factory=dict)

    # Diagnostics
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    meta: MutableMapping[str, Any] = field(default_factory=dict)

    @property
    def any_heat_demand(self) -> bool:
        return any(z.valve_on for z in self.zones.values())

    def __str__(self) -> str:
        # --- helper di formattazione compatti e robusti ---
        def fnum(x, nd=2):
            return f"{x:.{nd}f}" if x is not None else "-"

        def fint(x):
            return str(int(x)) if x is not None else "-"

        def fbool(b, on="On", off="Off"):
            return on if b is True else (off if b is False else "-")

        def fstr(s):
            return s if s else "-"

        def flist(xs):
            return " | ".join(xs) if xs else "-"

        def fdict_compact_any(d, max_items=8, max_val_chars=40):
            """
            dict compatto tipo: k=v, ... (taglia dopo max_items)
            e tronca i valori troppo lunghi.
            """
            if not d:
                return "-"
            items = sorted(d.items(), key=lambda kv: str(kv[0]))
            more = ""
            if len(items) > max_items:
                items = items[:max_items]
                more = f" (+{len(d) - max_items})"

            def sval(v):
                s = repr(v)
                return (s[: max_val_chars - 1] + "…") if len(s) > max_val_chars else s

            s = ", ".join(f"{k}={sval(v)}" for k, v in items)
            return s + more

        def fseq(seq: list[int] | None, preview: int = 24):
            """
            seq 0/1 -> stringa compatta + sommario.
            Esempio: 110010… (len=60,on=23)
            """
            if not seq:
                return "-"
            head = "".join(str(int(v)) for v in seq[:preview])
            tail = "…" if len(seq) > preview else ""
            on = sum(1 for v in seq if int(v) == 1)
            return f"{head}{tail} (len={len(seq)},on={on})"

        # --- top-level ---
        ts = self.ts.isoformat()
        n_zones = len(self.zones)
        n_on = sum(1 for z in self.zones.values() if z.valve_on)
        total_cost = sum(z.cost for z in self.zones.values())

        lines = [
            f"",
            f"Zones decision",
            f"  Timestamp          :: {ts}",
            f"  Δt                 :: {fint(self.dt_minutes)} min",
            f"  Horizon steps      :: {fint(self.horizon_steps)}",
            f"  Any heat demand    :: {fbool(self.any_heat_demand, 'True', 'False')}",
            f"  Reason             :: {fstr(self.reason)}",
        ]

        # --- warnings / meta ---
        if self.warnings:
            lines += [
                f"------------------------------------------------------------------",
                f"Warnings            :: {flist(self.warnings)}",
            ]

        if self.meta:
            lines += [
                f"------------------------------------------------------------------",
                f"Meta                :: {fdict_compact_any(self.meta)}",
            ]

        lines += [f"------------------------------------------------------------------"]

        # --- zones ---
        lines += [
            f"Zones",
            f"  Count              :: {n_zones} (on={n_on}, off={n_zones - n_on})",
            f"  Total cost         :: {fnum(total_cost, 3)}",
        ]

        if not self.zones:
            lines += [f"  -"]
            lines += [f"------------------------------------------------------------------"]
            return "\n".join(lines)

        for zn in sorted(self.zones.keys()):
            z = self.zones[zn]
            lines += [
                f"  {z.zone}",
                f"    Valve            :: {fbool(z.valve_on, 'On', 'Off')}",
                f"    Cost             :: {fnum(z.cost, 3)}",
                f"    Seq              :: {fseq(z.seq, preview=min(24, max(0, self.horizon_steps)) or 24)}",
            ]
            if z.debug:
                lines += [f"    Debug            :: {fdict_compact_any(z.debug)}"]

        lines += [f"------------------------------------------------------------------"]
        return "\n".join(lines)

