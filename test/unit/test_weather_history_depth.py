"""Test unitari per la profondità dello storico meteo (patch #14).

Problema risolto
----------------
Con 730 giorni di storico esisteva un solo anno meteo completo (dic→nov), quindi
la finestra ML era un campione unico. La profondità è ora una costante nominata
(``HISTORY_DAYS_BACK = 2250``) condivisa da fetcher e coordinator.

Copertura
---------
A. Valori e coerenza delle costanti (retention ≥ profondità).
B. Default di ``HistoricalFetcherConfig`` e ``WeatherCoordinatorConfig`` allineati.
C. Garanzia: in ogni giorno dell'anno ci sono ≥ 4 anni meteo completi nella finestra
   (con 730 giorni: 1, poi 2 solo tra dic e feb).
D. Il modello è compatibile: ``min_days_per_year`` ≤ giorni di un anno meteo.
"""
from __future__ import annotations

from datetime import date, timedelta

from custom_components.drp_climate_master_v2.controller.weather.historical_fetcher import (
    HISTORY_CACHE_MARGIN_DAYS,
    HISTORY_CACHE_MAX_DAYS,
    HISTORY_DAYS_BACK,
    HistoricalFetcherConfig,
)
from custom_components.drp_climate_master_v2.controller.weather_coordinator import (
    WeatherCoordinatorConfig,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoSeasonConfig,
)

MIN_COMPLETE_YEARS = 4


def _complete_meteo_years(today: date, days_back: int) -> int:
    """Conta gli anni meteo (1/12 anno-1 → 30/11 anno) interamente nella finestra."""
    oldest = today - timedelta(days=days_back - 1)
    n = 0
    for y in range(today.year - 10, today.year + 1):
        start, end = date(y - 1, 12, 1), date(y, 11, 30)
        if start >= oldest and end < today:
            n += 1
    return n


def test_costanti_profondita_e_retention() -> None:
    assert HISTORY_DAYS_BACK == 2250
    assert HISTORY_CACHE_MAX_DAYS == HISTORY_DAYS_BACK + HISTORY_CACHE_MARGIN_DAYS
    assert HISTORY_CACHE_MAX_DAYS > HISTORY_DAYS_BACK  # la retention non pota la finestra


def test_default_config_allineati() -> None:
    f, c = HistoricalFetcherConfig(), WeatherCoordinatorConfig()
    assert f.days_back == c.days_back == HISTORY_DAYS_BACK
    assert f.cache_max_days == c.cache_max_days == HISTORY_CACHE_MAX_DAYS


def test_almeno_quattro_anni_meteo_completi_in_ogni_giorno_dell_anno() -> None:
    for offset in range(0, 366 * 2):  # copre anche un anno bisestile (2028)
        today = date(2026, 10, 17) + timedelta(days=offset)
        assert _complete_meteo_years(today, HISTORY_DAYS_BACK) >= MIN_COMPLETE_YEARS, today


def test_regressione_730_giorni_non_garantiva_due_anni() -> None:
    assert _complete_meteo_years(date(2026, 10, 17), 730) == 1


def test_modello_compatibile_con_la_profondita() -> None:
    assert MeteoSeasonConfig().min_days_per_year <= 365
