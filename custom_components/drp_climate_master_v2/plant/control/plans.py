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

    # Costruisce lista piatta (area_name, area_slug, zone_key, valve_switch) da radiant_surfaces.
    # area_slug  = slugify(area.name)        → chiave logica (= chiave del decision layer)
    # zone_key   = area_slug | area_slug_{idx} → chiave diagnostica/comandi (unica per valvola)
    #
    # Invariante: desired.by_zone usa SEMPRE area_slug come chiave, mai zone_key indicizzato.
    # Il zone_key indicizzato serve solo per log e statistiche; non viene mai usato in lookup.
    valve_areas: list[tuple[str, str, str, str]] = []  # (area_name, area_slug, zone_key, valve_switch)
    for a in (runtime_areas or []):
        surfs = getattr(a, "radiant_surfaces", ()) or ()
        n = len(surfs)
        area_slug = slugify(a.name)
        for idx, surf in enumerate(surfs):
            vs = getattr(surf, "valve_switch", None)
            if vs:
                # zone_key indicizzato solo quando N>1 per evitare duplicati nel log.
                zkey = area_slug if n == 1 else f"{area_slug}_{idx}"
                valve_areas.append((a.name, area_slug, zkey, vs))

    if not valve_areas:
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

    areas = valve_areas  # alias per leggibilità nei blocchi sotto

    if force_close_valves:
        allow_valves = False

    last = stage.last_valves_desired or {}
    commands: list[ValveCommand] = []

    # Caso: non consentito -> chiudi tutte (fail-safe).
    if not allow_valves:
        closed_map: dict[str, bool] = {}
        for area_name, area_slug, zone_key, valve_switch in areas:
            closed_map[valve_switch] = False
            if last.get(valve_switch) is not False:
                commands.append(ValveCommand(area_name=area_name, zone_key=zone_key, valve_switch=valve_switch, state=False))

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
    for area_name, area_slug, zone_key, valve_switch in areas:
        # Lookup SEMPRE su area_slug (chiave logica del decision layer).
        # zone_key indicizzato non è mai prodotto dal decision layer (ZoneDecisionPlanner
        # itera snapshot.indoor_zones che usa slugify(area.name) senza indice).
        desired_map[valve_switch] = bool(desired.by_zone.get(area_slug, False))
        if last.get(valve_switch) != desired_map[valve_switch]:
            commands.append(ValveCommand(area_name=area_name, zone_key=zone_key, valve_switch=valve_switch, state=desired_map[valve_switch]))

    on_cnt = sum(1 for v in desired_map.values() if v)
    any_open = on_cnt > 0

    opening_transition = any(desired_map.get(vs, False) and not last.get(vs, False) for vs in desired_map)

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
        zones_total=len(areas),
        zones_on=on_cnt,
        requested_at=ts,
        elapsed_s=elapsed,
        opening_transition=opening_transition,
    )
    # desired restituisce zone_key → bool per diagnostica (zone_key può essere indicizzato)
    zone_desired_map = {zone_key: desired_map[vs] for _, _, zone_key, vs in areas if vs in desired_map}
    res = ZoneValvesActuationResult(ready=ready, desired=ZoneValvesDesired(by_zone=zone_desired_map), stats=stats)
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
