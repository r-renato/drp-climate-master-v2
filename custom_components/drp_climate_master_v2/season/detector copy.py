#
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Literal,
    cast,
)
import calendar
import logging

from homeassistant.core import HomeAssistant

from ..weather.provider import WeatherProvider

from ..domain.models import SeasonState

from ..domain.enums import Seasons

_LOGGER = logging.getLogger(__name__)

class SeasonCalendar:
    """
    Encapsulates seasonal windows for a given year & hemisphere.

    Features
    - `Seasons` as StrEnum
    - `SeasonCalendar` class encapsulating year & hemisphere
    - Leap‑year safe meteorological windows (DJF/MAM/JJA/SON)
    - Methods: `windows()`, `as_dict()`, `season_for(date)`
    - Fluent helpers: `with_year()`, `with_hemisphere()`
    - Backward compatibility: module functions + `SEASONS_BY_DATE` mapping

    By convention, `year` is the year where **WINTER ends** (Feb of `year`).
    Example: `SeasonCalendar(2025)` -> WINTER spans 2024-12-01 → 2025-02-28/29.
    """

    @dataclass(frozen=True, slots=True)
    class SeasonWindow:
        season: Seasons
        start: date  # inclusive
        end: date    # inclusive

        def contains(self, d: date) -> bool:
            return self.start <= d <= self.end

    def __init__(
        self,
        year: Optional[int] = None,
        *,
        hemisphere: Literal["north", "south"] = "north",
    ) -> None:
        self._year: int = year if year is not None else date.today().year
        self._hemisphere: Literal["north"] | Literal["south"] = hemisphere

    # ---- fluent helpers ------------------------------------------------------
    def with_year(self, year: int) -> "SeasonCalendar":
        return SeasonCalendar(year, hemisphere=self._hemisphere)

    def with_hemisphere(self, hemisphere: Literal["north", "south"]) -> "SeasonCalendar":
        return SeasonCalendar(self._year, hemisphere=hemisphere)

    # ---- API -----------------------------------------------------------------
    def windows(self) -> Dict[Seasons, "SeasonCalendar.SeasonWindow"]:
        """Return meteorological windows for the configured year/hemisphere."""
        if self._hemisphere == "south":
            return self._south_windows(self._year)
        return self._north_windows(self._year)

    def as_dict(self) -> Dict[Seasons, Tuple[date, date]]:
        """Convenience mapping like the legacy API (start, end) tuples."""
        wins = self.windows()
        return {s: (w.start, w.end) for s, w in wins.items()}

    def season_for(self, d: date) -> Seasons:
        """Determine season for a given date, handling cross‑year winter/summer."""
        # Check windows around the date's year to safely span DJF boundaries
        for y in (d.year - 1, d.year, d.year + 1):
            wins = (self.with_year(y)).windows().values()
            for w in wins:
                if w.contains(d):
                    return w.season
        # Fallback should be unreachable; pick SUMMER to avoid heating bias
        return Seasons.SUMMER

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _eom(y: int, m: int) -> int:
        return calendar.monthrange(y, m)[1]

    @classmethod
    def _north_windows(cls, year: int) -> Dict[Seasons, "SeasonCalendar.SeasonWindow"]:
        winter = cls.SeasonWindow(
            Seasons.WINTER,
            date(year - 1, 12, 1),
            date(year, 2, cls._eom(year, 2)),
        )
        spring = cls.SeasonWindow(Seasons.SPRING, date(year, 3, 1), date(year, 5, 31))
        summer = cls.SeasonWindow(Seasons.SUMMER, date(year, 6, 1), date(year, 8, 31))
        autumn = cls.SeasonWindow(Seasons.AUTUMN, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
        }

    @classmethod
    def _south_windows(cls, year: int) -> Dict[Seasons, "SeasonCalendar.SeasonWindow"]:
        summer = cls.SeasonWindow(
            Seasons.SUMMER,
            date(year - 1, 12, 1),
            date(year, 2, cls._eom(year, 2)),
        )
        autumn = cls.SeasonWindow(Seasons.AUTUMN, date(year, 3, 1), date(year, 5, 31))
        winter = cls.SeasonWindow(Seasons.WINTER, date(year, 6, 1), date(year, 8, 31))
        spring = cls.SeasonWindow(Seasons.SPRING, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
        }


# # ---- Backward‑compatibility helpers ------------------------------------------

# def seasonal_windows(year: int | None = None, *, hemisphere: str = "north") -> Dict[Seasons, SeasonCalendar.SeasonWindow]:
#     return SeasonCalendar(year, hemisphere=hemisphere).windows()


# def seasonal_windows_dict(year: int | None = None, *, hemisphere: str = "north") -> Dict[Seasons, Tuple[date, date]]:
#     return SeasonCalendar(year, hemisphere=hemisphere).as_dict()


# def season_for_date(d: date, *, hemisphere: str = "north") -> Seasons:
#     return SeasonCalendar(d.year, hemisphere=hemisphere).season_for(d)


# # Backward‑compatible constant for the *current* year
# SEASONS_BY_DATE: Mapping[Seasons, Tuple[date, date]] = SeasonCalendar().as_dict()

# -------------------- Season detection (meteorological + forecast-aware) -------------------- #

"""Season detection (meteorological + forecast-aware) for Climate Master v2.

Design goals
- Pure, testable core with dependency injection of the weather provider
- Season baseline from `SeasonCalendar` (meteorological DJF/MAM/JJA/SON)
- Optional forecast-based override with probabilistic scoring
- Gaussian time-in-season boost to stabilize results
- Async API compatible with Home Assistant

This module exposes:
- `WeatherProvider` Protocol and `HAWeatherProvider` implementation
- `SeasonDetector` class with `detect()` returning `SeasonData`

Assumptions:
- `SeasonData`, `CONFORT_ZONES`, `ConfortAttr` live in utils.const (back-compat)
- Forecast items include at least `temperature` (max) and `templow` (min)
"""


# ===== Weather provider abstraction ===========================================
# class WeatherProviderProtocol(Protocol):
#     async def async_get_daily_forecast(self, hass: HomeAssistant, entity_id: str) -> Optional[List[Mapping[str, Any]]]:
#         """Return daily forecasts list with at least keys: temperature (max), templow (min)."""
#         ...


# class HAWeatherProvider:
#     """Default provider using Home Assistant `weather.get_forecasts` service."""

#     async def async_get_daily_forecast(
#         self,
#         hass: HomeAssistant,
#         entity_id: str,
#     ) -> Optional[List[Mapping[str, Any]]]:
#         try:
#             resp: Any = await hass.services.async_call(
#                 domain="weather",
#                 service="get_forecasts",
#                 service_data={"type": "daily"},
#                 blocking=True,
#                 target={"entity_id": entity_id},
#                 return_response=True,
#             )
#         except Exception as e:  # noqa: BLE001
#             _LOGGER.warning("SeasonDetector: weather service call failed for %s: %s", entity_id, e)
#             return None

#         # Shape 1 (tipico): { "<entity_id>": { "forecast": [ {...}, ... ] } }
#         if isinstance(resp, dict):
#             payload: Any = resp.get(entity_id)
#             if isinstance(payload, dict):
#                 raw_fc: Any = payload.get("forecast")
#                 if isinstance(raw_fc, list):
#                     fc_list: List[Mapping[str, Any]] = [it for it in raw_fc if isinstance(it, dict)]
#                     return fc_list or None
#             # Shape 1b: { "<entity_id>": [ {...}, ... ] }
#             if isinstance(payload, list) and all(isinstance(it, dict) for it in payload):
#                 return cast(List[Mapping[str, Any]], payload) or None

#         # Shape 2 (fallback): top-level già lista di forecast
#         if isinstance(resp, list) and all(isinstance(it, dict) for it in resp):
#             return cast(List[Mapping[str, Any]], resp) or None

#         _LOGGER.warning(
#             "SeasonDetector: unexpected weather.get_forecasts response type: %s", type(resp).__name__
#         )
#         return None

# ===== Detector ================================================================
@dataclass(slots=True)
class _ScoreParams:
    day_decay: float = 0.1      # weight for day i: max(0, 1 - i*decay)
    boost_sigma: float = 0.20   # gaussian sigma around mid-season
    boost_amp: float = 0.20     # gaussian amplitude added to scores


class SeasonDetector:
    """Infers the current meteorological season with forecast-aware override."""

    def __init__(
        self,
        calendar: SeasonCalendar | None = None,
        *,
        weather_provider: WeatherProvider,
        params: _ScoreParams | None = None,
        season_weight: Optional[Mapping[Seasons, float]] = None,
    ) -> None:
        self._calendar = calendar or SeasonCalendar()
        self._weather_provider: WeatherProvider = weather_provider
        self._params = params or _ScoreParams()
        self._season_weight: Mapping[Seasons, float] = season_weight or {
            Seasons.SUMMER: 1.2,
            Seasons.WINTER: 1.2,
            Seasons.SPRING: 1.0,
            Seasons.AUTUMN: 1.0,
        }

    # ---- Public API -----------------------------------------------------------
    async def detect(self, hass: HomeAssistant, weather_entity_id: Optional[str]) -> SeasonState:
        """Return SeasonData based on calendar + (optionally) weather forecasts.

        If forecasts are unavailable, returns the pure calendar season.
        """
        baseline = self._season_data_for_date(date.today())

        if not weather_entity_id:
            return baseline

        forecasts = await self._provider.async_get_daily_forecast(hass, weather_entity_id)
        if not forecasts:
            return baseline

        scores = self._score_seasons(forecasts)
        boosted = self._apply_gaussian_boost(scores, today=date.today())
        probs = self._normalize(boosted)

        selected = max(boosted.items(), key=lambda x: x[1])[0]

        result = SeasonState(
            label=baseline.label,
            days=baseline.days,
            passed=baseline.passed,
            remaining=baseline.remaining,
            overridden=selected,
            weather_anomaly=(selected != baseline.label),
            season_scores={s: round(v, 4) for s, v in boosted.items()},
            season_probabilities={s: round(probs[s] * 100.0, 1) for s in boosted},
        )
        return result

    # ---- Internals ------------------------------------------------------------
    def _season_data_for_date(self, d: date) -> SeasonState:
        wins = self._calendar.windows()
        s = self._calendar.season_for(d)
        win = wins[s]
        total_days = (win.end - win.start).days + 1
        passed = (d - win.start).days
        remaining = (win.end - d).days
        return SeasonState(
            label=s,
            days=total_days,
            passed=passed,
            remaining=remaining,
            overridden=s,
            weather_anomaly=False,
        )

    def _score_seasons(self, forecast_list: Iterable[Mapping[str, Any]]) -> Dict[Seasons, float]:
        scores: Dict[Seasons, float] = {s: 0.0 for s in Seasons}
        for i, fc in enumerate(forecast_list):
            try:
                tmin = float(fc["templow"])  # daily min
                tmax = float(fc["temperature"])  # daily max
            except (KeyError, TypeError, ValueError):
                continue

            weight_day = max(0.0, 1.0 - i * self._params.day_decay)
            if weight_day == 0.0:
                break

            for season, comfort in CONFORT_ZONES.items():
                t_min = comfort[ConfortAttr.T_MIN.value] - comfort[ConfortAttr.DT.value]
                t_max = comfort[ConfortAttr.T_MAX.value] + comfort[ConfortAttr.DT.value]
                t_avg = (t_min + t_max) / 2.0

                # distance from season average band
                min_dev = abs(tmin - t_avg)
                max_dev = abs(tmax - t_avg)
                base = 1.0 / (1.0 + min_dev + max_dev)  # in (0, 1]

                scores[season] += (
                    weight_day * base * self._season_weight.get(season, 1.0)
                )
        return scores

    def _apply_gaussian_boost(self, scores: Mapping[Seasons, float], *, today: date) -> Dict[Seasons, float]:
        boosted = dict(scores)
        wins = self._calendar.windows()

        for season, win in wins.items():
            # Compute in-season progress in [0, 1]
            if win.start <= today <= win.end:
                total = max(1, (win.end - win.start).days)
                passed = (today - win.start).days
                progress = passed / total

                # Gaussian centered at mid-season
                center = 0.5
                sigma = self._params.boost_sigma
                exponent = -((progress - center) ** 2) / (2 * sigma * sigma)
                boost = self._params.boost_amp * math.exp(exponent)
            else:
                boost = 0.0

            boosted[season] = boosted.get(season, 0.0) + round(boost, 6)

        return boosted

    @staticmethod
    def _normalize(scores: Mapping[Seasons, float]) -> Dict[Seasons, float]:
        total = float(sum(scores.values()))
        if total <= 0:
            return {s: 0.0 for s in Seasons}
        return {s: scores.get(s, 0.0) / total for s in Seasons}
