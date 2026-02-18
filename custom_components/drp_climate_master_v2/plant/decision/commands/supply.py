from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....helpers.utils import as_float, clamp
from ....domain.models.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode
from ..zone.contracts import ZonesDecision


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
            s.debug.update(
                {
                    "adj_supply_flow_c": as_float(getattr(su, "sensor_adjustable_temp_system_supply", None)),
                    "adj_return_flow_c": as_float(getattr(su, "sensor_adjustable_temp_system_return", None)),
                    "direct_supply_flow_c": as_float(getattr(su, "sensor_direct_temp_system_supply", None)),
                    "direct_return_flow_c": as_float(getattr(su, "sensor_direct_temp_system_return", None)),
                    "boiler_supply_flow_c": as_float(getattr(su, "sensor_boiler_temp_system_supply", None)),
                    "boiler_return_flow_c": as_float(getattr(su, "sensor_boiler_temp_system_return", None)),
                }
            )
