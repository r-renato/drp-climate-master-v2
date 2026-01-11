"""Sensor aggregation + mapping layer for HVAC control (Home Assistant friendly).

What this module provides
- Robust sensor ingestion from Home Assistant entities.
- Per-sensor filtering pipeline (range, staleness, unit conversion, time-outliers, rate limiting, EMA, rolling median).
- Mapping layer: entities -> logical variables -> zones -> optional global/derived variables.
- Optional computed derived variables (e.g., dew point, heat index) from other group outputs.
- Aggregated values always come with quality metadata and reason codes.

What this module intentionally does NOT do
- No HVAC decisions (heat/cool/dehumidify). This is just the input layer.

When dew point / heat index sensors exist in HA
- You *can* ingest them as normal groups (e.g., living.indoor_dew_point).
- Prefer computing dew point / heat index from aggregated T/RH for consistency.
- Best practice: compute them here AND optionally compare with the HA sensor to detect calibration problems.

Notes
- Home Assistant state objects are expected to expose: state (str), last_updated (datetime), attributes (dict).
- Datetimes from HA are usually tz-aware; we defensively handle naive timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Literal, Optional, Tuple

import math
from collections import deque

from homeassistant.core import HomeAssistant, State

from ..const import DOMAIN, ENTITIES_STATE

# -----------------------------
# Types
# -----------------------------

Number = float

AggregationMethod = Literal["weighted_mean", "median", "trimmed_mean", "max", "min"]
CrossOutlierMethod = Literal["none", "hampel", "zscore"]
RateLimitMode = Literal["clip", "reject"]
DerivedKind = Literal["aggregate", "compute"]
ComputeFn = Literal["dew_point_c", "heat_index_c"]


# -----------------------------
# Specs (configuration)
# -----------------------------


@dataclass(frozen=True)
class FilterConfig:
    """Per-sensor filtering pipeline parameters."""

    # If True, attempt unit conversion based on HA unit_of_measurement.
    # Supported: °C/°F for temperature-like variables; % for humidity.
    auto_unit_convert: bool = True

    # Discard values older than this (based on HA last_updated)
    max_age: Optional[timedelta] = timedelta(minutes=15)

    # Basic physical plausibility range (applied after unit conversion)
    min_valid: Optional[float] = None
    max_valid: Optional[float] = None

    # Time-series outlier filter (Hampel on sensor history)
    time_hampel_k: Optional[float] = 4.0
    time_hampel_min_samples: int = 8

    # Rate limiting: prevent spikes (bad packets, bogus reads). Units: value per minute.
    max_rate_per_min: Optional[float] = None
    rate_limit_mode: RateLimitMode = "clip"  # clip keeps continuity; reject creates gaps

    # Smoothing
    ema_alpha: float = 0.25  # 0..1 (higher=less smoothing)
    rolling_median_window: int = 1  # 1 disables; >1 applies median on filtered history

    # History sizes
    raw_history_size: int = 30
    filtered_history_size: int = 30


@dataclass(frozen=True)
class SensorSpec:
    entity_id: str
    weight: float = 1.0
    filters: FilterConfig = field(default_factory=FilterConfig)


@dataclass(frozen=True)
class GroupSpec:
    """A logical variable derived from one or more sensors."""

    name: str
    sensors: Tuple[SensorSpec, ...]

    method: AggregationMethod = "weighted_mean"

    # Cross-sensor outlier handling at the current instant
    cross_outlier_method: CrossOutlierMethod = "hampel"
    cross_outlier_k: float = 3.0

    # Trimmed mean (only used if method='trimmed_mean')
    trimmed_fraction: float = 0.2  # drop 20% low + 20% high

    min_sources: int = 1

    clamp_min: Optional[float] = None
    clamp_max: Optional[float] = None

    # Optional group-level max_age; if None, uses strictest max_age among used sensors
    max_age: Optional[timedelta] = None


@dataclass(frozen=True)
class ZoneConfig:
    """Maps variables for a specific zone."""

    zone: str
    variables: Tuple[GroupSpec, ...]
    weight: float = 1.0  # used when computing global derived variables


@dataclass(frozen=True)
class DerivedSpec:
    """Defines a derived variable.

    Kinds
    - aggregate: aggregates other group outputs (weighted_mean/median/trimmed_mean)
    - compute: computes a derived metric from other group outputs (e.g., dew point from T/RH)

    Inputs
    - inputs is a list of (group_name, weight). For kind='compute', weights are ignored.
    """

    name: str  # e.g., "global.indoor_temperature"
    inputs: Tuple[Tuple[str, float], ...]

    kind: DerivedKind = "aggregate"
    compute: Optional[ComputeFn] = None

    method: AggregationMethod = "weighted_mean"
    trimmed_fraction: float = 0.2

    min_sources: int = 1
    clamp_min: Optional[float] = None
    clamp_max: Optional[float] = None

    max_age: Optional[timedelta] = None


@dataclass(frozen=True)
class MappingConfig:
    zones: Tuple[ZoneConfig, ...]
    derived: Tuple[DerivedSpec, ...] = ()


# -----------------------------
# Runtime state and outputs
# -----------------------------


@dataclass
class Sample:
    value: float
    ts: datetime


@dataclass
class SensorRuntime:
    spec: SensorSpec

    raw_history: Deque[Sample] = field(default_factory=deque)
    filtered_history: Deque[Sample] = field(default_factory=deque)

    last_raw: Optional[Sample] = None
    last_filtered: Optional[Sample] = None

    last_error: Optional[str] = None
    rejected_count: int = 0
    clipped_count: int = 0


@dataclass
class AggregatedValue:
    name: str
    value: Optional[float]
    ts: Optional[datetime]

    sources_total: int
    sources_used: int
    sources_rejected: int

    is_stale: bool
    is_insufficient: bool

    reasons: Tuple[str, ...] = ()


# -----------------------------
# Pure helpers
# -----------------------------


def _is_finite(x: float) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _mad(values: List[float], med: float) -> float:
    return _median([abs(v - med) for v in values])


def _hampel_keep_mask(values: List[float], k: float) -> List[bool]:
    if len(values) <= 2:
        return [True] * len(values)
    med = _median(values)
    mad = _mad(values, med)
    if mad == 0:
        return [True] * len(values)
    sigma = 1.4826 * mad
    return [abs(v - med) <= k * sigma for v in values]


def _zscore_keep_mask(values: List[float], k: float) -> List[bool]:
    if len(values) <= 2:
        return [True] * len(values)
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(1, (len(values) - 1))
    std = math.sqrt(var)
    if std == 0:
        return [True] * len(values)
    return [abs((v - mean) / std) <= k for v in values]


def _ema(prev: Optional[float], x: float, alpha: float) -> float:
    a = max(0.0, min(1.0, float(alpha)))
    return x if prev is None else (a * x + (1.0 - a) * prev)


def _clamp(x: float, lo: Optional[float], hi: Optional[float]) -> float:
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _trimmed_mean(values: List[float], frac: float) -> float:
    if not values:
        raise ValueError("empty")
    frac = max(0.0, min(0.49, float(frac)))
    s = sorted(values)
    n = len(s)
    k = int(math.floor(n * frac))
    core = s[k : n - k] if (n - 2 * k) > 0 else s
    return sum(core) / len(core)


def _agg(values: List[float], method: AggregationMethod, trimmed_fraction: float = 0.2) -> float:
    """Aggregate a list of floats using the requested method."""
    if not values:
        raise ValueError("empty")
    if method == "max":
        return float(max(values))
    if method == "min":
        return float(min(values))
    if method == "median":
        return float(_median(values))
    if method == "trimmed_mean":
        return float(_trimmed_mean(values, frac=trimmed_fraction))
    # weighted_mean handled elsewhere (needs weights)
    return float(sum(values) / len(values))


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _convert_unit_if_needed(value: float, unit: Optional[str]) -> float:
    """Convert supported units to canonical.

    Canonical:
    - temperature-like vars (temperature, dew point, heat index): °C
    - humidity: %

    If unit is unknown or already canonical, returns value unchanged.
    """
    if unit is None:
        return value

    u = unit.strip()
    if u in ("°F", "°f", "F", "degF", "fahrenheit"):
        return (value - 32.0) * (5.0 / 9.0)

    return value


# --- Psychrometric-ish computations (lightweight, deterministic) ---


def dew_point_c(t_c: float, rh_pct: float) -> float:
    """Dew point in °C from dry-bulb T (°C) and RH (%).

    Uses Magnus formula (good accuracy for typical indoor ranges).
    """
    # Guard
    rh = max(0.1, min(100.0, float(rh_pct)))
    t = float(t_c)

    # Magnus constants over water
    a = 17.62
    b = 243.12
    gamma = (a * t) / (b + t) + math.log(rh / 100.0)
    dp = (b * gamma) / (a - gamma)
    return float(dp)


def heat_index_c(t_c: float, rh_pct: float) -> float:
    """Heat index in °C from T (°C) and RH (%).

    Uses the Rothfusz regression (NOAA) in °F domain, then converts to °C.
    Valid primarily for warm/humid conditions (T >= ~26.7°C). Outside validity,
    returns T (i.e., 'feels like' approximately equal to air temperature).
    """
    t = float(t_c)
    rh = max(0.0, min(100.0, float(rh_pct)))

    if t < 26.7 or rh < 40.0:
        return t

    tf = t * 9.0 / 5.0 + 32.0

    hi_f = (
        -42.379
        + 2.04901523 * tf
        + 10.14333127 * rh
        - 0.22475541 * tf * rh
        - 0.00683783 * tf * tf
        - 0.05481717 * rh * rh
        + 0.00122874 * tf * tf * rh
        + 0.00085282 * tf * rh * rh
        - 0.00000199 * tf * tf * rh * rh
    )

    # Convert back to °C
    return float((hi_f - 32.0) * (5.0 / 9.0))


# -----------------------------
# SensorAggregator
# -----------------------------


class SensorAggregator:
    """Aggregates sensors according to a MappingConfig.

    Naming convention for computed variables
    - Zone variable:   "{zone}.{variable}"   (e.g., "living.indoor_temperature")
    - Derived variable: as provided by DerivedSpec (e.g., "global.indoor_temperature")

    Quality policy
    - Each sensor can be rejected for: unavailable, stale, non_numeric, out_of_range, time_outlier, rate_reject.
    - Rate limit can also clip values to preserve continuity.
    - Group can reject cross-sensor outliers.
    - Derived can be aggregate or compute.
    """

    def __init__(
            self, 
            entities_state_store: dict[str, State],
            mapping: MappingConfig
        ):
        self._entities_state_store = entities_state_store
        self._mapping = mapping

        # Flatten groups: zones
        self._groups: Dict[str, GroupSpec] = {}
        for z in mapping.zones:
            for g in z.variables:
                key = f"{z.zone}.{g.name}" if "." not in g.name else g.name
                if key in self._groups:
                    raise ValueError(f"Duplicate group name: {key}")
                self._groups[key] = GroupSpec(
                    name=key,
                    sensors=g.sensors,
                    method=g.method,
                    cross_outlier_method=g.cross_outlier_method,
                    cross_outlier_k=g.cross_outlier_k,
                    trimmed_fraction=g.trimmed_fraction,
                    min_sources=g.min_sources,
                    clamp_min=g.clamp_min,
                    clamp_max=g.clamp_max,
                    max_age=g.max_age,
                )

        self._derived: Dict[str, DerivedSpec] = {d.name: d for d in mapping.derived}

        # Runtime state per sensor entity
        self._sensor_rt: Dict[str, SensorRuntime] = {}
        for g in self._groups.values():
            for s in g.sensors:
                if s.entity_id not in self._sensor_rt:
                    self._sensor_rt[s.entity_id] = SensorRuntime(spec=s)

        self._latest: Dict[str, AggregatedValue] = {}
    
    # -------------------------
    # Public API
    # -------------------------

    def get(self, name: str) -> AggregatedValue:
        return self._latest.get(
            name,
            AggregatedValue(
                name=name,
                value=None,
                ts=None,
                sources_total=0,
                sources_used=0,
                sources_rejected=0,
                is_stale=True,
                is_insufficient=True,
                reasons=("not_computed",),
            ),
        )

    def latest_all(self) -> Dict[str, AggregatedValue]:
        return dict(self._latest)

    async def async_update(self) -> None:
        now = datetime.now(timezone.utc)

        # 1) Ingest sensors
        for _entity_id, rt in self._sensor_rt.items():
            self._update_sensor_from_hass(now=now, rt=rt)

        # 2) Compute base groups (zone variables)
        for group_name, gspec in self._groups.items():
            self._latest[group_name] = self._compute_group(now=now, gspec=gspec)

        # 3) Compute derived (globals / computed)
        for dname, dspec in self._derived.items():
            self._latest[dname] = self._compute_derived(now=now, dspec=dspec)

    # -------------------------
    # Sensor update pipeline
    # -------------------------

    def _update_sensor_from_hass(self, now: datetime, rt: SensorRuntime) -> None:
        spec = rt.spec
        f = spec.filters

        try:
            # state_obj = hass.states.get(spec.entity_id)
            state_obj = self._entities_state_store.get(spec.entity_id)
            if state_obj is None:
                rt.last_error = "entity_not_found"
                return

            raw_state = getattr(state_obj, "state", None)
            if raw_state in (None, "unknown", "unavailable", "none", "None", ""):
                rt.last_error = "unavailable"
                return

            try:
                raw_val = float(raw_state)
            except (TypeError, ValueError):
                rt.last_error = "non_numeric"
                return

            if not _is_finite(raw_val):
                rt.last_error = "non_finite"
                return

            ts = _ensure_tz(getattr(state_obj, "last_updated", None) or now)

            if f.max_age is not None and (now - ts) > f.max_age:
                rt.last_error = "stale"
                return

            # Unit conversion (optional)
            if f.auto_unit_convert:
                unit = None
                attrs = getattr(state_obj, "attributes", None)
                if isinstance(attrs, dict):
                    unit = attrs.get("unit_of_measurement")
                raw_val = _convert_unit_if_needed(raw_val, unit)

            # Range checks
            if f.min_valid is not None and raw_val < f.min_valid:
                rt.last_error = "below_min_valid"
                return
            if f.max_valid is not None and raw_val > f.max_valid:
                rt.last_error = "above_max_valid"
                return

            # Time-series outlier (Hampel on raw history)
            if f.time_hampel_k is not None and len(rt.raw_history) >= f.time_hampel_min_samples:
                hist_vals = [s.value for s in rt.raw_history]
                med = _median(hist_vals)
                mad = _mad(hist_vals, med)
                if mad != 0:
                    sigma = 1.4826 * mad
                    if abs(raw_val - med) > f.time_hampel_k * sigma:
                        rt.last_error = "time_outlier"
                        rt.rejected_count += 1
                        return

            # Rate limiting against last accepted raw
            candidate = raw_val
            if f.max_rate_per_min is not None and rt.last_raw is not None:
                dt_s = max(1.0, (ts - rt.last_raw.ts).total_seconds())
                dt_min = dt_s / 60.0
                max_delta = float(f.max_rate_per_min) * dt_min
                delta = candidate - rt.last_raw.value
                if abs(delta) > max_delta:
                    if f.rate_limit_mode == "reject":
                        rt.last_error = "rate_reject"
                        rt.rejected_count += 1
                        return
                    # clip
                    candidate = rt.last_raw.value + math.copysign(max_delta, delta)
                    rt.clipped_count += 1

            # Accept sample
            rt.last_error = None
            raw_sample = Sample(value=float(candidate), ts=ts)
            rt.last_raw = raw_sample
            rt.raw_history.append(raw_sample)
            while len(rt.raw_history) > f.raw_history_size:
                rt.raw_history.popleft()

            # EMA smoothing on accepted candidate
            prev_f = rt.last_filtered.value if rt.last_filtered else None
            filtered_val = _ema(prev_f, raw_sample.value, f.ema_alpha)
            filtered_sample = Sample(value=float(filtered_val), ts=ts)
            rt.last_filtered = filtered_sample
            rt.filtered_history.append(filtered_sample)
            while len(rt.filtered_history) > f.filtered_history_size:
                rt.filtered_history.popleft()

            # Rolling median on filtered history (optional)
            if f.rolling_median_window and f.rolling_median_window > 1:
                w = min(f.rolling_median_window, len(rt.filtered_history))
                if w >= 2:
                    last_vals = [s.value for s in list(rt.filtered_history)[-w:]]
                    med_val = _median(last_vals)
                    rt.last_filtered = Sample(value=float(med_val), ts=ts)

        except Exception as e:
            rt.last_error = f"exception:{type(e).__name__}"

    # -------------------------
    # Group computations
    # -------------------------

    def _compute_group(self, now: datetime, gspec: GroupSpec) -> AggregatedValue:
        used: List[Tuple[SensorSpec, float, datetime]] = []
        rejected: List[str] = []
        reasons: List[str] = []

        for ss in gspec.sensors:
            rt = self._sensor_rt.get(ss.entity_id)
            if rt is None:
                rejected.append(f"{ss.entity_id}:not_registered")
                continue

            if rt.last_error is not None:
                rejected.append(f"{ss.entity_id}:{rt.last_error}")
                continue

            if rt.last_filtered is None:
                rejected.append(f"{ss.entity_id}:no_data")
                continue

            used.append((ss, float(rt.last_filtered.value), rt.last_filtered.ts))

        sources_total = len(gspec.sensors)
        sources_used = len(used)

        if sources_used == 0:
            return AggregatedValue(
                name=gspec.name,
                value=None,
                ts=None,
                sources_total=sources_total,
                sources_used=0,
                sources_rejected=sources_total,
                is_stale=True,
                is_insufficient=True,
                reasons=("no_valid_sources",) + tuple(rejected),
            )

        group_ts = max(ts for _, _, ts in used)

        # Cross-sensor outliers
        vals = [v for _, v, _ in used]
        mask = [True] * len(vals)
        if gspec.cross_outlier_method == "hampel":
            mask = _hampel_keep_mask(vals, k=gspec.cross_outlier_k)
        elif gspec.cross_outlier_method == "zscore":
            mask = _zscore_keep_mask(vals, k=gspec.cross_outlier_k)

        used2: List[Tuple[SensorSpec, float, datetime]] = []
        if gspec.cross_outlier_method != "none":
            for keep, tup in zip(mask, used):
                if keep:
                    used2.append(tup)
                else:
                    ss, _v, _ts = tup
                    rejected.append(f"{ss.entity_id}:cross_outlier")
                    rt = self._sensor_rt.get(ss.entity_id)
                    if rt:
                        rt.rejected_count += 1
            if len(used2) != len(used):
                reasons.append("cross_outliers_rejected")
        else:
            used2 = used

        sources_used2 = len(used2)
        sources_rejected = sources_total - sources_used2

        is_insufficient = sources_used2 < max(1, gspec.min_sources)
        if is_insufficient:
            reasons.append("insufficient_sources")

        if sources_used2 == 0:
            return AggregatedValue(
                name=gspec.name,
                value=None,
                ts=group_ts,
                sources_total=sources_total,
                sources_used=0,
                sources_rejected=sources_total,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + ["no_sources_after_cross_filter"] + rejected),
            )

        # Aggregate
        values2 = [v for _, v, _ in used2]

        if gspec.method in ("max", "min", "median", "trimmed_mean"):
            agg = _agg(values2, method=gspec.method, trimmed_fraction=gspec.trimmed_fraction)
        else:  # weighted_mean
            num = 0.0
            den = 0.0
            for ss, v, _ in used2:
                w = ss.weight if ss.weight > 0 else 0.0
                num += w * v
                den += w
            agg = (num / den) if den > 0 else _median(values2)

        agg = _clamp(float(agg), gspec.clamp_min, gspec.clamp_max)

        # Staleness for group
        if gspec.max_age is not None:
            max_age = gspec.max_age
        else:
            ages = [ss.filters.max_age for ss, _, _ in used2 if ss.filters.max_age is not None]
            max_age = min(ages) if ages else timedelta(minutes=15)

        is_stale = (now - group_ts) > max_age
        if is_stale:
            reasons.append("group_stale")

        return AggregatedValue(
            name=gspec.name,
            value=float(agg),
            ts=group_ts,
            sources_total=sources_total,
            sources_used=sources_used2,
            sources_rejected=sources_rejected,
            is_stale=is_stale,
            is_insufficient=is_insufficient,
            reasons=tuple(reasons + rejected),
        )

    def _compute_derived(self, now: datetime, dspec: DerivedSpec) -> AggregatedValue:
        if dspec.kind == "compute":
            return self._compute_derived_compute(now=now, dspec=dspec)
        return self._compute_derived_aggregate(now=now, dspec=dspec)

    def _compute_derived_aggregate(self, now: datetime, dspec: DerivedSpec) -> AggregatedValue:
        used_vals: List[Tuple[float, float, datetime]] = []  # (value, weight, ts)
        rejected: List[str] = []
        reasons: List[str] = []

        for gname, w in dspec.inputs:
            av = self._latest.get(gname)
            if av is None or av.value is None or av.ts is None:
                rejected.append(f"{gname}:missing")
                continue
            if av.is_stale:
                rejected.append(f"{gname}:stale")
                continue
            if av.is_insufficient:
                rejected.append(f"{gname}:insufficient")
                continue
            used_vals.append((float(av.value), float(w), av.ts))

        sources_total = len(dspec.inputs)
        sources_used = len(used_vals)
        sources_rejected = sources_total - sources_used

        if sources_used == 0:
            return AggregatedValue(
                name=dspec.name,
                value=None,
                ts=None,
                sources_total=sources_total,
                sources_used=0,
                sources_rejected=sources_total,
                is_stale=True,
                is_insufficient=True,
                reasons=("no_valid_inputs",) + tuple(rejected),
            )

        ts = max(t for _, _, t in used_vals)

        is_insufficient = sources_used < max(1, dspec.min_sources)
        if is_insufficient:
            reasons.append("insufficient_sources")

        values = [v for v, _, _ in used_vals]

        if dspec.method == "median":
            agg = _median(values)
        elif dspec.method == "trimmed_mean":
            agg = _trimmed_mean(values, frac=dspec.trimmed_fraction)
        else:
            num = sum(v * w for v, w, _ in used_vals if w > 0)
            den = sum(w for _, w, _ in used_vals if w > 0)
            agg = (num / den) if den > 0 else _median(values)

        agg = _clamp(float(agg), dspec.clamp_min, dspec.clamp_max)

        max_age = dspec.max_age if dspec.max_age is not None else timedelta(minutes=15)
        is_stale = (now - ts) > max_age
        if is_stale:
            reasons.append("derived_stale")

        return AggregatedValue(
            name=dspec.name,
            value=float(agg),
            ts=ts,
            sources_total=sources_total,
            sources_used=sources_used,
            sources_rejected=sources_rejected,
            is_stale=is_stale,
            is_insufficient=is_insufficient,
            reasons=tuple(reasons + rejected),
        )

    def _compute_derived_compute(self, now: datetime, dspec: DerivedSpec) -> AggregatedValue:
        """Compute a derived metric from other group outputs.

        Supported computes:
        - dew_point_c(T, RH)
        - heat_index_c(T, RH)

        Inputs are read in order. For these computes we expect at least 2 inputs.
        """
        rejected: List[str] = []
        reasons: List[str] = []

        if dspec.compute not in ("dew_point_c", "heat_index_c"):
            return AggregatedValue(
                name=dspec.name,
                value=None,
                ts=None,
                sources_total=len(dspec.inputs),
                sources_used=0,
                sources_rejected=len(dspec.inputs),
                is_stale=True,
                is_insufficient=True,
                reasons=("unknown_compute", str(dspec.compute)),
            )

        # Gather input values
        inputs_vals: List[Tuple[str, float, datetime]] = []
        for gname, _w in dspec.inputs:
            av = self._latest.get(gname)
            if av is None or av.value is None or av.ts is None:
                rejected.append(f"{gname}:missing")
                continue
            if av.is_stale:
                rejected.append(f"{gname}:stale")
                continue
            if av.is_insufficient:
                rejected.append(f"{gname}:insufficient")
                continue
            inputs_vals.append((gname, float(av.value), av.ts))

        sources_total = len(dspec.inputs)
        sources_used = len(inputs_vals)
        sources_rejected = sources_total - sources_used

        is_insufficient = sources_used < max(2, dspec.min_sources)
        if is_insufficient:
            reasons.append("insufficient_sources")

        if sources_used < 2:
            return AggregatedValue(
                name=dspec.name,
                value=None,
                ts=None,
                sources_total=sources_total,
                sources_used=sources_used,
                sources_rejected=sources_rejected,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + ["need_two_inputs"] + rejected),
            )

        # For now we interpret first input as T and second as RH (simple, explicit).
        t = inputs_vals[0][1]
        rh = inputs_vals[1][1]
        ts = max(ti for _, _, ti in inputs_vals)

        try:
            if dspec.compute == "dew_point_c":
                val = dew_point_c(t, rh)
            else:
                val = heat_index_c(t, rh)
        except Exception as e:
            return AggregatedValue(
                name=dspec.name,
                value=None,
                ts=ts,
                sources_total=sources_total,
                sources_used=sources_used,
                sources_rejected=sources_rejected,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + [f"compute_exception:{type(e).__name__}"] + rejected),
            )

        val = _clamp(float(val), dspec.clamp_min, dspec.clamp_max)

        max_age = dspec.max_age if dspec.max_age is not None else timedelta(minutes=15)
        is_stale = (now - ts) > max_age
        if is_stale:
            reasons.append("derived_stale")

        return AggregatedValue(
            name=dspec.name,
            value=float(val),
            ts=ts,
            sources_total=sources_total,
            sources_used=sources_used,
            sources_rejected=sources_rejected,
            is_stale=is_stale,
            is_insufficient=is_insufficient,
            reasons=tuple(reasons + rejected),
        )


# -----------------------------
# Example mapping (edit for your installation)
# -----------------------------


def example_mapping_with_dewpoint_and_heatindex() -> MappingConfig:
    """Example with two zones + computed dew point and heat index.

    Notes
    - If you already have HA sensors for dew point/heat index, you can also ingest them as groups.
    - This example computes them from zone T/RH to keep everything consistent.
    """

    temp_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        min_valid=5.0,
        max_valid=35.0,
        time_hampel_k=4.0,
        max_rate_per_min=0.6,  # °C/min
        rate_limit_mode="clip",
        ema_alpha=0.2,
        rolling_median_window=3,
    )

    rh_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=4.0,
        max_rate_per_min=5.0,  # %RH/min
        rate_limit_mode="clip",
        ema_alpha=0.25,
        rolling_median_window=3,
    )

    living = ZoneConfig(
        zone="living",
        weight=1.0,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                method="weighted_mean",
                sensors=(SensorSpec("sensor.livingroom_temperature", weight=1.0, filters=temp_filters),),
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                method="median",
                sensors=(SensorSpec("sensor.livingroom_humidity", weight=1.0, filters=rh_filters),),
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
            # OPTIONAL: ingest HA dew point sensor (if you have it)
            # GroupSpec(
            #     name="indoor_dew_point",
            #     method="weighted_mean",
            #     sensors=(SensorSpec("sensor.livingroom_dewpoint", weight=1.0, filters=temp_filters),),
            #     min_sources=1,
            #     clamp_min=-10.0,
            #     clamp_max=30.0,
            # ),
        ),
    )

    bedroom = ZoneConfig(
        zone="bedroom",
        weight=0.7,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                method="weighted_mean",
                sensors=(SensorSpec("sensor.bedroom_temperature", weight=1.0, filters=temp_filters),),
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                method="median",
                sensors=(SensorSpec("sensor.bedroom_humidity", weight=1.0, filters=rh_filters),),
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    derived = (
        # Global aggregates (from per-zone groups)
        DerivedSpec(
            name="global.indoor_temperature",
            kind="aggregate",
            inputs=(("living.indoor_temperature", 1.0), ("bedroom.indoor_temperature", 0.7)),
            method="weighted_mean",
            min_sources=1,
            clamp_min=5.0,
            clamp_max=35.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_humidity",
            kind="aggregate",
            inputs=(("living.indoor_humidity", 1.0), ("bedroom.indoor_humidity", 0.7)),
            method="median",
            min_sources=1,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=10),
        ),

        # If you already have dew point / heat index sensors per zone, you can aggregate them.
        # For HVAC safety (condensation / dehumidify trigger), a conservative choice is MAX across zones.
        DerivedSpec(
            name="global.indoor_dew_point",
            kind="aggregate",
            inputs=(("living.indoor_dew_point", 1.0), ("bedroom.indoor_dew_point", 0.7)),
            method="max",
            min_sources=1,
            clamp_min=-20.0,
            clamp_max=30.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_heat_index",
            kind="aggregate",
            inputs=(("living.indoor_heat_index", 1.0), ("bedroom.indoor_heat_index", 0.7)),
            method="max",
            min_sources=1,
            clamp_min=-20.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
        ),

        # OPTIONAL: you can still compute dew point/heat index from global T/RH as a cross-check.
        # (Keep disabled if you don't need it.)
        # DerivedSpec(
        #     name="global.indoor_dew_point_calc",
        #     kind="compute",
        #     compute="dew_point_c",
        #     inputs=(("global.indoor_temperature", 1.0), ("global.indoor_humidity", 1.0)),
        #     min_sources=2,
        #     clamp_min=-20.0,
        #     clamp_max=30.0,
        #     max_age=timedelta(minutes=10),
        # ),
    )

    return MappingConfig(zones=(living, bedroom), derived=derived)



# =============================
# 3) ClimateRegimeEstimator
# =============================

Regime = Literal["heating", "cooling", "shoulder"]


@dataclass(frozen=True)
class RegimeConfig:
    """Configuration for climate regime estimation.

    This estimator is intentionally conservative: it changes regime only when
    a *running mean* of outdoor temperature crosses thresholds with hysteresis.

    Parameters you will likely tune onsite:
    - tau_days: smoothing time constant for running mean
    - heating_on/off, cooling_on/off: thresholds for regime selection
    - min_switch_interval: minimum time between regime changes (anti-flapping)
    """

    # Running-mean smoothing time constant
    tau_days: float = 5.0

    # Thresholds (°C) applied to running mean of outdoor temperature
    heating_on: float = 15.0
    heating_off: float = 16.5

    cooling_on: float = 20.0
    cooling_off: float = 18.5

    # Anti-flapping: minimum time between regime switches
    min_switch_interval: timedelta = timedelta(hours=8)

    # If outdoor temperature is missing/stale, keep last regime but decay confidence
    stale_grace: timedelta = timedelta(minutes=60)

    # Optional clamp for outdoor temperature to avoid poisoning the running mean
    outdoor_clamp_min: float = -40.0
    outdoor_clamp_max: float = 60.0


@dataclass
class ClimateRegimeState:
    regime: Regime
    ts: datetime

    outdoor_temp: Optional[float] = None
    running_mean_outdoor: Optional[float] = None

    last_switch_ts: Optional[datetime] = None

    # 0..1 quality indicator
    confidence: float = 0.0

    reasons: Tuple[str, ...] = ()


class ClimateRegimeEstimator:
    """Estimates the current climate regime (heating/cooling/shoulder).

    Inputs
    - Preferred: a *global* outdoor temperature from SensorAggregator, e.g. "global.outdoor_temperature".

    Algorithm
    - Maintains an exponential running mean of outdoor temperature with time constant tau_days.
    - Applies hysteresis thresholds on the running mean to decide regime.
    - Enforces a minimum switch interval.

    Why running mean?
    - Outdoor instantaneous temperature is noisy and affected by solar radiation/placement.
    - HVAC seasonal behavior should track climate, not hourly weather.
    """

    def __init__(self, config: RegimeConfig = RegimeConfig(), initial_regime: Regime = "shoulder"):
        self._cfg = config
        self._state = ClimateRegimeState(
            regime=initial_regime,
            ts=datetime.now(timezone.utc),
            outdoor_temp=None,
            running_mean_outdoor=None,
            last_switch_ts=None,
            confidence=0.0,
            reasons=("init",),
        )

    @property
    def state(self) -> ClimateRegimeState:
        return self._state

    def update_from_aggregator(self, agg: "SensorAggregator", outdoor_name: str = "global.outdoor_temperature") -> ClimateRegimeState:
        """Convenience wrapper using the SensorAggregator output."""
        av = agg.get(outdoor_name)
        now = datetime.now(timezone.utc)

        if av.value is None or av.ts is None:
            return self._keep_last(now, reason=f"missing:{outdoor_name}")

        # Treat stale/insufficient as weak input, but we may still keep previous regime.
        if av.is_stale:
            # if within grace, we still accept but reduce confidence
            if (now - av.ts) <= self._cfg.stale_grace:
                return self.update(outdoor_temp=float(av.value), ts=av.ts, quality=0.4, reasons=("stale_within_grace",))
            return self._keep_last(now, reason=f"stale:{outdoor_name}")

        if av.is_insufficient:
            return self.update(outdoor_temp=float(av.value), ts=av.ts, quality=0.6, reasons=("insufficient_sources",))

        return self.update(outdoor_temp=float(av.value), ts=av.ts, quality=1.0, reasons=("ok",))

    def update(
        self,
        outdoor_temp: float,
        ts: Optional[datetime] = None,
        quality: float = 1.0,
        reasons: Tuple[str, ...] = (),
    ) -> ClimateRegimeState:
        """Update estimator with a new outdoor temperature sample."""
        now = _ensure_tz(ts or datetime.now(timezone.utc))

        # Clamp to avoid poisoning the filter
        t_out = _clamp(float(outdoor_temp), self._cfg.outdoor_clamp_min, self._cfg.outdoor_clamp_max)

        # Update running mean with a continuous-time EMA
        rm_prev = self._state.running_mean_outdoor
        ts_prev = self._state.ts

        dt_s = max(1.0, (now - ts_prev).total_seconds())
        tau_s = max(60.0, float(self._cfg.tau_days) * 86400.0)
        alpha = 1.0 - math.exp(-dt_s / tau_s)

        rm = t_out if rm_prev is None else (alpha * t_out + (1.0 - alpha) * rm_prev)

        # Decide regime with hysteresis + minimum switch interval
        new_regime, decision_reasons = self._decide_regime(rm=rm, now=now)

        # Confidence: based on input quality and how far rm is from boundary
        conf = self._confidence(rm=rm, quality=quality, regime=new_regime)

        self._state = ClimateRegimeState(
            regime=new_regime,
            ts=now,
            outdoor_temp=t_out,
            running_mean_outdoor=float(rm),
            last_switch_ts=self._state.last_switch_ts,
            confidence=float(conf),
            reasons=tuple(reasons) + tuple(decision_reasons),
        )

        return self._state

    # -------------------------
    # Internals
    # -------------------------

    def _keep_last(self, now: datetime, reason: str) -> ClimateRegimeState:
        """Keep last regime; decay confidence."""
        prev = self._state
        decayed = max(0.0, prev.confidence * 0.8)
        self._state = ClimateRegimeState(
            regime=prev.regime,
            ts=now,
            outdoor_temp=prev.outdoor_temp,
            running_mean_outdoor=prev.running_mean_outdoor,
            last_switch_ts=prev.last_switch_ts,
            confidence=decayed,
            reasons=("keep_last", reason),
        )
        return self._state

    def _can_switch(self, now: datetime) -> bool:
        if self._state.last_switch_ts is None:
            return True
        return (now - self._state.last_switch_ts) >= self._cfg.min_switch_interval

    def _mark_switched(self, now: datetime) -> None:
        self._state.last_switch_ts = now

    def _decide_regime(self, rm: float, now: datetime) -> Tuple[Regime, Tuple[str, ...]]:
        cfg = self._cfg
        cur = self._state.regime
        reasons: List[str] = []

        # Helper flags
        want_heat = rm <= cfg.heating_on
        want_cool = rm >= cfg.cooling_on

        can_switch = self._can_switch(now)
        if not can_switch:
            reasons.append("switch_lockout")

        new = cur

        if cur == "heating":
            # exit heating only after rm rises above heating_off
            if rm >= cfg.heating_off and can_switch:
                new = "shoulder"
                reasons.append("exit_heating")
        elif cur == "cooling":
            # exit cooling only after rm drops below cooling_off
            if rm <= cfg.cooling_off and can_switch:
                new = "shoulder"
                reasons.append("exit_cooling")
        else:  # shoulder
            if want_heat and can_switch:
                new = "heating"
                reasons.append("enter_heating")
            elif want_cool and can_switch:
                new = "cooling"
                reasons.append("enter_cooling")

        if new != cur:
            # record switch time
            self._state.last_switch_ts = now
            reasons.append("switched")

        # Add boundary context
        reasons.append(f"rm={rm:.2f}")

        return new, tuple(reasons)

    def _confidence(self, rm: float, quality: float, regime: Regime) -> float:
        """Heuristic confidence score.

        - Starts from input quality.
        - Increases when rm is far from the nearest relevant boundary.
        """
        cfg = self._cfg
        q = max(0.0, min(1.0, float(quality)))

        if regime == "heating":
            d = max(0.0, cfg.heating_off - rm)  # margin before exit
        elif regime == "cooling":
            d = max(0.0, rm - cfg.cooling_off)
        else:
            # shoulder confidence highest mid-band
            d1 = abs(rm - cfg.heating_on)
            d2 = abs(rm - cfg.cooling_on)
            d = min(d1, d2)

        # Map °C margin to 0..1 (0°C => 0, 3°C => ~1)
        margin = max(0.0, min(1.0, d / 3.0))
        return float(0.2 * q + 0.8 * q * margin)


