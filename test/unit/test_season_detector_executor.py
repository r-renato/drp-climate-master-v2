"""Test unitari per l'esecuzione in executor del fit stagionale (patch #3).

Problema risolto
----------------
``MeteoContiguousSeasonModel.fit()`` è CPU-bound (decine di secondi con 2 anni di
storico) e girava dentro l'event loop di Home Assistant: blocco del loop e warning
di chiamata bloccante (§4.5). Ora fit e lookup girano in ``async_add_executor_job``;
se storico+forecast non cambiano, il modello già fittato è riutilizzato.

Copertura
---------
A. Il fit non gira nel thread dell'event loop.
B. Il risultato coincide con la pipeline sincrona precedente (nessuna regressione).
C. Cache: stessi dati → nessun nuovo fit; dati diversi → nuovo fit.
D. Forecast non disponibile: il rilevamento prosegue con il solo storico.
E. Storico troppo corto: ``ValueError`` propagata come prima, cache intatta.
F. Un'eccezione del fit si propaga al chiamante (che la logga) e non sporca la cache.
"""
from __future__ import annotations

import asyncio
import math
import threading
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.drp_climate_master_v2.controller.weather import season_detector as sd
from custom_components.drp_climate_master_v2.controller.weather.season_detector import (
    SeasonDetector,
    SeasonDetectorConfig,
)
from custom_components.drp_climate_master_v2.domain.models.season import Seasons
from custom_components.drp_climate_master_v2.helpers.season.season_weather_calendar import CalendarSeason
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
)

TODAY = date(2026, 10, 15)
HISTORY_DAYS = 330  # storico senza anno meteo segmentabile (< min_days_per_year utili): fit veloce, esito "inferred"


def _row(d: date) -> dict[str, Any]:
    doy = d.timetuple().tm_yday
    mean = 16.0 + 9.5 * math.cos(2.0 * math.pi * (doy - 206) / 365.0)
    return {
        "datetime": d.isoformat(),
        "native_temperature": mean + 4.0,
        "native_templow": mean - 4.0,
        "native_dew_point": mean - 7.0,
        "native_wind_speed": 8.0,
        "cloud_coverage": 0.3,
    }


def _history(days: int = HISTORY_DAYS, shift_c: float = 0.0) -> dict[date, Any]:
    out = {}
    for i in range(days):
        d = TODAY - timedelta(days=i)
        row = _row(d)
        row["native_temperature"] += shift_c
        out[d] = row
    return out


class _FakeHass:
    """hass minimale: executor reale (thread) e servizi che falliscono o rispondono."""

    def __init__(self, *, forecast_error: Exception | None = RuntimeError("nessun forecast")) -> None:
        self.executor_jobs: list[str] = []
        self.services = MagicMock()
        if forecast_error is not None:
            async def _call(*a: Any, **k: Any) -> Any:
                raise forecast_error
            self.services.async_call = _call

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        self.executor_jobs.append(getattr(func, "__name__", str(func)))
        return await asyncio.to_thread(func, *args)


def _detector(hass: _FakeHass | None = None) -> tuple[SeasonDetector, _FakeHass]:
    hass = hass or _FakeHass()
    return SeasonDetector(hass, SeasonDetectorConfig(forecast_provider="weather.test")), hass  # type: ignore[arg-type]


def _run(coro: Any) -> Any:
    """Esegue una coroutine in un event loop dedicato (nessun plugin asyncio richiesto)."""
    return asyncio.run(coro)


@pytest.fixture
def fit_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Registra il thread di ogni chiamata a fit(); lascia il fit reale."""
    threads: list[int] = []
    original = MeteoContiguousSeasonModel.fit

    def spy(self: MeteoContiguousSeasonModel, *a: Any, **k: Any) -> Any:
        threads.append(threading.get_ident())
        return original(self, *a, **k)

    monkeypatch.setattr(MeteoContiguousSeasonModel, "fit", spy)
    return threads


# ---------------------------------------------------------------- A
def test_fit_non_gira_nel_thread_dell_event_loop(fit_spy: list[int]) -> None:
    det, hass = _detector()

    state = _run(det.async_detect(_history(), TODAY))

    assert state is not None
    assert hass.executor_jobs == ["_fit_and_lookup"]
    assert fit_spy and all(t != threading.get_ident() for t in fit_spy)


# ---------------------------------------------------------------- B
def test_risultato_identico_alla_pipeline_sincrona_precedente() -> None:
    history = _history()
    det, _ = _detector()
    state = _run(det.async_detect(history, TODAY))

    # pipeline precedente, riprodotta in modo sincrono
    model = MeteoContiguousSeasonModel()
    model.fit(history={k.isoformat(): v for k, v in history.items()})
    cal = CalendarSeason.for_date(TODAY, hemisphere="north").season_for(TODAY)
    info = model.day(TODAY)
    source = "segmented"
    if info is None:
        info = model.infer(TODAY, expected_season=cal)
        source = "inferred"

    assert state is not None and info is not None
    assert state.detect_model == source
    assert state.weather.season == info.season
    assert state.weather.reason == info.reason
    assert state.weather.regime_hint == info.regime_hint
    assert state.weather.weather_day_signals == info.weather_day_signals
    assert state.season == cal


# ---------------------------------------------------------------- C
def test_cache_stessi_dati_nessun_nuovo_fit(fit_spy: list[int]) -> None:
    det, _ = _detector()
    history = _history()

    first = _run(det.async_detect(history, TODAY))
    second = _run(det.async_detect(dict(history), TODAY))

    assert len(fit_spy) == 1, "il secondo rilevamento riusa il modello"
    assert first is not None and second is not None
    assert first.weather.weather_day_signals == second.weather.weather_day_signals


def test_cache_dati_diversi_nuovo_fit(fit_spy: list[int]) -> None:
    det, _ = _detector()

    _run(det.async_detect(_history(), TODAY))
    _run(det.async_detect(_history(shift_c=1.5), TODAY))

    assert len(fit_spy) == 2


def test_cache_data_diversa_stesso_storico_riusa_il_fit(fit_spy: list[int]) -> None:
    # il fit non dipende da 'today': cambia solo il lookup
    det, _ = _detector()
    history = _history()

    _run(det.async_detect(history, TODAY))
    _run(det.async_detect(history, TODAY - timedelta(days=1)))

    assert len(fit_spy) == 1


# ---------------------------------------------------------------- D
def test_forecast_non_disponibile_prosegue_con_solo_storico() -> None:
    det, _ = _detector(_FakeHass(forecast_error=RuntimeError("provider offline")))

    state = _run(det.async_detect(_history(), TODAY))

    assert state is not None
    assert state.season == Seasons.AUTUMN


# ---------------------------------------------------------------- E
def test_storico_troppo_corto_propaga_value_error_come_prima() -> None:
    det, _ = _detector()

    # comportamento invariato: il fit rifiuta storici insufficienti e l'errore
    # arriva al chiamante (weather_coordinator lo logga), senza cache sporca
    with pytest.raises(ValueError, match="Not enough daily data"):
        _run(det.async_detect(_history(days=20), TODAY))
    assert det._fit_cache is None


# ---------------------------------------------------------------- F
def test_eccezione_del_fit_si_propaga_e_non_sporca_la_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    det, _ = _detector()

    def boom(self: MeteoContiguousSeasonModel, *a: Any, **k: Any) -> Any:
        raise RuntimeError("fit rotto")

    monkeypatch.setattr(MeteoContiguousSeasonModel, "fit", boom)

    with pytest.raises(RuntimeError, match="fit rotto"):
        _run(det.async_detect(_history(), TODAY))
    assert det._fit_cache is None
