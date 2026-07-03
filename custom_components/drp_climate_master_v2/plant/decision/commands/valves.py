from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from ....plant.monitor.plant import PlantSnapshot
from ....helpers.utils import as_float, slugify

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode
from ..safety.dew_guard import DewGuardResult
from ..zone.model import ZonesDecision


@dataclass(slots=True)
class ZoneValvesCommandBuilder:
    """Build the *immediate* electrovalves command (zone -> ON/OFF).

    Rationale
    ---------
    The Zones MPC subsystem can output a full horizon plan (`ZonesDecision`).
    For plant-level actuation, we also want an explicit command object that
    represents the **current** intent for each electrovalve in a stable way.

    This builder:
      - derives `PlantDecision.valves` from `ZonesDecision` when available
      - provides a deterministic fallback when MPC is missing/disabled
      - enforces dew-point safety overrides (radiant disabled => valves OFF)
    """

    cfg: PlantPlannerConfig

    def fill(
        self,
        dec: PlantDecision,
        snapshot: PlantSnapshot,
        demand: PlantDemandSignals,
        zones_decision: Optional[ZonesDecision],
        *,
        dew_guard: DewGuardResult,
        zones_decision_cool: Optional[ZonesDecision] = None,
        zone_dp_admitted: Optional[Mapping[str, bool]] = None,
    ) -> None:
        v = dec.valves
        v.by_zone.clear()
        v.debug.clear()

        def _apply_zone_dp_lockout() -> None:
            """Esclude (forza OFF) le zone in lockout DP (Step 1, FASE 5b planner).

            Indipendente dalla sorgente del comando (piano MPC o fallback
            per-metrica): è un AND-gate finale, applicato dopo qualunque
            altra logica di apertura — incluso il fallback "almeno una zona
            aperta" — e SOLO in modalità COOLING/DEHUM_ASSIST (stesso scope
            del dew-point guard whole-plant). Non tocca le zone già a False.
            """
            if not zone_dp_admitted:
                return
            if dec.mode not in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
                return
            for zk, admitted in zone_dp_admitted.items():
                key = slugify(str(zk))
                if not admitted and v.by_zone.get(key):
                    v.by_zone[key] = False
                    v.debug.setdefault("dp_lockout_excluded", []).append(key)

        if dec.mode in (PlantMode.OFF, PlantMode.VENT_ONLY):
            v.debug.update({"source": "mode_off_or_vent_only"})
            return

        if dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST) and not dew_guard.radiant_allowed:
            v.debug.update(
                {
                    "source": "dew_guard_disable_radiant",
                    "dew_guard_reason": dew_guard.reason,
                    "dp_max_c": dew_guard.dp_max_c,
                    "max_allowed_c": dew_guard.max_allowed_c,
                    "required_c": dew_guard.safe_required_c,
                }
            )
            return

        indoor = getattr(snapshot, "indoor_zones", None) or {}

        def is_actuable(zone_key: str) -> bool:
            z = indoor.get(zone_key)
            if z is None:
                return False
            return getattr(z, "radiant_valve", None) is not None

        _active_plan = (
            zones_decision_cool
            if dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST)
            else zones_decision
        )

        if _active_plan is not None and getattr(_active_plan, "zones", None):
            byz = _active_plan.zones or {}
            for zone_key, cmd in byz.items():
                zk = str(zone_key)
                if not is_actuable(zk):
                    continue
                v.by_zone[zk] = bool(getattr(cmd, "valve_on", False))

            _apply_zone_dp_lockout()

            v.debug.update(
                {
                    "source": (
                        "zones_mpc_cool"
                        if dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST)
                        else "zones_mpc"
                    ),
                    "zones": len(v.by_zone),
                    "on": sum(1 for x in v.by_zone.values() if x),
                }
            )
            return

        if dec.mode == PlantMode.HEATING:
            thr = as_float(getattr(dec.gating, "heat_on_thr_c", None))
            if thr is None:
                thr = float(self.cfg.comfort.heat_on_deficit_c)
            by_zone = getattr(demand, "heat_def_by_zone_c", None) or {}
            metric_key = "heat_def_by_zone_c"
        elif dec.mode in (PlantMode.COOLING, PlantMode.DEHUM_ASSIST):
            thr = as_float(getattr(dec.gating, "cool_on_thr_c", None))
            if thr is None:
                thr = float(self.cfg.comfort.cool_on_surplus_c)
            by_zone = getattr(demand, "cool_sur_by_zone_c", None) or {}
            metric_key = "cool_sur_by_zone_c"
        else:
            v.debug.update({"source": "unsupported_mode", "mode": getattr(dec.mode, "value", str(dec.mode))})
            return

        for zone_key, val in (by_zone or {}).items():
            zk = slugify(str(zone_key))
            if not is_actuable(zk):
                continue
            fv = float(as_float(val) or 0.0)
            v.by_zone[zk] = bool(fv >= float(thr))

        if not v.any_open and by_zone:
            try:
                worst_k = max(by_zone.items(), key=lambda kv: float(as_float(kv[1]) or 0.0))[0]
                wk = slugify(str(worst_k))
                if is_actuable(wk):
                    v.by_zone[wk] = True
            except Exception:
                pass

        _apply_zone_dp_lockout()

        v.debug.update(
            {
                "source": "fallback_by_zone_metric",
                "metric": metric_key,
                "thr_c": float(thr),
                "zones": len(v.by_zone),
                "on": sum(1 for x in v.by_zone.values() if x),
            }
        )
