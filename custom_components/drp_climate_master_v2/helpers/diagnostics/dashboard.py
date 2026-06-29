# custom_components/drp_climate_master_v2/helpers/diagnostics/dashboard.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from homeassistant.util import dt as dt_util

from ...helpers.utils import as_float
from ...plant.monitor.plant import PlantSnapshot


def _iso(ts: Optional[datetime]) -> str:
    if not isinstance(ts, datetime):
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_util.UTC)
    return ts.isoformat()


def _count_switches(seq: List[int]) -> int:
    if not seq:
        return 0
    sw = 0
    last = int(seq[0])
    for x in seq[1:]:
        x = int(x)
        if x != last:
            sw += 1
            last = x
    return sw


def build_dashboard(
    snapshot: PlantSnapshot,
    zones_decision: Any = None,
    plant_decision: Any = None,
) -> Dict[str, Any]:
    """Return a structured diagnostic payload (JSON-friendly)."""
    ts = getattr(snapshot, "timestamp", None)
    meta = {
        "ts": _iso(ts),
        "season": getattr(getattr(getattr(snapshot, "season", None), "season", None), "value", None),
        "hvac_mode": str(getattr(snapshot, "climate_hvac_mode", None) or "-"),
        "profile": getattr(getattr(snapshot, "climate_preset_mode", None), "value", None),
        "windows_closed": getattr(snapshot, "windows_closed", None),
        "vacation": getattr(snapshot, "presence_vacation", None),
        "nobodysin": getattr(snapshot, "presence_nobodysin", None),
    }

    indoor = getattr(snapshot, "indoor_zones", None) or {}
    zones_rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    stale: List[str] = []

    for zn, z in indoor.items():
        t_air = as_float(getattr(getattr(z, "temperature", None), "value", None))
        rh = as_float(getattr(getattr(z, "humidity", None), "value", None))
        dp = as_float(getattr(getattr(z, "dew_point", None), "value", None))
        t_op = as_float(getattr(getattr(z, "t_op", None), "value", None))
        pmv = as_float(getattr(getattr(z, "comfort_band", None), "pmv", None))
        ppd = as_float(getattr(getattr(z, "comfort_band", None), "ppd", None))
        ok = getattr(getattr(z, "comfort_band", None), "ok", None)
        t_min = as_float(getattr(getattr(z, "comfort_band", None), "t_op_min", None))
        t_max = as_float(getattr(getattr(z, "comfort_band", None), "t_op_max", None))

        # quality markers (AggregatedValue flags)
        for k, av in [
            ("temperature", getattr(z, "temperature", None)),
            ("humidity", getattr(z, "humidity", None)),
            ("t_op", getattr(z, "t_op", None)),
            ("dew_point", getattr(z, "dew_point", None)),
        ]:
            if av is None:
                continue
            if getattr(av, "is_insufficient", False):
                missing.append(f"{zn}.{k}")
            if getattr(av, "is_stale", False):
                stale.append(f"{zn}.{k}")

        if t_op is None and t_air is None:
            missing.append(f"{zn}.t_op_or_t_air")

        deficit = 0.0
        if t_op is not None and t_min is not None:
            deficit = max(0.0, float(t_min) - float(t_op))

        zones_rows.append(
            {
                "zone": zn,
                "t_air_c": t_air,
                "rh_pct": rh,
                "dp_c": dp,
                "t_op_c": t_op,
                "t_min_c": t_min,
                "t_max_c": t_max,
                "deficit_c": deficit,
                "pmv": pmv,
                "ppd": ppd,
                "in_band": ok,
                "valve_meas": as_float(getattr(getattr(z, "radiant_valve", None), "value", None)),
            }
        )

    # Rank by deficit
    zones_rows.sort(key=lambda r: (r.get("deficit_c") or 0.0), reverse=True)

    # MPC diagnostics
    mpc = {"available": False}
    if zones_decision and getattr(zones_decision, "zones", None):
        cmds = zones_decision.zones
        rows = []
        full_on = 0
        full_off = 0
        for zn, cmd in cmds.items():
            seq = list(getattr(cmd, "seq", None) or [])
            on = int(sum(seq)) if seq else 0
            duty = (on / len(seq)) if seq else 0.0
            deg = (duty == 0.0) or (duty == 1.0)
            if duty == 1.0:
                full_on += 1
            if duty == 0.0:
                full_off += 1
            rows.append(
                {
                    "zone": zn,
                    "valve_on": bool(getattr(cmd, "valve_on", False)),
                    "cost": float(getattr(cmd, "cost", 0.0) or 0.0),
                    "len": len(seq),
                    "on_steps": on,
                    "duty": round(duty, 3),
                    "switches": _count_switches(seq),
                    "degenerate": deg,
                    "min_switch": (getattr(cmd, "debug", {}) or {}).get("min_switch", {}),
                }
            )
        rows.sort(key=lambda r: r.get("cost", 0.0), reverse=True)
        mpc = {
            "available": True,
            "dt_min": getattr(zones_decision, "dt_minutes", None),
            "horizon_steps": getattr(zones_decision, "horizon_steps", None),
            "zones": rows,
            "full_on_pct": round(100.0 * full_on / max(1, len(rows)), 1),
            "full_off_pct": round(100.0 * full_off / max(1, len(rows)), 1),
            "total_cost": round(sum(r["cost"] for r in rows), 3),
        }

    # Plant summary
    plant = {"available": False}
    if plant_decision:
        sig = getattr(plant_decision, "signals", None)
        plant = {
            "available": True,
            "mode": getattr(getattr(plant_decision, "mode", None), "value", None),
            "warnings": list(getattr(plant_decision, "warnings", []) or []),
            "heat_def_max_c": getattr(sig, "heat_def_max_c", None) if sig else None,
            "heat_def_mean_c": getattr(sig, "heat_def_wmean_c", None) if sig else None,
            "heat_cov": getattr(sig, "heat_cov", None) if sig else None,
            "dp_max_c": getattr(sig, "dp_max_c", None) if sig else None,
            "pdc": getattr(plant_decision, "pdc", None),
            "supply": getattr(plant_decision, "supply", None),
            "vmc": getattr(plant_decision, "vmc", None),
        }

    # Alerts + status
    alerts = []
    worst = zones_rows[0] if zones_rows else None
    if worst and (worst.get("deficit_c") or 0.0) > 2.0:
        alerts.append(f"cold_zone:{worst['zone']} deficit={worst['deficit_c']:.2f}C")

    if missing:
        alerts.append(f"missing:{len(missing)}")
    if stale:
        alerts.append(f"stale:{len(stale)}")
    if mpc.get("available") and mpc.get("full_on_pct", 0) > 80:
        alerts.append("mpc_degenerate_full_on")

    score = 0
    score += 2 if getattr(snapshot, "faults", ()) else 0
    score += 1 if missing else 0
    score += 1 if stale else 0
    score += 1 if "mpc_degenerate_full_on" in alerts else 0
    score += 1 if any(r.get("ppd", 0) and r["ppd"] > 20 for r in zones_rows) else 0

    status = "ok"
    if score >= 3:
        status = "crit"
    elif score >= 1:
        status = "warn"

    return {
        "meta": meta,
        "status": status,
        "score": score,
        "alerts": alerts,
        "data_quality": {"missing": missing, "stale": stale},
        "comfort": {"zones": zones_rows},
        "mpc": mpc,
        "plant": plant,
    }


def render_dashboard_text(d: Dict[str, Any]) -> str:
    """Pretty multi-line text for logs / UI."""
    meta = d.get("meta", {})
    lines = []
    lines.append("=== DRP CLIMATE MASTER • DIAGNOSTICS DASHBOARD ===")
    lines.append(
        f"ts={meta.get('ts')} season={meta.get('season')} hvac={meta.get('hvac_mode')} profile={meta.get('profile')}"
    )
    lines.append(
        f"windows_closed={meta.get('windows_closed')} vacation={meta.get('vacation')} nobodysin={meta.get('nobodysin')}"
    )
    lines.append(f"status={d.get('status')} score={d.get('score')} alerts={d.get('alerts') or '-'}")
    lines.append("--------------------------------------------------")

    # Comfort
    zones = (d.get("comfort", {}) or {}).get("zones", []) or []
    lines.append(f"Comfort zones (ranked by deficit): count={len(zones)}")
    for r in zones[:6]:
        lines.append(
            f"- {r['zone']}: t_op={r.get('t_op_c','-')}  band=[{r.get('t_min_c','-')},{r.get('t_max_c','-')}] "
            f"def={r.get('deficit_c',0):.2f}  pmv={r.get('pmv','-')} ppd={r.get('ppd','-')} in_band={r.get('in_band')}"
        )

    # MPC
    mpc = d.get("mpc", {}) or {}
    if mpc.get("available"):
        lines.append("--------------------------------------------------")
        lines.append(
            f"ZonesPlan/MPC: dt={mpc.get('dt_min')}min horizon={mpc.get('horizon_steps')}  "
            f"total_cost={mpc.get('total_cost')}"
        )
        lines.append(f"degenerate: full_on={mpc.get('full_on_pct')}% full_off={mpc.get('full_off_pct')}%")
        for r in (mpc.get("zones") or [])[:6]:
            lines.append(
                f"- {r['zone']}: on={r['on_steps']}/{r['len']} duty={r['duty']} switches={r['switches']} "
                f"cost={r['cost']:.1f} deg={r['degenerate']}"
            )

    # Data quality
    dq = d.get("data_quality", {}) or {}
    if dq.get("missing") or dq.get("stale"):
        lines.append("--------------------------------------------------")
        lines.append(f"Data quality: missing={len(dq.get('missing') or [])} stale={len(dq.get('stale') or [])}")

    return "\n".join(lines)
