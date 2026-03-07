from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from ...helpers.utils import as_float, slugify

from ..decision.contracts import PlantDecision

from .model import (
    StagingState,
    SupplyActuationResult,
    ValveCommand,
    ZoneValvesActuationResult,
    ZoneValvesDesired,
    ZoneValvesPlan,
    ZoneValvesStats,
)


def compute_zone_valves_plan(
    stage: StagingState,
    *,
    runtime_areas: Sequence[Any],
    desired: ZoneValvesDesired,
    allow_valves: bool,
    force_close_valves: bool,
    now: datetime,
    valve_open_delay_s: float,
) -> ZoneValvesPlan:
    """Costruisce il piano valvole e aggiorna lo staging (senza I/O).

    Nota
    ----
    `allow_valves` non significa "apri tutte": significa "applica il desiderato".
    Lo spegnimento/chiusura forzata e gestito da `force_close_valves` (fail-safe).
    """

    areas = [a for a in (runtime_areas or []) if getattr(a, "thermal_collector_valve_switch", None)]

    if not areas:
        stage.valves_open_request_ts = None
        stage.last_valves_desired = {}
        desired_empty = ZoneValvesDesired(by_zone={})
        stats = ZoneValvesStats(
            zones_total=0,
            zones_on=0,
            requested_at=None,
            elapsed_s=None,
            opening_transition=False,
        )
        res = ZoneValvesActuationResult(ready=True, desired=desired_empty, stats=stats)
        return ZoneValvesPlan(result=res, commands=[])

    if force_close_valves:
        allow_valves = False

    last = stage.last_valves_desired or {}
    commands: list[ValveCommand] = []

    # Caso: non consentito -> chiudi tutte (fail-safe).
    if not allow_valves:
        closed_map: dict[str, bool] = {}
        for area in areas:
            zkey = slugify(area.name)
            closed_map[zkey] = False
            if last.get(zkey) is not False:
                commands.append(ValveCommand(area_name=area.name, zone_key=zkey, state=False))

        stage.valves_open_request_ts = None
        stage.last_valves_desired = closed_map

        desired_empty = ZoneValvesDesired(by_zone={})
        stats = ZoneValvesStats(
            zones_total=len(areas),
            zones_on=0,
            requested_at=None,
            elapsed_s=None,
            opening_transition=False,
        )
        res = ZoneValvesActuationResult(ready=False, desired=desired_empty, stats=stats)
        return ZoneValvesPlan(result=res, commands=commands)

    # Caso: consentito -> applica desiderato per zona.
    desired_map: dict[str, bool] = {}
    for area in areas:
        zkey = slugify(area.name)
        desired_map[zkey] = bool(desired.by_zone.get(zkey, False))
        if last.get(zkey) != desired_map[zkey]:
            commands.append(ValveCommand(area_name=area.name, zone_key=zkey, state=desired_map[zkey]))

    on_cnt = sum(1 for v in desired_map.values() if v)
    any_open = on_cnt > 0

    opening_transition = any(desired_map.get(z, False) and not last.get(z, False) for z in desired_map)

    if any_open:
        if stage.valves_open_request_ts is None or opening_transition:
            stage.valves_open_request_ts = now
    else:
        stage.valves_open_request_ts = None

    stage.last_valves_desired = desired_map

    ts = stage.valves_open_request_ts
    elapsed: Optional[float] = (now - ts).total_seconds() if ts is not None else None
    ready = bool(elapsed is not None and elapsed >= float(valve_open_delay_s))

    stats = ZoneValvesStats(
        zones_total=len(desired_map),
        zones_on=on_cnt,
        requested_at=ts,
        elapsed_s=elapsed,
        opening_transition=opening_transition,
    )
    res = ZoneValvesActuationResult(ready=ready, desired=ZoneValvesDesired(by_zone=desired_map), stats=stats)
    return ZoneValvesPlan(result=res, commands=commands)


def compute_supply_plan(
    decision: PlantDecision,
    *,
    supply_configured: bool,
    allow_pumps: bool,
    force_pumps_off: bool,
    energy_ok: bool,
    valves_ready: bool,
) -> SupplyActuationResult:
    """Calcola lo stato target per pompe/miscelatrice (nessun I/O).

    Scelta progettuale
    ------------------
    - `energy_ok` e calcolato a monte usando:
        - boiler_ready se sensori disponibili, altrimenti PDC ON osservata.
    - Il ramo miscelato richiede anche `valves_ready` (evita dead-head).
    """
    if not supply_configured or force_pumps_off or not allow_pumps:
        return SupplyActuationResult(False, False, None)

    supply_cmd = getattr(decision, "supply", None)
    if supply_cmd is None:
        return SupplyActuationResult(False, False, None)

    direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False))
    adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False))

    direct_on = bool(energy_ok and direct_desired)
    adj_on = bool(energy_ok and valves_ready and adj_desired)

    mv_applied: Optional[float] = None
    mv = as_float(getattr(supply_cmd, "mix_valve_pct", None))
    if mv is not None and adj_on:
        mv_applied = float(mv)

    return SupplyActuationResult(direct_on=direct_on, adj_on=adj_on, mix_valve_pct_applied=mv_applied)
