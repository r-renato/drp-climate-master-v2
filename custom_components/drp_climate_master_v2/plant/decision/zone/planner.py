from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from homeassistant.util import dt as dt_util

from ....helpers.utils import as_float
from ....helpers.sensor_aggregator import AggregatedValue
from ....plant.monitor.plant import PlantSnapshot, ZoneSnapshot
from ....domain.enums import HVACOperatingProfile

from .model import ZoneCommand, ZonesDecision
from .config import ControlConfig, MpcConfig
from ..config import DemandGatingConfig
from ..comfort_band.mpc.rc_model import RcZoneModel
from ..comfort_band.model import ComfortBandResult

def _binary_sequences(n: int) -> Iterable[list[int]]:
    """Generate all binary sequences of length n (as lists of 0/1)."""
    for bits in itertools.product((0, 1), repeat=n):
        yield list(bits)

def _violates_min_hold(seq: list[int], *, u_prev: int, hold_steps: int) -> bool:
    if hold_steps <= 1:
        return False
    last = int(u_prev)
    n = len(seq)
    for i in range(n):
        u = int(seq[i])
        if u != last:
            end = min(n, i + hold_steps)
            for j in range(i, end):
                if int(seq[j]) != u:
                    return True
            last = u
        else:
            last = u
    return False

def _band_with_slack(*, t_min: float, t_max: float, slack_c: float) -> tuple[float, float]:
    s = max(0.0, float(slack_c or 0.0))
    return float(t_min) - s, float(t_max) + s


@dataclass(slots=True)
class ZoneDecisionPlanner:
    """Per-zone MPC-lite over binary valve commands.

    Scope (v1):
      - Heating only
      - Per-zone optimization (no coupling constraints)
      - Uses comfort band (t_op_min/max) if available
    """

    cfg: ControlConfig = field(default_factory=ControlConfig)
    gating_cfg: Optional[DemandGatingConfig] = field(default=None)
    """Configurazione gating del plant planner.
    Se fornita, ``ctrl_aggr`` viene letto da ``gating_cfg.ctrl_aggr(profile)``
    (unica fonte di verità). Se None, si usa il fallback COMFORT nominale (1.0).
    """

    def _resolve_mpc_outdoor_temp(self, snapshot: PlantSnapshot) -> tuple[float | None, str]:
        """Temperatura esterna per il modello RC (sorgente più aggiornata disponibile).

        Ordine di priorità (dalla più aggiornata alla più datata):

        1. ``global_outdoor_temperature`` — sensore istantaneo aggregato (aggiornato
           ogni ciclo HA). È la sorgente corretta per la predizione RC a breve termine
           (orizzonte tipico: 10 min × 12 step = 2h). Viene scartata solo se stale
           o insufficiente secondo il flag del sensor_aggregator.

        2. ``season.weather_signals.t_mean`` — media giornaliera di oggi dal forecast
           meteo (es. PirateWeather). Aggiornamento orario/giornaliero. Accettabile
           come fallback perché riflette la temperatura media del giorno corrente, non
           di mesi fa. Non usa t_smooth che è una media mobile 2 mesi.

        3. ``None`` — il piano MPC viene abortito (mancanza di contesto termico).

        Nota: ``t_smooth`` (media mobile ~60 giorni) è deliberatamente ESCLUSO.
        È lo strumento giusto per la classificazione stagionale ML, ma usando
        22.7°C (media maggio) come temperatura RC a maggio con T_corrente=13°C
        la deriva predetta è quasi nulla → il modello non vede necessità di
        riscaldamento → piano MPC incoerente con la realtà istantanea.
        """
        # 1) Sensore istantaneo: sorgente preferita per la predizione RC.
        av = getattr(snapshot, "global_outdoor_temperature", None)
        if av is not None:
            t0 = as_float(getattr(av, "value", None))
            if t0 is not None and not getattr(av, "is_stale", False) and not getattr(av, "is_insufficient", False):
                return float(t0), "global_outdoor_temperature"

        # 2) Media giornaliera forecast (oggi): fallback accettabile a breve termine.
        # Usa t_mean (media giornaliera corrente), mai t_smooth (media 2 mesi).
        try:
            ws = getattr(getattr(snapshot, "season", None), "weather_signals", None)
            if ws is not None:
                t = as_float(getattr(ws, "t_mean", None))
                if t is not None:
                    return float(t), "season.weather_signals.t_mean"
        except Exception:
            pass  # best-effort

        # 3) Sensore stale come ultima risorsa prima di rinunciare.
        # Meglio una lettura vecchia di qualche minuto che nessuna stima.
        if av is not None:
            t0 = as_float(getattr(av, "value", None))
            if t0 is not None:
                return float(t0), "global_outdoor_temperature_stale"

        return None, "missing"

    def _profile_scaled_mpc(self, snapshot: PlantSnapshot, mpc: MpcConfig) -> tuple[MpcConfig, float]:
        """Scale MPC weights/slack based on the user preset/profile.

        This mirrors plant-level gating aggressiveness:
          - BOOST -> tighter comfort, less energy penalty
          - ECO/SLEEP/AWAY/VACATION -> larger slack, more energy penalty
        """
        preset = getattr(snapshot, "climate_preset_mode", None)
        profile: HVACOperatingProfile | None = None

        if isinstance(preset, HVACOperatingProfile):
            profile = preset
        elif preset is not None:
            s = str(preset).strip()
            for p in HVACOperatingProfile:
                if str(p.value).lower() == s.lower() or str(p.name).lower() == s.lower():
                    profile = p
                    break

        if self.gating_cfg is not None:
            ctrl_aggr = self.gating_cfg.ctrl_aggr(profile or HVACOperatingProfile.COMFORT)
        else:
            # Fallback: gating_cfg non fornito (test isolati, costruzione manuale).
            # COMFORT nominale = nessuna modulazione.
            ctrl_aggr = 1.0
        ctrl_eff = max(0.2, ctrl_aggr)

        mpc_eff = replace(
            mpc,
            w_comfort=float(mpc.w_comfort) * ctrl_eff,
            w_energy=float(mpc.w_energy) / ctrl_eff,
            # Switching penalties are mostly about hydraulics; keep them stable for now.
            w_switch=float(mpc.w_switch),
            comfort_slack_c=float(getattr(mpc, "comfort_slack_c", 0.0) or 0.0) / ctrl_eff,
        )
        return mpc_eff, ctrl_aggr

    def plan(self, *, snapshot: PlantSnapshot, reason: str, comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None) -> ZonesDecision:
        mpc_base = self.cfg.mpc
        mpc_eff, ctrl_aggr = self._profile_scaled_mpc(snapshot, mpc_base)
        plan = self._plan_once(snapshot=snapshot, reason=reason, mpc=mpc_eff, comfort_bands_by_zone=comfort_bands_by_zone, meta_extra={"mpc_retry": False, "mpc_ctrl_aggr": float(ctrl_aggr)})

        n = len(plan.zones)
        if n == 0:
            return plan

        full_on = sum(1 for cmd in plan.zones.values() if (cmd.seq and int(sum(cmd.seq)) == len(cmd.seq)))
        full_off = sum(1 for cmd in plan.zones.values() if (cmd.seq and int(sum(cmd.seq)) == 0))
        full_on_pct = 100.0 * full_on / max(1, n)
        full_off_pct = 100.0 * full_off / max(1, n)

        # --- Extra MPC KPIs (used by plant-level planners)
        # mpc_duty_avg_pct : mean duty across *all* zone-time slots in horizon (0..100)
        # mpc_on_now_pct   : fraction of zones ON at step 0 (now) (0..100)
        # mpc_first_on_step: earliest horizon step where any zone is scheduled ON (0..h-1)
        h = max(1, int(plan.horizon_steps))
        on_now = sum(1 for cmd in plan.zones.values() if (cmd.seq and len(cmd.seq) > 0 and int(cmd.seq[0]) == 1))
        total_on = sum(sum(int(x) for x in (cmd.seq or [])) for cmd in plan.zones.values())

        first_on_step: int | None = None
        for i in range(h):
            if any((cmd.seq and len(cmd.seq) > i and int(cmd.seq[i]) == 1) for cmd in plan.zones.values()):
                first_on_step = i
                break

        plan.meta["mpc_duty_avg_pct"] = round(100.0 * float(total_on) / float(max(1, n) * h), 1)
        plan.meta["mpc_on_now_pct"] = round(100.0 * float(on_now) / float(max(1, n)), 1)
        plan.meta["mpc_first_on_step"] = first_on_step

        plan.meta["mpc_full_on_pct"] = round(full_on_pct, 1)
        plan.meta["mpc_full_off_pct"] = round(full_off_pct, 1)

        all_in_band = True
        for zn in plan.zones.keys():
            z = (snapshot.indoor_zones or {}).get(zn)
            band = (comfort_bands_by_zone or {}).get(zn) if comfort_bands_by_zone is not None else (getattr(z, "comfort_band", None) if z else None)
            ok = getattr(band, "ok", None) if band is not None else None
            if ok is not True:
                all_in_band = False
                break

        if (
            bool(getattr(mpc_base, "degenerate_retry_enabled", False))
            and full_on_pct >= float(getattr(mpc_base, "degenerate_full_on_pct_thr", 1000.0))
            and (not bool(getattr(mpc_base, "degenerate_retry_only_if_all_in_band", True)) or all_in_band)
        ):
            mpc_retry = replace(
                mpc_base,
                comfort_slack_c=float(getattr(mpc_base, "degenerate_retry_comfort_slack_c", mpc_base.comfort_slack_c)),
                w_comfort=float(mpc_base.w_comfort) * float(getattr(mpc_base, "degenerate_retry_w_comfort_mult", 1.0)),
                w_energy=float(mpc_base.w_energy) * float(getattr(mpc_base, "degenerate_retry_w_energy_mult", 1.0)),
                w_switch=float(mpc_base.w_switch) * float(getattr(mpc_base, "degenerate_retry_w_switch_mult", 1.0)),
            )
            plan2 = self._plan_once(
                snapshot=snapshot,
                reason=reason,
                mpc=mpc_retry,
                comfort_bands_by_zone=comfort_bands_by_zone,
                meta_extra={
                    "mpc_retry": True,
                    "mpc_retry_reason": "degenerate_full_on",
                    "mpc_full_on_pct_prev": round(full_on_pct, 1),
                },
            )
            plan2.warnings.append("mpc_retry_degenerate_full_on")
            return plan2

        return plan

    def _plan_once(self, *, snapshot: PlantSnapshot, reason: str, mpc: MpcConfig, comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None, meta_extra: dict[str, Any] | None = None) -> ZonesDecision:
        ts = snapshot.timestamp if isinstance(snapshot.timestamp, datetime) else dt_util.utcnow()
        # Enforce timezone-aware timestamp (best effort)
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)

        meta: dict[str, Any] = {
            "season": (
                getattr(getattr(snapshot.season, "season", None), "value", None)
                if snapshot.season else None
            ),
            "windows_closed": snapshot.windows_close_state,
            "vacation": snapshot.presence_vacation,
            "mpc": {
                "dt_minutes": int(mpc.dt_minutes),
                "horizon_steps": int(mpc.horizon_steps),
                "w_comfort": float(mpc.w_comfort),
                "w_energy": float(mpc.w_energy),
                "w_switch": float(mpc.w_switch),
                "comfort_slack_c": float(getattr(mpc, "comfort_slack_c", 0.0) or 0.0),
                "min_switch_minutes": int(getattr(mpc, "min_switch_minutes", 0) or 0),
            },
        }
        if meta_extra:
            meta.update(meta_extra)

        plan = ZonesDecision(
            ts=ts,
            dt_minutes=mpc.dt_minutes,
            horizon_steps=mpc.horizon_steps,
            reason=reason,
            meta=meta,
        )

        # Outdoor trajectory: first iteration uses a flat profile.
        t_out0, t_out_src = self._resolve_mpc_outdoor_temp(snapshot)
        if t_out0 is None or (isinstance(t_out0, float) and (math.isnan(t_out0) or math.isinf(t_out0))):
            # fallback: do not attempt predictive control without outdoor context
            plan.warnings.append("missing_outdoor_temperature")
            return plan

        # If we had to fall back to the raw aggregated sensor, preserve the reliability warning.
        t_out0_av: AggregatedValue | None = getattr(snapshot, "global_outdoor_temperature", None)
        if t_out_src == "global_outdoor_temperature" and t_out0_av is not None:
            if getattr(t_out0_av, "is_stale", False) or getattr(t_out0_av, "is_insufficient", False):
                plan.warnings.append("outdoor_temperature_unreliable")

        plan.meta["t_out_mpc_c"] = float(t_out0)
        plan.meta["t_out_mpc_src"] = str(t_out_src)

        t_out_series: list[float] = [float(t_out0)] * int(mpc.horizon_steps)

        if not snapshot.indoor_zones:
            plan.warnings.append("missing_indoor_zones")
            return plan

        skipped_no_valve = 0
        for zone_name, z in snapshot.indoor_zones.items():
            # Plan only zones that appear to be actuated by a radiant valve.
            if getattr(z, "radiant_valve", None) is None:
                skipped_no_valve += 1
                continue
            zd = self._plan_zone(
                zone=z,
                zone_key=zone_name,
                t_out_series=t_out_series,
                windows_closed=snapshot.windows_close_state,
                reason=reason,
                mpc=mpc,
                comfort_bands_by_zone=comfort_bands_by_zone,
            )
            if zd is not None:
                plan.zones[zone_name] = zd

        if skipped_no_valve:
            plan.meta["skipped_no_radiant_valve"] = int(skipped_no_valve)

        return plan

    def _plan_zone(
        self,
        *,
        zone: ZoneSnapshot,
        zone_key: str,
        t_out_series: list[float],
        windows_closed: Optional[bool],
        reason: str,
        mpc: MpcConfig,
        comfort_bands_by_zone: Optional[Mapping[str, ComfortBandResult]] = None,
    ) -> ZoneCommand | None:
        # We need a controlled variable: prefer operative temperature.
        t_meas = as_float(getattr(zone.t_op, "value", None))
        if t_meas is None:
            t_meas = as_float(getattr(zone.temperature, "value", None))
        if t_meas is None:
            return None

        # Comfort band bounds
        band = (comfort_bands_by_zone or {}).get(zone_key) if comfort_bands_by_zone is not None else getattr(zone, "comfort_band", None)
        t_min = as_float(getattr(band, "t_op_min", None))
        t_max = as_float(getattr(band, "t_op_max", None))
        if t_min is None or t_max is None:
            # No band -> skip MPC (or fall back to a simple deadband later)
            return ZoneCommand(
                zone=zone_key,
                valve_on=False,
                seq=[0] * mpc.horizon_steps,
                cost=0.0,
                debug={"skip": "missing_comfort_band"},
            )

        # Window policy (v1):
        # - windows_closed == True  -> allow MPC to decide freely
        # - windows_closed != True  -> only allow heating if clearly below band
        if windows_closed is True:
            allow_heat = True
        else:
            allow_heat = (t_meas < (t_min - 1.0))

        # Previous actuation (to penalize switching)
        u_prev = 0
        u_prev_raw = as_float(getattr(getattr(zone, "radiant_valve", None), "value", None))
        if u_prev_raw is not None:
            u_prev = 1 if u_prev_raw >= 0.5 else 0

        # RC params
        p = self.cfg.rc_by_zone.get(zone_key) or self.cfg.rc_default
        model = RcZoneModel(params=p)

        best_seq: list[int] | None = None
        best_cost = float("inf")
        best_debug: dict[str, Any] = {}

        # Fast-path: heating disallowed by policy -> return OFF schedule
        if not allow_heat:
            return ZoneCommand(
                zone=zone_key,
                valve_on=False,
                seq=[0] * mpc.horizon_steps,
                cost=0.0,
                debug={"skip": "windows_or_policy", "windows_closed": windows_closed, "t_meas": t_meas, "t_min": t_min},
            )

        # Brute-force enumeration is fine at horizon<=12 (4096 combos).
        min_hold_steps = int(math.ceil(float(mpc.min_switch_minutes) / max(1.0, float(mpc.dt_minutes))))
        for seq in _binary_sequences(mpc.horizon_steps):
            if _violates_min_hold(seq, u_prev=u_prev, hold_steps=min_hold_steps):
                continue
            temps = model.simulate(t0_c=t_meas, t_out_c=t_out_series, u=seq, dt_minutes=mpc.dt_minutes)

            # cost terms
            c_comfort = 0.0
            c_energy = 0.0
            c_switch = 0.0

            last_u = u_prev
            t_min_eff, t_max_eff = _band_with_slack(t_min=t_min, t_max=t_max, slack_c=getattr(mpc, "comfort_slack_c", 0.0))
            for k, (t_k, u_k) in enumerate(zip(temps, seq)):
                # Comfort penalty: squared distance outside the band
                if t_k < t_min_eff:
                    d = t_min_eff - t_k
                    c_comfort += d * d
                elif t_k > t_max_eff:
                    d = t_k - t_max_eff
                    c_comfort += d * d

                c_energy += float(u_k)
                if u_k != last_u:
                    c_switch += 1.0
                last_u = u_k

            cost = (
                mpc.w_comfort * c_comfort
                + mpc.w_energy * c_energy
                + mpc.w_switch * c_switch
            )

            if cost < best_cost:
                best_cost = cost
                best_seq = seq
                best_debug = {
                    "t_meas": t_meas,
                    "t_min": t_min,
                    "t_max": t_max,
                    "t_min_eff": t_min_eff,
                    "t_max_eff": t_max_eff,
                    "u_prev": u_prev,
                    "rc": {"tau_h": p.tau_h, "k_c_per_h": p.k_c_per_h},
                    "min_switch": {"minutes": int(mpc.min_switch_minutes), "hold_steps": int(min_hold_steps)},
                    "terms": {
                        "comfort": c_comfort,
                        "energy": c_energy,
                        "switch": c_switch,
                    },
                    "t_end": temps[-1] if temps else None,
                }

        if best_seq is None:
            # Only possible when allow_heat=False and we filtered everything out.
            best_seq = [0] * mpc.horizon_steps
            best_cost = 0.0
            best_debug = {"skip": "windows_or_policy"}

        return ZoneCommand(
            zone=zone_key,
            valve_on=bool(best_seq[0]),
            seq=best_seq,
            cost=float(best_cost),
            debug=best_debug,
        )
