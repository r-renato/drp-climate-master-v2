from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from ...helpers.formatter import fpadstr
from ...plant.decision.context import DecisionDerivedInputs

from .zone.model import ZonesDecision

class MetricBasis(str, Enum):
    WEIGHTED = "weighted"
    COUNT = "count"
    NONE = "none"
    
class PlantMode(str, Enum):
    """High-level operating mode of the *whole plant* (impianto).

    This enum is an **output** of the plant decision algorithm (planner/supervisor),
    and represents the *resulting global state* that the controller intends the
    HVAC system to be in for the current control tick.

    Design goals
    ------------
    - Provide a compact, stable set of plant-level states, intentionally decoupled
      from vendor/device-specific enums (PDC, pumps, VMC).
    - Make each state **semantically strong**: a PlantMode is not just a label,
      it implies a set of expected actuator intents (invariants) and safety rules.
    - Support deterministic conflict resolution (heat vs cool vs latent needs)
      and enable later additions such as anti-flapping (min-runtime / hysteresis).

    Recommended invariants (intent-level, device-agnostic)
    ------------------------------------------------------
    The following is the intended meaning of each mode. Concrete mappings to real
    devices are performed by downstream layers (e.g., `_fill_*_commands` and/or
    Supervisor), but **must not contradict** these invariants.

    OFF
      - Plant idle: no active thermal production and no functional circulation.
      - VMC may be OFF as well (e.g., in VACATION), unless a safety/IAQ policy
        requires minimum ventilation.
      - Typical intent:
          PDC power = OFF
          Radiant/mixing pump(s) = OFF
          Direct/VMC hydraulic pump(s) = OFF
          VMC power = OFF (or minimum, by explicit policy)

    HEATING
      - Active sensible heating demand is present and allowed by season/user mode.
      - Primary production is in heating; secondary circuits may run depending on
        zone demand (valves/duty) and hydraulic architecture.
      - Typical intent:
          PDC mode = HEATING, power = ON
          Radiant circuit = ON if at least one zone requires heat
          VMC = neutral or heat-boost only if explicitly requested

    COOLING
      - Active sensible cooling demand is present and allowed by season/user mode.
      - Must respect **dew-point/condensation safety** (hard guard).
      - Typical intent:
          PDC mode = COOLING, power = ON
          Radiant circuit = ON if zones require cooling, with dew-guard supply target
          VMC = neutral or cool-boost only if explicitly requested

    DEHUM_ASSIST
      - Latent (humidity/dew point) control is driving the plant operation.
      - Commonly used to support dehumidification via VMC/coil or dedicated devices.
      - Radiant sensible cooling may be disabled or limited depending on the
        hydraulic layout and condensation risk.
      - Typical intent:
          PDC mode = COOLING, power = ON (to feed cold coil/dehumidifier)
          Direct/VMC hydraulic circuit = ON if needed
          Radiant circuit = optional / often OFF unless explicitly safe and required

    VENT_ONLY
      - Ventilation-only operation: no thermal production (no active heating/cooling).
      - Used for IAQ maintenance, mild shoulder operation, or winter dehumidification
        by air exchange when active cooling is not allowed.
      - Typical intent:
          PDC power = OFF
          Hydronic pumps = OFF (unless architecture requires otherwise)
          VMC power = ON (speed governed by IAQ/DP policy)

    Notes
    -----
    - PlantMode should be interpreted together with user intent (HA HVACMode and
      preset profile) and with safety constraints (dew point, window state, vacation).
    - If VMC autonomy is desired (managed independently from hydronics), consider
      introducing a separate `VmcMode` / `VentilationMode` state machine; otherwise
      ensure `OFF` and `VENT_ONLY` explicitly control VMC power.
    """

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DEHUM_ASSIST = "dehum_assist"
    VENT_ONLY = "vent_only"


@dataclass(slots=True)
class PlantDemandSignals:
    """Aggregated demand signals for the plant planner (multi-zone + dew-point + VMC).

    This dataclass is the **single observation layer** used by the plant-level decision:
    it summarizes *what the house needs* (comfort) and *what the devices request/can do*
    (VMC requests & feasibility), plus some **decision diagnostics** to explain why a
    mode was chosen.

    Design principles
    -----------------
    - **Clustered signals**: fields are logically grouped (zone comfort, dew-point,
      VMC, user intent, MPC) even if stored flat for logging and backward compatibility.
    - **Deterministic semantics**: each field has a clear unit, range and meaning.
    - **Observability-first**: most “extra” fields exist to make `ModeResolver.decide()`
      explainable in logs and to speed up commissioning/tuning.

    Cluster A - Zone comfort (sensible demand)
    ----------------------------------------
    For each zone *z*:
      - heat_def_z = max(0, T_min(z) - T_meas(z))   [°C]
      - cool_sur_z = max(0, T_meas(z) - T_max(z))   [°C]

    where T_meas is typically operative temp (T_op), fallback to air temperature.
    Aggregations:
      - *_max      : worst-case zone (safety/comfort override)
      - *_by_zone  : per-zone map (diagnostics)
      - *_wmean    : weighted mean (by zone weight) or count-based fallback
      - *_cov      : coverage of zones/weights out of band (0..1)
      - *_metric_basis : tells whether coverage/mean are WEIGHTED, COUNT or NONE

    Cluster B - Dew-point (safety + latent control)
    ----------------------------------------------
    - dp_max_c   : worst-case indoor dew point (condensation risk indicator)
    - dp_dehum_c : robust indoor dew point for latent control (e.g. percentile)
    - outdoor_dp_c : best-effort outdoor dew point (feasibility for ventilation-only dehumid)

    Cluster C - VMC (requests + thresholds)
    --------------------------------------
    - vmc_dp_sp_c / vmc_dehum_on_thr_c / vmc_dehum_off_thr_c : explain DP hysteresis control
    - vmc_dehum_feasible : whether dehumidification can work (coil vs ventilation-only constraints)
    - vmc_req_* : what VMC is asking from hydronics/plant (heat/cool/dehum/water)

    Cluster D - User intent & decision diagnostics (filled by `ModeResolver.decide()`)
    -------------------------------------------------------------------------
    - user_hvac_mode / user_profile / user_forced_off : HA intent mapping
    - ctrl_aggr, *_on_thr_c, quorum_cov_req : thresholds after profile scaling
    - *_override, *_quorum_ok, *_mean_ok : internal gating booleans (mostly for ECO/SLEEP/AWAY/VACATION)
    - any_heat/any_cool/any_dehum, heat_sensible/cool_sensible : final flags consumed by regime logic
    - runtime_season / operative_season : season mapping used for gating/conflict resolution

    Cluster E - MPC/ZonesPlan hints (optional)
    -----------------------------------------
    - zones_any_heat_demand, zones_full_on_pct, zones_mpc_heat_preheat_ok :
      allow integrating a zone-level MPC plan without making it silently override comfort logic.
    """

    # --- Worst-case (max across zones) ---
    heat_def_max_c: float = field(
        default=0.0,
        metadata={
            "doc": "Worst-case sensible heating deficit across zones.",
            "unit": "°C",
            "range": "[0..+inf)",
            "formula": "max_z max(0, T_min(z) - T_meas(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
        },
    )
    cool_sur_max_c: float = field(
        default=0.0,
        metadata={
            "doc": "Worst-case sensible cooling surplus across zones.",
            "unit": "°C",
            "range": "[0..+inf)",
            "formula": "max_z max(0, T_meas(z) - T_max(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
        },
    )

    # --- Per-zone maps ---
    heat_def_by_zone_c: Dict[str, float] = field(
        default_factory=dict,
        metadata={
            "doc": "Per-zone sensible heating deficit map.",
            "unit": "°C",
            "range": "values in [0..+inf)",
            "formula": "max(0, T_min(z) - T_meas(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
        },
    )
    cool_sur_by_zone_c: Dict[str, float] = field(
        default_factory=dict,
        metadata={
            "doc": "Per-zone sensible cooling surplus map.",
            "unit": "°C",
            "range": "values in [0..+inf)",
            "formula": "max(0, T_meas(z) - T_max(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
        },
    )

    # --- Comfort headroom (diagnostic / MPC preheat gating) ---
    heat_headroom_min_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Minimum heating headroom across zones: how close the house is to lower comfort bound.",
            "unit": "°C",
            "range": "(-inf..+inf)",
            "formula": "min_z (T_meas(z) - T_min(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
            "note": "Small/negative values mean at least one zone is near/below the lower bound (preheat gating).",
        },
    )
    cool_headroom_min_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Minimum cooling headroom across zones: how close the house is to upper comfort bound.",
            "unit": "°C",
            "range": "(-inf..+inf)",
            "formula": "min_z (T_max(z) - T_meas(z))",
            "source": "DemandSignalsBuilder.ZoneComfortCluster",
            "note": "Small/negative values mean at least one zone is near/above the upper bound (precool gating).",
        },
    )

    # --- Profile-aware multi-zone demand metrics ---
    heat_def_wmean_c: float = field(
        default=0.0,
        metadata={
            "doc": "Mean sensible heating deficit across zones (weighted if weights available).",
            "unit": "°C",
            "range": "[0..+inf)",
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )
    cool_sur_wmean_c: float = field(
        default=0.0,
        metadata={
            "doc": "Mean sensible cooling surplus across zones (weighted if weights available).",
            "unit": "°C",
            "range": "[0..+inf)",
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )
    heat_cov: float = field(
        default=0.0,
        metadata={
            "doc": "Heating coverage (0..1): fraction of zones/weights with heat_def > 0.",
            "unit": "1",
            "range": "[0..1]",
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )
    cool_cov: float = field(
        default=0.0,
        metadata={
            "doc": "Cooling coverage (0..1): fraction of zones/weights with cool_sur > 0.",
            "unit": "1",
            "range": "[0..1]",
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )
    heat_metric_basis: MetricBasis = field(
        default=MetricBasis.NONE,
        metadata={
            "doc": "Basis used to compute heat_cov and heat_def_wmean_c.",
            "unit": "-",
            "values": ["weighted", "count", "none"],
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )
    cool_metric_basis: MetricBasis = field(
        default=MetricBasis.NONE,
        metadata={
            "doc": "Basis used to compute cool_cov and cool_sur_wmean_c.",
            "unit": "-",
            "values": ["weighted", "count", "none"],
            "source": "DemandSignalsBuilder.ZoneDemandMetrics",
        },
    )

    # --- Dew point safety ---
    dp_max_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Worst-case indoor dew point across zones (condensation risk indicator).",
            "unit": "°C",
            "range": "(-50..+50) typical",
            "source": "DemandSignalsBuilder.DewPointCluster",
        },
    )
    dp_dehum_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Robust indoor dew point for latent control (e.g., percentile of zone DPs).",
            "unit": "°C",
            "range": "(-50..+50) typical",
            "source": "DemandSignalsBuilder.DewPointCluster",
        },
    )
    # --- VMC debug / transparency ---
    vmc_dp_sp_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Commanded VMC dew-point setpoint (device-effective, possibly adjusted for ΔDP quantization).",
            "unit": "°C",
            "source": "VmcPolicy",
        },
    )
    vmc_ddp_cmd_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Commanded VMC ΔDP (device step). Used with vmc_dp_sp_c to define ON/OFF thresholds.",
            "unit": "°C",
            "source": "VmcPolicy",
        },
    )
    vmc_dp_sp_raw_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Raw (psychrometric) DP setpoint before ΔDP device-quantization mapping.",
            "unit": "°C",
            "source": "VmcPolicy",
        },
    )
    vmc_dehum_on_thr_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Dehumidification ON threshold (dew-point): dp_sp + ddp.",
            "unit": "°C",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    vmc_dehum_off_thr_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Dehumidification OFF threshold (dew-point): on_thr - hysteresis.",
            "unit": "°C",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    outdoor_dp_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Best-effort outdoor dew point used to assess ventilation-only dehumid feasibility.",
            "unit": "°C",
            "source": "DemandSignalsBuilder.DewPointCluster",
        },
    )
    vmc_dehum_feasible: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Whether dehumidification is feasible given configuration/hardware constraints.",
            "unit": "bool",
            "values": ["True", "False", "None(unknown)"],
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )

    # --- VMC requests ---
    vmc_req_heating: bool = field(
        default=False,
        metadata={
            "doc": "VMC requests heating (hot water coil / thermal assist).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    vmc_req_cooling: bool = field(
        default=False,
        metadata={
            "doc": "VMC requests cooling (chilled water coil / thermal assist).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    vmc_req_dehumidif: bool = field(
        default=False,
        metadata={
            "doc": "VMC requests dehumidification (latent control).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    vmc_req_water: bool = field(
        default=False,
        metadata={
            "doc": "VMC requires hydronic water circulation (direct pump ON).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.VmcCluster",
        },
    )
    free_cool_delta_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Delta T indoor_mean − outdoor [°C]. Positivo = esterno più freddo.",
            "unit": "°C",
            "source": "DemandSignalsBuilder.FreeVentCluster",
        },
    )
    free_heat_delta_c: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Delta T outdoor − indoor_mean [°C]. Positivo = esterno più caldo.",
            "unit": "°C",
            "source": "DemandSignalsBuilder.FreeVentCluster",
        },
    )
    free_cool_dp_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "True se DP_outdoor < DP_indoor_max − margine (aria esterna non aggiunge umidità).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.FreeVentCluster",
        },
    )
    free_cool_feasible: bool = field(
        default=False,
        metadata={
            "doc": "True se free cooling ventilativo è fattibile (delta_T, DP e finestre ok).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.FreeVentCluster",
        },
    )
    free_heat_feasible: bool = field(
        default=False,
        metadata={
            "doc": "True se free heating ventilativo è fattibile (delta_T, stagione e finestre ok).",
            "unit": "bool",
            "source": "DemandSignalsBuilder.FreeVentCluster",
        },
    )
    # --- VMC free vent requests (prodotti da VmcPolicy) ---
    vmc_req_free_cooling: bool = field(
        default=False,
        metadata={
            "doc": "VMC richiede bypass recuperatore con aria esterna fredda (free cooling).",
            "unit": "bool",
            "source": "VmcPolicy",
        },
    )
    vmc_req_free_heating: bool = field(
        default=False,
        metadata={
            "doc": "VMC richiede bypass recuperatore con aria esterna calda (free heating).",
            "unit": "bool",
            "source": "VmcPolicy",
        },
    )

    zones_any_heat_demand: bool = field(
        default=False,
        metadata={
            "doc": "True if the zone planner (MPC/ZonesPlan) scheduled at least one valve ON for heating.",
            "unit": "bool",
            "source": "ZonesDecision",
        },
    )
    zones_full_on_pct: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Percentage of zones in FULL-ON state (duty=1.0) in MPC plan, if available.",
            "unit": "%",
            "range": "[0..100]",
            "source": "ZonesDecision.meta",
        },
    )
    zones_duty_avg_pct: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Mean duty across all zones and horizon steps (0..100), if MPC plan is available.",
            "unit": "%",
            "range": "[0..100]",
            "source": "ZonesDecision.meta",
        },
    )
    zones_on_now_pct: Optional[float] = field(
        default=None,
        metadata={
            "doc": "Percentage of zones ON at the first MPC step (now) (0..100), if available.",
            "unit": "%",
            "range": "[0..100]",
            "source": "ZonesDecision.meta",
        },
    )
    zones_first_on_step: Optional[int] = field(
        default=None,
        metadata={
            "doc": "Earliest MPC horizon step where any zone is scheduled ON (0..h-1), if available.",
            "unit": "step",
            "range": "[0..h-1]",
            "source": "ZonesDecision.meta",
        },
    )
    zones_mpc_heat_preheat_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "True if MPC heating is accepted as 'preheat' (only when close to the lower bound).",
            "unit": "bool",
            "values": ["True", "False", "None(not evaluated)"],
            "source": "ModeResolver.decide",
        },
    )

    # --- Other ---
    user_hvac_mode: str = field(
        default="off",
        metadata={
            "doc": "User HVAC mode as seen by the HA Climate entity (normalized string, e.g. 'off'/'auto').",
            "unit": "-",
            "source": "ModeResolver.decide",
        },
    )
    user_profile: str = field(
        default="off",
        metadata={
            "doc": "User preset/profile (HVACOperatingProfile.value), stored as a string for logs.",
            "unit": "-",
            "source": "ModeResolver.decide",
        },
    )
    user_forced_off: bool = field(
        default=False,
        metadata={
            "doc": "True when the user explicitly forces HVAC OFF (absolute override).",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    ctrl_aggr: float = field(
        default=0.0,
        metadata={
            "doc": "Control aggressiveness factor derived from profile (BOOST > 1, AWAY/VACATION < 1).",
            "unit": "1",
            "range": "(0..+inf)",
            "source": "ModeResolver.decide",
        },
    )
    heat_on_thr_c: float = field(
        default=0.0,
        metadata={
            "doc": "Effective heating ON threshold after profile scaling.",
            "unit": "°C",
            "range": "[0..+inf)",
            "source": "ModeResolver.decide",
        },
    )
    cool_on_thr_c: float = field(
        default=0.0,
        metadata={
            "doc": "Effective cooling ON threshold after profile scaling.",
            "unit": "°C",
            "range": "[0..+inf)",
            "source": "ModeResolver.decide",
        },
    )
    quorum_cov_req: float = field(
        default=0.0,
        metadata={
            "doc": "Required coverage (0..1) for energy-saving profiles (ECO/SLEEP/AWAY/VACATION).",
            "unit": "1",
            "range": "[0..1]",
            "source": "ModeResolver.decide",
        },
    )
    heat_override: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Heating override triggered by large worst-case deficit (bypasses quorum).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    heat_quorum_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Heating quorum check: heat_cov >= quorum_cov_req (energy-saving profiles).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    heat_mean_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Heating mean check: heat_def_wmean >= heat_on_thr * mean_factor (energy-saving profiles).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    cool_override: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Cooling override triggered by large worst-case surplus (bypasses quorum).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    cool_quorum_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Cooling quorum check: cool_cov >= quorum_cov_req (energy-saving profiles).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    cool_mean_ok: Optional[bool] = field(
        default=None,
        metadata={
            "doc": "Cooling mean check: cool_sur_wmean >= cool_on_thr * mean_factor (energy-saving profiles).",
            "unit": "bool",
            "values": ["True", "False", "None(not applicable)"],
            "source": "ModeResolver.decide",
        },
    )
    any_heat: bool = field(
        default=False,
        metadata={
            "doc": "Final aggregated flag: any heating reason exists (sensible, VMC, or MPC-preheat).",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    any_cool: bool = field(
        default=False,
        metadata={
            "doc": "Final aggregated flag: any cooling reason exists (sensible or VMC).",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    any_dehum: bool = field(
        default=False,
        metadata={
            "doc": "Final aggregated flag: any dehumidification reason exists (latent control).",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    heat_sensible: bool = field(
        default=False,
        metadata={
            "doc": "True if sensible heating demand is considered significant under current profile.",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    cool_sensible: bool = field(
        default=False,
        metadata={
            "doc": "True if sensible cooling demand is considered significant under current profile.",
            "unit": "bool",
            "source": "ModeResolver.decide",
        },
    )
    runtime_season: str = field(
        default="--",
        metadata={
            "doc": "Runtime season raw value coming from snapshot (e.g. 'winter', 'summer', ...).",
            "unit": "-",
            "source": "ModeResolver.decide",
        },
    )
    operative_season: str = field(
        default="--",
        metadata={
            "doc": "Operative bucket derived from runtime season (winter/summer/shoulder) used for gating.",
            "unit": "-",
            "values": ["winter", "summer", "shoulder", "--"],
            "source": "ModeResolver.decide",
        },
    )

    def __str__(self) -> str:
        # --- helper di formattazione compatti e robusti (coerenti con PlantDecision.__str__) ---
        def fnum(x, nd=1):
            if x is None:
                return "-"
            try:
                return f"{float(x):.{nd}f}"
            except (TypeError, ValueError):
                return str(x)

        def fpct01(x, nd=0):
            # x in [0..1] -> percentuale
            if x is None:
                return "-"
            try:
                return f"{(float(x) * 100):.{nd}f}%"
            except (TypeError, ValueError):
                return str(x)

        def fbool(b, on="True", off="False"):
            return on if b is True else (off if b is False else "-")

        def fint(x) -> str:
            if x is None:
                return "-"
            try:
                return str(int(x))
            except (TypeError, ValueError):
                return str(x)

        def fstr(s):
            return s if s else "-"

        def fmb(x: MetricBasis | None):
            return x.value if x is not None else "-"

        def fdict_compact(d: Dict[str, float] | None, nd=1, max_items=8):
            """
            dict compatto tipo: zona1=0.5, zona2=1.2, ...
            taglia dopo max_items per non esplodere i log.
            """
            if not d:
                return "-"
            items = sorted(d.items(), key=lambda kv: kv[0])
            more = ""
            if len(items) > max_items:
                items = items[:max_items]
                more = f" (+{len(d) - max_items})"
            s = ", ".join(f"{k}={fnum(v, nd)}" for k, v in items)
            return s + more

        def fdoc(field_name: str) -> str | None:
            f = getattr(self, "__dataclass_fields__", {}).get(field_name)
            if not f:
                return None
            d = f.metadata.get("doc")
            return d if d else None

        def emit(lines: list[str], label: str, field_name: str, value: str) -> None:
            # riga valore
            lines.append(f"{fpadstr(fstr(label), pad_before=2, field_width=30)} :: {value}")
            # riga doc sotto (se presente)
            d = fdoc(field_name)
            if d is not None:
                lines.append(f"{fpadstr(fstr(''), pad_before=2, field_width=30)} :: {d}")

        # NB: primo header "finto" per compatibilità con PlantDecision.__str__ che fa splitlines()[1:]
        lines: list[str] = ["PlantDemandSignals", "------------------------------------------------------------------", "Signals"]

        # -------------------------
        # Cluster: User intent
        # -------------------------
        lines += ["User intent"]
        emit(lines, "User HVAC mode", "user_hvac_mode", fstr(self.user_hvac_mode))
        emit(lines, "User profile", "user_profile", fstr(self.user_profile))
        emit(lines, "User forced off", "user_forced_off", fbool(self.user_forced_off, "True", "False"))
        emit(lines, "Ctrl aggress", "ctrl_aggr", fnum(self.ctrl_aggr, 2))

        # -------------------------
        # Cluster: Season
        # -------------------------
        lines += ["Season"]
        emit(lines, "Runtime season", "runtime_season", fstr(self.runtime_season))
        emit(lines, "Operative season", "operative_season", fstr(self.operative_season))

        # -------------------------
        # Cluster: Sensible demand (worst-case + headroom)
        # -------------------------
        lines += ["Demand (sensible)"]
        emit(lines, "Heat def max", "heat_def_max_c", f"{fnum(self.heat_def_max_c)} °C")
        emit(lines, "Cool sur max", "cool_sur_max_c", f"{fnum(self.cool_sur_max_c)} °C")
        emit(lines, "Heat headroom min", "heat_headroom_min_c", f"{fnum(self.heat_headroom_min_c)} °C")
        emit(lines, "Cool headroom min", "cool_headroom_min_c", f"{fnum(self.cool_headroom_min_c)} °C")

        # -------------------------
        # Cluster: Multi-zone metrics (mean/coverage)
        # -------------------------
        lines += ["Demand metrics"]
        emit(lines, "Heat def mean", "heat_def_wmean_c", f"{fnum(self.heat_def_wmean_c)} °C")
        emit(
            lines,
            "Heat coverage",
            "heat_cov",
            f"{fpct01(self.heat_cov, 0)} ({fmb(self.heat_metric_basis)})",
        )
        emit(lines, "Heat on thr", "heat_on_thr_c", f"{fnum(self.heat_on_thr_c)} °C")
        emit(lines, "Cool sur mean", "cool_sur_wmean_c", f"{fnum(self.cool_sur_wmean_c)} °C")
        emit(
            lines,
            "Cool coverage",
            "cool_cov",
            f"{fpct01(self.cool_cov, 0)} ({fmb(self.cool_metric_basis)})",
        )
        emit(lines, "Cool on thr", "cool_on_thr_c", f"{fnum(self.cool_on_thr_c)} °C")

        # -------------------------
        # Cluster: Gating / quorum (ECO/SLEEP/AWAY/VACATION)
        # -------------------------
        lines += ["Gating"]
        emit(lines, "Quorum cov req", "quorum_cov_req", fpct01(self.quorum_cov_req, 0))
        emit(lines, "Heat override", "heat_override", fbool(self.heat_override, "True", "False"))
        emit(lines, "Heat quorum ok", "heat_quorum_ok", fbool(self.heat_quorum_ok, "True", "False"))
        emit(lines, "Heat mean ok", "heat_mean_ok", fbool(self.heat_mean_ok, "True", "False"))
        emit(lines, "Cool override", "cool_override", fbool(self.cool_override, "True", "False"))
        emit(lines, "Cool quorum ok", "cool_quorum_ok", fbool(self.cool_quorum_ok, "True", "False"))
        emit(lines, "Cool mean ok", "cool_mean_ok", fbool(self.cool_mean_ok, "True", "False"))

        # -------------------------
        # Cluster: Dew point / latent
        # -------------------------
        lines += ["Dew point"]
        emit(lines, "DP max", "dp_max_c", f"{fnum(self.dp_max_c)} °C")
        emit(lines, "DP dehum", "dp_dehum_c", f"{fnum(self.dp_dehum_c)} °C")
        emit(lines, "Outdoor DP", "outdoor_dp_c", f"{fnum(self.outdoor_dp_c)} °C")
        emit(lines, "Dehum feasible", "vmc_dehum_feasible", fbool(self.vmc_dehum_feasible, "True", "False"))

        # -------------------------
        # Cluster: VMC thresholds (debug/trasparenza)
        # -------------------------
        lines += ["VMC thresholds"]
        emit(lines, "VMC DP sp (cmd)", "vmc_dp_sp_c", f"{fnum(self.vmc_dp_sp_c)} °C")
        emit(lines, "VMC ΔDP (cmd)", "vmc_ddp_cmd_c", f"{fnum(self.vmc_ddp_cmd_c, 0)} °C")
        emit(lines, "VMC DP sp (raw)", "vmc_dp_sp_raw_c", f"{fnum(self.vmc_dp_sp_raw_c)} °C")
        emit(lines, "VMC dehum ON", "vmc_dehum_on_thr_c", f"{fnum(self.vmc_dehum_on_thr_c)} °C")
        emit(lines, "VMC dehum OFF", "vmc_dehum_off_thr_c", f"{fnum(self.vmc_dehum_off_thr_c)} °C")

        # -------------------------
        # Cluster: VMC requests
        # -------------------------
        lines += ["VMC requests"]
        emit(lines, "VMC req heating", "vmc_req_heating", fbool(self.vmc_req_heating, "True", "False"))
        emit(lines, "VMC req cooling", "vmc_req_cooling", fbool(self.vmc_req_cooling, "True", "False"))
        emit(lines, "VMC req dehumidif", "vmc_req_dehumidif", fbool(self.vmc_req_dehumidif, "True", "False"))
        emit(lines, "VMC req water", "vmc_req_water", fbool(self.vmc_req_water, "True", "False"))
        emit(lines, "VMC req free cool", "vmc_req_free_cooling", fbool(self.vmc_req_free_cooling, "True", "False"))
        emit(lines, "VMC req free heat", "vmc_req_free_heating", fbool(self.vmc_req_free_heating, "True", "False"))

        # -------------------------
        # Cluster: Free conditioning feasibility
        # -------------------------
        lines += ["Free conditioning"]
        emit(lines, "Free cool delta", "free_cool_delta_c", f"{fnum(self.free_cool_delta_c)} °C")
        emit(lines, "Free heat delta", "free_heat_delta_c", f"{fnum(self.free_heat_delta_c)} °C")
        emit(lines, "Free cool DP ok", "free_cool_dp_ok", fbool(self.free_cool_dp_ok, "True", "False"))
        emit(lines, "Free cool feasible", "free_cool_feasible", fbool(self.free_cool_feasible, "True", "False"))
        emit(lines, "Free heat feasible", "free_heat_feasible", fbool(self.free_heat_feasible, "True", "False"))

        # -------------------------
        # Cluster: MPC / ZonesPlan hints
        # -------------------------
        lines += ["MPC hints"]
        emit(lines, "Zones MPC heat", "zones_any_heat_demand", fbool(self.zones_any_heat_demand, "True", "False"))
        emit(lines, "Zones MPC full-on", "zones_full_on_pct", f"{fnum(self.zones_full_on_pct, 1)} %")
        emit(lines, "Zones MPC duty avg", "zones_duty_avg_pct", f"{fnum(self.zones_duty_avg_pct, 1)} %")
        emit(lines, "Zones MPC on-now", "zones_on_now_pct", f"{fnum(self.zones_on_now_pct, 1)} %")
        emit(lines, "Zones MPC first ON", "zones_first_on_step", fint(self.zones_first_on_step))
        emit(lines, "Zones MPC preheat", "zones_mpc_heat_preheat_ok", fbool(self.zones_mpc_heat_preheat_ok, "True", "False"))

        # -------------------------
        # Cluster: Final flags
        # -------------------------
        lines += ["Flags"]
        emit(lines, "Any heat", "any_heat", fbool(self.any_heat, "True", "False"))
        emit(lines, "Any cool", "any_cool", fbool(self.any_cool, "True", "False"))
        emit(lines, "Any dehum", "any_dehum", fbool(self.any_dehum, "True", "False"))
        emit(lines, "Heat sensible", "heat_sensible", fbool(self.heat_sensible, "True", "False"))
        emit(lines, "Cool sensible", "cool_sensible", fbool(self.cool_sensible, "True", "False"))

        # -------------------------
        # Cluster: Per-zone maps (compatte)
        # -------------------------
        lines += ["By zone"]
        emit(lines, "Heat def by zone", "heat_def_by_zone_c", fdict_compact(self.heat_def_by_zone_c, nd=1))
        emit(lines, "Cool sur by zone", "cool_sur_by_zone_c", fdict_compact(self.cool_sur_by_zone_c, nd=1))

        return "\n".join(lines)

@dataclass(slots=True)
class PdcCommand:
    """Comandi desiderati per la PDC (produzione primaria).

    I campi sono volutamente 'Optional' perché questo layer NON esegue direttamente attuazioni
    (in questa fase il codice non è integrato nel Supervisor).
    """

    power: Optional[bool] = None
    mode: Optional[str] = None  # "heating" | "cooling"
    heat_wot_c: Optional[float] = None
    heat_dt_c: Optional[float] = None
    cool_wot_c: Optional[float] = None
    cool_dt_c: Optional[float] = None

    # Alcuni impianti distinguono "Workload FM power" vs "Device Power"
    fm_power: Optional[bool] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SupplyCommand:
    """Comandi desiderati per i circuiti secondari (pompe + miscelazione)."""

    # Pompe
    direct_pump_on: Optional[bool] = None   # circuito VMC (diretto)
    adj_pump_on: Optional[bool] = None      # circuito radiante (mix)

    # Miscelatrice 3-vie (0-100%)
    mix_valve_pct: Optional[float] = None

    # Target "fisici" (utile per logging/telemetria anche se non mappati 1:1 su attuatori)
    rad_supply_target_c: Optional[float] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ZoneValvesCommand:
    """Comandi desiderati per le elettrovalvole di zona (collettori radianti).

    Questo oggetto rappresenta **solo** l'intento logico (zona -> ON/OFF),
    senza contenere dettagli Home Assistant (entity_id). La mappatura tra
    zona logica e entity_id è responsabilità del layer di attuazione.

    Note termotecniche
    ------------------
    - Le elettrovalvole elettrotermiche hanno tipicamente tempi di apertura
      dell'ordine di 60-120s: per questo la logica di attuazione può applicare
      uno staging (valvole -> pompe) senza bloccare il loop.
    """

    by_zone: Dict[str, bool] = field(default_factory=dict)
    debug: Dict[str, Any] = field(default_factory=dict)

    @property
    def any_open(self) -> bool:
        return any(bool(v) for v in (self.by_zone or {}).values())


@dataclass(slots=True)
class VmcCommand:
    """Comandi desiderati per la VMC."""

    power: Optional[bool] = None
    mode: Optional[str] = None  # "winter" | "summer" | "off"
    air_speed: Optional[int] = None

    setpoint_t_c: Optional[float] = None
    setpoint_rh_pct: Optional[float] = None
    setpoint_dp_c: Optional[float] = None
    setpoint_ddp_c: Optional[int] = None

    # Free cooling ventilativo (bypass recuperatore)
    # Prerequisito di sicurezza: force_treatment_off deve essere True prima
    # di attivare enable_free_cooling e force_free_cooling.
    force_treatment_off: Optional[bool] = None
    enable_free_cooling: Optional[bool] = None
    force_free_cooling: Optional[bool] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlantDecision:
    """Risultato della pianificazione a livello impianto."""

    ts: datetime
    mode: PlantMode
    reason: str

    pdc: PdcCommand = field(default_factory=PdcCommand)
    supply: SupplyCommand = field(default_factory=SupplyCommand)
    valves: ZoneValvesCommand = field(default_factory=ZoneValvesCommand)
    vmc: VmcCommand = field(default_factory=VmcCommand)

    derived_input: DecisionDerivedInputs = field(default_factory=DecisionDerivedInputs)

    # Optional zones MPC plan produced/consumed by the plant-level orchestrator.
    # Kept here to allow a single decision object to carry *both* plant commands
    # and the zone valve plan for consistent actuation and logging.
    zones: Optional[ZonesDecision] = None

    signals: PlantDemandSignals = field(default_factory=PlantDemandSignals)
    warnings: List[str] = field(default_factory=list)

    def to_log_dict(self) -> Dict[str, Any]:
        """Versione 'log friendly' (serializzabile) della decisione."""
        # NOTE: dataclass(slots=True) non garantisce __dict__ sulle istanze.
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["mode"] = self.mode.value
        return d

    def __str__(self) -> str:
        # --- helper di formattazione compatti e robusti ---
        def fnum(x, nd=1):
            return f"{x:.{nd}f}" if x is not None else "-"

        def fpct01(x, nd=0):
            # x in [0..1] -> percentuale
            return f"{(x * 100):.{nd}f}%" if x is not None else "-"

        def fbool(b, on="on", off="off"):
            return on if b is True else (off if b is False else "-")

        def fstr(s):
            return s if s else "-"

        def flist(xs):
            return " | ".join(xs) if xs else "-"

        def fmb(x: MetricBasis | None):
            return x.value if x is not None else "-"

        def fdict_compact(d: Dict[str, float] | None, nd=1, max_items=8):
            """
            dict compatto tipo: zona1=0.5, zona2=1.2, ...
            taglia dopo max_items per non esplodere i log.
            """
            if not d:
                return "-"
            items = sorted(d.items(), key=lambda kv: kv[0])
            more = ""
            if len(items) > max_items:
                items = items[:max_items]
                more = f" (+{len(d) - max_items})"
            s = ", ".join(f"{k}={fnum(v, nd)}" for k, v in items)
            return s + more

        def fobj_summary(obj: Any) -> str:
            if obj is None:
                return "-"
            try:
                if is_dataclass(obj) and not isinstance(obj, type):
                    data = asdict(obj)
                elif hasattr(obj, "__dict__"):
                    data = {
                        k: v
                        for k, v in vars(obj).items()
                        if not k.startswith("_")
                    }
                else:
                    return str(obj)
            except Exception:
                return str(obj)

            if not data:
                return "-"

            parts = []
            for k, v in data.items():
                if isinstance(v, dict):
                    if not v:
                        continue
                    parts.append(f"{k}={fdict_compact(v, nd=1)}")
                else:
                    parts.append(f"{k}={v}")
            return ", ".join(parts) if parts else "-"

        # --- top-level ---
        ts = self.ts.isoformat()
        lines = [
            f"",
            f"Plant decision",
            f"  Timestamp          :: {ts}",
            f"  Mode               :: {self.mode.value if self.mode else '-'}",
            f"  Reason             :: {fstr(self.reason)}",
        ]

        # --- signals (formattati "a mano", non dump generico) ---
        s = self.signals
        if s is not None:
            lines += s.__str__().splitlines()[1:]  # skip header line
            # lines += [
            #     f"------------------------------------------------------------------",
            #     f"Signals",
            #     # user
            #     f"  User HVAC mode     :: {fstr(s.user_hvac_mode)}",
            #     f"  User profile       :: {fstr(s.user_profile)}",
            #     f"  User forced off    :: {fbool(s.user_forced_off, 'True', 'False')}",
            #     # season / runtime
            #     f"  Runtime season     :: {fstr(s.runtime_season)}",
            #     f"  Operative season   :: {fstr(s.operative_season)}",
            #     # demand worst-case
            #     f"  Heat def max       :: {fnum(s.heat_def_max_c)} °C",
            #     f"  Cool sur max       :: {fnum(s.cool_sur_max_c)} °C",
            #     f"  Heat headroom min  :: {fnum(getattr(s, 'heat_headroom_min_c', None))} °C",
            #     f"  Cool headroom min  :: {fnum(getattr(s, 'cool_headroom_min_c', None))} °C",
            #     # demand means + quorum
            #     f"  Heat def mean      :: {fnum(s.heat_def_wmean_c)} °C",
            #     f"  Heat coverage      :: {fpct01(s.heat_cov, 0)} ({fmb(s.heat_metric_basis)})",
            #     f"  Heat on thr        :: {fnum(s.heat_on_thr_c)} °C",
            #     f"  Heat quorum req    :: {fpct01(s.quorum_cov_req, 0)}",
            #     f"  Heat override      :: {fbool(s.heat_override, 'True', 'False')}",
            #     f"  Heat quorum ok     :: {fbool(s.heat_quorum_ok, 'True', 'False')}",
            #     f"  Heat mean ok       :: {fbool(s.heat_mean_ok, 'True', 'False')}",
            #     f"  Cool sur mean      :: {fnum(s.cool_sur_wmean_c)} °C",
            #     f"  Cool coverage      :: {fpct01(s.cool_cov, 0)} ({fmb(s.cool_metric_basis)})",
            #     f"  Cool on thr        :: {fnum(s.cool_on_thr_c)} °C",
            #     f"  Cool override      :: {fbool(s.cool_override, 'True', 'False')}",
            #     f"  Cool quorum ok     :: {fbool(s.cool_quorum_ok, 'True', 'False')}",
            #     f"  Cool mean ok       :: {fbool(s.cool_mean_ok, 'True', 'False')}",
            #     # any / sensible
            #     f"  Any heat           :: {fbool(s.any_heat, 'True', 'False')}",
            #     f"  Any cool           :: {fbool(s.any_cool, 'True', 'False')}",
            #     f"  Any dehum          :: {fbool(s.any_dehum, 'True', 'False')}",
            #     f"  Heat sensible      :: {fbool(s.heat_sensible, 'True', 'False')}",
            #     f"  Cool sensible      :: {fbool(s.cool_sensible, 'True', 'False')}",
            #     # dew point safety
            #     f"  DP max             :: {fnum(s.dp_max_c)} °C",
            #     f"  DP dehum           :: {fnum(getattr(s, 'dp_dehum_c', None))} °C",
            #     f"  Outdoor DP         :: {fnum(getattr(s, 'outdoor_dp_c', None))} °C",
            #     f"  VMC dehum feasible :: {fbool(getattr(s, 'vmc_dehum_feasible', None), 'True', 'False')}",
            #     # VMC debug thresholds
            #     f"  VMC DP sp          :: {fnum(getattr(s, 'vmc_dp_sp_c', None))} °C",
            #     f"  VMC dehum ON thr   :: {fnum(getattr(s, 'vmc_dehum_on_thr_c', None))} °C",
            #     f"  VMC dehum OFF thr  :: {fnum(getattr(s, 'vmc_dehum_off_thr_c', None))} °C",
            #     # VMC requests
            #     f"  VMC req heating    :: {fbool(s.vmc_req_heating, 'True', 'False')}",
            #     f"  VMC req cooling    :: {fbool(s.vmc_req_cooling, 'True', 'False')}",
            #     f"  VMC req dehumidif  :: {fbool(s.vmc_req_dehumidif, 'True', 'False')}",
            #     f"  VMC req water      :: {fbool(s.vmc_req_water, 'True', 'False')}",
            #     f"  Zones MPC heat     :: {fbool(getattr(s, 'zones_any_heat_demand', False), 'True', 'False')}",
            #     f"  Zones MPC full-on  :: {fnum(getattr(s, 'zones_full_on_pct', None), 1)} %",
            #     f"  Zones MPC preheat  :: {fbool(getattr(s, 'zones_mpc_heat_preheat_ok', None), 'True', 'False')}",
            #     # per-zone maps (compatte)
            #     f"  Heat def by zone   :: {fdict_compact(s.heat_def_by_zone_c, nd=1)}",
            #     f"  Cool sur by zone   :: {fdict_compact(s.cool_sur_by_zone_c, nd=1)}",
            # ]

        # --- derived inputs ---
        if getattr(self, "derived_input", None) is not None:
            try:
                lines += [
                    f"------------------------------------------------------------------",
                    f"Derived input",
                    f"  Summary            :: {fobj_summary(self.derived_input)}",
                ]
            except Exception:
                pass

        # --- zones MPC plan (valves) ---
        zdec = getattr(self, "zones", None)
        if zdec is not None:
            try:
                n_zones = len(getattr(zdec, "zones", {}) or {})
                n_on = sum(1 for c in (getattr(zdec, "zones", {}) or {}).values() if getattr(c, "valve_on", False))
                meta = getattr(zdec, "meta", {}) or {}
                duty = meta.get("mpc_duty_avg_pct")
                on_now = meta.get("mpc_on_now_pct")
                first_on = meta.get("mpc_first_on_step")
                full_on = meta.get("mpc_full_on_pct")
                full_off = meta.get("mpc_full_off_pct")

                # Compact per-zone status list (truncated)
                items = sorted((getattr(zdec, "zones", {}) or {}).items(), key=lambda kv: str(kv[0]))
                preview = items[:8]
                more = f" (+{len(items) - 8})" if len(items) > 8 else ""
                zs = ", ".join(f"{k}={'On' if getattr(v, 'valve_on', False) else 'Off'}" for k, v in preview)
                zs = (zs + more) if zs else "-"

                lines += [
                    f"------------------------------------------------------------------",
                    f"Zones MPC plan",
                    f"  Zones              :: {n_zones} (on={n_on}, off={max(0, n_zones - n_on)})",
                    f"  Duty avg           :: {fnum(duty, 1)} %",
                    f"  On-now             :: {fnum(on_now, 1)} %",
                    f"  First ON step      :: {first_on if first_on is not None else '-'}",
                    f"  Full-on / Full-off :: {fnum(full_on, 1)} % / {fnum(full_off, 1)} %",
                    f"  Valves             :: {zs}",
                ]
                if getattr(zdec, "warnings", None):
                    lines += [f"  Warnings           :: {flist(list(getattr(zdec, 'warnings', []) or []))}"]
            except Exception:
                # Never break decision logging due to zones formatting
                pass

        # --- warnings ---
        if self.warnings:
            lines += [
                f"------------------------------------------------------------------",
                f"Warnings            :: {flist(self.warnings)}",
            ]

        lines += [f"------------------------------------------------------------------"]

        # --- PDC ---
        lines += [f"PDC"]
        if self.pdc:
            lines += [
                f"  Power              :: {fbool(self.pdc.power, 'On', 'Off')}",
                f"  FM power           :: {fbool(self.pdc.fm_power, 'On', 'Off')}",
                f"  Mode               :: {self.pdc.mode or '-'}",
                f"  Heat-WOT           :: {fnum(self.pdc.heat_wot_c)} °C",
                f"  Heat-ΔT            :: {fnum(self.pdc.heat_dt_c)} °C",
                f"  Cool-WOT           :: {fnum(self.pdc.cool_wot_c)} °C",
                f"  Cool-ΔT            :: {fnum(self.pdc.cool_dt_c)} °C",
            ]
            if getattr(self.pdc, "debug", None):
                lines += [f"  Debug              :: {self.pdc.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        # --- Valves (zone electrovalves) ---
        lines += [f"Valves"]
        if getattr(self, "valves", None) is not None:
            try:
                byz = getattr(self.valves, "by_zone", None) or {}
                n_z = len(byz)
                n_on = sum(1 for v in byz.values() if bool(v))
                items = sorted(byz.items(), key=lambda kv: str(kv[0]))
                preview = items[:8]
                more = f" (+{len(items) - 8})" if len(items) > 8 else ""
                zs = ", ".join(f"{k}={'On' if v else 'Off'}" for k, v in preview)
                zs = (zs + more) if zs else "-"
                lines += [
                    f"  Zones              :: {n_z} (on={n_on}, off={max(0, n_z - n_on)})",
                    f"  States             :: {zs}",
                ]
                if getattr(self.valves, "debug", None):
                    lines += [f"  Debug              :: {self.valves.debug}"]
            except Exception:
                lines += [f"  -"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        # --- Supply ---
        lines += [f"Supply"]
        if self.supply:
            lines += [
                f"  Direct pump        :: {fbool(self.supply.direct_pump_on, 'On', 'Off')}",
                f"  Adj pump           :: {fbool(self.supply.adj_pump_on, 'On', 'Off')}",
                f"  Mix valve          :: {fnum(self.supply.mix_valve_pct, 0)} %",
                f"  Rad target         :: {fnum(self.supply.rad_supply_target_c)} °C",
            ]
            if getattr(self.supply, "debug", None):
                lines += [f"  Debug              :: {self.supply.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        # --- VMC ---
        lines += [f"VMC"]
        if self.vmc:
            lines += [
                f"  Power              :: {fbool(self.vmc.power, 'On', 'Off')}",
                f"  Mode               :: {self.vmc.mode or '-'}",
                f"  Air speed          :: {self.vmc.air_speed if self.vmc.air_speed is not None else '-'}",
                f"  Setpoint T         :: {fnum(self.vmc.setpoint_t_c)} °C",
                f"  Setpoint RH        :: {fnum(self.vmc.setpoint_rh_pct, 0)} %",
                f"  Setpoint DP        :: {fnum(self.vmc.setpoint_dp_c)} °C",
                f"  Setpoint ΔDP       :: {fnum(self.vmc.setpoint_ddp_c)} °C",
                f"  Treatment off      :: {fbool(getattr(self.vmc, 'force_treatment_off', None), 'On', 'Off')}",
                f"  Enable free cool   :: {fbool(getattr(self.vmc, 'enable_free_cooling', None), 'On', 'Off')}",
                f"  Force free cool    :: {fbool(getattr(self.vmc, 'force_free_cooling', None), 'On', 'Off')}",
            ]
            if getattr(self.vmc, "debug", None):
                lines += [f"  Debug              :: {self.vmc.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        return "\n".join(lines)
