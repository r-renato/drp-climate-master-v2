from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum, unique
from typing import Dict, List, Literal, Optional, Tuple, Union

# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
@unique
class Seasons(StrEnum):
    """Seasons."""
    WINTER = "winter"
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"

    @staticmethod
    def ordered() -> Tuple[Seasons, Seasons, Seasons, Seasons]:
        """..."""
        return (
            Seasons.WINTER,
            Seasons.SPRING,
            Seasons.SUMMER,
            Seasons.AUTUMN,
        )
    
    def __str__(self) -> str:
        """Rappresentazione leggibile in italiano."""
        mapping = {
            Seasons.WINTER: "Winter",
            Seasons.SPRING: "Spring",
            Seasons.SUMMER: "Summer",
            Seasons.AUTUMN: "Autumn",
        }
        return mapping.get(self, self.value)

class OperativeSeason(StrEnum):
    WINTER = "winter"
    SUMMER = "summer"
    SHOULDER = "shoulder"  # opzionale

    @classmethod
    def from_value(
        cls,
        value: Union[str, "OperativeSeason"],
        *,
        default: Optional["OperativeSeason"] = None,
    ) -> "OperativeSeason":
        # Se è già un membro dell'enum, ritorna subito
        if isinstance(value, cls):
            return value

        if value is None:
            if default is not None:
                return default
            raise ValueError("OperativeSeason.from_value: value is None")

        v = str(value).strip().lower()
        v_norm = v.replace("-", "_").replace(" ", "_")

        aliases = {
            "winter": cls.WINTER,
            # "inverno": cls.WINTER,
            # "w": cls.WINTER,
            "summer": cls.SUMMER,
            # "estate": cls.SUMMER,
            # "s": cls.SUMMER,
            "shoulder": cls.SHOULDER,
            "spring": cls.SHOULDER,
            "autumn": cls.SHOULDER,
            # "mezzastagione": cls.SHOULDER,
            # "mezzo_stagione": cls.SHOULDER,
            # "transitional": cls.SHOULDER,
        }

        season = aliases.get(v) or aliases.get(v_norm)
        if season is not None:
            return season

        if default is not None:
            return default

        raise ValueError(f"OperativeSeason.from_value: unknown season '{value}'")

# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------

RegimeHint = Literal["cold", "mild", "hot"]

# -----------------------------------------------------------------------------
# Core models
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SeasonWindow:
    """A closed (inclusive) calendar window for a season.

    Invariants:
      - start <= end
    """

    season: Seasons
    start: date  # inclusive
    end: date  # inclusive

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("SeasonWindow.start must be <= SeasonWindow.end")

    @property
    def days(self) -> int:
        """Length of the window in days (inclusive)."""
        # Inclusive window => +1
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

@dataclass(frozen=True, slots=True)
class WeatherDaySignals:
    d: date

    t_mean: float
    t_low: Optional[float]
    dew: Optional[float]
    wind: Optional[float]
    cloud: Optional[float]

    # engineered
    t_smooth: Optional[float]
    dew_smooth: Optional[float]
    trend: Optional[float]
    
@dataclass(frozen=True, slots=True)
class WeatherSeason:
    """Weather-based seasonal classification and diagnostics.

    Notes:
      - `anomaly_score` is expected to be >= 0.
      - `anomaly` is a boolean diagnostic flag; if you want strict consistency
        between calendar season and weather season, compute it in SeasonState.
    """

    season: Seasons
    anomaly: bool
    anomaly_score: float  # >= 0
    reason: str
    regime_hint: RegimeHint  # cold|mild|hot

    weather_day_signals: WeatherDaySignals

    windows: Optional[List[Tuple[date, date, Seasons]]] = None

    def __post_init__(self) -> None:
        if self.anomaly_score < 0:
            raise ValueError("WeatherSeason.weather_anomaly_score must be >= 0")

    def replace_windows(self, windows: List[Tuple[date, date, Seasons]]) -> WeatherSeason:
        return replace(self, windows=windows)

@dataclass(frozen=True, slots=True)
class SeasonState:
    """Immutable snapshot of the current seasonal status.

    This revision removes multiple-inheritance between slotted dataclasses
    (which can raise: `TypeError: multiple bases have instance lay-out conflict`).

    Design:
      - Composition (`window` + `weather`) to keep the model stable.
      - An explicit `as_of` date defines what `passed`/`remaining` refer to.
      - `days`, `passed`, `remaining`, `progress` are derived (no redundancy).

    Semantics (inclusive window):
      - passed = number of day-boundaries from start to as_of (clamped).
        * start day  -> passed = 0
        * end day    -> passed = days-1
      - remaining = number of day-boundaries from as_of to end (clamped).
        * start day  -> remaining = days-1
        * end day    -> remaining = 0
      - If as_of is within [start, end], then passed + remaining == days - 1.

    Backward compatibility:
      - Properties expose the previous flat attribute API:
        season/start/end and weather_* fields.
      - `copy()` is kept.
    """

    window: SeasonWindow
    weather: WeatherSeason
    as_of: date

    detect_model: str

    def __post_init__(self) -> None:
        # No strict requirement that as_of is inside the window:
        # we clamp derived metrics to keep the state usable.
        # But we *do* enforce that the window itself is sane.
        if self.window.days < 1:
            raise ValueError("SeasonState.window.days must be >= 1")

    # -------------------------------------------------------------------------
    # Backward-compatible flat accessors (calendar)
    # -------------------------------------------------------------------------

    @property
    def season(self) -> Seasons:
        return self.window.season

    @property
    def start(self) -> date:
        return self.window.start

    @property
    def end(self) -> date:
        return self.window.end

    # -------------------------------------------------------------------------
    # Derived span metrics
    # -------------------------------------------------------------------------

    @property
    def days(self) -> int:
        return self.window.days

    @property
    def passed(self) -> int:
        """Days passed since `start` as of `as_of` (clamped)."""
        if self.days <= 1:
            return 0
        raw = (self.as_of - self.start).days
        return max(0, min(self.days - 1, raw))

    @property
    def remaining(self) -> int:
        """Days remaining until `end` as of `as_of` (clamped)."""
        if self.days <= 1:
            return 0
        raw = (self.end - self.as_of).days
        return max(0, min(self.days - 1, raw))

    @property
    def progress(self) -> float:
        """Seasonal progress in [0, 1]."""
        if self.days <= 1:
            return 1.0
        return round(self.passed / (self.days - 1) * 100, 2)

    # -------------------------------------------------------------------------
    # Backward-compatible flat accessors (weather)
    # -------------------------------------------------------------------------

    @property
    def weather_season(self) -> Seasons:
        return self.weather.season

    @property
    def weather_anomaly(self) -> bool:
        return self.weather.anomaly

    @property
    def weather_anomaly_score(self) -> float:
        return self.weather.anomaly_score

    @property
    def weather_reason(self) -> str:
        return self.weather.reason

    @property
    def weather_regime_hint(self) -> RegimeHint:
        return self.weather.regime_hint

    @property
    def weather_detect_model(self) -> str:
        return self.detect_model if self.detect_model is not None else "unknown"

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def copy(self) -> "SeasonState":
        """Backward-compatible copy (object is immutable)."""
        return replace(self)

    def with_updates(
        self,
        *,
        window: SeasonWindow | None = None,
        weather: WeatherSeason | None = None,
        as_of: date | None = None,
    ) -> "SeasonState":
        """Return a new instance with selected fields replaced."""
        return replace(
            self,
            window=self.window if window is None else window,
            weather=self.weather if weather is None else weather,
            as_of=self.as_of if as_of is None else as_of,
        )

    def to_dict(self) -> Dict[str, object]:
        """Serialize to a plain dict suitable for JSON/logging."""
        return {
            # calendar
            "season": self.season.value,
            "season_start": self.start.isoformat(),
            "season_end": self.end.isoformat(),
            "as_of": self.as_of.isoformat(),
            "days": self.days,
            "passed": self.passed,
            "remaining": self.remaining,
            "progress": self.progress,
            # weather
            "weather_season": self.weather_season.value,
            "weather_anomaly": self.weather_anomaly,
            "weather_anomaly_score": self.weather_anomaly_score,
            "weather_reason": self.weather_reason,
            "weather_regime_hint": self.weather_regime_hint,
            "weather_detect_model": self.weather_detect_model,
        }

    def __str__(self) -> str:  # pragma: no cover
        return "\n".join(
            [
                "",
                f"Season                :: {self.season.value}",
                f"Window                :: {self.start.isoformat()} -> {self.end.isoformat()} (inclusive)",
                f"As of                 :: {self.as_of.isoformat()}",
                f"Total days            :: {self.days}",
                f"Days passed           :: {self.passed}",
                f"Days remaining        :: {self.remaining}",
                f"Progress              :: {self.progress:.1f} %",
                "-" * 60,
                f"Weather season        :: {self.weather_season.value}",
                f"Weather anomaly       :: {self.weather_anomaly}",
                f"Anomaly score         :: {self.weather_anomaly_score:.3f}",
                f"Weather reason        :: {self.weather_reason}",
                f"Regime hint           :: {self.weather_regime_hint}",
                f"Weather signals       :: t_low  = {self.weather.weather_day_signals.t_low} °C",
                f"                      :: t_mean = {self.weather.weather_day_signals.t_mean} °C",
                f"                      :: dew    = {self.weather.weather_day_signals.dew} °C",
                f"                      :: wind   = {self.weather.weather_day_signals.wind} km/h",
                f"                      :: cloud  = {((self.weather.weather_day_signals.cloud or 0) * 100):.1f} %",
                f"                      :: t_smooth (engineered )   = {f'{self.weather.weather_day_signals.t_smooth:.1f}' if self.weather.weather_day_signals.t_smooth is not None else '-'} °C",
                f"                      :: dew_smooth (engineered ) = {f'{self.weather.weather_day_signals.dew_smooth:.1f}' if self.weather.weather_day_signals.dew_smooth is not None else '-'} °C",
                f"                      :: trend (engineered )      = {f'{self.weather.weather_day_signals.trend:.1f}' if self.weather.weather_day_signals.trend is not None else '-'}",
                f"Detect model          :: {self.weather_detect_model}",
                "-" * 60,
            ]
        )