from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....helpers.utils import as_float, clamp
from ....domain.models.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode
from ..zone.contracts import ZonesDecision


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
    ) -> None:
        cfg = self.cfg
        s = dec.supply

        # Heuristic: circuito radiante (mix) attivo se stiamo in heating/cooling e almeno una zona è pianificata ON
        any_zone_on = False
        if zones_decision:
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
        dp_max = as_float(demand.dp_max_c)
        if dec.mode == PlantMode.HEATING:
            # Deriva un target radiante da PDC heating setpoint (offset mixing)
            if dec.pdc.heat_wot_c is not None:
                t = dec.pdc.heat_wot_c - cfg.radiant.heat_supply_offset_c
                s.rad_supply_target_c = float(clamp(t, cfg.radiant.heat_supply_min_c, cfg.radiant.heat_supply_max_c))

        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            if dp_max is not None:
                safe_required = float(dp_max) + float(cfg.dp_guard.dp_margin_c) + float(cfg.dp_guard.delta_surface_water_c)

                # HARD guard: if required safe temp is above max allowed, do not run radiant cooling.
                if safe_required > float(cfg.radiant.cool_supply_max_c) + 1e-6:
                    dec.warnings.append("dew_guard_unachievable_radiant_disabled")
                    s.adj_pump_on = False
                    s.rad_supply_target_c = None
                    s.debug.update(
                        {
                            "dew_guard_required_c": safe_required,
                            "dew_guard_max_c": float(cfg.radiant.cool_supply_max_c),
                            "dew_guard_action": "disable_radiant",
                        }
                    )
                else:
                    s.rad_supply_target_c = float(
                        clamp(
                            safe_required,
                            cfg.radiant.cool_supply_min_c,
                            cfg.radiant.cool_supply_max_c,
                        )
                    )
            else:
                dec.warnings.append("missing_dp_max_for_dew_guard")

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
