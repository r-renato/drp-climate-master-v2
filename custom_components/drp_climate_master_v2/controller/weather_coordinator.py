from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any, Callable, Literal, Mapping, Optional, TypedDict, cast

from homeassistant.core import HomeAssistant, CALLBACK_TYPE, Event, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.debounce import Debouncer
from homeassistant.util import dt as dt_util
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from ..domain.models.season import SeasonState
from ..domain.models.weather import Forecast, Historical

from ..helpers.season.season_calendar import CalendarSeason
from ..helpers.season.season_weather import MeteoContiguousSeasonModel, forecast_legacy_to_native
from ..helpers.logger import log_debug, log_exception, log_info, log_warning
from ..helpers.timeutils import as_iso_local
from ..helpers.cache import JsonObject, Codec, PersistentCache
from ..helpers.weather.pirateweather_client import (
    PirateWeatherConfig,
    PirateWeatherTimeMachineClient,
    ProviderOptions,
)

from .coordinator import ClimateCoordinator
from ..const import DOMAIN, SEASON_STATE

ForecastType = Literal["daily", "hourly", "twice_daily"]

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WeatherCoordinatorConfig:
    """Runtime config for weather updates."""

    days_back: int = 730

    # How often to *check* if a daily update is due.
    # (Remote fetch is gated to once every 24h via meta store.)
    update_interval: timedelta = timedelta(hours=6)

    # Extra trigger path (climate updates) is gated by age.
    refresh_on_climate_update_if_older_than: timedelta = timedelta(hours=2)

    # Cache settings
    cache_persist: bool = True
    cache_max_days: int = 740
    cache_save_cooldown_s: float = 60.0

    # Policy: always re-fetch today's day (provider may update intraday)
    always_refresh_today: bool = True

    # Hard requirement: attempt an update every 24 hours (persisted across restarts)
    daily_interval: timedelta = timedelta(hours=24)

    # --- FIXES: throttle heavy season detection + debounce HVAC decider ---

    # Min interval between expensive season detection runs (cache dump + get_forecasts + model.fit).
    # With ClimateCoordinator updates every 5 minutes, 30 minutes is a safe default.
    season_detect_min_interval: timedelta = timedelta(minutes=30)

    # Debounce for HVAC decider (prevents feedback loops / repeated work).
    decider_debounce_cooldown_s: float = 20.0


class WeatherCoordinatorMetaConfig(TypedDict, total=False):
    last_run_utc: str


class WeatherCoordinator:
    """Weather orchestrator: periodic refresh + cache + attach to ClimateCoordinator.

    Fixes included:
    - Dedup + throttle for expensive season detection.
    - Proper UTC handling for last daily run.
    - Debounced HVAC decider to avoid feedback loops.
    - Daily tick rescheduling bug fix.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ClimateCoordinator,
        wc_cfg: WeatherCoordinatorConfig | None = None,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator

        self._entry_id = self._coordinator.entry_id
        self._unit_system = self._coordinator.unit_system
        self._runtime_weather_config = self._coordinator.runtime_weather_config

        self._weather_forecast_provider = self._runtime_weather_config.forecast_data.provider

        self._pw_cfg = PirateWeatherConfig(
            lat=self._runtime_weather_config.historical_data.latitude,
            lon=self._runtime_weather_config.historical_data.longitude,
            api_key=self._runtime_weather_config.historical_data.token,
            units=self._coordinator.unit_system,
        )
        self._pw_opts = ProviderOptions()

        self._wc_cfg = wc_cfg or WeatherCoordinatorConfig()
        self._pw_client = PirateWeatherTimeMachineClient.from_hass(self._hass, self._pw_cfg, self._pw_opts)

        store_cache_key = (
            f"{DOMAIN}.pirateweather.historical."
            f"{self._unit_system}."
            f"{self._runtime_weather_config.historical_data.latitude:.4f}."
            f"{self._runtime_weather_config.historical_data.longitude:.4f}"
        )

        historical_codec: Codec[Historical] = Codec(
            encode=lambda f: cast(JsonObject, dict(f)),
            decode=lambda raw: cast(Historical, dict(raw)),
            clone=lambda f: cast(Historical, dict(f)),
        )

        self._cache = PersistentCache[Historical](
            hass,
            key=store_cache_key,
            version=1,
            codec=historical_codec,
            save_cooldown_s=self._wc_cfg.cache_save_cooldown_s,
            max_days=self._wc_cfg.cache_max_days,
            persist=self._wc_cfg.cache_persist,
        )

        self._meta_data_store: Store = Store(hass, version=1, key=f"{store_cache_key}.meta")

        self._unsub_coordinator: CALLBACK_TYPE | None = None
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_daily: CALLBACK_TYPE | None = None

        # Refresh dedup
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[dict[date, Historical]] | None = None

        # Daily gating lock
        self._daily_lock = asyncio.Lock()

        # NOTE: always stored as UTC
        self._last_daily_run_utc: datetime | None = None

        # Season detect dedup + throttle
        self._season_lock = asyncio.Lock()
        self._season_task: asyncio.Task[None] | None = None
        self._last_season_detect_utc: datetime | None = None

        self.data: dict[date, Historical] = {}

        # Debounced decider (prevents loops and reduces CPU)
        self._decider_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=self._wc_cfg.decider_debounce_cooldown_s,
            immediate=False,
            function=self._decide_and_act,
        )

        self._unsub_hastarted_event: Optional[Callable[[], None]] = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
        )

        log_info(
            _LOGGER,
            "Initialized (id=%s) entry=%s unit=%s update_interval=%s",
            hex(id(self)),
            self._entry_id,
            self._unit_system,
            self._wc_cfg.update_interval,
        )

    def _set_season_state(self, season_state: SeasonState) -> None:
        domain_store = self._hass.data.setdefault(DOMAIN, {})
        entry_store = domain_store.setdefault(self._entry_id, {})
        entry_store[SEASON_STATE] = season_state

    # -----------------------------
    # Lifecycle
    # -----------------------------

    @callback
    def _on_ha_started(self, event: Event) -> None:
        self._hass.async_create_task(self._async_weather_first_refresh(event))

    async def _async_weather_first_refresh(self, event: Event) -> None:
        await self._async_start()
        # Do NOT run heavy season detect repeatedly during startup storms; schedule once.
        self._schedule_season_detect("startup")
        log_info(_LOGGER, "Done.")

    async def _async_start(self) -> None:
        await self._cache.async_load()
        await self._meta_load()

        if self._unsub_coordinator is None:
            self._unsub_coordinator = self._coordinator.async_add_listener(self._on_coordinator_update)

        # Schedule the first daily tick
        self._schedule_next_daily_tick()

    async def async_stop(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval = None

        if self._unsub_daily is not None:
            self._unsub_daily()
            self._unsub_daily = None

        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None

        if self._season_task and not self._season_task.done():
            self._season_task.cancel()
            try:
                await self._season_task
            except asyncio.CancelledError:
                pass
            self._season_task = None

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

        await self._cache.async_shutdown(flush=True)

    # -----------------------------
    # Meta store (daily gating)
    # -----------------------------

    async def _meta_load(self) -> None:
        meta = await self._meta_data_store.async_load() or {}
        iso = meta.get("last_run_utc")
        if not isinstance(iso, str):
            self._last_daily_run_utc = None
            return
        dt = dt_util.parse_datetime(iso)
        self._last_daily_run_utc = dt_util.as_utc(dt) if dt else None

    async def _meta_save_last_run(self, when_utc: datetime) -> None:
        when_utc = dt_util.as_utc(when_utc)
        await self._meta_data_store.async_save({"last_run_utc": when_utc.isoformat()})

    def _next_due_utc(self) -> datetime:
        now = dt_util.utcnow()
        if self._last_daily_run_utc is None:
            return now
        due = self._last_daily_run_utc + self._wc_cfg.daily_interval
        return now if due <= now else due

    def _schedule_next_daily_tick(self) -> None:
        if self._unsub_daily is not None:
            return
        when = self._next_due_utc()
        log_debug(_LOGGER, "Scheduling next daily weather tick at %s (UTC)", when.isoformat())
        self._unsub_daily = async_track_point_in_time(self._hass, self._handle_daily_tick, when)

    async def _handle_daily_tick(self, _now) -> None:
        # FIX: unsubscribe first, so reschedule works correctly.
        if self._unsub_daily is not None:
            self._unsub_daily()
            self._unsub_daily = None

        await self.async_refresh_weather_daily(reason="daily_tick")
        self._schedule_season_detect("daily_tick")

        # always reschedule
        self._schedule_next_daily_tick()

    # -----------------------------
    # Triggers
    # -----------------------------

    def _should_refresh_on_climate_update(self) -> bool:
        if self._last_daily_run_utc is None:
            log_info(_LOGGER, "No previous weather update, refreshing on climate update.")
            return True
        age = dt_util.utcnow() - self._last_daily_run_utc
        log_debug(_LOGGER, "Last weather update age: %s / %s", age, self._wc_cfg.refresh_on_climate_update_if_older_than)
        return age >= self._wc_cfg.refresh_on_climate_update_if_older_than

    def _on_coordinator_update(self) -> None:
        """Called when ClimateCoordinator publishes an update (every ~5 minutes)."""

        # async def _async_handle() -> None:
        #     await self.async_refresh_weather_daily(reason="climate_update")
        #     # IMPORTANT: season detection is expensive; throttle+dedup.
        #     self._schedule_season_detect("climate_update")

        # if self._should_refresh_on_climate_update():
        #     log_debug(_LOGGER, "Climate coordinator update triggered weather daily gate check.")
        #     self._hass.async_create_task(_async_handle())
        # else:
        #     # Even if no refresh is due, we may still want occasional season updates,
        #     # but never on every 5-min tick.
        #     self._schedule_season_detect("climate_update_no_refresh")

        # Debounced HVAC decider (prevents feedback loop storms)
        self._schedule_decider()

    # -----------------------------
    # Season detection (dedup + throttle)
    # -----------------------------

    def _should_run_season_detect(self) -> bool:
        now = dt_util.utcnow()
        
        if self._last_season_detect_utc is None:
            return True
        
        return (now - self._last_season_detect_utc) >= self._wc_cfg.season_detect_min_interval

    def _schedule_season_detect(self, reason: str) -> None:
        # Dedup: if already running, do nothing.
        if self._season_task and not self._season_task.done():
            log_warning(_LOGGER, "Dedup: if already running, do nothing.")
            return
        
        # Throttle: run at most once per configured interval.
        if not self._should_run_season_detect():
            return
        
        self._season_task = self._hass.async_create_task(
            target=self._async_season_detect_guarded(reason),
            name=f"drp_season_detect:{reason}",
        )

    async def _async_season_detect_guarded(self, reason: str) -> None:
        async with self._season_lock:
            # Double-check after acquiring lock
            if not self._should_run_season_detect():
                return
            
            self._last_season_detect_utc = dt_util.utcnow()
            
            try:
                await self._async_season_detect()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log_exception(_LOGGER, "Season detect (%s) failed : %r", reason, e)

    # -----------------------------
    # Forecast + daily refresh
    # -----------------------------

    async def _async_get_forecast(self, entity_id: str, ftype: ForecastType = "daily") -> dict[str, Forecast]:
        resp = await self._hass.services.async_call(
            "weather",
            "get_forecasts",
            {"type": ftype},
            target={"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )
        if resp is None:
            raise RuntimeError("No response from weather.get_forecasts")

        resp_map = cast(Mapping[str, Any], resp)
        entity_payload = resp_map.get(entity_id)
        if not isinstance(entity_payload, Mapping):
            raise KeyError(f"Missing payload for {entity_id}: {resp_map}")

        forecasts_raw = entity_payload.get("forecast")
        if not isinstance(forecasts_raw, list):
            raise KeyError(f"Missing/invalid 'forecast' for {entity_id}: {entity_payload}")

        out: dict[str, Forecast] = {}
        for item in forecasts_raw:
            if not isinstance(item, dict):
                continue
            fc = forecast_legacy_to_native(item, drop_legacy=False)
            dt = fc.get("datetime")
            if not isinstance(dt, str) or not dt:
                continue
            dt_norm = dt.replace("Z", "+00:00")
            dt_key = datetime.fromisoformat(dt_norm).date().isoformat()
            out[dt_key] = fc

        if not out and forecasts_raw:
            raise ValueError(f"Forecast list for {entity_id} contains no valid 'datetime' entries")

        return out

    async def async_refresh_weather_daily(self, *, reason: str) -> dict[date, Historical]:
        """Attempt a remote refresh only if >=24h since last attempt (persisted via meta store)."""
        async with self._daily_lock:
            await self._meta_load()
            now_utc = dt_util.utcnow()

            due = self._last_daily_run_utc is None or (now_utc - self._last_daily_run_utc) >= self._wc_cfg.daily_interval

            if not due:
                # Ensure we at least attach cached data on startup if we have none.
                if reason == "startup" and not self.data:
                    days = self._target_days()
                    cached = await self._cache.async_get_many(days)
                    self.data = {d: cached[d] for d in days if d in cached}
                # Keep next tick scheduled
                self._schedule_next_daily_tick()
                return self.data

            # Mark attempt immediately (one attempt/24h even if it fails)
            self._last_daily_run_utc = now_utc
            await self._meta_save_last_run(now_utc)

        try:
            data = await self.async_refresh_historical_weather(reason=reason)
        finally:
            self._schedule_next_daily_tick()

        return data

    # -----------------------------
    # Date helpers
    # -----------------------------

    def _target_days(self) -> list[date]:
        today_local = dt_util.now().date()
        days_back = max(1, int(self._wc_cfg.days_back))
        start = today_local - timedelta(days=1)  # yesterday
        return [start - timedelta(days=i) for i in range(days_back)]

    # -----------------------------
    # Season detect core (unchanged logic; now guarded by throttle+dedup)
    # -----------------------------

    async def _async_season_detect(self) -> None:
        today = date.today()

        season_calendar = CalendarSeason()
        current_season = (season_calendar.windows())[CalendarSeason().season_for(today)]

        historical = await self._cache.async_dump()

        forecast_raw: dict[str, Forecast] = {}
        forecast_norm: dict[str, Forecast] = {}

        forecast_raw = await self._async_get_forecast(entity_id=self._weather_forecast_provider, ftype="daily")
        forecast_norm = {k: forecast_legacy_to_native(v, drop_legacy=False) for k, v in forecast_raw.items()}

        historical.update(forecast_norm)

        model = MeteoContiguousSeasonModel()
        model.fit(history=historical)

        info = model.day(today)
        source = "segmented"
        if info is None:
            info = model.infer(today)
            source = "inferred" if info is not None else "missing"

        if info is None:
            all_days = sorted(model.all())
            if all_days:
                last_day = all_days[-1]
                info = model.day(last_day)
                source = f"fallback_last_known({last_day})"

        if info is None:
            log_warning(_LOGGER, "Season model produced no usable output at all.")
            return

        season_state = SeasonState(
            as_of=today,
            window=current_season,
            weather=info.replace_windows(model.windows()),
            detect_model=source,
        )
        self._set_season_state(season_state)
        self._coordinator.set_season_state(season_state)

        log_debug(_LOGGER, "Model windows=%s", model.windows())
        log_debug(_LOGGER, "Season state=%s", season_state)

    # -----------------------------
    # Historical refresh (your existing dedup logic, with UTC bookkeeping fixed)
    # -----------------------------

    async def async_refresh_historical_weather(self, *, reason: str) -> dict[date, Historical]:
        async def _do() -> dict[date, Historical]:
            days = self._target_days()
            today = dt_util.now().date()

            cached: dict[date, Historical] = await self._cache.async_get_many(days)

            missing: list[date] = []
            for d in days:
                existing = cached.get(d)
                failed = int(existing.get("failed") or 0) if existing else 0

                if failed >= 3:
                    continue
                if existing is None or failed > 0:
                    missing.append(d)
                    continue
                if self._wc_cfg.always_refresh_today and d == today:
                    missing.append(d)
                    continue

            fetched: dict[date, Historical] = {}

            if missing:
                raw = await self._pw_client.async_fetch_days(missing)

                for d in missing:
                    fc = raw.get(d)
                    if fc is None:
                        fc = {"datetime": as_iso_local(d), "failed": 1}

                    is_failed = int(fc.get("failed") or 0) >= 1

                    if is_failed:
                        prev = cached.get(d)
                        prev_failed = int(prev.get("failed") or 0) if prev else 0
                        new_failed = min(3, prev_failed + 1) if prev else 1

                        if prev:
                            new_fc: dict[str, Any] = dict(prev)
                            new_fc["failed"] = new_failed
                            new_fc.setdefault("datetime", as_iso_local(d))
                        else:
                            new_fc = {"datetime": as_iso_local(d), "failed": new_failed}

                        fetched[d] = cast(Historical, new_fc)
                    else:
                        new_fc = dict(fc)
                        new_fc.pop("failed", None)
                        fetched[d] = cast(Historical, new_fc)

                try:
                    await self._cache.async_put_many(fetched)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log_debug(_LOGGER, "Cache put_many failed (%s), fallback: %r", reason, e)
                    for d, fc in fetched.items():
                        try:
                            await self._cache.async_put(d, fc)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e2:  # noqa: BLE001
                            log_debug(_LOGGER, "Cache put failed for %s: %r", d, e2)

            out: dict[date, Historical] = {}
            for d in days:
                v = fetched.get(d) or cached.get(d)
                if v is not None:
                    out[d] = v
            return out

        async with self._refresh_lock:
            task = self._refresh_task
            if task is None or task.done():
                task = self._hass.async_create_task(_do(), name=f"drp_weather_refresh:{reason}")
                self._refresh_task = task

        try:
            data = await task
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log_warning(_LOGGER, "Weather refresh failed (%s): %r", reason, e)
            return self.data

        self.data = data
        # IMPORTANT: keep UTC
        self._last_daily_run_utc = dt_util.utcnow()
        self._schedule_decider()
        return self.data

    # -----------------------------
    # HVAC decider (debounced)
    # -----------------------------

    def _schedule_decider(self) -> None:
        self._decider_debouncer.async_schedule_call()

    async def _decide_and_act(self) -> None:
        if hasattr(self._coordinator, "async_decide_and_act"):
            await self._coordinator.async_decide_and_act()  # type: ignore[attr-defined]
        else:
            await asyncio.sleep(0)
