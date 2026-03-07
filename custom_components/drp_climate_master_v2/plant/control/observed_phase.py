from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.models.plant import PlantSnapshot

from .model import PlantPhase
from .signals import PlantControlContext


@dataclass(slots=True)
class ObservedPlantPhase:
    """Stima della fase *osservata* (diagnostica) distinta dalla FSM.

    Perche serve
    ------------
    La FSM e la fonte di verita per il gating attuativo.
    Questa stima e utile per commissioning/debug, perche descrive "cosa sembra stia facendo"
    l'impianto in base agli stati osservati (PDC, pompe, valvole).
    """

    phase: PlantPhase
    reasons: list[str] = field(default_factory=list)


def estimate_observed_phase(snapshot: PlantSnapshot, ctx: PlantControlContext) -> ObservedPlantPhase:
    """Stima (best-effort) della fase osservata, senza influire sul controllo."""
    reasons: list[str] = []

    faults = getattr(snapshot, "faults", None) or []
    if faults:
        reasons.append(f"faults={','.join(map(str, faults))}")
        return ObservedPlantPhase(phase=PlantPhase.FAULT, reasons=reasons)

    su = getattr(snapshot, "supply_unit", None)
    direct_on = getattr(su, "direct_su_power_on", None) if su else None
    adj_on = getattr(su, "adjustable_su_power_on", None) if su else None

    # Traccia cio che sappiamo (e cio che manca).
    if direct_on is None:
        reasons.append("direct_pump_state=na")
    if adj_on is None:
        reasons.append("adj_pump_state=na")
    if not ctx.pdc.effective_known:
        reasons.append("pdc_state=na")

    any_observed_on = bool(ctx.pdc.effective_on or direct_on is True or adj_on is True)

    if ctx.request_on:
        # C'e richiesta: se osserviamo energia+pompe -> running, altrimenti starting.
        if any_observed_on and (ctx.needs_valves is False or adj_on is True):
            return ObservedPlantPhase(phase=PlantPhase.RUNNING, reasons=reasons or ["running_obs"])
        return ObservedPlantPhase(phase=PlantPhase.STARTING, reasons=reasons or ["starting_obs"])

    # Nessuna richiesta: se qualcosa e ancora acceso -> stopping; altrimenti off.
    if any_observed_on:
        return ObservedPlantPhase(phase=PlantPhase.STOPPING, reasons=reasons or ["stopping_obs"])
    return ObservedPlantPhase(phase=PlantPhase.OFF, reasons=reasons or ["off_obs"])
