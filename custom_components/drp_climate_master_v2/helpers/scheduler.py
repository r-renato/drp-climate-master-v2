# ============================================================
# FILE: custom_components/drp_climate_master_v2/helpers/scheduler.py
# ============================================================

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from homeassistant.core import HomeAssistant, CALLBACK_TYPE
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


class IntervalGatedSchedulerBase:
    """Persisted *interval-gated* point-in-time scheduler.

    Despite the historical name, this class supports ANY interval
    (e.g. 24h, 6h, 8m, 30s, ...).

    What it guarantees
    - A tick is always scheduled (and re-armed with unsubscribe-first semantics).
    - A run is attempted at most once per *gate interval* (persisted across restarts).
    - The persisted marker is *last attempt* (not last success).

    Two time notions
    - gate_interval: the minimum time between attempts (persisted in Store).
    - tick_interval: how often to wake up and check the gate.
        * If None: the scheduler wakes up exactly at the next due instant.
        * If set: the scheduler wakes up at least every tick_interval AND also
          at the due instant if it comes earlier (next_tick = min(due, now+tick_interval)).

    Notes
    - We persist last attempt in UTC.
    - Subclasses implement `_async_on_due(reason)`.
    """

    META_LAST_ATTEMPT_UTC = "last_attempt_utc"

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        store_key: str,
        gate_interval: timedelta | None = None,
        tick_interval: timedelta | None = None,
        # Back-compat name (will be removed later)
        daily_interval: timedelta | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._hass = hass
        self._store = Store(hass, version=1, key=f"{store_key}.meta")

        # Prefer gate_interval; fallback to daily_interval; default 24h
        if gate_interval is None:
            gate_interval = daily_interval or timedelta(hours=24)

        if gate_interval <= timedelta(0):
            raise ValueError(f"gate_interval must be > 0, got {gate_interval!r}")
        if tick_interval is not None and tick_interval <= timedelta(0):
            raise ValueError(f"tick_interval must be > 0, got {tick_interval!r}")

        self._gate_interval = gate_interval
        self._tick_interval = tick_interval

        self._log = logger or logging.getLogger(__name__)

        self._unsub_tick: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()

        self._last_attempt_utc: datetime | None = None
        self._run_task: asyncio.Task[None] | None = None

    # -----------------------------
    # Properties
    # -----------------------------

    @property
    def last_attempt_utc(self) -> datetime | None:
        return self._last_attempt_utc

    @property
    def gate_interval(self) -> timedelta:
        return self._gate_interval

    @property
    def tick_interval(self) -> timedelta | None:
        return self._tick_interval

    # Back-compat property name
    @property
    def daily_interval(self) -> timedelta:
        return self._gate_interval

    # -----------------------------
    # Public lifecycle
    # -----------------------------

    async def async_start(self, *, run_immediately: bool = True) -> None:
        """Start scheduling.

        If run_immediately is True, we will attempt a run right away if due.
        In any case, a tick will be scheduled.
        """
        await self._meta_load()

        if run_immediately:
            # Startup path respects the gate.
            await self.async_run_if_due(reason="startup")

        self._reschedule_tick()

    async def async_stop(self) -> None:
        self._cancel_tick()

        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        self._run_task = None

    # -----------------------------
    # Hooks
    # -----------------------------

    async def _async_on_due(self, reason: str) -> None:
        """Override in subclasses: do the actual job."""
        raise NotImplementedError

    # -----------------------------
    # Meta store
    # -----------------------------

    async def _meta_load(self) -> None:
        meta = await self._store.async_load() or {}
        iso = meta.get(self.META_LAST_ATTEMPT_UTC)
        if not isinstance(iso, str):
            self._last_attempt_utc = None
            return
        dt = dt_util.parse_datetime(iso)
        self._last_attempt_utc = dt_util.as_utc(dt) if dt else None

    async def _meta_save_last_attempt(self, when_utc: datetime) -> None:
        when_utc = dt_util.as_utc(when_utc)
        await self._store.async_save({self.META_LAST_ATTEMPT_UTC: when_utc.isoformat()})

    # -----------------------------
    # Due logic + scheduling
    # -----------------------------

    def _is_due(self, now_utc: datetime) -> bool:
        if self._last_attempt_utc is None:
            return True
        return (now_utc - self._last_attempt_utc) >= self._gate_interval

    def _next_due_utc(self) -> datetime:
        now = dt_util.utcnow()
        if self._last_attempt_utc is None:
            return now
        due = self._last_attempt_utc + self._gate_interval
        return now if due <= now else due

    def _next_tick_utc(self) -> datetime:
        """Compute the next wake-up time.

        - If tick_interval is None: wake up at due.
        - Else: wake up at min(due, now + tick_interval)
          so we never delay a due run, while still supporting periodic checks.
        """
        due = self._next_due_utc()
        if self._tick_interval is None:
            return due

        now = dt_util.utcnow()
        check = now + self._tick_interval
        return due if due <= check else check

    def _cancel_tick(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    def _reschedule_tick(self) -> None:
        """Cancel + reschedule. Avoids stale ticks after meta changes."""
        self._cancel_tick()
        when = self._next_tick_utc()
        self._log.debug(
            "Scheduling next tick at %s (UTC) gate=%s tick=%s",
            when.isoformat(),
            self._gate_interval,
            self._tick_interval,
        )
        self._unsub_tick = async_track_point_in_time(self._hass, self._handle_tick, when)

    async def _handle_tick(self, _now) -> None:
        # Unsubscribe first (critical) so reschedule works even if the job errors.
        self._cancel_tick()

        await self.async_run_if_due(reason="tick")

        # Always reschedule
        self._reschedule_tick()

    async def async_reschedule(self) -> None:
        """Force a reschedule based on the current persisted meta."""
        await self._meta_load()
        self._reschedule_tick()

    async def async_run_if_due(self, *, reason: str) -> bool:
        """Run the job only if due.

        Returns True if it ran (or awaited a running job); False otherwise.
        """
        task_to_await: asyncio.Task[None] | None = None

        async with self._lock:
            await self._meta_load()
            now = dt_util.utcnow()

            if not self._is_due(now):
                return False

            # Mark attempt immediately (one attempt/interval even if the job fails).
            self._last_attempt_utc = now
            await self._meta_save_last_attempt(now)

            # Dedup: if already running, await it.
            if self._run_task and not self._run_task.done():
                task_to_await = self._run_task
            else:
                self._run_task = self._hass.async_create_task(
                    self._async_on_due(reason),
                    name=f"{self.__class__.__name__}:{reason}",
                )
                task_to_await = self._run_task

        # Await outside lock
        try:
            await task_to_await
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Swallow here; subclasses should log. We still keep gating semantics.
            self._log.exception("Scheduled job failed (%s)", reason)

        return True


class ThrottledAsyncJob:
    """Small helper for throttling + dedup of expensive async jobs.

    - If a job is running, schedule() does nothing (dedup).
    - If the last run attempt was more recent than min_interval, schedule() does nothing (throttle).
    - A guarded wrapper double-checks after acquiring the lock.

    Intended usage:
        self._job = ThrottledAsyncJob(hass, min_interval=..., name_prefix="...", logger=_LOGGER)
        self._job.schedule(lambda: self._async_work(), reason="startup")

    Notes
    - Throttle is based on *attempt* time, not success.
    - Exceptions are logged to avoid "Task exception was never retrieved".
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        min_interval: timedelta,
        name_prefix: str,
        logger: logging.Logger,
    ) -> None:
        if min_interval <= timedelta(0):
            raise ValueError(f"min_interval must be > 0, got {min_interval!r}")

        self._hass = hass
        self._min_interval = min_interval
        self._name_prefix = name_prefix
        self._log = logger

        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_run_utc: datetime | None = None

    @property
    def last_run_utc(self) -> datetime | None:
        return self._last_run_utc

    def schedule(self, coro_factory: Callable[[], Awaitable[None]], *, reason: str) -> None:
        # Dedup
        if self._task and not self._task.done():
            self._log.debug("Dedup %s: already running (%s)", self._name_prefix, reason)
            return

        # Throttle
        now = dt_util.utcnow()
        if self._last_run_utc and (now - self._last_run_utc) < self._min_interval:
            return

        async def _guarded() -> None:
            try:
                async with self._lock:
                    now2 = dt_util.utcnow()
                    if self._last_run_utc and (now2 - self._last_run_utc) < self._min_interval:
                        return
                    self._last_run_utc = now2

                # Execute outside lock
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._log.exception("Throttled job failed (%s:%s)", self._name_prefix, reason)

        self._task = self._hass.async_create_task(
            _guarded(),
            name=f"{self._name_prefix}:{reason}",
        )

    async def async_cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
