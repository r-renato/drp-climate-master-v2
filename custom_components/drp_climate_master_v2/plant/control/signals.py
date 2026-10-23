from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ...plant.monitor.plant import PlantSnapshot
from ...helpers.utils import as_float

from ..decision.contracts import PdcCommand, PlantDecision

from .config import PlantActuatorConfig
from .model import ZoneValvesDesired


def mode_value(decision: PlantDecision) -> str:
    """Normalizza `decision.mode` in una stringa (es. 'heating', 'cooling', ...)."""
    m = getattr(decision, "mode", None)
    return getattr(m, "value", str(m))


def pdc_requested_power_on(pdc: Optional[PdcCommand]) -> bool:
    """True se la decisione richiede PDC ON (power o fm_power)."""
    if pdc is None:
        return False
    return bool(getattr(pdc, "power", False) or getattr(pdc, "fm_power", False))


def compute_best_plant_t_target_c(decision: PlantDecision) -> Optional[float]:
    """Miglior target disponibile come *target di controllo* (acqua).

    Priorita:
    1) `decision.supply.rad_supply_target_c` (se presente)
    2) `decision.pdc.heat_wot_c` o `decision.pdc.cool_wot_c` in base alla modalita
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
    Quindi, in riscaldamento, il punto tecnico deve essere piu alto del target utile.
    In raffrescamento, deve essere piu basso.

    Questo metodo incapsula l'assunzione impiantistica in un solo punto.
    """
    if t_control_target_c is None:
        return None
    if mode == "heating":
        return float(t_control_target_c) + float(heat_bias_c)
    if mode in ("cooling", "dehum_assist"):
        return float(t_control_target_c) + float(cool_bias_c)
    return None


def desired_zone_valves(decision: PlantDecision) -> ZoneValvesDesired:
    """Calcola lo stato desiderato delle valvole di zona (zona -> bool).

    Ordine sorgenti:
    1) `decision.valves.by_zone` se presente
    2) `decision.zones.zones[*].valve_on` se presente
    3) fallback da `decision.signals` con soglie di richiesta
    """
    desired: dict[str, bool] = {}

    valves_cmd = getattr(decision, "valves", None)
    by_zone_cmd = getattr(valves_cmd, "by_zone", None) if valves_cmd is not None else None
    if by_zone_cmd is not None:
        # by_zone_cmd == {} (dict vuoto) significa "nessuna valvola richiesta"
        # (es. mode=VENT_ONLY o OFF - ZoneValvesCommandBuilder produce {} dopo .clear()).
        # NON si deve cadere nel branch zones_plan che leggerebbe valve_on=True
        # dall'MPC e causerebbe request_on=True -> FSM stall loop in VENT_ONLY.
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


@dataclass(slots=True, frozen=True)
class PdcSignals:
    """Segnali PDC separando *richiesta* da *osservazione*."""

    requested_on: bool
    fm_power_on: Optional[bool]
    power_on: Optional[bool]
    compressor_on: Optional[bool]

    @property
    def effective_on(self) -> bool:
        """Stato ON *osservato* (il piu affidabile disponibile)."""
        return bool(self.power_on is True or self.compressor_on is True)

    @property
    def effective_known(self) -> bool:
        """True se almeno uno dei sensori di stato e disponibile."""
        return self.power_on is not None or self.compressor_on is not None


@dataclass(slots=True, frozen=True)
class BoilerSignals:
    """Segnali per readiness (mandata tecnica vs target)."""

    t_supply_c: Optional[float]
    t_control_target_c: Optional[float]
    t_ready_ref_c: Optional[float]

    @property
    def available(self) -> bool:
        return self.t_supply_c is not None and self.t_ready_ref_c is not None


@dataclass(slots=True, frozen=True)
class SupplyDemand:
    """Domanda lato distribuzione (pompe/miscelatrice)."""

    direct_desired: bool
    adjustable_desired: bool

    @property
    def any(self) -> bool:
        return bool(self.direct_desired or self.adjustable_desired)


@dataclass(slots=True, frozen=True)
class PlantControlContext:
    """Contesto *immutabile* per un ciclo di controllo plant/control.

    Obiettivo: concentrare in un solo posto tutte le derivazioni da (snapshot, decision, cfg)
    per evitare variabili sparse, duplicazioni e significati ambigui (requested vs observed).
    """

    now: datetime
    mode: str
    faults_present: bool

    request_on: bool
    needs_valves: bool
    # P-07b: domanda VMC indipendente da request_on idronico — guida la
    # fase FSM VMC_ONLY (nessun idronico attivo, solo ventilazione).
    vmc_request_on: bool

    pdc: PdcSignals
    boiler: BoilerSignals
    supply: SupplyDemand
    desired_valves: ZoneValvesDesired

    def __str__(self) -> str:
        """Rappresentazione compatta multi-linea per log/commissioning (§10)."""

        def fbool(b: object) -> str:
            return "True" if b is True else ("False" if b is False else "-")

        def fnum(x: Optional[float], nd: int = 2) -> str:
            return f"{float(x):.{nd}f}" if x is not None else "-"

        lines: list[str] = [
            "",
            "Plant control context",
            f"  Timestamp          :: {self.now.isoformat()}",
            f"  Mode               :: {self.mode}",
            f"  Faults present     :: {fbool(self.faults_present)}",
            "------------------------------------------------------------------",
            "Requests",
            f"  request_on         :: {fbool(self.request_on)}",
            f"  needs_valves       :: {fbool(self.needs_valves)}",
            f"  vmc_request_on     :: {fbool(self.vmc_request_on)}",
            "------------------------------------------------------------------",
            "PDC",
            f"  requested_on       :: {fbool(self.pdc.requested_on)}",
            f"  fm_power_on        :: {fbool(self.pdc.fm_power_on)}",
            f"  power_on           :: {fbool(self.pdc.power_on)}",
            f"  compressor_on      :: {fbool(self.pdc.compressor_on)}",
            f"  effective_on       :: {fbool(self.pdc.effective_on)}",
            f"  effective_known    :: {fbool(self.pdc.effective_known)}",
            "------------------------------------------------------------------",
            "Boiler",
            f"  t_supply_c         :: {fnum(self.boiler.t_supply_c)} degC",
            f"  t_control_target_c :: {fnum(self.boiler.t_control_target_c)} degC",
            f"  t_ready_ref_c      :: {fnum(self.boiler.t_ready_ref_c)} degC",
            f"  available          :: {fbool(self.boiler.available)}",
            "------------------------------------------------------------------",
            "Supply demand",
            f"  direct_desired     :: {fbool(self.supply.direct_desired)}",
            f"  adjustable_desired :: {fbool(self.supply.adjustable_desired)}",
            f"  any                :: {fbool(self.supply.any)}",
            "------------------------------------------------------------------",
            "Zone valves desired",
            f"  on_count           :: {self.desired_valves.on_count}",
            f"  any_open           :: {fbool(self.desired_valves.any_open)}",
            f"  by_zone            :: {self.desired_valves.by_zone or '-'}",
        ]
        return "\n".join(lines)


def build_control_context(
    *,
    now: datetime,
    snapshot: PlantSnapshot,
    decision: PlantDecision,
    cfg: PlantActuatorConfig,
) -> PlantControlContext:
    """Costruisce il contesto di controllo tipizzato per il ciclo corrente."""
    mode = mode_value(decision)

    # --- PDC signals
    pdc_command = getattr(decision, "pdc", None)
    req_power_on = pdc_requested_power_on(pdc_command)

    pdc_snapshot = getattr(snapshot, "pdc", None)
    pdc_signals = PdcSignals(
        requested_on=bool(req_power_on),
        fm_power_on=getattr(pdc_snapshot, "fm_power_on", None) if pdc_snapshot else None,
        power_on=getattr(pdc_snapshot, "power_on", None) if pdc_snapshot else None,
        compressor_on=getattr(pdc_snapshot, "sensor_compressor_state", None) if pdc_snapshot else None,
    )

    # --- Boiler signals (readiness)
    t_boiler_supply_c: Optional[float] = (
        as_float(getattr(getattr(snapshot, "supply_unit", None), "sensor_boiler_temp_system_supply", None))
        if getattr(snapshot, "supply_unit", None)
        else None
    )
    t_control_target_c = compute_best_plant_t_target_c(decision)
    t_ready_ref_c = compute_operation_plant_t_ready_ref_c(
        mode=mode,
        t_control_target_c=t_control_target_c,
        heat_bias_c=cfg.boiler_ready_heat_bias_c,
        cool_bias_c=cfg.boiler_ready_cool_bias_c,
    )
    boiler_signals = BoilerSignals(
        t_supply_c=t_boiler_supply_c,
        t_control_target_c=t_control_target_c,
        t_ready_ref_c=t_ready_ref_c,
    )

    # --- Supply demand
    supply_cmd = getattr(decision, "supply", None)
    direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False)) if supply_cmd is not None else False
    adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False)) if supply_cmd is not None else False
    supply = SupplyDemand(direct_desired=direct_desired, adjustable_desired=adj_desired)

    # --- Desired zone valves
    desired_valves = desired_zone_valves(decision)

    # --- VMC demand (P-07b): indipendente dalla domanda idronica, guida
    # la fase FSM VMC_ONLY (vent_only/iaq_only in stato stazionario).
    vmc_cmd = getattr(decision, "vmc", None)
    vmc_request_on = bool(getattr(vmc_cmd, "power", False)) if vmc_cmd is not None else False

    # --- Request derivation (single source of truth)
    request_on = bool(pdc_signals.requested_on or supply.any or desired_valves.any_open)
    needs_valves = bool(supply.adjustable_desired and desired_valves.any_open)

    faults_present = bool(getattr(snapshot, "faults", None))

    return PlantControlContext(
        now=now,
        mode=mode,
        faults_present=faults_present,
        request_on=request_on,
        needs_valves=needs_valves,
        vmc_request_on=vmc_request_on,
        pdc=pdc_signals,
        boiler=boiler_signals,
        supply=supply,
        desired_valves=desired_valves,
    )
