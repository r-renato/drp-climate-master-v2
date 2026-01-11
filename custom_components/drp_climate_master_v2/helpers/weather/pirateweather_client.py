"""Pirate Weather Time Machine (daily) — Home Assistant ready.

This module is designed to be dropped into a Home Assistant custom integration.

Key goals (vs. a standalone aiohttp ClientSession):
- Uses HA-managed aiohttp session via homeassistant.helpers.aiohttp_client.
- Does NOT create/close the shared session.
- Keeps your behavior: retries + exponential backoff + jitter + Retry-After support.
- Avoids leaking the API key in logs (redacted URLs).
- Optional: concurrency limiting + single-flight dedup for multi-day fetch.
- Optional: DataUpdateCoordinator wrapper for periodic refresh.

Typical usage (inside your integration setup):

    session = async_get_clientsession(hass)
    cfg = PirateWeatherConfig(api_key=..., lat=..., lon=..., units="si")
    opts = ProviderOptions(max_concurrency=6, retries=2, http_timeout_s=20)
    client = PirateWeatherTimeMachineClient(cfg, opts, session)

    data = await client.async_fetch_days([date.today() - timedelta(days=i) for i in range(7)])

"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import random
import time
from typing import Any, Callable, Coroutine, Optional, cast
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from custom_components.drp_climate_master_v2.helpers.timeutils import as_iso_local

from ...domain.models.weather import Forecast, Historical

from ..logger import log_debug, log_warning
from ..utils import as_float, ratio_or_percent_to_int

_LOGGER = logging.getLogger(__name__)


# ---------------------------
# Types & helpers
# ---------------------------

# class Forecast(TypedDict, total=False):
#     """Normalized daily forecast."""

#     datetime: str  # ISO, local midnight
#     temp_max: float
#     temp_min: float
#     dew_point: float
#     humidity: int  # 0..100
#     wind_speed: float
#     wind_gust_speed: float
#     uv_index: float
#     precipitation_accumulation: float  # mm/day (internal)

def _jittered_backoff(base: float, factor: float, attempt: int, jitter_frac: float) -> float:
    delay = base * (factor ** attempt)
    jitter = delay * jitter_frac
    return max(0.0, delay + random.uniform(-jitter, jitter))


def _parse_retry_after(value: Optional[str]) -> float:
    """Support seconds or HTTP-date. Return 0.0 if unparsable."""
    if not value:
        return 0.0
    try:
        sec = float(value)
        if sec >= 0:
            return sec
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            now = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - now).total_seconds())
    except Exception:
        pass

    return 0.0


def _timeout_cm(seconds: float):
    """Compatibility timeout context manager.

    - On Python 3.11+ use asyncio.timeout.
    - Fallback to async_timeout on older environments.
    """
    try:
        timeout = asyncio.timeout  # type: ignore[attr-defined]
        return timeout(seconds)
    except Exception:
        import async_timeout  # type: ignore

        return async_timeout.timeout(seconds)


# ---------------------------
# Config dataclasses
# ---------------------------

@dataclass(slots=True, frozen=True)
class PirateWeatherConfig:
    """Pirate Weather Time Machine config."""

    api_key: str
    lat: float
    lon: float
    units: str = "si"  # "si" or "us"
    base_url: str = "https://timemachine.pirateweather.net/forecast"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ValueError("Invalid api_key: must be a non-empty string")

        if not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lon <= 180.0):
            raise ValueError(f"Invalid lat/lon: {self.lat}, {self.lon}")

        if self.units not in {"si", "us"}:
            raise ValueError(f"Invalid units: {self.units} (allowed: 'si', 'us')")

        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid base_url: {self.base_url} (expected http(s)://host[/...])")

        if self.base_url.endswith("/"):
            object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


@dataclass(slots=True, frozen=True)
class ProviderOptions:
    """Runtime options: concurrency, retries, backoff, timeouts."""

    max_concurrency: int = 6    # Max number of concurrent in-flight HTTP operations (via asyncio.Semaphore). Must be >= 1.
    retries: int = 0            # Number of retries *after* the first attempt. Total tries = retries + 1.
    backoff_base: float = 0.5   # Base delay (seconds) for exponential backoff: base * factor**attempt.
    backoff_factor: float = 2.0 # Exponential multiplier for backoff (>= 1.0). 2.0 doubles each retry.
    jitter: float = 0.25        # Symmetric fractional jitter (0..1) applied to backoff to avoid thundering herd.
    http_timeout_s: int = 20    # Per-request timeout in seconds (applied with asyncio.timeout/async_timeout).

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.backoff_base <= 0:
            raise ValueError("backoff_base must be > 0")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter must be between 0.0 and 1.0 (inclusive)")
        if self.http_timeout_s <= 0:
            raise ValueError("http_timeout_s must be > 0")

    @property
    def total_tries(self) -> int:
        return self.retries + 1


# ---------------------------
# Single-flight (dedupe)
# ---------------------------

class _SingleFlight:
    """Deduplicate concurrent requests for the same key."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._inflight: dict[date, asyncio.Future] = {}

    async def _cleanup_key(self, key: date) -> None:
        async with self._lock:
            self._inflight.pop(key, None)

    async def run(self, key: date, coro_factory: Callable[[], Coroutine[Any, Any, Any]]):
        async with self._lock:
            fut = self._inflight.get(key)
            if fut is None or fut.cancelled():
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._inflight[key] = fut

                async def _runner() -> None:
                    try:
                        res = await coro_factory()
                        if not fut.cancelled() and not fut.done():
                            fut.set_result(res)
                    except asyncio.CancelledError:
                        if not fut.cancelled() and not fut.done():
                            fut.cancel()
                        raise
                    except Exception as e:  # noqa: BLE001
                        if not fut.cancelled() and not fut.done():
                            fut.set_exception(e)
                    finally:
                        asyncio.create_task(self._cleanup_key(key))

                asyncio.create_task(_runner())

        return await fut


# ---------------------------
# Day client (one day only)
# ---------------------------

class PirateWeatherDayClient:
    """HTTP client for a single day request.

    IMPORTANT:
    - This client does NOT own the aiohttp session.
    - The session must be provided (ideally HA shared session).
    """

    NON_RETRYABLE_STATUSES = {
        400, 401, 403, 404, 405, 406, 409, 410, 411, 413, 414, 415, 422, 501, 505
    }
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, cfg: PirateWeatherConfig, opts: ProviderOptions, session: aiohttp.ClientSession) -> None:
        self._cfg = cfg
        self._opts = opts
        self._session = session

    @classmethod
    def from_hass(
        cls,
        hass: HomeAssistant,
        cfg: PirateWeatherConfig,
        opts: ProviderOptions | None = None,
    ) -> "PirateWeatherDayClient":
        session = async_get_clientsession(hass)
        return cls(cfg, opts or ProviderOptions(), session)

    def _build_url(self, when: datetime) -> str:
        iso = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return f"{self._cfg.base_url}/{self._cfg.api_key}/{self._cfg.lat},{self._cfg.lon},{iso}"

    def _safe_url_for_log(self, when: datetime) -> str:
        iso = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        # redact key
        return f"{self._cfg.base_url}/***REDACTED***/{self._cfg.lat},{self._cfg.lon},{iso}"

    @staticmethod
    def _failed_forecast(d: date) -> Forecast:
        # datetime obbligatoria nel tuo TypedDict, failed flag per downstream
        return cast(Forecast, {
            "datetime": as_iso_local(d),
            "failed": 1,
        })
    
    @staticmethod
    def _parse_daily_payload(d: date, payload: dict[str, Any], *, units: str) -> Optional[Forecast]:
        """Parse PirateWeather TimeMachine payload into a daily Forecast for local day `d`.

        Goals (robust for "ultimo periodo" storico):
        - Validate structure (daily/data), and verify units via flags.units.
        - Select the *correct* daily record matching `d` using payload timezone (not blindly data[0]).
        - Extract daily min/max, dew point, wind, pressure, cloud cover, precip, humidity, uv.
        - Provide sensible fallbacks when daily fields are missing (hourly aggregation; last resort estimates).

        Notes:
        - The caller should pass `d` in the *location* calendar (ideally same as payload timezone).
        - Returned dict uses your Forecast/Historical TypedDict keys (native_* fields).
        """

        # --- Validate daily block
        daily = payload.get("daily")
        if not isinstance(daily, dict):
            log_warning(_LOGGER, "PW daily(%s) missing daily in payload", d)
            return None

        data = daily.get("data")
        if not isinstance(data, list) or not data:
            log_warning(_LOGGER, "PW daily(%s) missing daily.data in payload", d)
            return None

        # --- Validate units (BUGFIX: flags is a dict; compare flags['units'] to `units`)
        flags = payload.get("flags")
        units_in_payload: str | None = None
        if isinstance(flags, dict):
            u = flags.get("units")
            units_in_payload = u if isinstance(u, str) else None

        # If the payload declares units and it doesn't match, fail fast (avoids silent wrong conversions)
        if units_in_payload is not None and units_in_payload != units:
            log_warning(
                _LOGGER,
                "PW daily(%s) unexpected units flag: %s (expected: %s)",
                d,
                units_in_payload,
                units,
            )
            return None

        # --- Timezone handling (use payload timezone when matching the day)
        tz_name = payload.get("timezone")
        tz = dt_util.get_time_zone(tz_name) if isinstance(tz_name, str) else None
        tz = tz or dt_util.DEFAULT_TIME_ZONE

        def _day_from_epoch(item_time: Any) -> date | None:
            """Convert epoch seconds -> date in payload timezone."""
            t = as_float(item_time, default=None)
            if t is None:
                return None
            try:
                dt_utc = datetime.fromtimestamp(float(t), tz=dt_util.UTC)
                return dt_utc.astimezone(tz).date()
            except Exception:
                return None

        # --- Pick the right daily item for `d`
        di: dict[str, Any] | None = None
        for it in data:
            if not isinstance(it, dict):
                continue
            it_day = _day_from_epoch(it.get("time"))
            if it_day == d:
                di = it
                break

        if di is None:
            # Fallback: some responses return only one element; use data[0] but warn.
            first = data[0] if data else None
            if isinstance(first, dict):
                di = first
                it_day = _day_from_epoch(di.get("time"))
                log_warning(
                    _LOGGER,
                    "PW daily(%s) no exact day match in daily.data (picked %s)",
                    d,
                    it_day,
                )
            else:
                log_warning(_LOGGER, "PW daily(%s) daily.data[0] not a dict", d)
                return None

        # --- Helpers: hourly aggregation for the requested day
        hourly = payload.get("hourly")
        hourly_data = hourly.get("data") if isinstance(hourly, dict) else None

        def _hourly_items_for_day(day: date) -> list[dict[str, Any]]:
            if not isinstance(hourly_data, list) or not hourly_data:
                return []
            out: list[dict[str, Any]] = []
            for h in hourly_data:
                if not isinstance(h, dict):
                    continue
                hd = _day_from_epoch(h.get("time"))
                if hd == day:
                    out.append(h)
            return out

        h_items = _hourly_items_for_day(d)

        # --- Extract core daily values (prefer Min/Max, fallback to Low/High)
        tmin = as_float(di.get("temperatureMin"), default=None)
        if tmin is None:
            tmin = as_float(di.get("temperatureLow"), default=None)

        tmax = as_float(di.get("temperatureMax"), default=None)
        if tmax is None:
            tmax = as_float(di.get("temperatureHigh"), default=None)

        dp = as_float(di.get("dewPoint"), default=None)
        pressure = as_float(di.get("pressure"), default=None)

        ws = as_float(di.get("windSpeed"), default=None)
        wg = as_float(di.get("windGust"), default=None)

        wind_bearing = di.get("windBearing")
        # Normalize wind bearing: accept float/int/str; coerce numeric when possible
        if wind_bearing is not None and not isinstance(wind_bearing, str):
            wind_bearing = as_float(wind_bearing, default=wind_bearing)

        cloud = ratio_or_percent_to_int(as_float(di.get("cloudCover"), default=None))
        precip_prob = ratio_or_percent_to_int(as_float(di.get("precipProbability"), default=None))

        # UV index (daily) or fallback to hourly max
        uvi = as_float(di.get("uvIndex"), default=None)
        if uvi is None and h_items:
            vals = [as_float(h.get("uvIndex"), default=None) for h in h_items]
            vals2 = [v for v in vals if v is not None]
            if vals2:
                uvi = max(vals2)

        # Humidity (daily), fallback to hourly mean, fallback to estimate from T & dew point
        rh_int: int | None = ratio_or_percent_to_int(as_float(di.get("humidity"), default=None))

        if rh_int is None and h_items:
            hrs = [ratio_or_percent_to_int(as_float(h.get("humidity"), default=None)) for h in h_items]
            hrs2 = [x for x in hrs if x is not None]
            if hrs2:
                rh_int = int(round(sum(hrs2) / len(hrs2)))

        if rh_int is None and dp is not None:
            # Very rough fallback: estimate RH from dew point and mean temperature.
            # RH = 100 * es(Td)/es(T), using Magnus formula (good enough for control heuristics).
            # Pick Tmean as (tmin+tmax)/2 when available, else cannot estimate.
            if tmin is not None and tmax is not None:
                t_mean = 0.5 * (float(tmin) + float(tmax))
                try:
                    import math

                    def _es(temp_c: float) -> float:
                        # hPa proportional; ratio cancels out constants
                        return math.exp((17.625 * temp_c) / (243.04 + temp_c))

                    rh_est = 100.0 * (_es(float(dp)) / _es(float(t_mean)))
                    rh_int = int(round(max(0.0, min(100.0, rh_est))))
                except Exception:
                    rh_int = None

        # Precip accumulation: prefer daily precipAccumulation, else integrate hourly precipIntensity.
        pacc = as_float(di.get("precipAccumulation"), default=None)
        if pacc is None and h_items:
            total = 0.0
            any_pi = False
            for h in h_items:
                pi = as_float(h.get("precipIntensity"), default=None)
                if pi is None:
                    continue
                any_pi = True
                total += float(pi)  # per-hour intensity

            if any_pi:
                # In SI, precipIntensity is typically mm/h -> sum over hours = mm.
                # In US, precipIntensity is inch/h -> sum over hours = inch, convert to mm.
                pacc = total * (25.4 if units == "us" else 1.0)

        if pacc is None:
            # Last-resort fallback (coarse): daily precipIntensity * 24
            pi = as_float(di.get("precipIntensity"), default=None)
            if pi is not None:
                pacc = float(pi) * 24.0 * (25.4 if units == "us" else 1.0)

        # Condition: keep provider icon if present; summary is often free-form.
        condition = di.get("icon") if isinstance(di.get("icon"), str) else None

        # --- Build Forecast/Historical (only include non-None)
        dt_iso = as_iso_local(d)
        if dt_iso is None:
            # ultra-defensive fallback
            dt_iso = dt_util.start_of_local_day(d).isoformat()

        base: dict[str, Any] = {
            "datetime": dt_iso,
            "timezone": tz_name,
            "units": units,
        }
        optional: dict[str, Any] = {
            "condition": condition,
            "humidity": float(rh_int) if rh_int is not None else None,
            "precipitation_probability": precip_prob,
            "cloud_coverage": cloud,
            "native_precipitation": float(pacc) if pacc is not None else None,
            "native_pressure": float(pressure) if pressure is not None else None,
            "native_temperature": float(tmax) if tmax is not None else None,
            "native_templow": float(tmin) if tmin is not None else None,
            "native_dew_point": float(dp) if dp is not None else None,
            "uv_index": float(uvi) if uvi is not None else None,
            "native_wind_speed": float(ws) if ws is not None else None,
            "native_wind_gust_speed": float(wg) if wg is not None else None,
            "wind_bearing": wind_bearing,
        }

        base.update({k: v for k, v in optional.items() if v is not None})
        return cast(Forecast, base)


    async def async_fetch_day(self, d: date) -> tuple[Forecast, Optional[float]]:
        """Fetch one day with retries.

        Returns (forecast|None, retry_after_s|None).
        """
        start_t = time.perf_counter()
        res: Optional[Forecast] = None
        last_retry_after_s: float = 0.0
        status: int | str = "n/a"

        when = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
        url = self._build_url(when)
        safe_url = self._safe_url_for_log(when)

        params = {
            "units": self._cfg.units,
            "exclude": "hourly,minutely,alerts,flags,currently",
        }
        headers = {"Accept": "application/json"}

        for attempt in range(self._opts.total_tries):
            try:
                async with _timeout_cm(self._opts.http_timeout_s):
                    async with self._session.get(url, params=params, headers=headers) as resp:
                        status = resp.status

                        if status == 200:
                            try:
                                payload: dict[str, Any] = await resp.json()
                            except aiohttp.ContentTypeError:
                                payload = {}
                            except Exception as e:  # noqa: BLE001
                                log_debug(_LOGGER, "PW daily(%s) json error: %r", d, e)
                                payload = {}

                            res = self._parse_daily_payload(d, payload, units=self._cfg.units)
                            if res is not None:
                                break

                            # Payload unexpected: treat as transient (unless last try)
                            if attempt >= self._opts.retries:
                                log_warning(_LOGGER, "PW daily(%s) 200 but payload invalid; url=%s", d, safe_url)
                                break

                        elif status in self.RETRYABLE_STATUSES:
                            ra = _parse_retry_after(resp.headers.get("Retry-After"))
                            last_retry_after_s = max(last_retry_after_s, ra)

                            if attempt >= self._opts.retries:
                                log_warning(_LOGGER,
                                    "PW daily(%s) retryable status=%s; Retry-After=%.1fs url=%s",
                                    d,
                                    status,
                                    ra,
                                    safe_url,
                                )
                                break

                            sleep_s = max(
                                ra,
                                _jittered_backoff(
                                    self._opts.backoff_base,
                                    self._opts.backoff_factor,
                                    attempt,
                                    self._opts.jitter,
                                ),
                            )
                            log_debug(_LOGGER,
                                "PW daily(%s) retryable status=%s attempt=%d sleep=%.2fs",
                                d,
                                status,
                                attempt,
                                sleep_s,
                            )
                            await asyncio.sleep(sleep_s)
                            continue

                        elif status in self.NON_RETRYABLE_STATUSES:
                            log_warning(_LOGGER, "PW daily(%s) non-retryable status=%s url=%s", d, status, safe_url)
                            break

                        else:
                            # Unknown status -> conservative retry
                            if attempt >= self._opts.retries:
                                log_warning(_LOGGER, "PW daily(%s) unexpected status=%s url=%s", d, status, safe_url)
                                break

                            sleep_s = _jittered_backoff(
                                self._opts.backoff_base,
                                self._opts.backoff_factor,
                                attempt,
                                self._opts.jitter,
                            )
                            log_debug(_LOGGER,
                                "PW daily(%s) unexpected status=%s attempt=%d sleep=%.2fs",
                                d,
                                status,
                                attempt,
                                sleep_s,
                            )
                            await asyncio.sleep(sleep_s)
                            continue

            except asyncio.TimeoutError as e:
                status = "timeout"
                if attempt >= self._opts.retries:
                    log_warning(_LOGGER, "PW daily(%s) timeout: %r url=%s", d, e, safe_url)
                    break
                sleep_s = _jittered_backoff(self._opts.backoff_base, self._opts.backoff_factor, attempt, self._opts.jitter)
                log_debug(_LOGGER, "PW daily(%s) timeout attempt=%d sleep=%.2fs", d, attempt, sleep_s)
                await asyncio.sleep(sleep_s)

            except aiohttp.ClientError as e:
                status = type(e).__name__
                if attempt >= self._opts.retries:
                    log_warning(_LOGGER, "PW daily(%s) aiohttp error (%s): %r url=%s", d, status, e, safe_url)
                    break
                sleep_s = _jittered_backoff(self._opts.backoff_base, self._opts.backoff_factor, attempt, self._opts.jitter)
                log_debug(_LOGGER, "PW daily(%s) aiohttp error %s attempt=%d sleep=%.2fs", d, status, attempt, sleep_s)
                await asyncio.sleep(sleep_s)

            except Exception as e:  # noqa: BLE001
                status = type(e).__name__
                if attempt >= self._opts.retries:
                    log_warning(_LOGGER,
                        "PW daily(%s) exception (%s): %r url=%s",
                        d,
                        status,
                        e,
                        safe_url,
                        exc_info=True,
                    )
                    break
                sleep_s = _jittered_backoff(self._opts.backoff_base, self._opts.backoff_factor, attempt, self._opts.jitter)
                log_debug(_LOGGER, "PW daily(%s) exception %s attempt=%d sleep=%.2fs", d, status, attempt, sleep_s)
                await asyncio.sleep(sleep_s)

        # Se dopo tutti i tentativi non abbiamo dati validi, restituiamo Forecast "failed"
        if res is None:
            res = self._failed_forecast(d)

        log_debug(_LOGGER,
            "PW daily(%s): status=%s elapsed=%.1fms ok=%s failed=%s",
            d,
            status,
            (time.perf_counter() - start_t) * 1000.0,
            res.get("failed") != 1,
            res.get("failed"),
        )

        return res, (last_retry_after_s if res.get("failed") == 1 and last_retry_after_s > 0 else None)

# ---------------------------
# Multi-day client (concurrency + single-flight)
# ---------------------------

class PirateWeatherTimeMachineClient:
    """Orchestrates multiple day fetches.

    - Concurrency limit via semaphore.
    - Single-flight dedup for the same day.
    """

    def __init__(
        self,
        cfg: PirateWeatherConfig,
        opts: ProviderOptions | None,
        session: aiohttp.ClientSession,
    ) -> None:
        self._cfg = cfg
        self._opts = opts or ProviderOptions()
        self._session = session
        self._day = PirateWeatherDayClient(cfg, self._opts, session)
        self._sem = asyncio.Semaphore(self._opts.max_concurrency)
        self._sf = _SingleFlight()

    @classmethod
    def from_hass(
        cls,
        hass: HomeAssistant,
        cfg: PirateWeatherConfig,
        opts: ProviderOptions | None = None,
    ) -> "PirateWeatherTimeMachineClient":
        session = async_get_clientsession(hass)
        return cls(cfg, opts, session)

    async def _fetch_one(self, d: date) -> Optional[Forecast]:
        async with self._sem:
            fc, retry_after = await self._day.async_fetch_day(d)
            if fc.get("failed") == 1 and retry_after:
                log_debug(_LOGGER, "PW daily(%s) suggests cooldown %.1fs", d, retry_after)
            return fc


    async def async_fetch_day(self, d: date) -> Optional[Forecast]:
        return await self._sf.run(d, lambda: self._fetch_one(d))

    async def async_fetch_days(self, days: list[date]) -> dict[date, Historical]:
        """Fetch multiple days and return only successful ones."""
        # Preserve order is not required; return mapping.
        tasks = [asyncio.create_task(self.async_fetch_day(d)) for d in days]

        # With return_exceptions=True, aio hints this as T | BaseException.
        results: list[Forecast | None | BaseException] = await asyncio.gather(*tasks, return_exceptions=True)

        out: dict[date, Forecast] = {}
        for d, r in zip(days, results):
            # Never swallow cancellations: HA uses cancellations for shutdown/unload.
            if isinstance(r, asyncio.CancelledError):
                raise r

            # Pylance expects BaseException here (not just Exception).
            if isinstance(r, BaseException):
                log_debug(_LOGGER, "PW daily(%s) task failed: %r", d, r)
                continue

            if r is None:
                continue

            out[d] = r

        return out
