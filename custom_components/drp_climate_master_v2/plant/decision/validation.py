from __future__ import annotations

from .contracts import PlantDecision, PlantMode


def validate_decision(dec: PlantDecision) -> list[str]:
    """Best-effort coherence checks between PlantMode and compiled commands.

    This prevents silent contradictions (e.g. OFF but VMC ON).
    Returns warnings to be appended to PlantDecision.warnings.
    """

    w: list[str] = []

    if dec.mode == PlantMode.OFF:
        if bool(getattr(dec.pdc, "power", False)):
            w.append("incoherent_off_pdc_power_true")
        if bool(getattr(dec.supply, "adj_pump_on", False)) or bool(getattr(dec.supply, "direct_pump_on", False)):
            w.append("incoherent_off_pumps_on")
        if bool(getattr(dec.vmc, "power", False)):
            w.append("incoherent_off_vmc_power_true")
        if bool(getattr(getattr(dec, "valves", None), "any_open", False)):
            w.append("incoherent_off_valves_open")

    if dec.mode == PlantMode.VENT_ONLY:
        if bool(getattr(dec.pdc, "power", False)):
            w.append("incoherent_vent_only_pdc_power_true")
        # Pumps should generally be off in vent-only (unless architecture requires otherwise)
        if bool(getattr(dec.supply, "adj_pump_on", False)) or bool(getattr(dec.supply, "direct_pump_on", False)):
            w.append("incoherent_vent_only_pumps_on")
        if bool(getattr(getattr(dec, "valves", None), "any_open", False)):
            w.append("incoherent_vent_only_valves_open")

    if dec.mode == PlantMode.IAQ_ONLY:
        if bool(getattr(dec.pdc, "power", False)):
            w.append("incoherent_iaq_only_pdc_power_true")
        if bool(getattr(dec.supply, "adj_pump_on", False)) or bool(getattr(dec.supply, "direct_pump_on", False)):
            w.append("incoherent_iaq_only_pumps_on")
        if bool(getattr(getattr(dec, "valves", None), "any_open", False)):
            w.append("incoherent_iaq_only_valves_open")
        if bool(getattr(dec.vmc, "force_free_cooling", False)):
            w.append("incoherent_iaq_only_free_cooling_active")

    if dec.mode == PlantMode.HEATING:
        if getattr(dec.pdc, "mode", None) not in (None, "heating"):
            w.append("incoherent_heating_pdc_mode_not_heating")

    if dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
        if getattr(dec.pdc, "mode", None) not in (None, "cooling"):
            w.append("incoherent_cooling_pdc_mode_not_cooling")

    return w
