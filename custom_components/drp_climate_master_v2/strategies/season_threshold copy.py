from __future__ import annotations

"""
Seasonal threshold strategy (reorganized for Climate Master v2)

- Dependency injection for bias provider (Influx or any backend)
- Caching with TTL per (season, hysteresis_mode)
- Robust date handling and ISO windows
- Clear separation of: bias calc → dynamic comfort zone → HVAC thresholds
- No hardcoded secrets or entity ids

Assumes these symbols are available from the project (backward compatible
with current layout where they live in utils.const):
  - Seasons, SeasonThreshold, HvacThreshold
  - CONFORT_ZONES, ConfortAttr, SEASONS_BY_DATE
  - HEATING, COOLING

Psychrometric helpers:
  - dew_point(t, rh), relative_humidity(t, dp)
"""

from datetime import date, datetime, time
from statistics import mean
from typing import Protocol, Optional, Dict, Tuple
import logging
import time as _time

_LOGGER = logging.getLogger(__name__)


# ===== Interfaces / Protocols ==================================================
class BiasProvider(Protocol):
    """Abstraction over the bias backend (InfluxDB or any other storage).

    Implementations must return a temperature/humidity bias (float in °C or %RH)
    for the given entity over the requested time window, compared to `ideal_temp`.
    """

    async def async_query_entity_bias(
        self,
        *,
        entity_id: str,
        date_start: str,
        date_stop: str,
        ideal_temp: float,
    ) -> Optional[float]:
        ...


# ===== Implementation ===========================================================
class SeasonThresholdStrategy:
    """Compute seasonal dynamic setpoints and HVAC thresholds with caching.

    Bias is computed per-season from the configured ambient entities.
    The dynamic comfort zone is derived by applying seasonal hysteresis factors
    and measured bias to the static comfort band.
    """

    # Default hysteresis multipliers per season & mode
    _DEFAULT_HYSTERESIS: Dict[Seasons, Dict[str, float]] = {
        Seasons.WINTER: {"comfort": 1.5, "eco": 2.5, "away": 3.0},
        Seasons.SUMMER: {"comfort": 1.5, "eco": 2.5, "away": 3.0},
        Seasons.SPRING: {"comfort": 1.7, "eco": 3.0, "away": 3.5},
        Seasons.AUTUMN: {"comfort": 1.7, "eco": 3.0, "away": 3.5},
    }

    def __init__(
        self,
        bias_provider: BiasProvider,
        *,
        ambient_temp_entity_id: str = "ambient_home_current_temperature",
        ambient_humi_entity_id: str = "ambient_home_current_humidity",
        hysteresis_season_map: Optional[Dict[Seasons, Dict[str, float]]] = None,
        cache_ttl_s: int = 6 * 3600,
    ) -> None:
        self._bias_provider = bias_provider
        self._ambient_temp_entity_id = ambient_temp_entity_id
        self._ambient_humi_entity_id = ambient_humi_entity_id

        self._hysteresis_season_map: Dict[Seasons, Dict[str, float]] = (
            hysteresis_season_map or self._DEFAULT_HYSTERESIS
        )

        # Cache of computed SeasonThreshold per (season, mode)
        self._season_threshold_cache: Dict[Tuple[Seasons, str], SeasonThreshold] = {}
        self._season_threshold_cache_timestamps: Dict[Tuple[Seasons, str], float] = {}
        self._cache_ttl_s = cache_ttl_s

        # Seasonal bias maps (°C and %RH)
        self._temp_bias_season_map: Dict[Seasons, float] = {}
        self._humi_bias_season_map: Dict[Seasons, float] = {}
        self._bias_last_compute_ts: Optional[float] = None
        self._bias_ttl_s = cache_ttl_s  # reuse same TTL by default

    # ---- Public API -----------------------------------------------------------
    def season_threshold(self, season: Seasons, *, mode: str = "comfort") -> Optional[SeasonThreshold]:
        """Return cached SeasonThreshold if available and fresh."""
        key = (season, mode)
        ts = self._season_threshold_cache_timestamps.get(key)
        if ts is not None and (_time.time() - ts) < self._cache_ttl_s:
            return self._season_threshold_cache.get(key)
        return None

    async def async_compute_season_threshold(self, hysteresis_mode: str = "comfort") -> None:
        """Compute thresholds for all seasons if missing/expired for the given mode."""
        # Refresh seasonal bias if needed
        await self._maybe_refresh_bias_maps()

        for season, (start_date, end_date) in SEASONS_BY_DATE.items():
            key = (season, hysteresis_mode)
            ts = self._season_threshold_cache_timestamps.get(key)
            if ts is not None and (_time.time() - ts) < self._cache_ttl_s:
                continue  # still fresh

            temp_bias = self._temp_bias_season_map.get(season, 0.0)
            humi_bias = self._humi_bias_season_map.get(season, 0.0)
            static_cz = CONFORT_ZONES[season]

            hysteresis_factor = self._hysteresis_for(season, hysteresis_mode)

            dyn_cz = await self._async_compute_dynamic_comfort_zone(
                static_cz,
                hysteresis_factor=hysteresis_factor,
                temp_bias=temp_bias,
                humi_bias=humi_bias,
            )

            thresholds = await self._async_compute_threshold(
                season=season,
                setpoint_temp_min=dyn_cz["setpoint_temp_min"],
                setpoint_temp_max=dyn_cz["setpoint_temp_max"],
                setpoint_dew_point=dyn_cz["setpoint_dew_point"],
            )

            season_threshold = SeasonThreshold(
                season=season,
                setpoint_temp_min=dyn_cz["setpoint_temp_min"],
                setpoint_temp_max=dyn_cz["setpoint_temp_max"],
                setpoint_humi_min=dyn_cz["setpoint_humi_min"],
                setpoint_humi_max=dyn_cz["setpoint_humi_max"],
                setpoint_dew_point=dyn_cz["setpoint_dew_point"],
                threshold_heating=thresholds["threshold_heating"],
                threshold_cooling=thresholds["threshold_cooling"],
                threshold_dehumidifying=thresholds["threshold_dehumidifying"],
            )

            _LOGGER.debug("Season '%s' thresholds computed: %s", season, season_threshold)

            self._season_threshold_cache[key] = season_threshold
            self._season_threshold_cache_timestamps[key] = _time.time()

    # ---- Internals ------------------------------------------------------------
    async def _maybe_refresh_bias_maps(self) -> None:
        """Refresh seasonal bias if TTL expired or never computed."""
        now = _time.time()
        if self._bias_last_compute_ts is not None and (now - self._bias_last_compute_ts) < self._bias_ttl_s:
            return

        for season, (start_date, end_date) in SEASONS_BY_DATE.items():
            temp_ideal = (
                (CONFORT_ZONES[season][ConfortAttr.T_MIN.value] + CONFORT_ZONES[season][ConfortAttr.T_MAX.value]) / 2.0
            ) + CONFORT_ZONES[season][ConfortAttr.DT.value]

            humi_ideal = (
                (CONFORT_ZONES[season][ConfortAttr.H_MIN.value] + CONFORT_ZONES[season][ConfortAttr.H_MAX.value]) / 2.0
            ) + CONFORT_ZONES[season][ConfortAttr.DH.value]

            tbias = await self._async_compute_bias(
                entity_id=self._ambient_temp_entity_id,
                temp=temp_ideal,
                start_date=start_date,
                end_date=end_date,
            )
            hbias = await self._async_compute_bias(
                entity_id=self._ambient_humi_entity_id,
                temp=humi_ideal,
                start_date=start_date,
                end_date=end_date,
            )

            self._temp_bias_season_map[season] = tbias or 0.0
            self._humi_bias_season_map[season] = hbias or 0.0

        self._bias_last_compute_ts = now
        _LOGGER.debug("Refreshed seasonal bias maps: temp=%s humi=%s", self._temp_bias_season_map, self._humi_bias_season_map)

    async def _async_compute_bias(
        self,
        *,
        entity_id: str,
        temp: float,
        start_date: date,
        end_date: date,
    ) -> Optional[float]:
        """Compute bias for an entity within the season date window (current year)."""
        current_year = date.today().year
        year_diff = end_date.year - start_date.year

        start = start_date.replace(year=current_year)
        end = end_date.replace(year=current_year + (year_diff if year_diff > 0 else 0))

        start_dt = datetime.combine(start, time.min)
        end_dt = datetime.combine(end, time(23, 59, 59))

        start_iso = start_dt.isoformat() + "Z"
        end_iso = end_dt.isoformat() + "Z"

        bias = await self._bias_provider.async_query_entity_bias(
            entity_id=entity_id,
            date_start=start_iso,
            date_stop=end_iso,
            ideal_temp=temp,
        )
        return bias

    async def _async_compute_dynamic_comfort_zone(
        self,
        static_season_comfort_zone: dict,
        *,
        hysteresis_factor: float,
        temp_bias: float,
        humi_bias: float,
    ) -> dict:
        """Apply bias and hysteresis to static comfort band to derive dynamic setpoints."""
        cz_t_min = static_season_comfort_zone[ConfortAttr.T_MIN.value]
        cz_t_max = static_season_comfort_zone[ConfortAttr.T_MAX.value]
        cz_dt = static_season_comfort_zone[ConfortAttr.DT.value]
        cz_h_min = static_season_comfort_zone[ConfortAttr.H_MIN.value]
        cz_h_max = static_season_comfort_zone[ConfortAttr.H_MAX.value]
        cz_dh = static_season_comfort_zone[ConfortAttr.DH.value]
        cz_dp_min = static_season_comfort_zone[ConfortAttr.DP.value]

        cz_temp_avg = mean([cz_t_min, cz_t_max])
        cz_hum_avg = mean([cz_h_min, cz_h_max])

        # Expand/narrow deltas by hysteresis factor
        hysteresis_delta_temp = cz_dt * hysteresis_factor
        hysteresis_delta_hum = cz_dh * hysteresis_factor

        dynamic_t_min = round(cz_temp_avg + temp_bias - hysteresis_delta_temp, 1)
        dynamic_t_max = round(cz_temp_avg + temp_bias + hysteresis_delta_temp, 1)

        dynamic_h_min = round(cz_hum_avg + humi_bias - hysteresis_delta_hum, 1)
        dynamic_h_max = round(cz_hum_avg + humi_bias + hysteresis_delta_hum, 1)

        # Compute dynamic dew point (ensure it's not below seasonal minimum)
        dynamic_dew_point = dew_point(dynamic_t_max, dynamic_h_max)
        if dynamic_dew_point < cz_dp_min:
            dynamic_dew_point = cz_dp_min
            # Recompute max humidity so that dp = dp_min at T_max
            dynamic_h_max = round(relative_humidity(dynamic_t_max, dynamic_dew_point), 1)

        return {
            "setpoint_temp_min": dynamic_t_min,
            "setpoint_temp_max": dynamic_t_max,
            "setpoint_humi_min": dynamic_h_min,
            "setpoint_humi_max": dynamic_h_max,
            "setpoint_dew_point": dynamic_dew_point,
        }

    async def _async_compute_threshold(
        self,
        *,
        season: Seasons,
        setpoint_temp_min: float,
        setpoint_temp_max: float,
        setpoint_dew_point: float,
    ) -> Dict[str, Optional[HvacThreshold]]:
        """Build HVAC thresholds for the season, including dehumidifying if applicable."""
        heating: Optional[HvacThreshold] = None
        cooling: Optional[HvacThreshold] = None
        dehumidifying: Optional[HvacThreshold] = None

        # Enable dehumidifying in moist seasons
        if season in (Seasons.SUMMER, Seasons.AUTUMN, Seasons.SPRING):
            dew_point_on = round(setpoint_dew_point + 0.0, 1)
            dew_point_off = round(setpoint_dew_point - 1.5, 1)
            dehumidifying = HvacThreshold(
                mode="dehumidifying",
                state_on=dew_point_on,
                state_off=dew_point_off,
            )

        if season is Seasons.WINTER:
            heating = HvacThreshold(
                mode=HEATING,
                state_on=setpoint_temp_min,
                state_off=setpoint_temp_max,
            )
        elif season is Seasons.SUMMER:
            cooling = HvacThreshold(
                mode=COOLING,
                state_on=round(setpoint_temp_max - 1.0, 1),
                state_off=round(setpoint_temp_min + 0.5, 1),
            )
        elif season is Seasons.SPRING:
            cooling = HvacThreshold(
                mode=COOLING,
                state_on=round(CONFORT_ZONES[Seasons.SUMMER][ConfortAttr.T_MIN.value] + 1.5, 1),
                state_off=round(CONFORT_ZONES[Seasons.SUMMER][ConfortAttr.T_MIN.value] + 0.5, 1),
            )
            heating = HvacThreshold(
                mode=HEATING,
                state_on=round(
                    CONFORT_ZONES[Seasons.WINTER][ConfortAttr.T_MIN.value]
                    - (CONFORT_ZONES[Seasons.WINTER][ConfortAttr.DT.value] * 2),
                    1,
                ),
                state_off=setpoint_temp_min,
            )
        elif season is Seasons.AUTUMN:
            heating = HvacThreshold(
                mode=HEATING,
                state_on=round(
                    setpoint_temp_min - (CONFORT_ZONES[Seasons.AUTUMN][ConfortAttr.DT.value] * 2),
                    1,
                ),
                state_off=round(setpoint_temp_min + 0.5, 1),
            )
            cooling = HvacThreshold(
                mode=COOLING,
                state_on=round(CONFORT_ZONES[Seasons.SUMMER][ConfortAttr.T_MIN.value] + 1.5, 1),
                state_off=round(CONFORT_ZONES[Seasons.SUMMER][ConfortAttr.T_MIN.value] + 0.5, 1),
            )

        return {
            "threshold_heating": heating,
            "threshold_cooling": cooling,
            "threshold_dehumidifying": dehumidifying,
        }

    def _hysteresis_for(self, season: Seasons, mode: str) -> float:
        """Get hysteresis factor for (season, mode), fallback to 'comfort'."""
        season_map = self._hysteresis_season_map.get(season, {})
        if mode in season_map:
            return season_map[mode]
        # Fallbacks
        if "comfort" in season_map:
            return season_map["comfort"]
        # Last resort: neutral factor
        return 1.0
