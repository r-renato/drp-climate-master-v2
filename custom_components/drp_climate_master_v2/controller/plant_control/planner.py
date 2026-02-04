from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from homeassistant.util import dt as dt_util

from ...helpers.utils import as_float
from ...domain.models.plant import PlantSnapshot
from ..rcmpc.contracts import ControlPlan

from .contracts import PlantDecision


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _is_true01(v: Optional[float]) -> Optional[bool]:
    if v is None:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return bool(v >= 0.5)


@dataclass(slots=True)
class PlantDecisionPlanner:
    """Compute plant-level decisions from snapshot (+ optional zone ControlPlan).

    v1 scope (safe, winter-first):
      - decide heating ON/OFF (PDC) based on zone demand (from MPC plan) + VMC water request
      - compute a conservative heating WOT setpoint from outdoor temp + worst deficit
      - decide pumps ON/OFF (mix/direct)
      - DO NOT try to close the loop on mixing valve yet (will come later)
    """

    # Basic heating curve knobs (tunable later)
    heat_wot_min_c: float = 28.0
    heat_wot_max_c: float = 46.0
    heat_wot_base_c: float = 30.0
    heat_wot_slope_per_c: float = 0.6   # per (15 - Tout)
    heat_wot_boost_per_c_deficit: float = 2.0

    heat_dt_k: float = 2.0

    def plan(
        self,
        *,
        snapshot: PlantSnapshot,
        zone_plan: Optional[ControlPlan],
        reason: str,
    ) -> PlantDecision:
        ts = snapshot.timestamp if isinstance(snapshot.timestamp, datetime) else dt_util.utcnow()
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)

        # Season (best-effort, tolerant)
        season = None
        try:
            season = getattr(getattr(snapshot, "season", None), "season", None)
            season = getattr(season, "value", None) if season is not None else None
        except Exception:
            season = None

        windows_closed = snapshot.windows_close_state is True

        # Outdoor temp: align with MpcLitePlanner style (AggregatedValue on snapshot)
        t_out_av = getattr(snapshot, "global_outdoor_temperature", None)
        t_out = as_float(getattr(t_out_av, "value", None))

        # VMC request water (0/1) - tolerant
        vmc = getattr(snapshot, "vmc", None)
        vmc_req_water = _is_true01(as_float(getattr(getattr(vmc, "req_water", None), "value", None)))

        # Demand from zone plan (preferred)
        heat_demand = False
        worst_deficit = 0.0
        if zone_plan and zone_plan.zones:
            heat_demand = any(bool(z.valve_on) for z in zone_plan.zones.values())
            # worst deficit from MPC debug (t_min - t_meas)
            for z in zone_plan.zones.values():
                t_meas = as_float(z.debug.get("t_meas")) if isinstance(z.debug, dict) else None
                t_min = as_float(z.debug.get("t_min")) if isinstance(z.debug, dict) else None
                if t_meas is not None and t_min is not None:
                    worst_deficit = max(worst_deficit, max(0.0, t_min - t_meas))
        else:
            # Fallback: scan snapshot.indoor_zones (robust)
            zones = getattr(snapshot, "indoor_zones", None) or {}
            for _, z in zones.items():
                t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
                if t_meas is None:
                    t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))
                cb = getattr(z, "confort_band", None)
                t_min = as_float(getattr(cb, "t_op_min", None))
                if t_meas is None or t_min is None:
                    continue
                d = max(0.0, t_min - t_meas)
                worst_deficit = max(worst_deficit, d)
                if d > 0.2:
                    heat_demand = True

        # Window policy (same spirit as MPC-lite):
        # if windows not closed, only heat if clearly below band
        if not windows_closed:
            heat_demand = bool(worst_deficit >= 1.0)

        # Default mode
        mode = "off"
        pdc_on = False
        pdc_mode = None

        # Winter-first behavior
        if season == "winter":
            pdc_on = bool(heat_demand or (vmc_req_water is True))
            pdc_mode = "heating" if pdc_on else None
            mode = "heating" if pdc_on else "off"
        else:
            # Not implemented yet (cooling/dehum/free-cooling will be introduced later)
            mode = "off"

        # Heating WOT target
        wot = None
        if pdc_on and pdc_mode == "heating":
            if t_out is None:
                # still can run, but we cannot compute a curve reliably
                # choose a conservative mid value
                wot = 36.0
            else:
                base = self.heat_wot_base_c + (15.0 - float(t_out)) * self.heat_wot_slope_per_c
                wot = base + worst_deficit * self.heat_wot_boost_per_c_deficit
                wot = _clamp(float(wot), self.heat_wot_min_c, self.heat_wot_max_c)

        # Pumps
        pump_mix_on = bool(heat_demand) if season == "winter" else None
        pump_direct_on = bool(vmc_req_water) if vmc_req_water is not None else None

        d = PlantDecision(
            # ts=ts,
            reason=reason,
            mode=mode,
            pdc_power=pdc_on if season == "winter" else None,
            # pdc_mode=pdc_mode,
            pdc_heat_wot_c=wot,
            # pdc_heat_dt_k=self.heat_dt_k if (pdc_on and pdc_mode == "heating") else None,
            pump_mix_on=pump_mix_on,
            pump_direct_on=pump_direct_on,
            # mix_valve_pct=None,
            # vmc_season="winter" if season == "winter" else None,
            # vmc_setpoints={},
            warnings=[],
            # debug={
            #     "season": season,
            #     "windows_closed": windows_closed,
            #     "t_out": t_out,
            #     "vmc_req_water": vmc_req_water,
            #     "heat_demand": heat_demand,
            #     "worst_deficit": worst_deficit,
            # },
        )

        if t_out is None:
            d.warnings.append("missing_outdoor_temperature_for_curve")
        if season is None:
            d.warnings.append("missing_season")
        return d
