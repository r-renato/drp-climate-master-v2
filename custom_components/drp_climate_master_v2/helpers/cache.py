import asyncio
from dataclasses import dataclass
import logging
from datetime import date
from typing import Callable, Dict, Generic, Mapping, Optional, TypeVar

from homeassistant.core import HomeAssistant, CALLBACK_TYPE
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store

from ..helpers.logger import log_debug, log_warning

_LOGGER = logging.getLogger(__name__)

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

T = TypeVar("T")


@dataclass(frozen=True)
class Codec(Generic[T]):
    """Bridge tra T (tipo dominio) e JsonObject (persistenza HA Store)."""

    encode: Callable[[T], JsonObject]      # T -> JSON
    decode: Callable[[JsonObject], T]      # JSON -> T
    clone: Callable[[T], T] | None = None  # copie difensive (opzionale)

    def safe_clone(self, v: T) -> T:
        return self.clone(v) if self.clone else v
    
class PersistentCache(Generic[T]):
    """Persistent `.storage` cache keyed by ISO day (YYYY-MM-DD) with debounced saves.

    This cache stores per-day payloads in memory and (optionally) persists them in
    Home Assistant's `.storage` via `Store`. Writes are debounced to reduce I/O.

    Concurrency model:
        - asyncio-safe (uses asyncio locks), not multi-thread safe.

    Persistence model:
        - Values are stored as JSON-serializable objects via the provided `codec`.
        - If `persist=False`, no disk I/O is performed and no stop-listener is registered.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        key: str,
        *,
        codec: Codec[T],
        version: int = 1,
        save_cooldown_s: float = 30.0,
        max_days: int = 10,
        persist: bool = True,
    ) -> None:
        """Initialize the persistent daily cache.

        Args:
            hass: Home Assistant instance used for:
                - scheduling the debounced save callbacks,
                - accessing the event bus (flush on shutdown),
                - creating the `.storage` `Store` when persistence is enabled.

            key: Storage key used by Home Assistant `Store`. This determines the file
                name under `.storage` (namespaced by HA). Use a stable, unique key per
                integration and cache (e.g. `"my_integration_forecast_cache"`).

            codec: Serialization bridge between your domain type `T` and JSON.
                - `codec.encode(T) -> JsonObject` must return JSON-serializable data.
                - `codec.decode(JsonObject) -> T` reconstructs the domain object.
                - `codec.clone(T) -> T` (optional) provides defensive copies on get/put.

            version: Schema version for the stored payload. Bump this when you change
                the on-disk format produced by `codec.encode`. Home Assistant `Store`
                will treat a version change as a migration boundary (you can optionally
                implement migrations outside this class if desired).

            save_cooldown_s: Debounce window (seconds) used to batch frequent writes.
                Multiple `async_put()` calls within this cooldown will result in a
                single persisted save. Set lower for more immediate persistence, higher
                to reduce disk writes.

            max_days: Retention limit (number of distinct ISO-day keys) kept in memory
                and persisted. When exceeded, the oldest days (lexicographically sorted
                ISO keys) are pruned first. Set to 0 or a negative value to disable
                automatic pruning.

            persist: If True, enable `.storage` persistence and automatic flush on
                Home Assistant stop. If False, operate as an in-memory cache only:
                no `Store`, no debouncer, no stop listener.
        """

        self._hass = hass
        self._persist = persist
        self._codec = codec

        # Nello Store salviamo sempre JSON puro
        self._store: Store[dict[str, JsonObject]] | None = Store(hass, version, key) if persist else None

        self._data: Dict[str, T] = {}
        self._data_lock = asyncio.Lock()

        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()

        self._dirty = False
        self._gen = 0

        self._max_days = max_days

        self._debouncer: Debouncer | None = None
        if persist:
            self._debouncer = Debouncer(
                hass,
                _LOGGER,
                cooldown=save_cooldown_s,
                immediate=False,
                function=self._async_cache_save_now,
            )

        self._unsub_stop: CALLBACK_TYPE | None = None
        if persist:
            self._unsub_stop = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop
            )

    async def _async_on_hass_stop(self, _event) -> None:
        try:
            await self.async_save()
        except Exception as e:  # noqa: BLE001
            log_warning(_LOGGER, "Final cache flush failed on stop: %r", e)

    def _date_key(self, d: date) -> str:
        return d.isoformat()

    def _cache_prune_locked(self, keep_days: int) -> None:
        if keep_days <= 0:
            return
        n = len(self._data)
        if n <= keep_days:
            return
        keys_sorted = sorted(self._data.keys())
        for k in keys_sorted[: n - keep_days]:
            self._data.pop(k, None)

    async def _async_ensure_loaded_once(self) -> None:
        if self._loaded:
            return

        async with self._load_lock:
            if self._loaded:
                return

            if not self._persist or self._store is None:
                self._loaded = True
                return

            raw = await self._store.async_load() or {}
            loaded: Dict[str, T] = {}

            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        try:
                            loaded[k] = self._codec.decode(v)
                        except Exception:  # noqa: BLE001
                            # record corrotto/non compatibile: lo ignoriamo
                            continue

            async with self._data_lock:
                merged = dict(loaded)
                merged.update(self._data)  # RAM wins
                self._data = merged
                self._loaded = True

            log_debug(_LOGGER, "PersistentCache loaded %d days", len(self._data))

    async def _async_cache_save_now(self) -> None:
        if not self._persist or self._store is None:
            return

        # Evita wipe se non hai mai caricato e non hai mai scritto
        if not self._loaded and not self._dirty:
            log_debug(_LOGGER, "Skipping save: cache not loaded and not dirty")
            return

        await self._async_ensure_loaded_once()

        async with self._data_lock:
            if not self._dirty:
                return
            snapshot = {k: self._codec.encode(v) for k, v in self._data.items()}
            gen_snapshot = self._gen

        async with self._save_lock:
            try:
                await self._store.async_save(snapshot)
            except Exception as e:  # noqa: BLE001
                log_warning(_LOGGER, "Store save failed: %s", e)
                return

        async with self._data_lock:
            if self._gen == gen_snapshot:
                self._dirty = False

        log_debug(_LOGGER, "PersistentCache saved %d days", len(snapshot))

    # -------- Public API --------

    async def async_load(self) -> None:
        await self._async_ensure_loaded_once()

    async def async_save(self) -> None:
        await self._async_cache_save_now()

    async def async_shutdown(self, *, flush: bool = True) -> None:
        if flush:
            try:
                await self.async_save()
            except Exception as e:  # noqa: BLE001
                log_warning(_LOGGER, "Cache flush failed on shutdown: %r", e)

        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None

    async def async_get(self, day: date) -> Optional[T]:
        await self._async_ensure_loaded_once()
        key = self._date_key(day)
        async with self._data_lock:
            v = self._data.get(key)
            return self._codec.safe_clone(v) if v is not None else None

    async def async_put(self, day: date, value: T) -> None:
        await self._async_ensure_loaded_once()
        key = self._date_key(day)

        async with self._data_lock:
            self._data[key] = self._codec.safe_clone(value)
            self._cache_prune_locked(self._max_days)
            self._dirty = True
            self._gen += 1

        if self._debouncer is not None:
            self._debouncer.async_schedule_call()

    async def async_get_many(self, days: list[date]) -> dict[date, T]:
        """Fetch cached values for multiple days in one shot.

        This method is optimized vs calling `async_get()` in a loop:
        - loads the cache once (if needed)
        - acquires the internal data lock only once
        - returns defensive clones via `codec.safe_clone`.

        Args:
            days: List of day keys to retrieve.

        Returns:
            dict[date, T]: mapping {day: value} for entries that exist in cache.
        """
        await self._async_ensure_loaded_once()

        # (opzionale) dedupe preservando ordine
        # days = list(dict.fromkeys(days))

        out: dict[date, T] = {}
        async with self._data_lock:
            data = self._data
            clone = self._codec.safe_clone
            for d in days:
                v = data.get(self._date_key(d))
                if v is not None:
                    out[d] = clone(v)
        return out

    async def async_put_many(self, items: Mapping[date, T]) -> None:
        """Put multiple day entries into the cache in a single lock acquisition.

        This method is optimized vs calling `async_put()` in a loop:
        - loads the cache once (if needed)
        - acquires the internal data lock only once
        - prunes once at the end
        - schedules the debounced save at most once.

        Args:
            items: Mapping {day: value} to store.
        """
        if not items:
            return

        await self._async_ensure_loaded_once()

        async with self._data_lock:
            data = self._data
            clone = self._codec.safe_clone

            for d, value in items.items():
                data[self._date_key(d)] = clone(value)

            self._cache_prune_locked(self._max_days)
            self._dirty = True
            self._gen += len(items)  # coerente con async_put (monotonic + “per write”)

        if self._debouncer is not None:
            self._debouncer.async_schedule_call()

    async def async_dump(self, *, sorted_by_day: bool = True) -> dict[str, T]:
        """Return a snapshot of all cached items.

        Args:
            sorted_by_day: If True, return items ordered by ISO day key (chronological).

        Returns:
            A new dict mapping ISO day (YYYY-MM-DD) to cached values (defensive copies).
        """
        await self._async_ensure_loaded_once()
        async with self._data_lock:
            items = self._data.items()
            if sorted_by_day:
                items = sorted(items, key=lambda kv: kv[0])
            return {k: self._codec.safe_clone(v) for k, v in items}

    async def async_prune(self, keep_days: int) -> None:
        await self._async_ensure_loaded_once()
        async with self._data_lock:
            before = len(self._data)
            self._cache_prune_locked(keep_days)
            if len(self._data) != before:
                self._dirty = True
                self._gen += 1

        if self._debouncer is not None:
            self._debouncer.async_schedule_call()
