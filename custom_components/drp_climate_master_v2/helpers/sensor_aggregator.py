"""
Sensor aggregation + mapping layer for HVAC control (Home Assistant friendly).

Key updates (2026-01 hold-last-good + HA timestamp quirks)
- HA may NOT update last_updated/last_reported when state doesn't change.
  So we DO NOT use HA timestamps as freshness for HVAC decisions by default.
- Freshness is based on "last successful numeric read" (observed timestamp = now).
- Transient Modbus/HA issues (unavailable/non_numeric/stale_source/etc.) trigger HOLD-LAST-GOOD
  for a configurable grace window, instead of collapsing groups to None immediately.

What this module provides
- Robust sensor ingestion from Home Assistant entities.
- Per-sensor filtering pipeline (range, staleness (optional source-based), unit conversion,
  time-outliers, rate limiting, EMA, rolling median).
- Mapping layer: entities -> logical variables -> zones -> optional global/derived variables.
- Optional computed derived variables (e.g., dew point, heat index, MRT, T_op).

Important semantics
- "observed_ts" = time when aggregator reads a valid numeric state (now, UTC). Used for freshness.
- "source_ts"    = HA-provided timestamps (last_reported/last_updated). Used only optionally
                  (max_source_age) as a hard guard; NOT relied upon by default.

Notes
- Home Assistant state objects are expected to expose: state (str), last_updated (datetime), attributes (dict).
  Some builds also expose last_reported; we use it if present, defensively.
- Datetimes from HA are usually tz-aware; we defensively handle naive timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Deque, Dict, List, Literal, Optional, Tuple
import logging
import math
from collections import deque

from homeassistant.core import State
from homeassistant.util import dt as dt_util

from .logger import log_debug, log_warning

# -----------------------------
# Types
# -----------------------------

_LOGGER = logging.getLogger(__name__)

Number = float

class AggregationMethod(StrEnum):
    WEIGHTED_MEAN = "weighted_mean"
    MEDIAN = "median"
    TRIMMED_MEAN = "trimmed_mean"
    MAX = "max"
    MIN = "min"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, v: str | AggregationMethod) -> AggregationMethod:
        if isinstance(v, cls):
            return v
        try:
            return cls(v)
        except ValueError as ex:
            raise ValueError(f"Invalid {cls.__name__}: {v!r}. Allowed: {cls.values()}") from ex


class CrossOutlierMethod(StrEnum):
    NONE = "none"
    HAMPEL = "hampel"
    ZSCORE = "zscore"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, v: str | CrossOutlierMethod) -> CrossOutlierMethod:
        if isinstance(v, cls):
            return v
        try:
            return cls(v)
        except ValueError as ex:
            raise ValueError(f"Invalid {cls.__name__}: {v!r}. Allowed: {cls.values()}") from ex


class RateLimitMode(StrEnum):
    CLIP = "clip"
    REJECT = "reject"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, v: str | RateLimitMode) -> RateLimitMode:
        if isinstance(v, cls):
            return v
        try:
            return cls(v)
        except ValueError as ex:
            raise ValueError(f"Invalid {cls.__name__}: {v!r}. Allowed: {cls.values()}") from ex


class DerivedKind(StrEnum):
    AGGREGATE = "aggregate"
    COMPUTE = "compute"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, v: str | DerivedKind) -> DerivedKind:
        if isinstance(v, cls):
            return v
        try:
            return cls(v)
        except ValueError as ex:
            raise ValueError(f"Invalid {cls.__name__}: {v!r}. Allowed: {cls.values()}") from ex


class ComputeFn(StrEnum):
    DEW_POINT_C = "dew_point_c"
    HEAT_INDEX_C = "heat_index_c"
    MRT_C = "mrt_c"
    MRT_GATED_C = "mrt_gated_c"
    T_OP_C = "t_op_c"
    CONDENSATION_MARGIN_C = "condensation_margin_c"
    AND01 = "and01"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, v: str | ComputeFn) -> ComputeFn:
        if isinstance(v, cls):
            return v
        try:
            return cls(v)
        except ValueError as ex:
            raise ValueError(f"Invalid {cls.__name__}: {v!r}. Allowed: {cls.values()}") from ex


# -----------------------------
# Numerics
# -----------------------------
# Float epsilon used to decide whether an input is "effectively unchanged".
EPS_UNCHANGED = 1e-6


# -----------------------------
# Specs (configuration)
# -----------------------------


@dataclass(frozen=True)
class FilterConfig:
    """Per-sensor filtering pipeline parameters.

    Freshness and resilience
    - max_age: how long a LAST-GOOD reading remains "fresh" (for groups/derived) before being stale.
              IMPORTANT: This is NOT based on HA timestamps; it's based on observed_ts (now) when
              the aggregator successfully reads a numeric state.
    - hold_last_good: additional grace window during which last-good value can be used even if the
              sensor becomes unavailable/non_numeric/etc. (Modbus glitches, HA transient issues).
    - max_source_age: OPTIONAL hard guard based on HA timestamps (last_reported/last_updated).
              Leave None by default because HA may not update timestamps when state doesn't change.
    """

    # If True, attempt unit conversion based on HA unit_of_measurement.
    # Supported: °C/°F for temperature-like variables; % for humidity.
    auto_unit_convert: bool = True

    # Freshness window (observed_ts-based; see note above)
    max_age: Optional[timedelta] = timedelta(minutes=15)

    # Grace window for hold-last-good when sensor errors occur
    hold_last_good: timedelta = timedelta(minutes=5)

    # OPTIONAL: Hard staleness based on HA timestamps (source_ts). Default None.
    max_source_age: Optional[timedelta] = None

    # Basic physical plausibility range (applied after unit conversion)
    min_valid: Optional[float] = None
    max_valid: Optional[float] = None

    # Time-series outlier filter (Hampel on sensor history)
    time_hampel_k: Optional[float] = 4.0
    time_hampel_min_samples: int = 8

    # Rate limiting: prevent spikes (bad packets, bogus reads). Units: value per minute.
    max_rate_per_min: Optional[float] = None
    rate_limit_mode: RateLimitMode = RateLimitMode.CLIP  # clip keeps continuity; reject creates gaps

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

    method: AggregationMethod = AggregationMethod.WEIGHTED_MEAN

    # Cross-sensor outlier handling at the current instant
    cross_outlier_method: CrossOutlierMethod = CrossOutlierMethod.HAMPEL
    cross_outlier_k: float = 3.0

    # Trimmed mean (only used if method='trimmed_mean')
    trimmed_fraction: float = 0.2  # drop 20% low + 20% high

    min_sources: int = 1

    clamp_min: Optional[float] = None
    clamp_max: Optional[float] = None

    # Optional group-level max_age; if None, uses strictest max_age among used sensors
    max_age: Optional[timedelta] = None

    # Optional group-level grace; if None, uses strictest (min) hold_last_good among used sensors
    hold_last_good: Optional[timedelta] = None


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

    Staleness
    - Derived staleness is evaluated on the max(ts) of used inputs, plus dspec.max_age.
      Since group freshness uses observed_ts, derived freshness inherits that behavior.
    """

    name: str  # e.g., "global.indoor_temperature"
    inputs: Tuple[Tuple[str, float], ...]

    kind: DerivedKind = DerivedKind.AGGREGATE
    compute: Optional[ComputeFn] = None

    method: AggregationMethod = AggregationMethod.WEIGHTED_MEAN
    trimmed_fraction: float = 0.2

    min_sources: int = 1
    clamp_min: Optional[float] = None
    clamp_max: Optional[float] = None

    max_age: Optional[timedelta] = None
    # Optional derived-level "hold last good".
    # If derived cannot be computed (missing/stale/insufficient inputs or compute error),
    # reuse previous derived value for up to (max_age + hold_last_good).
    # Leave None to disable (preserves legacy behavior).
    hold_last_good: Optional[timedelta] = None


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
    ts: datetime  # observed timestamp (freshness), NOT HA source timestamp


@dataclass
class SensorRuntime:
    spec: SensorSpec

    raw_history: Deque[Sample] = field(default_factory=deque)
    filtered_history: Deque[Sample] = field(default_factory=deque)

    last_raw: Optional[Sample] = None
    last_filtered: Optional[Sample] = None

    # Freshness / diagnostics
    last_seen_ok: Optional[datetime] = None          # when we last read a valid numeric state (observed)
    last_source_ts_seen: Optional[datetime] = None   # latest HA timestamp observed (diagnostic)
    last_source_ts_ok: Optional[datetime] = None     # HA timestamp associated to last accepted value (diagnostic)

    last_error: Optional[str] = None
    rejected_count: int = 0
    clipped_count: int = 0


@dataclass
class AggregatedValue:
    name: str
    value: Optional[float]
    ts: Optional[datetime]  # observed timestamp of the aggregated value (freshness)

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
    core = s[k: n - k] if (n - 2 * k) > 0 else s
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

def _ensure_utc(ts: datetime) -> datetime:
    # Se naive: assumo timezone di HA (Europe/Rome nel tuo caso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    # Normalizzo sempre a UTC
    return dt_util.as_utc(ts)

def _source_ts(state_obj: State, now_utc: datetime) -> tuple[datetime | None, datetime | None, datetime]:
    """Best-effort HA timestamp for diagnostics/hard guards.

    Prefer last_reported if present, else last_updated, else now.
    """
    last_reported = getattr(state_obj, "last_reported", None)
    last_updated = getattr(state_obj, "last_updated", None)

    # ts = (
    #     getattr(state_obj, "last_reported", None)
    #     or getattr(state_obj, "last_updated", None)
    #     or now_utc
    # )
    # return _ensure_utc(ts)

    return (
        _ensure_utc(last_reported) if last_reported else None,
        _ensure_utc(last_updated) if last_updated else None,
        now_utc
    )

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

# --- Condensation margin (dew point guard) -----------------------------------

# Surface is usually slightly warmer than mean water temp in cooling mode.
# Keep conservative (small) so margin stays "safer" (smaller margin => earlier guard).
DEFAULT_SURFACE_OFFSET_C = 1.0  # °C

def condensation_margin_c(
    radiant_mean_temp_c: float,
    dew_point_c: float,
    surface_offset_c: float = DEFAULT_SURFACE_OFFSET_C,
) -> float:
    """
    Condensation safety margin [°C].

    Positive => surface estimated above dew point (safe).
    Near 0   => borderline.
    Negative => likely condensation.

    Inputs:
      - radiant_mean_temp_c: mean radiant water temp (e.g. global.radiant_mean_temperature)
      - dew_point_c: air dew point (zone or global)
      - surface_offset_c: empirical offset (surface ≈ water_mean + offset)
    """
    # If upstream guarantees floats, this is enough; keep defensive anyway.
    if radiant_mean_temp_c is None or dew_point_c is None:
        log_warning(
            _LOGGER, 
            "condensation_margin_c missing inputs [radiant_mean_temp_c=%s, dew_point_c=%s, surface_offset_c=%s]",
            radiant_mean_temp_c, dew_point_c, surface_offset_c
        )
        raise ValueError("condensation_margin_c requires two valid numeric inputs")

    return (float(radiant_mean_temp_c) + float(surface_offset_c)) - float(dew_point_c)


def dew_point_c(t_c: float, rh_pct: float) -> float:
    """Punto di rugiada (°C) da T bulbo-secco (°C) e UR (%) — formula di Magnus.

    ATTENZIONE — stub interno al compute engine di SensorAggregator.
    Questa funzione è invocata dal dispatch ComputeFn in _compute_derived()
    ed è intenzionalmente priva di dipendenze esterne: sensor_aggregator deve
    restare importabile anche se psychrolib non è disponibile, così una
    mancata installazione produce degradazione localizzata (solo le derived
    che usano psychrolib) invece di un ImportError che blocca l'intero modulo.

    Per il calcolo del dew point nei layer di policy (vmc/policy.py,
    signals/builder.py, dew_guard) usare helpers.psychrometric.dew_point_celsius
    che usa Buck via psychroLib. La differenza rispetto a Magnus è < 0.14 °C
    nel range HVAC (entrambe rientrano nei margini anti-condensa di §3.2).

    Gestione edge-case: rh viene clampato silenziosamente a [0.1, 100].
    rh=0 produce dp ≈ -57 °C senza eccezione (il dispatch ha try/except,
    ma il clamp evita math.log(0)).
    """
    rh = max(0.1, min(100.0, float(rh_pct)))
    t = float(t_c)

    a = 17.62
    b = 243.12
    gamma = (a * t) / (b + t) + math.log(rh / 100.0)
    dp = (b * gamma) / (a - gamma)
    return float(dp)


def heat_index_c(t_c: float, rh_pct: float) -> float:
    """Heat index (°C) da T (°C) e UR (%) — regressione di Rothfusz (NOAA).

    Stub interno al compute engine di SensorAggregator (vedi nota in dew_point_c).
    Usato come sensore diagnostico di disagio termico estivo, non come input
    del controllo attivo.

    Differenza rispetto a helpers.psychrometric.heat_index_celsius:
    questa implementazione applica la regressione Rothfusz direttamente senza
    lo step preliminare Steadman e senza gli aggiustamenti NWS per RH < 13%
    o RH > 85%. La differenza è ≤ 0.25 °C nel range residenziale (RH 40–90%,
    T 27–35 °C): irrilevante per uso diagnostico.

    Validità: T ≥ 26.7 °C e RH ≥ 40 %; al di fuori ritorna t_c.
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

    return float((hi_f - 32.0) * (5.0 / 9.0))


# ---------------------------------------------------------------------------
# Costante fisica per il modello MRT (soffitto radiante)
# ---------------------------------------------------------------------------
# Valore: 0.20  (adimensionale, range utile 0.15–0.30)
#
# Origine fisica — due contributi combinati:
#
#  1) Fattore di vista persona → soffitto (F_soffitto):
#     Per una persona in piedi in una stanza tipica (altezza 2.7 m, superficie
#     soffitto ≈ 15–20 m²), il fattore di vista corpo umano → soffitto è
#     circa 0.18–0.22 (valori tabulati ISO 7726:1998, Annex A).
#     Il soffitto è l'unica superficie a temperatura diversa da T_aria; le
#     pareti e il pavimento sono assunti a T_aria (worst-case conservativo).
#
#  2) Salto termico acqua → superficie pannello (ΔT_sup):
#     T_acqua_media (mandata+ritorno)/2 è circa 1–3°C più fredda di T_aria
#     lato pannello in raffrescamento (resistenza intonaco + massetto leggero).
#     Questo salto riduce l'effetto reale della MRT rispetto alla T_acqua.
#     Il k=0.20 lo compensa in modo conservativo: sottostima la MRT radiante,
#     il che è SAFE per l'anti-condensa (margine aggiuntivo).
#
# Limiti del modello:
#  - T_rad_mean è la media idraulica (mandata+ritorno), NON la T_superficie.
#    Pertanto questa formula NON implementa la MRT di ISO 7726 (che richiederebbe
#    T_superficie e fattori di vista esatti per ogni elemento dell'involucro).
#  - La MRT è condivisa tra tutte le zone (T_acqua del circuito è globale).
#    Zone con valvola chiusa sono corrette al gating; ma due zone aperte con
#    aree soffitto molto diverse ricevono la stessa T_rad_mean.
#  - Per condizioni HVAC indoor tipiche (T_aria 24–28°C, T_acqua 16–22°C) l'errore
#    rispetto a una MRT misurata con globotermometro è dell'ordine di ±1–2°C,
#    che si propaga in ±0.5–1°C su T_op e ±0.1–0.2 PMV: accettabile per il
#    controllo di comfort, non per certificazione energetica.
#
# Per variare: aggiungere k_rad come campo configurabile per area in AreaConfig.
# ---------------------------------------------------------------------------
MRT_K_RAD_DEFAULT: float = 0.20


def mrt_c(t_air_c: float, t_rad_mean_c: float, k_rad: float = MRT_K_RAD_DEFAULT) -> float:
    """Stima della Mean Radiant Temperature (MRT) in °C — soffitto radiante.

    Implementa una mistura lineare parametrica:
        MRT = T_aria + k_rad × (T_rad_media − T_aria)

    dove T_rad_media è la media idraulica (mandata+ritorno) del circuito radiante.
    NON è la MRT di ISO 7726 (che richiede T_superfici e fattori di vista).
    Vedere la docstring di MRT_K_RAD_DEFAULT per la giustificazione fisica di k_rad.

    Args:
        t_air_c:      Temperatura aria zona (°C).
        t_rad_mean_c: Media idraulica mandata/ritorno circuito radiante (°C).
        k_rad:        Coefficiente di influenza radiante [0–1], default MRT_K_RAD_DEFAULT.

    Returns:
        MRT stimata in °C.
    """
    k = max(0.0, min(1.0, float(k_rad)))
    t_air = float(t_air_c)
    t_rad = float(t_rad_mean_c)
    return float(t_air + k * (t_rad - t_air))


def t_op_c(t_air_c: float, mrt_c_val: float) -> float:
    """Temperatura operante in °C (regime a bassa velocità dell'aria, v < 0.2 m/s).

    Formula ISO 7730 semplificata: T_op = 0.5 × (T_aria + MRT).
    Valida per velocità aria < 0.2 m/s; a velocità maggiori il peso di T_aria
    cresce (v. formula completa ISO 7730 §A.2).

    Args:
        t_air_c:   Temperatura aria zona (°C).
        mrt_c_val: Mean Radiant Temperature stimata (°C), tipicamente da mrt_c().

    Returns:
        Temperatura operante in °C.
    """
    return float(0.5 * (float(t_air_c) + float(mrt_c_val)))


def mrt_gated_c(
    t_air_c: float,
    t_rad_mean_c: float,
    valve_open_01: float,
    pump_on_01: float,
    k_rad: float = MRT_K_RAD_DEFAULT,
) -> float:
    """Stima MRT in °C con gating stato circuito (valvola × pompa).

    Quando il circuito è inattivo (valvola chiusa o pompa ferma), la superficie
    del pannello tende a T_aria: il gating azzera l'effetto radiante in modo
    continuo moltiplicando k_rad per il prodotto valve × pump.

    MRT = T_aria + (k_rad × valve × pump) × (T_rad_media − T_aria)

    Se entrambi valve=1 e pump=1 → equivalente a mrt_c().
    Se valve=0 o pump=0 → MRT = T_aria (pannello passivo = parete neutra).

    Nota: valve_open_01 può essere il segnale composto «plant_active» (già
    prodotto di valve×pump a monte); in quel caso passare pump_on_01=1.0.

    Args:
        t_air_c:       Temperatura aria zona (°C).
        t_rad_mean_c:  Media idraulica mandata/ritorno circuito radiante (°C).
        valve_open_01: Stato valvola zona [0–1] (o segnale plant_active composto).
        pump_on_01:    Stato pompa [0–1] (usare 1.0 se valve_open_01 già composto).
        k_rad:         Coefficiente di influenza radiante [0–1], default MRT_K_RAD_DEFAULT.

    Returns:
        MRT stimata in °C, gated per stato circuito.
    """
    def _clamp01(x: float) -> float:
        return max(0.0, min(1.0, float(x)))

    u = _clamp01(valve_open_01) * _clamp01(pump_on_01)
    k_eff = max(0.0, min(1.0, float(k_rad))) * u
    t_air = float(t_air_c)
    t_rad = float(t_rad_mean_c)
    return float(t_air + k_eff * (t_rad - t_air))

def and01(*xs: float, threshold: float = 0.5) -> float:
    """Logical AND for 0/1-ish signals.
    Returns 1.0 if all inputs are >= threshold, else 0.0.
    """
    if not xs:
        log_warning(_LOGGER, "and01 requires at least one input [xs=%s, threshold=%s]", xs, threshold)
        raise ValueError("and01 requires at least one input")
    return 1.0 if all(float(x) >= threshold for x in xs) else 0.0

# -----------------------------
# SensorAggregator
# -----------------------------


class SensorAggregator:
    """Aggregates sensors according to a MappingConfig.

    Naming convention for computed variables
    - Zone variable:   "{zone}.{variable}"   (e.g., "living.indoor_temperature")
    - Derived variable: as provided by DerivedSpec (e.g., "global.indoor_temperature")

    Quality policy (with hold-last-good)
    - Each sensor can be rejected for: entity_not_found, unavailable, non_numeric, non_finite,
      stale_source (optional), out_of_range, time_outlier, rate_reject.
    - If a sensor is rejected, groups may still use last_filtered value for up to:
        max_age + hold_last_good
      and will annotate reasons with "hold_last_good:<error>".
    - Cross-sensor outliers may also be rejected.
    """

    def __init__(
        self,
        entities_state: dict[str, State],
        entities_observed_ts: dict[str, datetime],
        mapping: MappingConfig,
    ):
        self._entities_state = entities_state
        self._entities_observed_ts = entities_observed_ts
        self._mapping = mapping

        # Flatten groups: zones
        self._groups: Dict[str, GroupSpec] = {}
        for z in mapping.zones:
            for g in z.variables:
                key = f"{z.zone}.{g.name}" if "." not in g.name else g.name
                if key in self._groups:
                    log_warning(_LOGGER, "Duplicate group name: %s", key)
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
                    hold_last_good=getattr(g, "hold_last_good", None),
                )

        self._derived: Dict[str, DerivedSpec] = {d.name: d for d in mapping.derived}
        # Order derived specs by dependency (derived-on-derived). Prevents "missing" due to
        # accidental user-defined ordering in the mapping.
        self._derived_order: List[str] = self._build_derived_order()

        # Runtime state per sensor entity
        self._sensor_rt: Dict[str, SensorRuntime] = {}
        for g in self._groups.values():
            for s in g.sensors:
                if s.entity_id not in self._sensor_rt:
                    self._sensor_rt[s.entity_id] = SensorRuntime(spec=s)
                else:
                    # NOTE: same entity_id reused across groups.
                    # Current behavior: keep first-registered spec for runtime pipeline.
                    # (Weights are per-group, so OK; filters should ideally be identical.)
                    pass

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
        for dname in self._derived_order:
            dspec = self._derived[dname]
            self._latest[dname] = self._compute_derived(now=now, dspec=dspec)

    def _build_derived_order(self) -> List[str]:
        """Topologically sort derived variables by derived-on-derived dependencies."""
        names = list(self._derived.keys())  # preserves mapping order as tie-breaker
        deps: Dict[str, List[str]] = {}
        for dname, dspec in self._derived.items():
            d_deps: List[str] = []
            for in_name, _w in dspec.inputs:
                if in_name in self._derived and in_name != dname:
                    d_deps.append(in_name)
            deps[dname] = d_deps

        perm: set[str] = set()
        temp: set[str] = set()
        out: List[str] = []

        def visit(n: str, stack: List[str]) -> None:
            if n in perm:
                return
            if n in temp:
                cycle = " -> ".join(stack + [n])
                raise ValueError(f"Cycle detected in derived specs: {cycle}")
            temp.add(n)
            for m in deps.get(n, []):
                visit(m, stack + [n])
            temp.remove(n)
            perm.add(n)
            out.append(n)

        for n in names:
            visit(n, [])
        return out

    def _maybe_hold_last_good_derived(
        self,
        now: datetime,
        dspec: DerivedSpec,
        sources_total: int,
        fail_reasons: Tuple[str, ...],
    ) -> Optional[AggregatedValue]:
        """If enabled, reuse previous derived value when current computation fails."""
        if dspec.hold_last_good is None:
            return None

        prev = self._latest.get(dspec.name)
        if prev is None or prev.value is None or prev.ts is None:
            return None

        max_age = dspec.max_age if dspec.max_age is not None else timedelta(minutes=15)
        grace = dspec.hold_last_good
        # Consider "fresh enough" within (max_age + grace), similar to group semantics.
        if (now - prev.ts) > (max_age + grace):
            return None

        return AggregatedValue(
            name=dspec.name,
            value=float(prev.value),
            ts=prev.ts,
            sources_total=sources_total,
            sources_used=0,
            sources_rejected=sources_total,
            is_stale=False,
            is_insufficient=False,
            reasons=("derived_hold_last_good",) + tuple(fail_reasons),
        )

    # -------------------------
    # Sensor update pipeline
    # -------------------------

    def _update_sensor_from_hass(self, now: datetime, rt: SensorRuntime) -> None:
        spec = rt.spec
        f = spec.filters

        try:
            state_obj = self._entities_state.get(spec.entity_id)
            if state_obj is None:
                rt.last_error = "entity_not_found"
                log_warning(_LOGGER, f"Entity {spec.entity_id} not found.")
                return

            raw_state = getattr(state_obj, "state", None)
            if raw_state in (None, "unknown", "unavailable", "none", "None", ""):
                rt.last_error = "unavailable"
                log_warning(_LOGGER, f"Entity {spec.entity_id} unavailable.")
                return

            if isinstance(raw_state, str):
                s = raw_state.strip().lower()
                if s in ("on", "true", "open", "opened"):
                    raw_state = 1.0
                elif s in ("off", "false", "closed", "close"):
                    raw_state = 0.0

            try:
                raw_val = float(raw_state)
            except (TypeError, ValueError):
                rt.last_error = "non_numeric"
                log_warning(_LOGGER, f"Entity {spec.entity_id} non-numeric state: {raw_state!r}.")
                return

            if not _is_finite(raw_val):
                rt.last_error = "non_finite"
                log_warning(_LOGGER, f"Entity {spec.entity_id} non-finite state: {raw_state!r}.")
                return

            # Diagnostic source timestamp (may not advance when value doesn't change)
            ts_src = now
            ts_src_tp = _source_ts(state_obj, now)
            ts_obs_raw = self._entities_observed_ts.get(spec.entity_id)
            ts_obs = _ensure_utc(ts_obs_raw) if ts_obs_raw else None

            # log_debug(
            #     _LOGGER, 
            #     f"datate time cmp {spec.entity_id} now={ts_src_tp[2]} last_report={ts_src_tp[0]} last_update={ts_src_tp[1]} observed={ts_obs}"
            # )

            if ts_obs and ts_src_tp[0]:
                ts_src = max(ts_obs, ts_src_tp[0])
            else:
                ts_src = ts_src_tp[1] or ts_src_tp[2]

            rt.last_source_ts_seen = ts_src

            # OPTIONAL hard guard on HA timestamps (off by default)
            if f.max_source_age is not None and (now - ts_src) > f.max_source_age:
                rt.last_error = "stale_source"
                log_warning(
                    _LOGGER,
                    f"Entity {spec.entity_id} stale source timestamp: last_source_ts={ts_src}, now={now}, max_source_age={f.max_source_age}.",
                )
                return

            # Unit conversion (optional)
            candidate = float(raw_val)
            if f.auto_unit_convert:
                unit = None
                attrs = getattr(state_obj, "attributes", None)
                if isinstance(attrs, dict):
                    unit = attrs.get("unit_of_measurement")
                candidate = _convert_unit_if_needed(candidate, unit)

            # Range checks
            if f.min_valid is not None and candidate < f.min_valid:
                rt.last_error = "below_min_valid"
                log_warning(
                    _LOGGER,
                    f"Entity {spec.entity_id} value below min_valid: {candidate} < {f.min_valid}.",
                )
                return
            if f.max_valid is not None and candidate > f.max_valid:
                rt.last_error = "above_max_valid"
                log_warning(
                    _LOGGER,
                    f"Entity {spec.entity_id} value above max_valid: {candidate} > {f.max_valid}.",
                )
                return

            # If value did not change AND HA timestamps did not change, treat as a "heartbeat":
            # refresh observed timestamps to keep freshness, without expanding histories.
            #
            # IMPORTANT: compare against last_raw when possible (filtered value may differ due to EMA/median).
            if rt.last_filtered is not None:
                prev_ref = (
                    float(rt.last_raw.value) if rt.last_raw is not None
                    else float(rt.last_filtered.value)
                )
            else:
                prev_ref = None

            if (
                prev_ref is not None
                and rt.last_filtered is not None
                and abs(candidate - prev_ref) <= EPS_UNCHANGED
                and rt.last_source_ts_ok is not None
                and ts_src == rt.last_source_ts_ok
            ):
                rt.last_error = None
                rt.last_seen_ok = now
                # refresh timestamps to keep group freshness
                rt.last_raw = Sample(value=prev_ref, ts=now)
                rt.last_filtered = Sample(value=rt.last_filtered.value, ts=now)
                # Heartbeat è atteso (specie su switch): non è un WARNING.
                log_debug(_LOGGER, "Entity %s heartbeat (no change): value=%s, ts_src=%s",
                          spec.entity_id, candidate, ts_src)
                return

            # Time-series outlier (Hampel on raw history)
            if f.time_hampel_k is not None and len(rt.raw_history) >= f.time_hampel_min_samples:
                hist_vals = [s.value for s in rt.raw_history]
                med = _median(hist_vals)
                mad = _mad(hist_vals, med)
                if mad != 0:
                    sigma = 1.4826 * mad
                    if abs(candidate - med) > f.time_hampel_k * sigma:
                        # Se siamo in CLIP e abbiamo rate-limit, preferiamo clippare e NON bucare la pipeline.
                        # Questo evita cascata di hold_last_good/insufficient sui derived.
                        if (
                            f.max_rate_per_min is not None
                            and rt.last_raw is not None
                            and f.rate_limit_mode == RateLimitMode.CLIP
                        ):
                            dt_s = max(1.0, (now - rt.last_raw.ts).total_seconds())
                            dt_min = dt_s / 60.0
                            max_delta = float(f.max_rate_per_min) * dt_min
                            delta = candidate - rt.last_raw.value
                            if abs(delta) > max_delta:
                                candidate = rt.last_raw.value + math.copysign(max_delta, delta)
                                rt.clipped_count += 1
                            # Log a DEBUG: è un "soft outlier" gestito.
                            log_debug(_LOGGER,
                                      "Entity %s time-series outlier -> clipped/accepted: cand=%s med=%s mad=%s",
                                      spec.entity_id, candidate, med, mad)
                        else:
                            # Modalità REJECT (o senza rate-limit): mantieni comportamento attuale.
                            rt.last_error = "time_outlier"
                            rt.rejected_count += 1
                            log_warning(
                                _LOGGER,
                                "Entity %s time-series outlier (rejected): candidate=%s, med=%s, mad=%s",
                                spec.entity_id, candidate, med, mad
                            )
                            return

            # Rate limiting against last accepted raw (use observed dt; conservative)
            if f.max_rate_per_min is not None and rt.last_raw is not None:
                dt_s = max(1.0, (now - rt.last_raw.ts).total_seconds())
                dt_min = dt_s / 60.0
                max_delta = float(f.max_rate_per_min) * dt_min
                delta = candidate - rt.last_raw.value
                if abs(delta) > max_delta:
                    if f.rate_limit_mode == "reject":
                        rt.last_error = "rate_reject"
                        rt.rejected_count += 1
                        log_warning(
                            _LOGGER,
                            f"Entity {spec.entity_id} rate limit reject: candidate={candidate}, last={rt.last_raw.value}, delta={delta}, max_delta={max_delta}.",)
                        return
                    # clip
                    candidate = rt.last_raw.value + math.copysign(max_delta, delta)
                    rt.clipped_count += 1

            # Accept sample (freshness based on observed timestamp)
            rt.last_error = None
            rt.last_seen_ok = now
            rt.last_source_ts_ok = ts_src

            raw_sample = Sample(value=float(candidate), ts=now)
            rt.last_raw = raw_sample
            rt.raw_history.append(raw_sample)
            while len(rt.raw_history) > f.raw_history_size:
                rt.raw_history.popleft()

            # EMA smoothing on accepted candidate
            prev_f = rt.last_filtered.value if rt.last_filtered else None
            filtered_val = _ema(prev_f, raw_sample.value, f.ema_alpha)
            filtered_sample = Sample(value=float(filtered_val), ts=now)
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
                    rt.last_filtered = Sample(value=float(med_val), ts=now)

        except Exception as e:
            rt.last_error = f"exception:{type(e).__name__}"
            log_warning(
                _LOGGER,
                f"Exception updating entity {spec.entity_id}: {e}",
            )

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
                log_warning(_LOGGER, f"Sensor {ss.entity_id} not registered in runtime.")
                continue

            # If error: try hold-last-good
            if rt.last_error is not None:
                if rt.last_filtered is not None:
                    max_age = ss.filters.max_age if ss.filters.max_age is not None else timedelta(minutes=15)
                    grace = ss.filters.hold_last_good
                    if (now - rt.last_filtered.ts) <= (max_age + grace):
                        used.append((ss, float(rt.last_filtered.value), rt.last_filtered.ts))
                        reasons.append(f"{ss.entity_id}:hold_last_good:{rt.last_error}")
                        continue

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

        # NOTE: group_ts will be recomputed after cross-outlier rejection (used2).

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
            # No sources survived cross-outlier filtering -> no meaningful freshness timestamp.
            return AggregatedValue(
                name=gspec.name,
                value=None,
                ts=None,
                sources_total=sources_total,
                sources_used=0,
                sources_rejected=sources_total,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + ["no_sources_after_cross_filter"] + rejected),
            )

        # Recompute timestamp after outlier rejection (CRITICAL for correctness)
        group_ts = max(ts for _, _, ts in used2)

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

        # Staleness for group (observed_ts based) + grace
        if gspec.max_age is not None:
            max_age = gspec.max_age
        else:
            ages = [ss.filters.max_age for ss, _, _ in used2 if ss.filters.max_age is not None]
            max_age = min(ages) if ages else timedelta(minutes=15)

        if gspec.hold_last_good is not None:
            grace = gspec.hold_last_good
        else:
            graces = [ss.filters.hold_last_good for ss, _, _ in used2]
            grace = min(graces) if graces else timedelta(0)

        is_stale = (now - group_ts) > (max_age + grace)
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

    # -------------------------
    # Derived computations
    # -------------------------

    def _compute_derived(self, now: datetime, dspec: DerivedSpec) -> AggregatedValue:
        if dspec.kind == "compute":
            return self._compute_derived_compute(now=now, dspec=dspec)
        return self._compute_derived_aggregate(now=now, dspec=dspec)

    def _compute_derived_aggregate(self, now: datetime, dspec: DerivedSpec) -> AggregatedValue:
        # (input_name, value, weight, ts)
        used_vals: List[Tuple[str, float, float, datetime]] = []
        rejected: List[str] = []
        reasons: List[str] = []
        degraded = False

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
            if av.reasons and any("hold_last_good" in r for r in av.reasons):
                degraded = True
            used_vals.append((gname, float(av.value), float(w), av.ts))

        sources_total = len(dspec.inputs)
        sources_used = len(used_vals)
        sources_rejected = sources_total - sources_used

        if sources_used == 0:
            fail = AggregatedValue(
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
            held = self._maybe_hold_last_good_derived(
                now=now,
                dspec=dspec,
                sources_total=sources_total,
                fail_reasons=fail.reasons,
            )
            return held or fail

        ts = max(t for _, _, _, t in used_vals)

        is_insufficient = sources_used < max(1, dspec.min_sources)
        if is_insufficient:
            log_warning(_LOGGER, f"Derived {dspec.name} insufficient sources: {sources_used} < {dspec.min_sources}")
            reasons.append("insufficient_sources")
        if degraded:
            log_warning(_LOGGER, f"Derived {dspec.name} inputs degraded (hold_last_good used)")
            reasons.append("inputs_degraded")

        values = [v for _n, v, _w, _t in used_vals]
        
        # Support "max"/"min"/"median"/"trimmed_mean" explicitly.
        # For these methods, weights are intentionally ignored.
        if dspec.method in ("max", "min", "median", "trimmed_mean"):
            agg = _agg(values, method=dspec.method, trimmed_fraction=dspec.trimmed_fraction)

            # Identify limiting input for min/max
            if dspec.method in ("min", "max"):
                if dspec.method == "min":
                    best = min(used_vals, key=lambda x: x[1])  # by value
                    tag = "argmin"
                else:
                    best = max(used_vals, key=lambda x: x[1])
                    tag = "argmax"

                best_name = best[0]
                best_zone = best_name.split(".", 1)[0] if "." in best_name else best_name
                reasons.append(f"{tag}_input={best_name}")
                reasons.append(f"{tag}_zone={best_zone}")
        else:
            # weighted_mean (default) or any other fallback -> weighted mean
            num = sum(v * w for _n, v, w, _t in used_vals if w > 0)
            den = sum(w for _n, _v, w, _t in used_vals if w > 0)
            agg = (num / den) if den > 0 else _median(values)

        agg = _clamp(float(agg), dspec.clamp_min, dspec.clamp_max)

        max_age = dspec.max_age if dspec.max_age is not None else timedelta(minutes=15)
        is_stale = (now - ts) > max_age
        if is_stale:
            log_warning(_LOGGER, f"Derived {dspec.name} is stale: now={now}, ts={ts}, max_age={max_age}")
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
        - mrt_c(T_air, T_rad_mean, [k_rad])
        - t_op_c(T_air, MRT)
        - condensation_margin_c(T_rad_mean, dew_point, [surface_offset])

        Inputs are read in order. For these computes we expect at least 2 inputs.
        """
        rejected: List[str] = []
        reasons: List[str] = []
        degraded = False

        if dspec.compute not in (
            "dew_point_c",
            "heat_index_c",
            "mrt_c",
            "mrt_gated_c",
            "t_op_c",
            "condensation_margin_c",
            "and01",
        ):
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
            if av.reasons and any("hold_last_good" in r for r in av.reasons):
                degraded = True
            inputs_vals.append((gname, float(av.value), av.ts))

        sources_total = len(dspec.inputs)
        sources_used = len(inputs_vals)
        sources_rejected = sources_total - sources_used

        # --- required inputs per compute (evita IndexError e compute parziali) ---
        def _required_inputs_for_compute() -> int:
            # 2-input computes (3rd param optional dove previsto)
            if dspec.compute in ("dew_point_c", "heat_index_c", "t_op_c", "condensation_margin_c"):
                return 2
            if dspec.compute == "mrt_c":
                return 2  # k_rad opzionale
            if dspec.compute == "and01":
                return 1
            if dspec.compute == "mrt_gated_c":
                # Supporta due convenzioni:
                # - (t_air, t_rad_mean, active_01[, k_rad]) -> 3 richiesti (k_rad opzionale anche se configurato)
                # - (t_air, t_rad_mean, valve_01, pump_01[, k_rad]) -> 4 richiesti (k_rad opzionale)
                cfg_n = len(dspec.inputs)
                return 3 if cfg_n in (3, 4) else 4
            return 2

        required_n_base = _required_inputs_for_compute()
        required_n = max(required_n_base, int(dspec.min_sources or 1))

        is_insufficient = sources_used < required_n
        if is_insufficient:
            log_warning(_LOGGER, f"Derived {dspec.name} insufficient sources: {sources_used} < {required_n}")   
            reasons.append("insufficient_sources")
        if degraded:
            log_warning(_LOGGER, f"Derived {dspec.name} inputs degraded (hold_last_good used)")
            reasons.append("inputs_degraded")

        if sources_used < required_n:
            fail = AggregatedValue(
                name=dspec.name,
                value=None,
                ts=None,
                sources_total=sources_total,
                sources_used=sources_used,
                sources_rejected=sources_rejected,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + [f"need_{required_n}_inputs"] + rejected),
            )
            held = self._maybe_hold_last_good_derived(
                now=now,
                dspec=dspec,
                sources_total=sources_total,
                fail_reasons=fail.reasons,
            )
            return held or fail

        ts = max(ti for _, _, ti in inputs_vals)

        try:
            if dspec.compute == "dew_point_c":
                t = inputs_vals[0][1]
                rh = inputs_vals[1][1]
                val = dew_point_c(t, rh)
            elif dspec.compute == "heat_index_c":
                t = inputs_vals[0][1]
                rh = inputs_vals[1][1]
                val = heat_index_c(t, rh)
            elif dspec.compute == "mrt_c":
                t_air = inputs_vals[0][1]
                t_rad_mean = inputs_vals[1][1]
                k_rad = inputs_vals[2][1] if len(inputs_vals) >= 3 else MRT_K_RAD_DEFAULT
                val = mrt_c(t_air, t_rad_mean, k_rad)
            elif dspec.compute == "condensation_margin_c":
                # expects: (radiant_mean_temp_c, dew_point_c, [optional surface_offset_c])
                t_rad_mean = inputs_vals[0][1]
                dp = inputs_vals[1][1]
                offset = inputs_vals[2][1] if len(inputs_vals) >= 3 else DEFAULT_SURFACE_OFFSET_C
                val = condensation_margin_c(t_rad_mean, dp, surface_offset_c=offset)
            elif dspec.compute == "mrt_gated_c":
                t_air = inputs_vals[0][1]
                t_rad_mean = inputs_vals[1][1]

                # Determina la convenzione dalla config (non dalla lunghezza residua degli input validi)
                cfg_n = len(dspec.inputs)
                if cfg_n in (3, 4):
                    # (t_air, t_rad_mean, active_01[, k_rad])  -- k_rad opzionale
                    active_01 = inputs_vals[2][1]
                    k_rad = inputs_vals[3][1] if len(inputs_vals) >= 4 else MRT_K_RAD_DEFAULT
                    val = mrt_gated_c(
                        t_air,
                        t_rad_mean,
                        valve_open_01=active_01,  # active = valve*pump già “composto”
                        pump_on_01=1.0,
                        k_rad=k_rad,
                    )
                else:
                    # (t_air, t_rad_mean, valve_01, pump_01[, k_rad]) -- k_rad opzionale
                    valve_01 = inputs_vals[2][1]
                    pump_01 = inputs_vals[3][1]
                    k_rad = inputs_vals[4][1] if len(inputs_vals) >= 5 else MRT_K_RAD_DEFAULT
                    val = mrt_gated_c(t_air, t_rad_mean, valve_01, pump_01, k_rad=k_rad)
            elif dspec.compute == "and01":
                vals01 = [v for _n, v, _ts in inputs_vals]
                val = and01(*vals01)
            else:
                t_air = inputs_vals[0][1]
                mrt_val = inputs_vals[1][1]
                val = t_op_c(t_air, mrt_val)
        except Exception as e:
            _exc = type(e).__name__
            _msg = str(e).strip()
            _tag = f"{_exc}:{_msg}" if _msg else _exc
            fail = AggregatedValue(
                name=dspec.name,
                value=None,
                ts=ts,
                sources_total=sources_total,
                sources_used=sources_used,
                sources_rejected=sources_rejected,
                is_stale=True,
                is_insufficient=True,
                reasons=tuple(reasons + [f"compute_exception:{_tag}"] + rejected),
            )
            held = self._maybe_hold_last_good_derived(
                now=now,
                dspec=dspec,
                sources_total=sources_total,
                fail_reasons=fail.reasons,
            )
            return held or fail

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
    - Prefer computing dew point / heat index from aggregated T/RH for consistency.
    """

    temp_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        max_source_age=None,  # keep None unless you *know* HA source timestamps advance reliably
        min_valid=5.0,
        max_valid=35.0,
        time_hampel_k=4.0,
        max_rate_per_min=0.6,  # °C/min (tune carefully if you see clipping)
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.2,
        rolling_median_window=3,
    )

    rh_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        max_source_age=None,
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=4.0,
        max_rate_per_min=5.0,  # %RH/min
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.25,
        rolling_median_window=3,
    )

    living = ZoneConfig(
        zone="living",
        weight=1.0,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                method=AggregationMethod.WEIGHTED_MEAN,
                sensors=(SensorSpec("sensor.livingroom_temperature", weight=1.0, filters=temp_filters),),
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                method=AggregationMethod.MEDIAN,
                sensors=(SensorSpec("sensor.livingroom_humidity", weight=1.0, filters=rh_filters),),
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    bedroom = ZoneConfig(
        zone="bedroom",
        weight=0.7,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                method=AggregationMethod.WEIGHTED_MEAN,
                sensors=(SensorSpec("sensor.bedroom_temperature", weight=1.0, filters=temp_filters),),
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                method=AggregationMethod.MEDIAN,
                sensors=(SensorSpec("sensor.bedroom_humidity", weight=1.0, filters=rh_filters),),
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    derived = (
        DerivedSpec(
            name="global.indoor_temperature",
            kind=DerivedKind.AGGREGATE,
            inputs=(("living.indoor_temperature", 1.0), ("bedroom.indoor_temperature", 0.7)),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=1,
            clamp_min=5.0,
            clamp_max=35.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_humidity",
            kind=DerivedKind.AGGREGATE,
            inputs=(("living.indoor_humidity", 1.0), ("bedroom.indoor_humidity", 0.7)),
            method=AggregationMethod.MEDIAN,
            min_sources=1,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=10),
        ),
    )

    return MappingConfig(zones=(living, bedroom), derived=derived)


# =============================
# 3) ClimateRegimeEstimator (unchanged from your snippet)
# =============================

Regime = Literal["heating", "cooling", "shoulder"]


@dataclass(frozen=True)
class RegimeConfig:
    """Configuration for climate regime estimation.

    This estimator is intentionally conservative: it changes regime only when
    a *running mean* of outdoor temperature crosses thresholds with hysteresis.
    """

    tau_days: float = 5.0

    heating_on: float = 15.0
    heating_off: float = 16.5

    cooling_on: float = 20.0
    cooling_off: float = 18.5

    min_switch_interval: timedelta = timedelta(hours=8)

    stale_grace: timedelta = timedelta(minutes=60)

    outdoor_clamp_min: float = -40.0
    outdoor_clamp_max: float = 60.0


@dataclass
class ClimateRegimeState:
    regime: Regime
    ts: datetime

    outdoor_temp: Optional[float] = None
    running_mean_outdoor: Optional[float] = None

    last_switch_ts: Optional[datetime] = None

    confidence: float = 0.0

    reasons: Tuple[str, ...] = ()


class ClimateRegimeEstimator:
    """Estimates the current climate regime (heating/cooling/shoulder)."""

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
        av = agg.get(outdoor_name)
        now = datetime.now(timezone.utc)

        if av.value is None or av.ts is None:
            return self._keep_last(now, reason=f"missing:{outdoor_name}")

        if av.is_stale:
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
        now = _ensure_tz(ts or datetime.now(timezone.utc))

        t_out = _clamp(float(outdoor_temp), self._cfg.outdoor_clamp_min, self._cfg.outdoor_clamp_max)

        rm_prev = self._state.running_mean_outdoor
        ts_prev = self._state.ts

        dt_s = max(1.0, (now - ts_prev).total_seconds())
        tau_s = max(60.0, float(self._cfg.tau_days) * 86400.0)
        alpha = 1.0 - math.exp(-dt_s / tau_s)

        rm = t_out if rm_prev is None else (alpha * t_out + (1.0 - alpha) * rm_prev)

        new_regime, decision_reasons = self._decide_regime(rm=rm, now=now)

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

    def _keep_last(self, now: datetime, reason: str) -> ClimateRegimeState:
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

    def _decide_regime(self, rm: float, now: datetime) -> Tuple[Regime, Tuple[str, ...]]:
        cfg = self._cfg
        cur = self._state.regime
        reasons: List[str] = []

        want_heat = rm <= cfg.heating_on
        want_cool = rm >= cfg.cooling_on

        can_switch = self._can_switch(now)
        if not can_switch:
            reasons.append("switch_lockout")

        new = cur

        if cur == "heating":
            if rm >= cfg.heating_off and can_switch:
                new = "shoulder"
                reasons.append("exit_heating")
        elif cur == "cooling":
            if rm <= cfg.cooling_off and can_switch:
                new = "shoulder"
                reasons.append("exit_cooling")
        else:
            if want_heat and can_switch:
                new = "heating"
                reasons.append("enter_heating")
            elif want_cool and can_switch:
                new = "cooling"
                reasons.append("enter_cooling")

        if new != cur:
            self._state.last_switch_ts = now
            reasons.append("switched")

        reasons.append(f"rm={rm:.2f}")

        return new, tuple(reasons)

    def _confidence(self, rm: float, quality: float, regime: Regime) -> float:
        cfg = self._cfg
        q = max(0.0, min(1.0, float(quality)))

        if regime == "heating":
            d = max(0.0, cfg.heating_off - rm)
        elif regime == "cooling":
            d = max(0.0, rm - cfg.cooling_off)
        else:
            d1 = abs(rm - cfg.heating_on)
            d2 = abs(rm - cfg.cooling_on)
            d = min(d1, d2)

        margin = max(0.0, min(1.0, d / 3.0))
        return float(0.2 * q + 0.8 * q * margin)
