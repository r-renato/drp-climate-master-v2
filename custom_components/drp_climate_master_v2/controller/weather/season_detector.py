"""season_detector.py — Rilevamento stagionale da dati storici + forecast.

Responsabilità unica: dati meteo → SeasonState.
Non conosce cache, PDC, pompe né entità HA.

Contratto pubblico:
    async_detect(historical, today) -> SeasonState | None
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import logging
from typing import Literal, Mapping, cast

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...domain.models.season import SeasonState, Seasons
from ...domain.models.weather import Forecast, Historical
from ...helpers.logger import log_debug, log_warning
from ...helpers.season.season_weather_calendar import CalendarSeason
from ...helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    forecast_legacy_to_native,
)

_LOGGER = logging.getLogger(__name__)

ForecastType = Literal["daily", "hourly", "twice_daily"]
_Hemisphere = Literal["north", "south"]

@dataclass(slots=True)
class SeasonDetectorConfig:
    """Parametri operativi del rilevatore stagionale."""

    forecast_provider: str
    """Entity ID del provider forecast HA (es. weather.pirateweather_home)."""

    hemisphere: _Hemisphere = "north"


class SeasonDetector:
    """Produce SeasonState a partire da dati storici + forecast live.

    Accetta i dati storici come parametro esplicito (iniettati da HistoricalFetcher):
    non ha accesso diretto alla cache, quindi non può inquinarla.

    Il merge storico+forecast avviene su una copia locale — nessun side-effect
    sui dati del chiamante.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        cfg: SeasonDetectorConfig,
    ) -> None:
        self._hass = hass
        self._cfg = cfg

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    async def async_detect(
        self,
        historical: dict[date, Historical],
        today: date | None = None,
    ) -> SeasonState | None:
        """Esegue il rilevamento stagionale.

        Args:
            historical: snapshot dei dati storici (copia — verrà modificata localmente).
            today:      data di riferimento (default: oggi locale).

        Returns:
            SeasonState se il modello produce un risultato, None altrimenti.
            None non è un errore: il chiamante deve gestire il fallback.
        """
        today = today or dt_util.now().date()

        # --- Calendario ---
        cal = CalendarSeason.for_date(today, hemisphere=self._cfg.hemisphere)
        calendar_season: Seasons = cal.season_for(today)
        current_window = cal.windows()[calendar_season]

        # --- Merge storico + forecast (su copia locale: nessun side-effect) ---
        # MeteoContiguousSeasonModel.fit() vuole chiavi str (date ISO "YYYY-MM-DD").
        # Convertiamo lo snapshot storico (chiavi date) in str, poi aggiungiamo
        # i forecast (già in str) senza ulteriori conversioni.
        working_data: dict[str, Historical] = {
            k.isoformat(): v for k, v in historical.items()
        }
        try:
            forecast_norm = await self._get_forecast_normalized()
            for k, v in forecast_norm.items():
                # Forecast e Historical condividono la stessa struttura dict —
                # il cast è giustificato: MeteoContiguousSeasonModel.fit() accetta entrambi.
                working_data[k] = cast(Historical, v)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_warning(
                _LOGGER,
                "Forecast non disponibile, procedo con solo storico: %r",
                exc,
            )
            # Continua: il modello può fittare con solo storico

        # --- Fit modello ---
        model = MeteoContiguousSeasonModel()
        model.fit(history=working_data)

        # --- Lookup giorno corrente ---
        info = model.day(today)
        source = "segmented"

        if info is None:
            info = model.infer(today, expected_season=calendar_season)
            source = "inferred" if info is not None else "missing"

        if info is None:
            all_days = sorted(model.all())
            if all_days:
                last_day = all_days[-1]
                info = model.day(last_day)
                source = f"fallback_last_known({last_day})"

        if info is None:
            log_warning(_LOGGER, "Il modello stagionale non ha prodotto output per %s.", today)
            return None

        season_state = SeasonState(
            as_of=today,
            window=current_window,
            weather=info.replace_windows(model.windows()),
            detect_model=source,
        )

        log_debug(_LOGGER, "Model windows=%s", model.windows())
        log_debug(_LOGGER, "Season state=%s", season_state)

        return season_state

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_forecast_normalized(self) -> dict[str, Forecast]:
        """Scarica e normalizza il forecast dal provider HA."""
        entity_id = self._cfg.forecast_provider
        ftype: ForecastType = "daily"

        resp = await self._hass.services.async_call(
            "weather",
            "get_forecasts",
            {"type": ftype},
            target={"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )

        # Validazione runtime esplicita (cast non è sufficiente)
        if not isinstance(resp, Mapping):
            raise RuntimeError(
                f"Risposta inattesa da weather.get_forecasts: {type(resp)!r}"
            )

        entity_payload = resp.get(entity_id)
        if not isinstance(entity_payload, Mapping):
            raise KeyError(
                f"Payload mancante per {entity_id}: chiavi disponibili={list(resp.keys())}"
            )

        forecasts_raw = entity_payload.get("forecast")
        if not isinstance(forecasts_raw, list):
            raise KeyError(
                f"'forecast' mancante o non lista per {entity_id}: {entity_payload!r}"
            )

        out: dict[str, Forecast] = {}
        for item in forecasts_raw:
            if not isinstance(item, dict):
                continue
            fc = forecast_legacy_to_native(item, drop_legacy=False)
            dt_val = fc.get("datetime")
            if not isinstance(dt_val, str) or not dt_val:
                continue
            dt_norm = dt_val.replace("Z", "+00:00")
            dt_key = datetime.fromisoformat(dt_norm).date().isoformat()
            out[dt_key] = fc

        if not out and forecasts_raw:
            raise ValueError(
                f"Lista forecast per {entity_id} non contiene 'datetime' validi"
            )

        return out
