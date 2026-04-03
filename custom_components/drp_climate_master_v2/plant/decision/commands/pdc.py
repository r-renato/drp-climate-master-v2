from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ....helpers.utils import clamp
from ....domain.enums import HVACOperatingProfile
from ....plant.monitor.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode


@dataclass(slots=True)
class PdcCommandBuilder:
    """Build PDC commands for a chosen PlantMode.

    The builder owns *anti-hunting* state (rate limiting / deadband) so the main
    planner remains stateless and easier to test.
    """

    cfg: PlantPlannerConfig
    _last_heat_wot_c: Optional[float] = field(default=None, init=False, repr=False)
    _last_heat_wot_ts: Optional[datetime] = field(default=None, init=False, repr=False)

    def fill(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        t_out: Optional[float],
        demand: PlantDemandSignals,
    ) -> None:
        cfg = self.cfg
        pdc = dec.pdc

        profile = HVACOperatingProfile.from_value(
            getattr(snapshot, "climate_preset_mode", None),
            default=HVACOperatingProfile.COMFORT,
        ) or HVACOperatingProfile.COMFORT

        if dec.mode in (PlantMode.HEATING,):
            pdc.mode = "heating"
            pdc.power = True

            # --- 1) base curve (outdoor-driven)
            if t_out is not None:
                curve = cfg.heating.curve.base_c + cfg.heating.curve.k_c_per_c * (
                    cfg.heating.curve.ref_outdoor_c - float(t_out)
                )
            else:
                # fallback conservativo
                curve = cfg.heating.curve.base_c

            # --- 2) profile offset (by preset/profile)
            prof_offset = float(cfg.heating.enh.offset(profile))

            # --- 3) indoor feedback
            heat_def_max = float(demand.heat_def_max_c)
            heat_def_wmean = float(demand.heat_def_wmean_c)

            fb_unscaled = float(cfg.heating.enh.feedback_gain_c_per_c) * heat_def_wmean

            # If MPC-lite KPIs are available, scale feedback when only a few zones are expected to be active.
            # Thermo rationale: with many zone valves closed the circuit shortens and the delivered power
            # saturates quickly -> avoid chasing the mean deficit too aggressively to reduce cycling.
            activity_pct = demand.zones_on_now_pct if demand.zones_on_now_pct is not None else demand.zones_duty_avg_pct
            activity_scale = 1.0
            if activity_pct is not None:
                # 0..50% -> 0.5..1.0, >=50% -> 1.0
                activity_scale = clamp(float(activity_pct) / 50.0, 0.5, 1.0)

            fb = clamp(
                fb_unscaled * activity_scale,
                -float(cfg.heating.enh.feedback_max_down_c),
                float(cfg.heating.enh.feedback_max_up_c),
            )

            kick = 0.0
            if heat_def_max >= float(cfg.heating.enh.kick_on_max_def_c):
                kick = float(cfg.heating.enh.kick_extra_c)

            # --- B2: regime meteorologico → delta WOT in riscaldamento.
            # Legge cold_snap e regime_hint da snapshot.season (WeatherSeason, segnale ML).
            # Fail-safe: se il campo non è disponibile, regime_delta_c = 0.
            _season_state = getattr(snapshot, "season", None)
            _weather = getattr(_season_state, "weather", None)
            regime_hint: str = str(getattr(_weather, "regime_hint", "mild")) if _weather else "mild"
            cold_snap_active: bool = bool(getattr(_weather, "cold_snap", False)) if _weather else False

            # Priorità: regime_hint="cold" (assoluto) > cold_snap (relativo alla stagione).
            regime_delta_c: float = 0.0
            if regime_hint == "cold":
                regime_delta_c = float(cfg.heating.enh.regime_cold_delta_c)
            elif cold_snap_active:
                regime_delta_c = float(cfg.heating.enh.regime_cold_snap_delta_c)

            target = curve + prof_offset + fb + kick + regime_delta_c
            target = clamp(target, cfg.heating.curve.wot_min_c, cfg.heating.curve.wot_max_c)

            # --- 4) deadband + rate-limit (anti-hunting)
            now = dec.ts
            prev = self._last_heat_wot_c
            prev_ts = self._last_heat_wot_ts

            if prev is not None and prev_ts is not None:
                dt_min = max(0.001, (now - prev_ts).total_seconds() / 60.0)
                max_step = float(cfg.heating.enh.wot_rate_limit_c_per_min) * dt_min

                if abs(float(target) - float(prev)) < float(cfg.heating.enh.wot_deadband_c):
                    target = float(prev)
                else:
                    target = clamp(float(target), float(prev) - max_step, float(prev) + max_step)

            self._last_heat_wot_c = float(target)
            self._last_heat_wot_ts = now

            pdc.heat_wot_c = math.ceil(target)
            pdc.heat_dt_c = math.ceil(cfg.heating.curve.dt_c)
            pdc.debug.update(
                {
                    "t_out_c": t_out,
                    "curve": "linear+profile+feedback",
                    "curve_base": float(curve),
                    "profile_key": profile.value,
                    "profile_offset_c": float(prof_offset),
                    "heat_def_wmean_c": float(heat_def_wmean),
                    "heat_def_max_c": float(heat_def_max),
                    "feedback_c": float(fb),
                    "feedback_unscaled_c": float(fb_unscaled),
                    "zones_on_now_pct": demand.zones_on_now_pct,
                    "zones_duty_avg_pct": demand.zones_duty_avg_pct,
                    "zones_first_on_step": demand.zones_first_on_step,
                    "activity_scale": float(activity_scale),
                    "kick_c": float(kick),
                    "regime_hint": regime_hint,
                    "cold_snap": cold_snap_active,
                    "regime_delta_c": float(regime_delta_c),
                    "wot_target_pre_rate_c": float(curve + prof_offset + fb + kick + regime_delta_c),
                }
            )

        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            pdc.mode = "cooling"
            pdc.power = True
            # In questa fase usiamo un setpoint flat; in futuro: profilo + vincoli batteria VMC.
            pdc.cool_wot_c = math.ceil(
                clamp(
                    cfg.cooling.wot_default_c + cfg.cooling.offset(profile),
                    cfg.cooling.wot_min_c,
                    cfg.cooling.wot_max_c,
                )
            )
            pdc.cool_dt_c = math.ceil(cfg.cooling.dt_c)

        elif dec.mode == PlantMode.VENT_ONLY:
            # Nota: in molte PDC conviene spegnere, salvo logiche anti-gelo/anti-stallo gestite nativamente.
            pdc.power = False

        else:
            pdc.power = False

        # Snapshot values for diagnostics
        if snapshot.pdc:
            pdc.debug.update(
                {
                    "pdc_device_power": getattr(snapshot.pdc, "power_on", None),
                    "pdc_compressor": getattr(snapshot.pdc, "sensor_compressor_state", None),
                }
            )
