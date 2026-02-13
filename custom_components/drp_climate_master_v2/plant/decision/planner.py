from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional
import logging

from homeassistant.util import dt as dt_util
from homeassistant.components.climate.const import HVACMode

from .zone.contracts import ZonesDecision

from ...helpers.logger import log_debug

from ...helpers.utils import as_float
from ...helpers.psychrometric import dew_point_celsius
from ...domain.models.plant import PlantSnapshot
from ...domain.enums import HVACOperatingProfile

from .config import PlantPlannerConfig
from .contracts import MetricBasis, PlantDecision, PlantDemandSignals, PlantMode

_LOGGER = logging.getLogger(__name__)

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# Profile -> controller aggressiveness (kept aligned with ComfortPolicy defaults).
# Used to scale ON thresholds (BOOST reacts earlier; AWAY/VACATION later).
_PROFILE_CTRL_AGGR = {
    HVACOperatingProfile.COMFORT: 1.00,
    HVACOperatingProfile.BOOST: 1.35,
    HVACOperatingProfile.ECO: 0.85,
    HVACOperatingProfile.SLEEP: 0.75,
    HVACOperatingProfile.AWAY: 0.50,
    HVACOperatingProfile.VACATION: 0.50,
}

def _zone_weight(z: Any) -> float:
    """Best-effort zone weight extraction. Defaults to 1.0.

    Weights are used only for quorum/coverage and weighted means (energy-saving profiles).
    If weights are missing or all zero, we fallback to count-based metrics.
    """
    w = as_float(getattr(z, "weight", None))
    if w is None:
        w = as_float(getattr(z, "area_weight", None))
    if w is None:
        w = 1.0
    try:
        wf = float(w)
    except Exception:
        wf = 1.0
    # Negative weights make no sense; allow 0 (zone ignored in weighted metrics)
    return max(0.0, wf)


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

    # --- minimal state for setpoint rate limiting (anti-hunting)
    _last_heat_wot_c: Optional[float] = field(default=None, init=False, repr=False)
    _last_heat_wot_ts: Optional[datetime] = field(default=None, init=False, repr=False)

    def plan(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
        zones_decision: Optional[ZonesDecision] = None,
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

        # --- Determine regime
        mode = self._infer_mode(snapshot, demand, zones_decision)
        dec.mode = mode

        # Diagnostics / warnings derived from enriched demand
        if getattr(demand, "vmc_dehum_feasible", None) is False:
            dec.warnings.append("vmc_dehum_unfeasible_outdoor_dp")
        if demand.zones_any_heat_demand and getattr(demand, "zones_mpc_heat_preheat_ok", None) is False:
            dec.warnings.append("zones_mpc_heat_ignored_headroom")

        log_debug(_LOGGER, "Computed plant demands: %s", demand)
        log_debug(_LOGGER, "Computed plant regime: %s", mode)
        dec.signals = demand

        # --- HARD dew-guard: if we intended COOLING but the required safe radiant supply
        # cannot be achieved within configured bounds, degrade mode to avoid condensation risk.
        # (We still allow DEHUM_ASSIST if latent demand exists.)
        if dec.mode == PlantMode.COOLING and demand.dp_max_c is not None:
            safe_required = float(demand.dp_max_c) + float(self.cfg.dp_margin_c) + float(self.cfg.delta_surface_water_c)
            if safe_required > float(self.cfg.cool_rad_supply_max_c) + 1e-6:
                dec.warnings.append("dew_guard_unachievable_switch_mode")
                dec.mode = PlantMode.DEHUM_ASSIST if bool(demand.vmc_req_dehumidif) else PlantMode.VENT_ONLY

        # --- Build device commands for the chosen mode
        self._fill_pdc_commands(dec, snapshot, t_out, demand)
        self._fill_supply_commands(dec, snapshot, demand, zones_decision)
        self._fill_vmc_commands(dec, snapshot, demand)

        # --- Coherence validation (PlantMode invariants)
        dec.warnings.extend(self._validate_decision(dec))

        return dec

    def _compute_demands(self, snapshot: PlantSnapshot) -> PlantDemandSignals:
        """Compute high-level demand metrics from zones + VMC signals."""
        heat_def_max = 0.0
        cool_sur_max = 0.0
        heat_def_by_zone: Dict[str, float] = {}
        cool_sur_by_zone: Dict[str, float] = {}
        heat_headroom_min_c: Optional[float] = None
        cool_headroom_min_c: Optional[float] = None

        # Weighted/quorum metrics (profile-aware gating)
        heat_den_w = 0.0
        heat_out_w = 0.0
        heat_sum_wdef = 0.0
        heat_den_n = 0
        heat_out_n = 0
        heat_sum_ndef = 0.0

        cool_den_w = 0.0
        cool_out_w = 0.0
        cool_sum_wsur = 0.0
        cool_den_n = 0
        cool_out_n = 0
        cool_sum_nsur = 0.0

        dp_max = None

        for zone_key, z in (snapshot.indoor_zones or {}).items():
            t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
            if t_meas is None:
                t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))

            band = getattr(z, "confort_band", None)
            t_min = as_float(getattr(band, "t_op_min", None))
            t_max = as_float(getattr(band, "t_op_max", None))

            # comfort headroom (MPC preheat gating)
            if t_meas is not None and t_min is not None:
                hh = float(t_meas) - float(t_min)
                heat_headroom_min_c = hh if heat_headroom_min_c is None else min(heat_headroom_min_c, hh)
            if t_meas is not None and t_max is not None:
                ch = float(t_max) - float(t_meas)
                cool_headroom_min_c = ch if cool_headroom_min_c is None else min(cool_headroom_min_c, ch)

            if t_meas is not None and t_min is not None:
                d = max(0.0, float(t_min) - float(t_meas))
                heat_def_by_zone[zone_key] = d
                heat_def_max = max(heat_def_max, d)

                w = _zone_weight(z)
                # Count-based always
                heat_den_n += 1
                heat_sum_ndef += d
                if d > 0.0:
                    heat_out_n += 1
                # Weighted only if weight > 0
                if w > 0.0:
                    heat_den_w += w
                    heat_sum_wdef += w * d
                    if d > 0.0:
                        heat_out_w += w

            if t_meas is not None and t_max is not None:
                d = max(0.0, float(t_meas) - float(t_max))
                cool_sur_by_zone[zone_key] = d
                cool_sur_max = max(cool_sur_max, d)

                w = _zone_weight(z)
                cool_den_n += 1
                cool_sum_nsur += d
                if d > 0.0:
                    cool_out_n += 1
                if w > 0.0:
                    cool_den_w += w
                    cool_sum_wsur += w * d
                    if d > 0.0:
                        cool_out_w += w

            dp = as_float(getattr(getattr(z, "dew_point", None), "value", None))
            if dp is not None:
                dp_max = dp if dp_max is None else max(dp_max, dp)

        vmc = snapshot.vmc
        vmc_raw_req_dehum = bool(getattr(vmc, "request_dehumidification", False)) if vmc else False

        # Compute profile-aware metrics with safe fallbacks
        if heat_den_w > 0.0:
            heat_def_wmean = heat_sum_wdef / heat_den_w
            heat_cov = heat_out_w / heat_den_w
            heat_metric_basis = MetricBasis.WEIGHTED
        elif heat_den_n > 0:
            heat_def_wmean = heat_sum_ndef / float(heat_den_n)
            heat_cov = float(heat_out_n) / float(heat_den_n)
            heat_metric_basis = MetricBasis.COUNT
        else:
            heat_def_wmean = 0.0
            heat_cov = 0.0
            heat_metric_basis = MetricBasis.NONE

        if cool_den_w > 0.0:
            cool_sur_wmean = cool_sum_wsur / cool_den_w
            cool_cov = cool_out_w / cool_den_w
            cool_metric_basis = MetricBasis.WEIGHTED
        elif cool_den_n > 0:
            cool_sur_wmean = cool_sum_nsur / float(cool_den_n)
            cool_cov = float(cool_out_n) / float(cool_den_n)
            cool_metric_basis = MetricBasis.COUNT
        else:
            cool_sur_wmean = 0.0
            cool_cov = 0.0
            cool_metric_basis = MetricBasis.NONE

        # --- VMC: ventilazione/deumidifica come primario; heating/cooling solo boost ---
        vmc_dp_sp_c = self._compute_vmc_dp_setpoint_c(snapshot)

        # Outdoor dew point (best effort): disable impossible dehumidification in ventilation-only mode
        outdoor_dp_c = as_float(getattr(getattr(snapshot, "global_outdoor_dew_point", None), "value", None))
        if outdoor_dp_c is None:
            t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
            rh_out = as_float(getattr(getattr(snapshot, "global_outdoor_humidity", None), "value", None))
            if t_out is not None and rh_out is not None:
                try:
                    outdoor_dp_c = float(dew_point_celsius(t_out, rh_out))
                except Exception:
                    outdoor_dp_c = None

        vmc_need_dehum = self._vmc_need_dehumidification(dp_max, vmc_dp_sp_c)
        vmc_boost_heat = self._vmc_allow_heat_boost(
            snapshot,
            float(heat_def_max),
            float(heat_def_wmean),
            float(heat_cov),
        )
        vmc_boost_cool = self._vmc_allow_cool_boost(
            snapshot,
            float(cool_sur_max),
            float(cool_sur_wmean),
            float(cool_cov),
        )

        vmc_req_heat = bool(vmc_boost_heat)
        vmc_req_cool = bool(vmc_boost_cool)
        # Dehumidification feasibility (ventilation-only vs coil water)
        vmc_dehum_feasible: Optional[bool] = None
        if bool(self.cfg.vmc_water_on_for_dehumid):
            vmc_dehum_feasible = True
        elif outdoor_dp_c is not None:
            headroom = float(getattr(self.cfg, "vmc_dehum_outdoor_dp_headroom_c", 0.0))
            vmc_dehum_feasible = outdoor_dp_c <= (float(vmc_dp_sp_c) - headroom)
        vmc_req_dehum = bool(vmc_need_dehum or vmc_raw_req_dehum) and (vmc_dehum_feasible is not False)
        vmc_req_water = (
            vmc_req_heat
            or vmc_req_cool
            or (vmc_req_dehum and bool(self.cfg.vmc_water_on_for_dehumid))
        )

        return PlantDemandSignals(
            heat_def_max_c=heat_def_max,
            cool_sur_max_c=cool_sur_max,
            heat_def_by_zone_c=heat_def_by_zone,
            cool_sur_by_zone_c=cool_sur_by_zone,
            heat_headroom_min_c=heat_headroom_min_c,
            cool_headroom_min_c=cool_headroom_min_c,
            outdoor_dp_c=outdoor_dp_c,
            vmc_dehum_feasible=vmc_dehum_feasible,
            # Profile-aware multi-zone demand metrics
            heat_def_wmean_c=float(heat_def_wmean),
            cool_sur_wmean_c=float(cool_sur_wmean),
            heat_cov=float(heat_cov),
            cool_cov=float(cool_cov),
            heat_metric_basis=heat_metric_basis,
            cool_metric_basis=cool_metric_basis,
            dp_max_c=dp_max,
            vmc_req_heating=vmc_req_heat,
            vmc_req_cooling=vmc_req_cool,
            vmc_req_dehumidif=vmc_req_dehum,
            vmc_req_water=vmc_req_water,
        )

    def _infer_mode(self, snapshot: PlantSnapshot, demand: PlantDemandSignals, zones_decision: Optional[ZonesDecision] = None) -> PlantMode:
        """Decide the high-level plant regime.

        Inputs
        - `demand`: aggregated zone deltas (heat_def/cool_sur) + VMC requests.
        - `snapshot.climate_hvac_mode`: user override (OFF/AUTO).
        - `snapshot.climate_preset_mode`: user profile (Comfort/Eco/Boost/Sleep/Away/Vacation).

        Goals
        - User override is absolute: HVACMode.OFF => PlantMode.OFF.
        - Preset modulates sensitivity (BOOST reacts faster; ECO/SLEEP slower; AWAY/VACATION much slower).
        - Conservative season gating: WINTER avoids active cooling; SUMMER avoids active heating.
        - Resolve conflicting heat+cool requests deterministically.
        """
        cfg = self.cfg

        heat_def = float(demand.heat_def_max_c)
        cool_sur = float(demand.cool_sur_max_c)
        heat_cov = float(demand.heat_cov)
        cool_cov = float(demand.cool_cov)
        heat_def_wmean = float(demand.heat_def_wmean_c)
        cool_sur_wmean = float(demand.cool_sur_wmean_c)

        def _profile_quorum(p: HVACOperatingProfile) -> float:
            if p == HVACOperatingProfile.ECO:
                return float(cfg.quorum_cov_eco)
            if p == HVACOperatingProfile.SLEEP:
                return float(cfg.quorum_cov_sleep)
            if p == HVACOperatingProfile.AWAY:
                return float(cfg.quorum_cov_away)
            if p == HVACOperatingProfile.VACATION:
                return float(cfg.quorum_cov_vacation)
            # COMFORT/BOOST and any unknown profile: no quorum
            return 0.0

        # --------------------
        # 0) User intent (HA Climate)
        # --------------------
        hvac_mode_raw = getattr(snapshot, "climate_hvac_mode", None)
        hvac_mode_val = getattr(hvac_mode_raw, "value", hvac_mode_raw)
        hvac_mode_s = str(hvac_mode_val).strip().lower() if hvac_mode_val is not None else "auto"

        preset_raw = getattr(snapshot, "climate_preset_mode", None)
        profile = HVACOperatingProfile.from_value(preset_raw, default=HVACOperatingProfile.COMFORT) or HVACOperatingProfile.COMFORT

        # Expose to signals for observability (ends up in PlantDecision.signals)
        demand.user_hvac_mode = hvac_mode_s
        demand.user_profile = profile.value

        zones_any_heat = bool(zones_decision.any_heat_demand) if zones_decision else False
        zones_full_on_pct = zones_decision.meta.get('mpc_full_on_pct') if zones_decision else None
        demand.zones_any_heat_demand = zones_any_heat
        demand.zones_full_on_pct = zones_full_on_pct

        # Absolute override
        if hvac_mode_s == HVACMode.OFF.value:
            demand.user_forced_off = True
            return PlantMode.OFF
        demand.user_forced_off = False

        # --------------------
        # 1) Preset-aware sensitivity
        # --------------------
        ctrl_aggr = float(_PROFILE_CTRL_AGGR.get(profile, 1.0))
        ctrl_eff = max(0.2, ctrl_aggr)  # avoid division blow-ups

        heat_thr = float(cfg.heat_on_deficit_c) / ctrl_eff
        cool_thr = float(cfg.cool_on_surplus_c) / ctrl_eff

        demand.ctrl_aggr = ctrl_aggr
        demand.heat_on_thr_c = heat_thr
        demand.cool_on_thr_c = cool_thr

        # --------------------
        # 2) Demand flags
        # --------------------
        vmc_req_heat = bool(demand.vmc_req_heating)
        vmc_req_cool = bool(demand.vmc_req_cooling)
        vmc_req_dehum = bool(demand.vmc_req_dehumidif)

        quorum = _profile_quorum(profile)
        demand.quorum_cov_req = quorum

        # Sensible gating (profile-aware)
        if profile in (HVACOperatingProfile.COMFORT, HVACOperatingProfile.BOOST):
            heat_sensible = (heat_def >= heat_thr)
            cool_sensible = (cool_sur >= cool_thr)
        else:
            heat_override = (heat_def >= heat_thr * float(cfg.demand_override_factor))
            heat_quorum_ok = (heat_cov >= quorum)
            heat_mean_ok = (heat_def_wmean >= heat_thr * float(cfg.demand_mean_factor))
            heat_sensible = heat_override or ((heat_def >= heat_thr) and (heat_quorum_ok or heat_mean_ok))

            cool_override = (cool_sur >= cool_thr * float(cfg.demand_override_factor))
            cool_quorum_ok = (cool_cov >= quorum)
            cool_mean_ok = (cool_sur_wmean >= cool_thr * float(cfg.demand_mean_factor))
            cool_sensible = cool_override or ((cool_sur >= cool_thr) and (cool_quorum_ok or cool_mean_ok))

            demand.heat_override = heat_override
            demand.heat_quorum_ok = heat_quorum_ok
            demand.heat_mean_ok = heat_mean_ok
            demand.cool_override = cool_override
            demand.cool_quorum_ok = cool_quorum_ok
            demand.cool_mean_ok = cool_mean_ok

        # MPC may schedule heating far from the band; accept it only as preheat close to T_min.
        zones_preheat_ok = False
        if zones_any_heat and getattr(demand, "heat_headroom_min_c", None) is not None:
            zones_preheat_ok = demand.heat_headroom_min_c <= float(getattr(cfg, "zones_mpc_preheat_headroom_c", 0.4))
        if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            zones_preheat_ok = False
        demand.zones_mpc_heat_preheat_ok = zones_preheat_ok

        any_heat = bool(heat_sensible) or vmc_req_heat or (zones_any_heat and zones_preheat_ok)
        any_cool = bool(cool_sensible) or vmc_req_cool

        # Dehumidification may be needed even when there is no sensible surplus.
        any_cool_or_dehum = any_cool or vmc_req_dehum

        demand.any_heat = any_heat
        demand.any_cool = any_cool
        demand.any_dehum = vmc_req_dehum
        demand.heat_sensible = bool(heat_sensible)
        demand.cool_sensible = bool(cool_sensible)

        # --------------------
        # 3) Season gating / conflict resolution
        # --------------------
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)
        demand.runtime_season = season_val or "unknown"

        # Map Seasons -> operative buckets
        if season_val == "winter":
            operative = "winter"
        elif season_val == "summer":
            operative = "summer"
        else:
            operative = "shoulder"
        demand.operative_season = operative

        # --------------------
        # 3.a) VACATION override (may switch plant fully OFF, including VMC),
        #      except when dew-point risk suggests keeping ventilation/dehumidification active.
        # --------------------
        if bool(snapshot.presence_vacation) and bool(cfg.vacation_allows_vmc_off):
            dp_cur = as_float(demand.dp_max_c)
            dp_sp = float(self._compute_vmc_dp_setpoint_c(snapshot))
            ddp_vac = float(cfg.vacation_ddp_on_c)
            dew_risk = (dp_cur is not None) and (float(dp_cur) > (dp_sp + ddp_vac))

            if dew_risk:
                # Winter: avoid active cooling; keep ventilation only.
                if operative == "winter":
                    return PlantMode.VENT_ONLY
                # Summer/shoulder: allow latent assist if configured and dehumidification is requested.
                if bool(cfg.vacation_allow_dehum_assist) and bool(demand.vmc_req_dehumidif):
                    return PlantMode.DEHUM_ASSIST
                return PlantMode.VENT_ONLY

            # No dew risk: fully OFF (including VMC)
            return PlantMode.OFF

        if operative == "winter":
            if any_heat:
                return PlantMode.HEATING
            if any_cool_or_dehum:
                # Avoid active cooling in winter: prefer ventilation only.
                return PlantMode.VENT_ONLY
            # Idle: in AWAY/VACATION save energy by not forcing ventilation at plant level
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return PlantMode.OFF
            return PlantMode.VENT_ONLY

        if operative == "summer":
            if any_cool_or_dehum:
                return PlantMode.DEHUM_ASSIST if vmc_req_dehum else PlantMode.COOLING
            if any_heat:
                # Avoid active heating in summer: ventilation only.
                return PlantMode.VENT_ONLY
            if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
                return PlantMode.OFF
            return PlantMode.VENT_ONLY

        # SHOULDER (spring/autumn or unknown): allow both, resolve conflicts by dominant error
        if any_heat and not any_cool_or_dehum:
            return PlantMode.HEATING

        if any_cool_or_dehum and not any_heat:
            return PlantMode.DEHUM_ASSIST if vmc_req_dehum else PlantMode.COOLING

        if any_heat and any_cool_or_dehum:
            # If latent is requested and we are also warm, prioritize dehumidification.
            if vmc_req_dehum and cool_sur >= 0.1:
                return PlantMode.DEHUM_ASSIST
            return PlantMode.HEATING if heat_def >= cool_sur else (PlantMode.DEHUM_ASSIST if vmc_req_dehum else PlantMode.COOLING)

        # Idle shoulder
        return PlantMode.OFF

    def _fill_pdc_commands(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        t_out: Optional[float],
        demand: PlantDemandSignals,
    ) -> None:
        cfg = self.cfg
        pdc = dec.pdc

        if dec.mode in (PlantMode.HEATING,):
            pdc.mode = "heating"
            pdc.power = True

            # --- 1) base curve (outdoor-driven)
            if t_out is not None:
                curve = cfg.heat_curve_base_c + cfg.heat_curve_k_c_per_c * (
                    cfg.heat_curve_ref_outdoor_c - float(t_out)
                )
            else:
                # fallback conservativo
                curve = cfg.heat_curve_base_c

            # --- 2) profile offset (by preset/profile)
            prof_key = snapshot.climate_preset_mode if snapshot.climate_preset_mode else HVACOperatingProfile.COMFORT
            prof_offset = float(cfg.heat_profile_offset_c.get(prof_key, 0.0))

            # --- 3) indoor feedback (use already computed signals)
            heat_def_max = float(demand.heat_def_max_c)
            heat_def_wmean = float(demand.heat_def_wmean_c)

            fb = float(cfg.heat_feedback_gain_c_per_c) * heat_def_wmean
            fb = _clamp(fb, -float(cfg.heat_feedback_max_down_c), float(cfg.heat_feedback_max_up_c))

            kick = 0.0
            if heat_def_max >= float(cfg.heat_kick_on_max_def_c):
                kick = float(cfg.heat_kick_extra_c)

            target = curve + prof_offset + fb + kick
            target = _clamp(target, cfg.heat_wot_min_c, cfg.heat_wot_max_c)

            # --- 4) deadband + rate-limit (anti-hunting, stateful)
            now = dec.ts
            prev = self._last_heat_wot_c
            prev_ts = self._last_heat_wot_ts

            if prev is not None and prev_ts is not None:
                dt_min = max(0.001, (now - prev_ts).total_seconds() / 60.0)
                max_step = float(cfg.heat_wot_rate_limit_c_per_min) * dt_min

                if abs(float(target) - float(prev)) < float(cfg.heat_wot_deadband_c):
                    target = float(prev)
                else:
                    target = _clamp(float(target), float(prev) - max_step, float(prev) + max_step)

            self._last_heat_wot_c = float(target)
            self._last_heat_wot_ts = now

            pdc.heat_wot_c = math.ceil(target)
            pdc.heat_dt_c = math.ceil(cfg.heat_dt_c)
            pdc.debug.update({
                "t_out_c": t_out,
                "curve": "linear+profile+feedback",
                "curve_base": float(curve),
                "profile_key": prof_key,
                "profile_offset_c": float(prof_offset),
                "heat_def_wmean_c": float(heat_def_wmean),
                "heat_def_max_c": float(heat_def_max),
                "feedback_c": float(fb),
                "kick_c": float(kick),
                "wot_target_pre_rate_c": float(curve + prof_offset + fb + kick),
            })

        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            pdc.mode = "cooling"
            pdc.power = True
            # In questa fase usiamo un setpoint flat; in futuro: profilo + vincoli batteria VMC.
            pdc.cool_wot_c = math.ceil(_clamp(cfg.cool_wot_default_c, cfg.cool_wot_min_c, cfg.cool_wot_max_c))
            pdc.cool_dt_c = math.ceil(cfg.cool_dt_c)

        elif dec.mode == PlantMode.VENT_ONLY:
            # Nota: in molte PDC conviene spegnere, salvo logiche anti-gelo/anti-stallo gestite nativamente.
            pdc.power = False

        else:
            pdc.power = False

        # Se abbiamo segnali di affidabilità bassi sulla PDC (compressore off ecc.), li mettiamo in debug.
        if snapshot.pdc:
            pdc.debug.update({
                "pdc_device_power": getattr(snapshot.pdc, "power_on", None),
                "pdc_compressor": getattr(snapshot.pdc, "sensor_compressor_state", None),
            })

    def _fill_supply_commands(
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
            any_zone_on = (float(demand.heat_def_max_c) >= cfg.heat_on_deficit_c) or (
                float(demand.cool_sur_max_c) >= cfg.cool_on_surplus_c
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
                t = dec.pdc.heat_wot_c - cfg.heat_rad_supply_offset_c
                s.rad_supply_target_c = float(_clamp(t, cfg.heat_rad_supply_min_c, cfg.heat_rad_supply_max_c))
        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            if dp_max is not None:
                safe_required = float(dp_max) + float(cfg.dp_margin_c) + float(cfg.delta_surface_water_c)

                # HARD guard: if required safe temp is above max allowed, do not run radiant cooling.
                if safe_required > float(cfg.cool_rad_supply_max_c) + 1e-6:
                    dec.warnings.append("dew_guard_unachievable_radiant_disabled")
                    s.adj_pump_on = False
                    s.rad_supply_target_c = None
                    s.debug.update({
                        "dew_guard_required_c": safe_required,
                        "dew_guard_max_c": float(cfg.cool_rad_supply_max_c),
                        "dew_guard_action": "disable_radiant",
                    })
                else:
                    s.rad_supply_target_c = float(_clamp(safe_required, cfg.cool_rad_supply_min_c, cfg.cool_rad_supply_max_c))
            else:
                dec.warnings.append("missing_dp_max_for_dew_guard")

        s.debug["any_zone_on"] = any_zone_on

        # Snapshot values for diagnostics
        if snapshot.supply_unit:
            su = snapshot.supply_unit
            s.debug.update({
                "adj_supply_flow_c": as_float(getattr(su, "sensor_adjustable_temp_system_supply", None)),
                "adj_return_flow_c": as_float(getattr(su, "sensor_adjustable_temp_system_return", None)),
                "direct_supply_flow_c": as_float(getattr(su, "sensor_direct_temp_system_supply", None)),
                "direct_return_flow_c": as_float(getattr(su, "sensor_direct_temp_system_return", None)),
                "boiler_supply_flow_c": as_float(getattr(su, "sensor_boiler_temp_system_supply", None)),
                "boiler_return_flow_c": as_float(getattr(su, "sensor_boiler_temp_system_return", None)),
            })

    def _fill_vmc_commands(self, dec: PlantDecision, snapshot: PlantSnapshot, demand: PlantDemandSignals) -> None:
        """Populate VMC commands.

        Rational:
        - La VMC serve per ventilare e deumidificare (via DP setpoint).
        - Il contributo termico (heating/cooling) è solo boost quando fuori comfort in modo significativo.
        - Il setpoint T neutro evita di trascinare la PDC per inseguire 24°C in inverno.
        """
        cfg = self.cfg
        v = dec.vmc

        # OFF means plant idle, including VMC (unless a different policy is implemented).
        if dec.mode == PlantMode.OFF:
            v.power = False
            v.mode = "off"
            v.air_speed = 0
            v.setpoint_t_c = None
            v.setpoint_rh_pct = None
            v.setpoint_dp_c = None
            v.setpoint_ddp_c = None
            v.debug.update({
                "reason": "plant_mode_off",
            })
            return

        if demand.operative_season == "winter":
            mode = cfg.vmc_mode_winter
        elif demand.operative_season == "summer":
            mode = cfg.vmc_mode_summer
        else:
            mode = getattr(snapshot.vmc, "processing_mode", None) or cfg.vmc_mode_winter

        t_ref_c = self._get_indoor_reference_temp_c(snapshot)
        rh_target_pct = float(cfg.vmc_setpoint_rh_pct)
        profile = HVACOperatingProfile.from_value(demand.user_profile)
        if profile == HVACOperatingProfile.SLEEP and cfg.vmc_setpoint_rh_sleep_pct is not None:
            rh_target_pct = float(cfg.vmc_setpoint_rh_sleep_pct)

        dp_sp_c = self._compute_vmc_dp_setpoint_c_from(t_ref_c, rh_target_pct)
        ddp_sp_c = float(cfg.vmc_setpoint_ddp_c)

        dp_current = demand.dp_max_c
        boost_active = bool(demand.vmc_req_heating or demand.vmc_req_cooling or demand.vmc_req_dehumidif)

        air_speed = self._compute_vmc_air_speed(
            snapshot,
            dp_current,
            dp_sp_c,
            boost_active,
        )

        if demand.vmc_req_heating:
            t_sp = float(cfg.vmc_boost_setpoint_heat_c)
        elif demand.vmc_req_cooling:
            t_sp = float(cfg.vmc_boost_setpoint_cool_c)
        else:
            dead = float(cfg.vmc_temp_neutral_deadband_c)
            if mode == cfg.vmc_mode_winter:
                t_sp = t_ref_c - dead
            elif mode == cfg.vmc_mode_summer:
                t_sp = t_ref_c + dead
            else:
                t_sp = t_ref_c

        t_sp = _clamp(float(t_sp), float(cfg.vmc_temp_min_c), float(cfg.vmc_temp_max_c))

        v.power = True
        v.mode = mode
        v.setpoint_t_c = round(t_sp, 1)
        v.setpoint_rh_pct = round(rh_target_pct, 0)
        v.setpoint_dp_c = round(dp_sp_c, 1)
        v.setpoint_ddp_c = round(ddp_sp_c, 1)
        v.air_speed = int(air_speed)

        # Diagnostics: report current vmc state
        if snapshot.vmc:
            vmc = snapshot.vmc
            v.debug.update({
                "recirculation": cfg.vmc_recirculation,
                "device_power": getattr(vmc, "power_on", None),
                "req_water": getattr(vmc, "request_water", None),
                "req_heating": getattr(vmc, "request_heating", None),
                "req_cooling": getattr(vmc, "request_cooling", None),
                "req_dehumidif": getattr(vmc, "request_dehumidification", None),
                "ambient_t_c": as_float(getattr(vmc, "sensor_t_ambient", None)),
                "ambient_rh_pct": as_float(getattr(vmc, "sensor_h_ambient", None)),
                "water_t_c": as_float(getattr(vmc, "sensor_t_water", None)),
                "outdoor_t_c": as_float(getattr(vmc, "sensor_t_outdoor", None)),
                "alarm_dew_point": getattr(vmc, "alarm_dew_point", None),
                "alarm_general": getattr(vmc, "alarm_alarm", None),
            })

    # ---- VMC helpers -----------------------------------------------------
    def _get_indoor_reference_temp_c(self, snapshot: PlantSnapshot) -> float:
        z = snapshot.global_indoor_zone
        if z is not None:
            for av in (getattr(z, "t_op", None), getattr(z, "temperature", None)):
                v = as_float(getattr(av, "value", None))
                if v is not None:
                    return float(v)
        return float(self.cfg.vmc_setpoint_t_c)

    def _compute_vmc_dp_setpoint_c(self, snapshot: PlantSnapshot) -> float:
        t_ref = self._get_indoor_reference_temp_c(snapshot)
        return self._compute_vmc_dp_setpoint_c_from(t_ref, float(self.cfg.vmc_setpoint_rh_pct))

    def _compute_vmc_dp_setpoint_c_from(self, t_c: float, rh_pct: float) -> float:
        if bool(getattr(self.cfg, "vmc_dp_setpoint_from_psychrometrics", True)):
            try:
                dp = float(dew_point_celsius(t_c, rh_pct))
            except Exception:
                dp = float(self.cfg.vmc_setpoint_dp_c)
        else:
            dp = float(self.cfg.vmc_setpoint_dp_c)
        return _clamp(dp, float(self.cfg.vmc_dp_sp_min_c), float(self.cfg.vmc_dp_sp_max_c))

    def _vmc_need_dehumidification(self, dp_current_c: float | None, dp_setpoint_c: float) -> bool:
        if dp_current_c is None:
            return False
        ddp = float(self.cfg.vmc_setpoint_ddp_c)
        return float(dp_current_c) > (float(dp_setpoint_c) + ddp)

    def _vmc_allow_heat_boost(
        self,
        snapshot: PlantSnapshot,
        heat_def_max_c: float,
        heat_def_wmean_c: float,
        heat_cov: float,
    ) -> bool:
        if not bool(self.cfg.vmc_boost_enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if heat_def_max_c >= float(self.cfg.vmc_boost_heat_def_max_thr_c):
            return True
        if heat_def_wmean_c >= float(self.cfg.vmc_boost_heat_def_wmean_thr_c) and heat_cov >= 0.6:
            return True
        return False

    def _vmc_allow_cool_boost(
        self,
        snapshot: PlantSnapshot,
        cool_sur_max_c: float,
        cool_sur_wmean_c: float,
        cool_cov: float,
    ) -> bool:
        if not bool(self.cfg.vmc_boost_enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if cool_sur_max_c >= float(self.cfg.vmc_boost_cool_sur_max_thr_c):
            return True
        if cool_sur_wmean_c >= float(self.cfg.vmc_boost_cool_sur_wmean_thr_c) and cool_cov >= 0.6:
            return True
        return False

    def _compute_vmc_air_speed(
        self,
        snapshot: PlantSnapshot,
        dp_current_c: Optional[float],
        dp_setpoint_c: float,
        boost: bool,
    ) -> int:
        if not bool(snapshot.windows_close_state):
            sp = int(self.cfg.vmc_speed_windows_open)
        elif bool(snapshot.presence_vacation) or bool(snapshot.presence_nobodysin):
            sp = int(self.cfg.vmc_speed_vacation)
        else:
            sp = int(self.cfg.vmc_speed_base)

        if dp_current_c is not None:
            delta = float(dp_current_c) - float(dp_setpoint_c)
            if delta > float(self.cfg.vmc_speed_dp_boost_step1_c):
                sp += 1
            if delta > float(self.cfg.vmc_speed_dp_boost_step2_c):
                sp += 1
            if delta > float(self.cfg.vmc_speed_dp_boost_step3_c):
                sp += 1

        if boost:
            sp = max(sp, int(self.cfg.vmc_boost_min_air_speed))

        return int(_clamp(float(sp), float(self.cfg.vmc_speed_min), float(self.cfg.vmc_speed_max)))

    # ---- Validation ------------------------------------------------------
    def _validate_decision(self, dec: PlantDecision) -> list[str]:
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

        if dec.mode == PlantMode.VENT_ONLY:
            if bool(getattr(dec.pdc, "power", False)):
                w.append("incoherent_vent_only_pdc_power_true")
            # Pumps should generally be off in vent-only (unless architecture requires otherwise)
            if bool(getattr(dec.supply, "adj_pump_on", False)) or bool(getattr(dec.supply, "direct_pump_on", False)):
                w.append("incoherent_vent_only_pumps_on")

        if dec.mode == PlantMode.HEATING:
            if getattr(dec.pdc, "mode", None) not in (None, "heating"):
                w.append("incoherent_heating_pdc_mode_not_heating")

        if dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            if getattr(dec.pdc, "mode", None) not in (None, "cooling"):
                w.append("incoherent_cooling_pdc_mode_not_cooling")

        return w
