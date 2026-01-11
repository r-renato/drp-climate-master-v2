#
from __future__ import annotations

from datetime import date
from typing import List, Optional, Protocol

from homeassistant.components.weather import Forecast

class WeatherHistoricalProvider(Protocol):
    """
    Protocollo per provider meteo **storico** (es. Pirate Weather Time Machine).

    Obiettivi:
    - Esporre API `daily()` e `daily_range()` restituendo `Forecast` normalizzati.
    - Gestire un doppio livello di cache: RAM (TTL) + store persistente su disco.
    - Supportare l'uso opzionale di sessioni HTTP riutilizzabili via context manager.
    - Non propagare errori transitori e, quando sensato, restituire risultati parziali.
    """

    # ---------- API principali ----------
    async def daily(self, day: date) -> Optional[Forecast]:
        """
        Ritorna il forecast giornaliero per `day`.
        Ordine di risoluzione raccomandato:
        1) Cache RAM (TTL), 2) Store persistente, 3) Rete (HTTP).
        Se ottenuto da rete, deve aggiornare RAM e store.
        """

    async def daily_range(self, start: date, end: date) -> List[Forecast]:
        """
        Ritorna la lista di `Forecast` nell'intervallo [start..end] **inclusivo**,
        idealmente in ordine cronologico (campo `datetime` crescente).

        Requisiti raccomandati:
        - Prima tentare RAM e store per ciascun giorno; solo i miss vanno in rete.
        - Concurrency limit (Semaphore) e retry/backoff sui miss.
        - Possibile budget/timeout per chunk; in caso di superamento, ritornare
          i **risultati parziali** già disponibili.
        - Persistere in blocco i nuovi record scaricati (debounced).
        """
        ...

    # ---------- Gestione risorse ----------
    async def _close_session(self) -> None:
        """
        Chiude eventuali risorse di rete (es. `aiohttp.ClientSession`) e
        reimposta lo stato interno. Deve essere **idempotente**.
        """

    # ---------- Context manager opzionale ----------
    async def __aenter__(self) -> "WeatherHistoricalProvider":
        """
        Inizializza le risorse necessarie (es. aprire la sessione HTTP).
        Ritorna `self` per l'uso con `async with`.
        """
        ...

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """
        Chiusura del context: tipicamente forza `_cache_save()` e poi chiude
        le risorse con `_close_session()`. Non deve sollevare eccezioni.
        """
        ...


class WeatherForecastProvider(Protocol):
    """
    Protocollo per provider di **previsioni** (non storico), normalizzate
    in `Forecast` compatibili con Home Assistant.
    """

    async def async_get_forecast_days(self, *, start: date, days: int) -> List[Forecast]:
        """
        Ritorna un elenco di `Forecast` a partire da `start` per `days` giorni,
        in ordine cronologico. Gli elementi dovrebbero includere almeno:
        - `datetime` (ISO locale a mezzanotte del giorno),
        - `temperature` (max) e `templow` (min),
        più eventuali campi opzionali (es. `humidity`, `dew_point`, ecc.).
        """
        ...
