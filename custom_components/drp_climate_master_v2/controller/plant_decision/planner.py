from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from homeassistant.util import dt as dt_util

from ...helpers.utils import as_float
from ...domain.models.plant import PlantSnapshot, ZoneSnapshot

from .config import PlantPlannerConfig
from .contracts import PlantDecision, PlantMode


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(slots=True)
class PlantDecisionPlanner:
    """Planner impianto (PDC + pompe + VMC) - versione disaccoppiata.

    Obiettivo:
      - produrre un PlantDecision (regime + target principali) usando PlantSnapshot
        e il piano di valvole di zona.
      - NON esegue attuazioni dirette (lascia a layer superiori / Supervisor).

    Filosofia:
      - decisione a livelli:
          1) determina il regime (heating/cooling/dehum_assist/vent_only/off) anche in base alle scelte dell'utente
             (climate hvac_mode: Off/auto + preset_mode: HVACOperatingProfile)
          2) calcola setpoint PDC (primario) e target secondario radiante
          3) calcola enable pompe e (in futuro) posizione miscelatrice
          4) suggerisce setpoint VMC (se richiesto)
    """

    cfg: PlantPlannerConfig = field(default_factory=PlantPlannerConfig)

    def plan(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
        zone_valve_plan: Optional[Mapping[str, int]] = None,
    ) -> PlantDecision:
        ts = snapshot.timestamp if isinstance(snapshot.timestamp, datetime) else dt_util.utcnow()
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)

        dec = PlantDecision(ts=ts, mode=PlantMode.OFF, reason=reason)

        # --- Outdoor temperature (required to compute curves, but we can fallback)
        t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
        if t_out is None or (isinstance(t_out, float) and (math.isnan(t_out) or math.isinf(t_out))):
            dec.warnings.append("missing_outdoor_temperature")
            t_out = None

        # --- Extract indoor demand signals
        demand = self._compute_demands(snapshot)
        dec.signals.update(demand)

        # --- Determine regime (very first version)
        mode = self._infer_mode(snapshot, demand)
        dec.mode = mode

        # --- Build device commands for the chosen mode
        self._fill_pdc_commands(dec, snapshot, t_out, demand)
        self._fill_supply_commands(dec, snapshot, demand, zone_valve_plan)
        self._fill_vmc_commands(dec, snapshot, demand)

        return dec

    def _compute_demands(self, snapshot: PlantSnapshot) -> Dict[str, Any]:
        """Compute high-level demand metrics from zones + VMC signals."""
        heat_def_max = 0.0
        cool_sur_max = 0.0
        heat_def_by_zone: Dict[str, float] = {}
        cool_sur_by_zone: Dict[str, float] = {}

        dp_max = None

        for zone_key, z in (snapshot.indoor_zones or {}).items():
            t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
            if t_meas is None:
                t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))

            band = getattr(z, "confort_band", None)
            t_min = as_float(getattr(band, "t_op_min", None))
            t_max = as_float(getattr(band, "t_op_max", None))

            if t_meas is not None and t_min is not None:
                d = max(0.0, float(t_min) - float(t_meas))
                heat_def_by_zone[zone_key] = d
                heat_def_max = max(heat_def_max, d)

            if t_meas is not None and t_max is not None:
                d = max(0.0, float(t_meas) - float(t_max))
                cool_sur_by_zone[zone_key] = d
                cool_sur_max = max(cool_sur_max, d)

            dp = as_float(getattr(getattr(z, "dew_point", None), "value", None))
            if dp is not None:
                dp_max = dp if dp_max is None else max(dp_max, dp)

        vmc = snapshot.vmc
        vmc_req_heat = bool(getattr(vmc, "req_heating", False)) if vmc else False
        vmc_req_cool = bool(getattr(vmc, "req_cooling", False)) if vmc else False
        vmc_req_dehum = bool(getattr(vmc, "req_dehumidif", False)) if vmc else False
        vmc_req_water = bool(getattr(vmc, "req_water", False)) if vmc else False

        return {
            "heat_def_max_c": heat_def_max,
            "cool_sur_max_c": cool_sur_max,
            "heat_def_by_zone_c": heat_def_by_zone,
            "cool_sur_by_zone_c": cool_sur_by_zone,
            "dp_max_c": dp_max,
            "vmc_req_heating": vmc_req_heat,
            "vmc_req_cooling": vmc_req_cool,
            "vmc_req_dehumidif": vmc_req_dehum,
            "vmc_req_water": vmc_req_water,
        }

    def _infer_mode(self, snapshot: PlantSnapshot, demand: Dict[str, Any]) -> PlantMode:
        cfg = self.cfg

        heat_def = float(demand.get("heat_def_max_c") or 0.0)
        cool_sur = float(demand.get("cool_sur_max_c") or 0.0)

        any_heat = heat_def >= cfg.heat_on_deficit_c or bool(demand.get("vmc_req_heating"))
        any_cool = cool_sur >= cfg.cool_on_surplus_c or bool(demand.get("vmc_req_cooling"))
        any_dehum = bool(demand.get("vmc_req_dehumidif"))

        # In questa versione non gestiamo ancora il cambio stagione sofisticato:
        # usiamo la stagione del runtime, se presente, come "prior".
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)

        if any_heat and not any_cool:
            return PlantMode.HEATING

        if any_cool:
            # Se c'è richiesta latente (dehum) -> DEHUM_ASSIST
            if any_dehum:
                return PlantMode.DEHUM_ASSIST
            # Se siamo in inverno ma c'è surplus, potresti avere sensori/valori anomali:
            # manteniamo comunque COOLING per coerenza con la richiesta.
            return PlantMode.COOLING

        # Nessuna richiesta "forte" -> ventilazione/standby
        if season_val in ("winter", "summer"):
            return PlantMode.VENT_ONLY

        return PlantMode.OFF

    def _fill_pdc_commands(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        t_out: Optional[float],
        demand: Dict[str, Any],
    ) -> None:
        cfg = self.cfg
        pdc = dec.pdc

        if dec.mode in (PlantMode.HEATING,):
            pdc.mode = "heating"
            pdc.power = True

            if t_out is not None:
                target = cfg.heat_curve_base_c + cfg.heat_curve_k_c_per_c * (cfg.heat_curve_ref_outdoor_c - float(t_out))
                target = _clamp(target, cfg.heat_wot_min_c, cfg.heat_wot_max_c)
            else:
                # fallback conservativo
                target = _clamp(cfg.heat_curve_base_c, cfg.heat_wot_min_c, cfg.heat_wot_max_c)

            pdc.heat_wot_c = float(target)
            pdc.heat_dt_c = float(cfg.heat_dt_c)
            pdc.debug["t_out_c"] = t_out
            pdc.debug["curve"] = "linear"

        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            pdc.mode = "cooling"
            pdc.power = True
            # In questa fase usiamo un setpoint flat; in futuro: profilo + vincoli batteria VMC.
            pdc.cool_wot_c = float(_clamp(cfg.cool_wot_default_c, cfg.cool_wot_min_c, cfg.cool_wot_max_c))
            pdc.cool_dt_c = float(cfg.cool_dt_c)

        elif dec.mode == PlantMode.VENT_ONLY:
            # Nota: in molte PDC conviene spegnere, salvo logiche anti-gelo/anti-stallo gestite nativamente.
            pdc.power = False

        else:
            pdc.power = False

        # Se abbiamo segnali di affidabilità bassi sulla PDC (compressore off ecc.), li mettiamo in debug.
        if snapshot.pdc:
            pdc.debug.update({
                "pdc_device_power": getattr(snapshot.pdc, "device_power", None),
                "pdc_compressor": getattr(snapshot.pdc, "compressor", None),
            })

    def _fill_supply_commands(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        demand: Dict[str, Any],
        zone_valve_plan: Optional[Mapping[str, int]],
    ) -> None:
        cfg = self.cfg
        s = dec.supply

        # Heuristic: circuito radiante (mix) attivo se stiamo in heating/cooling e almeno una zona è pianificata ON
        any_zone_on = False
        if zone_valve_plan:
            any_zone_on = any(int(v) == 1 for v in zone_valve_plan.values())
        else:
            # fallback: se c'è deficit/surplus, assumiamo che almeno una zona debba essere aperta
            any_zone_on = (float(demand.get("heat_def_max_c") or 0.0) >= cfg.heat_on_deficit_c) or (
                float(demand.get("cool_sur_max_c") or 0.0) >= cfg.cool_on_surplus_c
            )

        if dec.mode in (PlantMode.HEATING, PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            s.adj_pump_on = bool(any_zone_on)
        else:
            s.adj_pump_on = False

        # Circuito diretto (VMC) se la VMC richiede acqua
        s.direct_pump_on = bool(demand.get("vmc_req_water"))

        # Target mandata radiante (solo come segnale/telemetria, non è ancora un attuatore diretto)
        dp_max = as_float(demand.get("dp_max_c"))
        if dec.mode == PlantMode.HEATING:
            # Deriva un target radiante da PDC heating setpoint (offset mixing)
            if dec.pdc.heat_wot_c is not None:
                t = dec.pdc.heat_wot_c - cfg.heat_rad_supply_offset_c
                s.rad_supply_target_c = float(_clamp(t, cfg.heat_rad_supply_min_c, cfg.heat_rad_supply_max_c))
        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            if dp_max is not None:
                safe = float(dp_max) + cfg.dp_margin_c + cfg.delta_surface_water_c
                s.rad_supply_target_c = float(_clamp(safe, cfg.cool_rad_supply_min_c, cfg.cool_rad_supply_max_c))
            else:
                dec.warnings.append("missing_dp_max_for_dew_guard")

        s.debug["any_zone_on"] = any_zone_on

        # Snapshot values for diagnostics
        if snapshot.supply_unit:
            su = snapshot.supply_unit
            s.debug.update({
                "adj_supply_flow_c": as_float(getattr(getattr(su, "adj_supply_flow", None), "value", None)),
                "adj_return_flow_c": as_float(getattr(getattr(su, "adj_return_flow", None), "value", None)),
                "direct_supply_flow_c": as_float(getattr(getattr(su, "direct_supply_flow", None), "value", None)),
                "direct_return_flow_c": as_float(getattr(getattr(su, "direct_return_flow", None), "value", None)),
            })

    def _fill_vmc_commands(self, dec: PlantDecision, snapshot: PlantSnapshot, demand: Dict[str, Any]) -> None:
        cfg = self.cfg
        v = dec.vmc

        # In questa fase: non forziamo spegnimenti/accensioni aggressive, ma prepariamo setpoint suggeriti.
        if dec.mode in (PlantMode.HEATING, PlantMode.VENT_ONLY):
            v.power = True
            v.mode = cfg.vmc_mode_winter
        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            v.power = True
            v.mode = cfg.vmc_mode_summer
        else:
            v.power = None  # lascia a supervisor/policy
            v.mode = None

        v.setpoint_t_c = cfg.vmc_setpoint_t_c
        v.setpoint_rh_pct = cfg.vmc_setpoint_rh_pct
        v.setpoint_dp_c = cfg.vmc_setpoint_dp_c
        v.setpoint_ddp_c = cfg.vmc_setpoint_ddp_c

        # Diagnostics: report current vmc state
        if snapshot.vmc:
            vmc = snapshot.vmc
            v.debug.update({
                "device_power": getattr(vmc, "device_power", None),
                "req_water": getattr(vmc, "req_water", None),
                "req_heating": getattr(vmc, "req_heating", None),
                "req_cooling": getattr(vmc, "req_cooling", None),
                "req_dehumidif": getattr(vmc, "req_dehumidif", None),
                "ambient_t_c": as_float(getattr(getattr(vmc, "sensor_ambient_t", None), "value", None)),
                "ambient_rh_pct": as_float(getattr(getattr(vmc, "sensor_ambient_rh", None), "value", None)),
            })
