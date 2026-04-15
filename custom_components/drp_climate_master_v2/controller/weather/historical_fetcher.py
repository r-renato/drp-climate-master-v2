"""historical_fetcher.py — Fetch e cache dei dati meteo storici PirateWeather.

Responsabilità unica: scaricare, cachare e restituire i dati giornalieri
storici. Non conosce stagioni, modelli ML né decisioni HVAC.

Contratto pubblico:
    async_refresh(reason) -> dict[date, Historical]
    async_dump()          -> dict[date, Historical]   (snapshot cache corrente)
    async_shutdown()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any, cast
from collections.abc import Mapping as CMapping

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...domain.models.weather import Historical
from ...helpers.cache import JsonObject, Codec, PersistentCache
from ...helpers.logger import log_debug, log_warning
from ...helpers.timeutils import as_iso_local
from ...helpers.weather.pirateweather_client import PirateWeatherTimeMachineClient

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HistoricalFetcherConfig:
    """Parametri operativi del fetcher storico."""

    days_back: int = 730
    """Finestra temporale da mantenere in cache (giorni indietro da oggi)."""

    always_refresh_today: bool = True
    """Ri-scarica sempre il giorno corrente (PirateWeather aggiorna intraday)."""

    cache_persist: bool = True
    cache_max_days: int = 740
    cache_save_cooldown_s: float = 60.0

    max_fetch_failures: int = 3
    """Dopo N fallimenti consecutivi per un giorno, smette di ritentare."""


class HistoricalFetcher:
    """Scarica e mantiene in cache i dati meteo giornalieri da PirateWeather.

    È una classe pura di servizio: nessun listener HA, nessun coordinator.
    Viene orchestrata da WeatherCoordinator.

    Thread-safety: usa un asyncio.Lock per deduplicare fetch concorrenti.
    La cache persistente è gestita da PersistentCache (thread-safe internamente).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        pw_client: PirateWeatherTimeMachineClient,
        cache_key: str,
        cfg: HistoricalFetcherConfig | None = None,
    ) -> None:
        self._hass = hass
        self._pw_client = pw_client
        self._cfg = cfg or HistoricalFetcherConfig()

        historical_codec: Codec[Historical] = Codec(
            encode=lambda f: cast(JsonObject, dict(f)),
            decode=lambda raw: cast(Historical, dict(raw)),
            clone=lambda f: cast(Historical, dict(f)),
        )
        self._cache: PersistentCache[Historical] = PersistentCache(
            hass,
            key=cache_key,
            version=1,
            codec=historical_codec,
            save_cooldown_s=self._cfg.cache_save_cooldown_s,
            max_days=self._cfg.cache_max_days,
            persist=self._cfg.cache_persist,
        )

        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[dict[date, Historical]] | None = None

        # Snapshot in-memory: aggiornato dopo ogni refresh riuscito.
        self._data: dict[date, Historical] = {}
        self._last_success_utc: datetime | None = None
        self._is_healthy: bool = False

    # ------------------------------------------------------------------
    # Proprietà pubbliche
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict[date, Historical]:
        """Snapshot corrente (aggiornato dopo ogni refresh riuscito)."""
        return self._data

    @property
    def is_healthy(self) -> bool:
        """True se almeno un refresh è andato a buon fine dall'ultimo startup."""
        return self._is_healthy

    @property
    def last_success_utc(self) -> datetime | None:
        """UTC dell'ultimo refresh completato con successo."""
        return self._last_success_utc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Carica la cache persistente. Va chiamato una sola volta allo startup."""
        await self._cache.async_load()
        await self._populate_from_cache()

    async def async_shutdown(self) -> None:
        """Flush cache e cancella task in corso."""
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        self._active_task = None
        await self._cache.async_shutdown(flush=True)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def async_refresh(self, *, reason: str) -> dict[date, Historical]:
        """Scarica i giorni mancanti/scaduti e aggiorna la cache.

        Deduplicato: se una task di refresh è già in corso, attende quella
        invece di lanciarne una nuova.

        Restituisce sempre self.data (anche in caso di errore: dati cached).
        """

        async def _do() -> dict[date, Historical]:
            return await self._fetch_and_cache(reason=reason)

        # Deduplicazione: creazione task dentro lock, await fuori.
        async with self._lock:
            task = self._active_task
            if task is None or task.done():
                task = self._hass.async_create_task(
                    _do(), name=f"drp_weather_refresh:{reason}"
                )
                self._active_task = task

        try:
            data = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_warning(_LOGGER, "Refresh fallito (%s): %r — restituiti dati cached.", reason, exc)
            # Segnala unhealthy SOLO se non abbiamo mai avuto successo.
            # Se abbiamo dati cached, continuiamo a operare in DEGRADED.
            if not self._is_healthy:
                log_warning(
                    _LOGGER,
                    "Nessun dato storico disponibile (primo avvio o cache vuota). "
                    "Il sistema opererà in modalità degradata fino al prossimo refresh.",
                )
            return self._data

        self._data = data
        self._last_success_utc = dt_util.utcnow()
        self._is_healthy = True
        return self._data

    async def async_dump(self) -> dict[date, Historical]:
        """Restituisce una *copia* del contenuto corrente della cache.

        Le chiavi vengono convertite da str ISO a date.
        IMPORTANTE: è una copia esplicita — il chiamante può modificarla
        senza inquinare la cache interna.
        """
        from datetime import date as _date
        raw: dict[str, Historical] = await self._cache.async_dump()
        result: dict[_date, Historical] = {}
        for k, v in raw.items():
            try:
                result[_date.fromisoformat(k)] = v
            except (ValueError, TypeError):
                log_debug(_LOGGER, "async_dump: chiave non convertibile a date ignorata: %r", k)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _target_days(self) -> list[date]:
        """Lista di date da mantenere in cache, dalla più recente alla più vecchia."""
        today = dt_util.now().date()
        days_back = max(1, self._cfg.days_back)
        return [today - timedelta(days=i) for i in range(days_back)]

    async def _populate_from_cache(self) -> None:
        """Precarica self._data dalla cache persistente (usato allo startup)."""
        if self._data:
            return
        days = self._target_days()
        cached = await self._cache.async_get_many(days)
        self._data = {d: cached[d] for d in days if d in cached}
        if self._data:
            self._is_healthy = True  # abbiamo dati cached: considerato healthy

    async def _fetch_and_cache(self, *, reason: str) -> dict[date, Historical]:
        """Logica di fetch effettivo: determina giorni mancanti, scarica, salva."""
        days = self._target_days()
        today = dt_util.now().date()

        cached: dict[date, Historical] = await self._cache.async_get_many(days)

        missing: list[date] = []
        for d in days:
            existing = cached.get(d)
            failures = int(existing.get("failed") or 0) if existing else 0

            if failures >= self._cfg.max_fetch_failures:
                continue  # troppi fallimenti: skip silenzioso
            if existing is None or failures > 0:
                missing.append(d)
                continue
            if self._cfg.always_refresh_today and d == today:
                missing.append(d)

        fetched: dict[date, Historical] = {}

        if missing:
            raw = await self._pw_client.async_fetch_days(missing)
            fetched = self._merge_fetch_results(missing, raw, cached)

            await self._save_to_cache(fetched, reason=reason)

        out: dict[date, Historical] = {}
        for d in days:
            v = fetched.get(d) or cached.get(d)
            if v is not None:
                out[d] = v
        return out

    def _merge_fetch_results(
        self,
        missing: list[date],
        raw: CMapping[date, Historical | None],
        cached: CMapping[date, Historical],
    ) -> dict[date, Historical]:
        """Unisce i risultati del fetch con i dati cached gestendo i fallimenti."""
        fetched: dict[date, Historical] = {}
        for d in missing:
            fc = raw.get(d)
            if fc is None:
                fc = cast(Historical, {"datetime": as_iso_local(d), "failed": 1})

            is_failed = int(fc.get("failed") or 0) >= 1

            if is_failed:
                prev = cached.get(d)
                prev_failed = int(prev.get("failed") or 0) if prev else 0
                new_failed = min(self._cfg.max_fetch_failures, prev_failed + 1) if prev else 1

                new_fc: dict[str, Any] = dict(prev) if prev else {}
                new_fc["failed"] = new_failed
                new_fc.setdefault("datetime", as_iso_local(d))
                fetched[d] = cast(Historical, new_fc)
            else:
                new_fc = dict(fc)
                new_fc.pop("failed", None)
                fetched[d] = cast(Historical, new_fc)

        return fetched

    async def _save_to_cache(
        self, fetched: dict[date, Historical], *, reason: str
    ) -> None:
        """Salva i dati nella cache con fallback per-giorno se put_many fallisce."""
        try:
            await self._cache.async_put_many(fetched)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_debug(_LOGGER, "Cache put_many fallita (%s), fallback per-giorno: %r", reason, exc)
            for d, fc in fetched.items():
                try:
                    await self._cache.async_put(d, fc)
                except asyncio.CancelledError:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    log_debug(_LOGGER, "Cache put fallita per %s: %r", d, exc2)
