from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....helpers.utils import as_float, clamp
from ....domain.models.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode
from ..safety.dew_guard import DewGuardResult
from ..zone.model import ZonesDecision


def _mix_valve_target_pct(
    *,
    t_target_c: float | None,
    t_primary_c: float | None,
    t_return_c: float | None,
    eps_c: float = 0.10,
) -> float | None:
    """Compute mixing valve target percentage (0..100) for a desired mixed supply.

    Decision-level approximation using a linear mixing law::

        T_mix = x*T_primary + (1-x)*T_return

    Returned value is x*100, i.e. the fraction of primary/PDC water admitted.
    Works for both heating and cooling (denominator sign changes).
    """
    if t_target_c is None or t_primary_c is None or t_return_c is None:
        return None

    denom = t_primary_c - t_return_c
    if abs(denom) < eps_c:
        # Primary and return are essentially identical -> valve position is irrelevant.
        # Default to 100% (no mixing) as a safe monotonic choice.
        return 100.0

    x = (t_target_c - t_return_c) / denom
    x = clamp(x, 0.0, 1.0)
    return round(x * 100.0, 1)


@dataclass(slots=True)
class SupplyCommandBuilder:
    """Build commands for pumps/supply unit based on PlantMode and zone plan."""

    cfg: PlantPlannerConfig

    def fill(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        demand: PlantDemandSignals,
        zones_decision: Optional[ZonesDecision],
        dew_guard: Optional[DewGuardResult] = None,
    ) -> None:
        cfg = self.cfg
        s = dec.supply

        # Heuristic: circuito radiante (mix) attivo se abbiamo almeno una elettrovalvola "ON".
        # Preferiamo la sorgente unificata `dec.valves` (builder dedicato), altrimenti
        # ricadiamo su MPC/fallback per retro-compatibilità.
        any_zone_on = False
        if getattr(dec, "valves", None) is not None and bool(getattr(dec.valves, "any_open", False)):
            any_zone_on = True
        elif zones_decision:
            any_zone_on = any(v.valve_on is True for v in zones_decision.zones.values())
        else:
            # fallback: se c'è deficit/surplus, assumiamo che almeno una zona debba essere aperta
            any_zone_on = (float(demand.heat_def_max_c) >= cfg.comfort.heat_on_deficit_c) or (
                float(demand.cool_sur_max_c) >= cfg.comfort.cool_on_surplus_c
            )

        if dec.mode in (PlantMode.HEATING, PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            s.adj_pump_on = bool(any_zone_on)
        else:
            s.adj_pump_on = False

        # Circuito diretto (VMC) se la VMC richiede acqua (solo quando il plant è attivo lato idronico)
        if dec.mode in (PlantMode.HEATING, PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            s.direct_pump_on = bool(demand.vmc_req_water)
        else:
            s.direct_pump_on = False

        # Target mandata radiante (solo come segnale/telemetria, non è ancora un attuatore diretto)
        if dec.mode == PlantMode.HEATING:
            # Deriva un target radiante da PDC heating setpoint (offset mixing)
            if dec.pdc.heat_wot_c is not None:
                t = dec.pdc.heat_wot_c - cfg.radiant.heat_supply_offset_c
                s.rad_supply_target_c = float(clamp(t, cfg.radiant.heat_supply_min_c, cfg.radiant.heat_supply_max_c))

        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            # Dew-point safety is evaluated upstream by DewGuardPolicy (single source of truth).
            if dew_guard is None:
                dec.warnings.append("dew_guard_missing_result_radiant_disabled")
                s.adj_pump_on = False
                s.rad_supply_target_c = None
                s.debug.update(
                    {
                        "dew_guard_action": "disable_radiant",
                        "dew_guard_reason": "missing_result",
                    }
                )
            elif not dew_guard.radiant_allowed:
                # FAIL-SAFE: if radiant is not allowed, force the mixing circuit off.
                if dew_guard.reason == "missing_dp_max":
                    dec.warnings.append("dew_guard_missing_dp_max_radiant_disabled")
                elif dew_guard.reason == "unachievable":
                    dec.warnings.append("dew_guard_unachievable_radiant_disabled")
                else:
                    dec.warnings.append(f"dew_guard_{dew_guard.reason}_radiant_disabled")

                s.adj_pump_on = False
                s.rad_supply_target_c = None
                s.debug.update(
                    {
                        "dew_guard_dp_max_c": dew_guard.dp_max_c,
                        "dew_guard_required_c": dew_guard.safe_required_c,
                        "dew_guard_max_c": dew_guard.max_allowed_c,
                        "dew_guard_action": "disable_radiant",
                        "dew_guard_reason": dew_guard.reason,
                        "dew_guard_suggested_mode": dew_guard.suggested_mode.value if dew_guard.suggested_mode else None,
                    }
                )
            else:
                s.rad_supply_target_c = float(dew_guard.radiant_target_c) if dew_guard.radiant_target_c is not None else None
                s.debug.update(
                    {
                        "dew_guard_dp_max_c": dew_guard.dp_max_c,
                        "dew_guard_required_c": dew_guard.safe_required_c,
                        "dew_guard_max_c": dew_guard.max_allowed_c,
                        "dew_guard_action": "set_radiant_target",
                        "dew_guard_reason": dew_guard.reason,
                    }
                )

        s.debug["any_zone_on"] = any_zone_on

        # Snapshot values for diagnostics
        if snapshot.supply_unit:
            su = snapshot.supply_unit
            t_adj_supply = as_float(getattr(su, "sensor_adjustable_temp_system_supply", None))
            t_adj_return = as_float(getattr(su, "sensor_adjustable_temp_system_return", None))
            t_dir_supply = as_float(getattr(su, "sensor_direct_temp_system_supply", None))
            t_dir_return = as_float(getattr(su, "sensor_direct_temp_system_return", None))
            t_boiler_supply = as_float(getattr(su, "sensor_boiler_temp_system_supply", None))
            t_boiler_return = as_float(getattr(su, "sensor_boiler_temp_system_return", None))

            # Decision-level mixing valve target: percentage of primary water admitted (0..100).
            s.mix_valve_pct = _mix_valve_target_pct(
                t_target_c=s.rad_supply_target_c,
                t_primary_c=t_boiler_supply,
                t_return_c=t_adj_return,
            )

            s.debug.update(
                {
                    "adj_supply_flow_c": t_adj_supply,
                    "adj_return_flow_c": t_adj_return,
                    "direct_supply_flow_c": t_dir_supply,
                    "direct_return_flow_c": t_dir_return,
                    "boiler_supply_flow_c": t_boiler_supply,
                    "boiler_return_flow_c": t_boiler_return,
                    "mix_valve_calc": {
                        "t_target_c": s.rad_supply_target_c,
                        "t_primary_c": t_boiler_supply,
                        "t_return_c": t_adj_return,
                        "mix_valve_pct": s.mix_valve_pct,
                    },
                }
            )
