from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from ...domain.models.plant import PlantSnapshot

from ...helpers.utils import as_float, slugify

from ..decision.contracts import PdcCommand, PlantDecision

from .model import (
    BoilerReadinessDebug,
    BoilerReadinessUpdate,
    PlantFsmOutput,
    PlantPhase,
    SupplyActuationResult,
    ValveCommand,
    ZoneValvesActuationResult,
    ZoneValvesDesired,
    ZoneValvesPlan,
    ZoneValvesStats,
    StagingState,
)

def compute_current_plant_phase(snapshot: PlantSnapshot, decision: PlantDecision):
        """Stima la fase corrente dell'impianto (stateful)."""

        now = snapshot.timestamp
        plat_phase = PlantPhase.UNDEFINED

        reasons: list[str] = []

        # 1) FAULT espliciti
        if snapshot.faults:
            reasons.append(f"faults={','.join(snapshot.faults)}")
            return {
                "phase": PlantPhase.FAULT,
                "reasons": reasons,
            }

        pdc_cmd = getattr(decision, "pdc", None)
        pdc_req_power_on = pdc_requested_on(pdc_cmd) if pdc_cmd is not None else None

        pdc_snapshot = getattr(snapshot, "pdc", None)
        pdc_sta_power_on = getattr(pdc_snapshot, "power_on", None) if pdc_snapshot else None
        
        if pdc_req_power_on is None or pdc_sta_power_on is None:
            reasons.append(f", pdc_req_power_on={pdc_req_power_on}")
            reasons.append(f", pdc_sta_power_on={pdc_sta_power_on}")

        supply_cmd = getattr(decision, "supply", None)
        direct_pump_req_power_on = bool(getattr(supply_cmd, "direct_pump_on", False)) if supply_cmd is not None else None
        adj_pump_req_power_on = bool(getattr(supply_cmd, "adj_pump_on", False)) if supply_cmd is not None else None

        if direct_pump_req_power_on is None or adj_pump_req_power_on is None:
            reasons.append(f", direct_pump_req_power_on={direct_pump_req_power_on}")
            reasons.append(f", adj_pump_req_power_on={adj_pump_req_power_on}")

        supply_unit_snapshot = getattr(snapshot, "supply_unit", None)
        direct_pump_sta_power_on = getattr(supply_unit_snapshot, "direct_su_power_on", None) if supply_unit_snapshot else None
        adj_pump_sta_power_on = getattr(supply_unit_snapshot, "adjustable_su_power_on", None) if supply_unit_snapshot else None
        three_point_mixing_valve = getattr(supply_unit_snapshot, "three_point_mixing_valve", None) if supply_unit_snapshot else None

        closed_valve_req = (desired_zone_valves(decision=decision)).on_count
        closed_valve_sta = snapshot.indoor_zone_open_count

        if direct_pump_sta_power_on is None or adj_pump_sta_power_on is None:
            reasons.append(f", direct_pump_sta_power_on={direct_pump_sta_power_on}")
            reasons.append(f", adj_pump_sta_power_on={adj_pump_sta_power_on}")

        if len(reasons) > 0:
            return {
                "phase": PlantPhase.UNDEFINED,
                "reasons": reasons,
            }

        if pdc_req_power_on and adj_pump_req_power_on and closed_valve_req > 0:
            plat_phase = PlantPhase.STARTING

            if pdc_sta_power_on and adj_pump_sta_power_on:
                plat_phase = PlantPhase.RUNNING
        else:
            plat_phase = PlantPhase.STOPPING

            if not pdc_sta_power_on and not adj_pump_sta_power_on:
                plat_phase = PlantPhase.OFF

        return {
            "phase": plat_phase,
            "reasons": reasons,
        }




def mode_value(decision: PlantDecision) -> str:
    """Ritorna il valore stringa della modalità (heating/cooling/...) anche se è un Enum."""
    m = getattr(decision, "mode", None)
    return getattr(m, "value", str(m))


def pdc_requested_on(pdc: PdcCommand) -> bool:
    """True se la decisione richiede PDC ON (power o fm_power)."""
    return bool(getattr(pdc, "power", False) or getattr(pdc, "fm_power", False))


def compute_best_plant_t_target_c(decision: PlantDecision) -> Optional[float]:
    """Calcola il miglior target di temperatura acqua disponibile come *target di controllo*.

    Nota: questo non è necessariamente il target migliore per la *readiness* se il sensore
    di riferimento (es. t_boiler_supply) è in un punto diverso (pre/post miscelazione).
    """
    mode = mode_value(decision)
    supply = getattr(decision, "supply", None)
    if supply is not None:
        t = as_float(getattr(supply, "rad_supply_target_c", None))
        if t is not None:
            return float(t)

    pdc = getattr(decision, "pdc", None)
    if pdc is None:
        return None

    if mode == "heating":
        return as_float(getattr(pdc, "heat_wot_c", None))
    if mode in ("cooling", "dehum_assist"):
        return as_float(getattr(pdc, "cool_wot_c", None))
    return None

def compute_operation_plant_t_ready_ref_c(
    *,
    mode: str,
    t_control_target_c: Optional[float],
    heat_bias_c: float,
    cool_bias_c: float,
) -> Optional[float]:
    """Deriva il *target di readiness* coerente con il punto di misura.

    Caso tipico: `t_boiler_supply` misurata **prima** della miscelatrice.
    - Heating: il circuito a valle è più freddo del buffer outlet -> serve un bias positivo.
    - Cooling: il circuito a valle tende a essere più caldo del buffer outlet -> serve un bias negativo.

    Parametri
    ---------
    mode:
        Modalita impianto (heating/cooling/dehum_assist/...).
    t_control_target_c:
        Target di controllo (es. rad_supply_target o WOT).
    heat_bias_c / cool_bias_c:
        Offset (degC) applicato al target di controllo per ottenere il riferimento readiness.
    """
    if t_control_target_c is None:
        return None

    if mode == "heating":
        return float(t_control_target_c) + float(heat_bias_c)
    if mode in ("cooling", "dehum_assist"):
        return float(t_control_target_c) + float(cool_bias_c)
    return None


def update_boiler_ready(
    stage: StagingState,
    *,
    mode: str,
    t_boiler_supply: Optional[float],
    t_target: Optional[float],
    on_margin_c: float,
    off_margin_c: float,
) -> BoilerReadinessUpdate:
    """Aggiorna `stage.boiler_ready` con isteresi e ritorna diagnostica tipizzata."""
    dbg = BoilerReadinessDebug(
        t_boiler_supply_c=t_boiler_supply,
        t_target_c=t_target,
        on_thr_c=None,
        off_thr_c=None,
    )

    if t_boiler_supply is None or t_target is None:
        return BoilerReadinessUpdate(ready=stage.boiler_ready, debug=dbg)

    on_margin = float(on_margin_c)
    off_margin = float(off_margin_c)

    if mode == "heating":
        on_thr = float(t_target) - on_margin
        off_thr = float(t_target) - off_margin
        dbg.on_thr_c = on_thr
        dbg.off_thr_c = off_thr

        if not stage.boiler_ready:
            if float(t_boiler_supply) >= on_thr:
                stage.boiler_ready = True
        else:
            if float(t_boiler_supply) < off_thr:
                stage.boiler_ready = False

    elif mode in ("cooling", "dehum_assist"):
        on_thr = float(t_target) + on_margin
        off_thr = float(t_target) + off_margin
        dbg.on_thr_c = on_thr
        dbg.off_thr_c = off_thr

        if not stage.boiler_ready:
            if float(t_boiler_supply) <= on_thr:
                stage.boiler_ready = True
        else:
            if float(t_boiler_supply) > off_thr:
                stage.boiler_ready = False

    else:
        stage.boiler_ready = False

    return BoilerReadinessUpdate(ready=stage.boiler_ready, debug=dbg)


def desired_zone_valves(decision: PlantDecision) -> ZoneValvesDesired:
    """Calcola lo stato desiderato delle valvole di zona (zona -> bool)."""
    desired: dict[str, bool] = {}

    valves_cmd = getattr(decision, "valves", None)
    by_zone_cmd = getattr(valves_cmd, "by_zone", None) if valves_cmd is not None else None
    if by_zone_cmd:
        for zone_key, valve_on in by_zone_cmd.items():
            desired[str(zone_key)] = bool(valve_on)
        return ZoneValvesDesired(by_zone=desired)

    zones_plan = getattr(decision, "zones", None)
    zones_map = getattr(zones_plan, "zones", None) if zones_plan is not None else None
    if zones_map:
        for zone_key, zcmd in zones_map.items():
            desired[str(zone_key)] = bool(getattr(zcmd, "valve_on", False))
        return ZoneValvesDesired(by_zone=desired)

    sig = getattr(decision, "signals", None)
    if sig is None:
        return ZoneValvesDesired(by_zone=desired)

    mode = mode_value(decision)
    if mode == "heating":
        by_zone = getattr(sig, "heat_def_by_zone_c", None) or {}
        thr = as_float(getattr(sig, "heat_on_thr_c", None)) or 0.3
        for k, v in by_zone.items():
            fv = as_float(v) or 0.0
            desired[str(k)] = bool(fv >= float(thr))
    elif mode in ("cooling", "dehum_assist"):
        by_zone = getattr(sig, "cool_sur_by_zone_c", None) or {}
        thr = as_float(getattr(sig, "cool_on_thr_c", None)) or 0.3
        for k, v in by_zone.items():
            fv = as_float(v) or 0.0
            desired[str(k)] = bool(fv >= float(thr))

    return ZoneValvesDesired(by_zone=desired)


def fsm_step(
    stage: StagingState,
    *,
    now: datetime,
    request_on: bool,
    pdc_on: bool,
    compressor_on: Optional[bool],
    boiler_ready: bool,
    boiler_signal_available: bool,
    valves_ready: bool,
    needs_valves: bool,
    min_on_s: float,
    min_off_s: float,
    start_timeout_s: float,
    stop_timeout_s: float,
) -> PlantFsmOutput:
    """Esegue uno step della FSM di plant e restituisce un gating "forte".

    Scopo
    -----
    Stabilizzare la sequenza di accensione/spegnimento evitando oscillazioni anomale.
    """

    st = stage.fsm
    reasons: list[str] = []

    if st.entered_at is None:
        st.entered_at = now

    def elapsed_s() -> float:
        return (now - (st.entered_at or now)).total_seconds()

    def transition(new_phase: PlantPhase) -> None:
        nonlocal reasons
        if st.phase != new_phase:
            reasons.append(f"phase:{st.phase.value}->{new_phase.value}")
        st.phase = new_phase
        st.entered_at = now
        st.start_deadline = None
        st.stop_deadline = None
        if new_phase == PlantPhase.STARTING:
            st.start_deadline = now + timedelta(seconds=float(start_timeout_s))
        if new_phase == PlantPhase.STOPPING:
            st.stop_deadline = now + timedelta(seconds=float(stop_timeout_s))

    # Energia "credibile" disponibile.
    # - se sensori boiler disponibili: usa boiler_ready
    # - altrimenti fallback su pdc_on/compressore
    energy_ok = bool(boiler_ready) if boiler_signal_available else bool(pdc_on or compressor_on is True)

    # --- Transizioni ---
    if st.phase == PlantPhase.OFF:
        if request_on:
            if elapsed_s() >= float(min_off_s):
                transition(PlantPhase.STARTING)
            else:
                reasons.append("min_off_hold")

    elif st.phase == PlantPhase.STARTING:
        if not request_on:
            transition(PlantPhase.STOPPING)
        else:
            if st.start_deadline is not None and now > st.start_deadline:
                transition(PlantPhase.FAULT)
                reasons.append("start_timeout")
            else:
                ready_to_run = bool(energy_ok and (not needs_valves or valves_ready))
                if ready_to_run:
                    transition(PlantPhase.RUNNING)

    elif st.phase == PlantPhase.RUNNING:
        if not request_on:
            if elapsed_s() >= float(min_on_s):
                transition(PlantPhase.STOPPING)
            else:
                reasons.append("min_on_hold")

    elif st.phase == PlantPhase.STOPPING:
        # Attendi una finestra breve prima di dichiararti OFF.
        if st.stop_deadline is None:
            st.stop_deadline = now + timedelta(seconds=float(stop_timeout_s))
        if request_on and elapsed_s() >= float(stop_timeout_s):
            # ripartenza dopo una breve finestra di stop
            transition(PlantPhase.STARTING)
        elif st.stop_deadline is not None and now >= st.stop_deadline:
            transition(PlantPhase.OFF)

    elif st.phase == PlantPhase.FAULT:
        # Recupero solo quando richiesta OFF (operatore/algoritmo) e dwell minimo.
        if not request_on and elapsed_s() >= float(min_off_s):
            transition(PlantPhase.OFF)

    st.last_request_on = bool(request_on)

    # --- Output / gating forte ---
    phase = st.phase
    if phase == PlantPhase.OFF:
        return PlantFsmOutput(
            phase=phase,
            plant_on=False,
            allow_valves=False,
            allow_pumps=False,
            force_close_valves=True,
            force_pumps_off=True,
            reasons=reasons or ["off"],
        )

    if phase == PlantPhase.STARTING:
        # Durante avviamento abilitiamo le valvole in modo "robusto":
        # - apre presto per compensare i ~95s
        # - evita chiusure immediate su flap compressore
        allow_valves = bool(pdc_on and (compressor_on is None or compressor_on is True or boiler_ready))
        allow_pumps = False  # le pompe vengono abilitate in RUNNING (gating forte)
        return PlantFsmOutput(
            phase=phase,
            plant_on=True,
            allow_valves=allow_valves,
            allow_pumps=allow_pumps,
            force_close_valves=not allow_valves,
            force_pumps_off=True,
            reasons=reasons or ["starting"],
        )

    if phase == PlantPhase.RUNNING:
        return PlantFsmOutput(
            phase=phase,
            plant_on=True,
            allow_valves=True,
            allow_pumps=True,
            force_close_valves=False,
            force_pumps_off=False,
            reasons=reasons or ["running"],
        )

    if phase == PlantPhase.STOPPING:
        return PlantFsmOutput(
            phase=phase,
            plant_on=False,
            allow_valves=False,
            allow_pumps=False,
            force_close_valves=True,
            force_pumps_off=True,
            reasons=reasons or ["stopping"],
        )

    # FAULT
    return PlantFsmOutput(
        phase=phase,
        plant_on=False,
        allow_valves=False,
        allow_pumps=False,
        force_close_valves=True,
        force_pumps_off=True,
        reasons=reasons or ["fault"],
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
    """Costruisce il piano valvole e aggiorna lo staging (senza I/O)."""

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
    pdc_on: bool,
    compressor_on: Optional[bool],
    boiler_ready: bool,
    boiler_signal_available: bool,
    valves_ready: bool,
) -> SupplyActuationResult:
    """Calcola lo stato target per pompe/miscelatrice (nessun I/O)."""
    if not supply_configured or force_pumps_off or not allow_pumps:
        return SupplyActuationResult(False, False, None)

    supply_cmd = getattr(decision, "supply", None)
    if supply_cmd is None:
        return SupplyActuationResult(False, False, None)

    direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False))
    adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False))

    # Energia ok: se sensori boiler disponibili usa boiler_ready, altrimenti fallback su pdc_on/compressore.
    energy_ok = bool(boiler_ready) if boiler_signal_available else bool(pdc_on or compressor_on is True)

    direct_on = bool(pdc_on and energy_ok and direct_desired)
    adj_on = bool(pdc_on and energy_ok and valves_ready and adj_desired)

    mv_applied: Optional[float] = None
    mv = as_float(getattr(supply_cmd, "mix_valve_pct", None))
    if mv is not None and adj_on:
        mv_applied = float(mv)

    return SupplyActuationResult(direct_on=direct_on, adj_on=adj_on, mix_valve_pct_applied=mv_applied)
