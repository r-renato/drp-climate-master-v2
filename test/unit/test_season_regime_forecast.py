"""Test unitari per il regime dinamico previsto (forecast) con peso (patch #16c).

Problema risolto
----------------
La timeline mostrava la parte futura come "non determinata", ma il forecast arriva
già al modello e i suoi giorni hanno un regime dinamico calcolato (RMOT causale).
Lo esponiamo con un peso di affidabilità: i primi ``FORECAST_RELIABLE_DAYS`` giorni
sono i più attendibili (candidati alla gestione futura), i successivi solo
informativi. Resta diagnostica: i giorni previsti non entrano mai nei periodi
osservati e non guidano l'impianto.

Copertura
---------
A. ``regime_forecast_days``: anticipo, pesi sul confine dei 5 giorni, salti, ordine.
B. Costanti nominate.
C. Modello: forecast attaccato allo storico; i periodi osservati NON cambiano.
D. Degradazione: nessun forecast, righe senza Tmin → lista vuota/saltata, senza errori.
E. Attributi: forma, ``until``, giorni affidabili, JSON, integrazione.
F. Detector: forecast da ``weather.get_forecasts`` → stato; forecast in errore → vuoto.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.drp_climate_master_v2.controller.weather.season_detector import (
    SeasonDetector,
    SeasonDetectorConfig,
)
from custom_components.drp_climate_master_v2.domain.models.season import (
    RegimeForecastDay,
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import (
    build_regime_forecast_attributes,
    build_season_attributes,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)
from custom_components.drp_climate_master_v2.helpers.season.thermal_regime import (
    FORECAST_RELIABLE_DAYS,
    FORECAST_WEIGHT_INFORMATIVE,
    FORECAST_WEIGHT_RELIABLE,
    forecast_weight,
    regime_forecast_days,
)

D = date
TODAY = D(2026, 10, 15)


# ---------------------------------------------------------------- A
def _by_day(start: date, regimes: list[str | None]) -> dict[date, str | None]:
    return {start + timedelta(days=i): r for i, r in enumerate(regimes)}


def test_anticipo_e_peso_per_giorno() -> None:
    series = _by_day(TODAY - timedelta(days=2), ["mild"] * 11)  # 2 giorni osservati + oggi + 8 previsti

    days = regime_forecast_days(series, today=TODAY)

    assert [d.lead_days for d in days] == list(range(1, 9))
    assert [d.day for d in days] == [TODAY + timedelta(days=i) for i in range(1, 9)]
    assert all(d.regime == "mild" for d in days)


def test_peso_cambia_esattamente_dopo_il_quinto_giorno() -> None:
    days = regime_forecast_days(_by_day(TODAY + timedelta(days=1), ["mild"] * 8), today=TODAY)

    weights = {d.lead_days: d.weight for d in days}
    assert [weights[i] for i in range(1, 6)] == [FORECAST_WEIGHT_RELIABLE] * 5
    assert [weights[i] for i in range(6, 9)] == [FORECAST_WEIGHT_INFORMATIVE] * 3


def test_oggi_e_il_passato_non_sono_forecast() -> None:
    series = _by_day(TODAY - timedelta(days=5), ["hot"] * 6)  # fino a oggi incluso

    assert regime_forecast_days(series, today=TODAY) == []


def test_giorni_senza_regime_sono_saltati_e_l_anticipo_resta_reale() -> None:
    series = {TODAY + timedelta(days=1): "mild", TODAY + timedelta(days=2): None, TODAY + timedelta(days=3): "hot"}

    days = regime_forecast_days(series, today=TODAY)

    assert [(d.lead_days, d.regime) for d in days] == [(1, "mild"), (3, "hot")]


def test_serie_vuota() -> None:
    assert regime_forecast_days({}, today=TODAY) == []


def test_ordine_cronologico_indipendente_dall_ordine_di_inserimento() -> None:
    series = {TODAY + timedelta(days=3): "mild", TODAY + timedelta(days=1): "mild", TODAY + timedelta(days=2): "hot"}

    assert [d.lead_days for d in regime_forecast_days(series, today=TODAY)] == [1, 2, 3]


# ---------------------------------------------------------------- B
def test_costanti_nominate() -> None:
    assert FORECAST_RELIABLE_DAYS == 5
    assert FORECAST_WEIGHT_RELIABLE == 1.0
    assert 0.0 < FORECAST_WEIGHT_INFORMATIVE < FORECAST_WEIGHT_RELIABLE


@pytest.mark.parametrize(("lead", "expected"), [(1, 1.0), (5, 1.0), (6, 0.3), (14, 0.3)])
def test_forecast_weight(lead: int, expected: float) -> None:
    assert forecast_weight(lead) == expected


# ---------------------------------------------------------------- C
def _climate_row(d: date, delta: float = 0.0) -> dict[str, Any]:
    doy = d.timetuple().tm_yday
    mean = 16.0 + 9.5 * math.cos(2.0 * math.pi * (doy - 206) / 365.0) + delta
    return {
        "datetime": d.isoformat(),
        "native_temperature": mean + 4.0,
        "native_templow": mean - 4.0,
        "native_dew_point": mean - 7.0,
        "native_wind_speed": 8.0,
        "cloud_coverage": 0.3,
    }


def _model(today: date, *, forecast_days: int, forecast_delta: float = 0.0, days: int = 400) -> MeteoContiguousSeasonModel:
    daily = {today - timedelta(days=i): _climate_row(today - timedelta(days=i)) for i in range(days)}
    for i in range(1, forecast_days + 1):
        day = today + timedelta(days=i)
        daily[day] = _climate_row(day, forecast_delta)
    model = MeteoContiguousSeasonModel(MeteoSeasonConfig())
    model._signals = model._compute_signals(daily)
    return model


def test_modello_espone_otto_giorni_previsti_con_peso() -> None:
    model = _model(TODAY, forecast_days=8)

    forecast = model.regime_forecast(TODAY)

    assert [d.lead_days for d in forecast] == list(range(1, 9))
    assert [d.weight for d in forecast] == [1.0] * 5 + [0.3] * 3
    assert {d.regime for d in forecast} == {"mild"}  # ottobre sulla curva sintetica


def test_il_forecast_non_modifica_i_periodi_osservati() -> None:
    without = _model(TODAY, forecast_days=0).regime_periods(until=TODAY)
    with_hot_forecast = _model(TODAY, forecast_days=8, forecast_delta=12.0).regime_periods(until=TODAY)

    assert with_hot_forecast == without  # nemmeno un forecast molto caldo sposta l'osservato
    assert with_hot_forecast[-1].end is None


def test_forecast_molto_caldo_puo_cambiare_il_regime_previsto_non_l_osservato() -> None:
    model = _model(TODAY, forecast_days=8, forecast_delta=14.0)

    regimes = [d.regime for d in model.regime_forecast(TODAY)]

    assert regimes[0] == "mild"           # la conferma di 2 giorni impedisce lo scatto immediato
    assert regimes[-1] == "hot"           # ma con un forecast stabilmente caldo il regime cambia
    assert regimes == sorted(regimes, key=lambda r: r == "hot")  # una sola transizione mild → hot


# ---------------------------------------------------------------- D
def test_senza_forecast_lista_vuota() -> None:
    assert _model(TODAY, forecast_days=0).regime_forecast(TODAY) == []
    assert MeteoContiguousSeasonModel(MeteoSeasonConfig()).regime_forecast(TODAY) == []


def test_righe_di_forecast_senza_tmin_sono_saltate_senza_errori() -> None:
    daily = {TODAY - timedelta(days=i): _climate_row(TODAY - timedelta(days=i)) for i in range(400)}
    for i in range(1, 9):
        row = _climate_row(TODAY + timedelta(days=i))
        if i in (2, 3):
            row.pop("native_templow")
        daily[TODAY + timedelta(days=i)] = row
    model = MeteoContiguousSeasonModel(MeteoSeasonConfig())
    model._signals = model._compute_signals(daily)

    leads = [d.lead_days for d in model.regime_forecast(TODAY)]

    # I giorni 2 e 3 non hanno la media vera. Anche il giorno 4 resta senza regime: la RMOT usa
    # solo i giorni precedenti e, mancando i due ultimi, perde il 36% del peso (soglia di
    # copertura 70%). Dal giorno 5 la copertura risale al 71% e il regime torna disponibile.
    assert leads == [1, 5, 6, 7, 8]


# ---------------------------------------------------------------- E
def _state(forecast: list[RegimeForecastDay] | None) -> SeasonState:
    sig = WeatherDaySignals(
        d=TODAY, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.5, cloud=0.31,
        t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0,
    )
    return SeasonState(
        window=SeasonWindow(Seasons.AUTUMN, D(2026, 9, 1), D(2026, 11, 30)),
        weather=WeatherSeason(
            season=Seasons.AUTUMN, anomaly=False, anomaly_score=1.28, reason="infer",
            regime_hint="mild", weather_day_signals=sig, regime_forecast=forecast,
        ),
        as_of=TODAY,
        detect_model="inferred",
    )


FORECAST = [
    RegimeForecastDay(TODAY + timedelta(days=1), "mild", 1, 1.0),
    RegimeForecastDay(TODAY + timedelta(days=6), "hot", 6, 0.3),
]


def test_attributi_forma_until_e_giorni_affidabili() -> None:
    attrs = build_regime_forecast_attributes(_state(FORECAST))

    assert attrs["dynamic regime forecast"][0] == {"day": "2026-10-16", "regime": "mild", "lead days": 1, "weight": 1.0}
    assert attrs["dynamic regime forecast"][1]["weight"] == 0.3
    assert attrs["dynamic regime forecast until"] == "2026-10-21"
    assert attrs["dynamic regime forecast reliable days"] == FORECAST_RELIABLE_DAYS


@pytest.mark.parametrize("forecast", [None, []])
def test_attributi_senza_forecast_degradano(forecast: list[RegimeForecastDay] | None) -> None:
    attrs = build_regime_forecast_attributes(_state(forecast))

    assert attrs["dynamic regime forecast"] == []
    assert attrs["dynamic regime forecast until"] is None


def test_integrazione_serializzabile_e_senza_regressioni() -> None:
    attrs = build_season_attributes(_state(FORECAST))

    for key in ("dynamic regime periods", "dynamic regime spans", "dynamic windows", "seasons"):
        assert key in attrs, key
    assert json.loads(json.dumps(attrs)) == attrs


# ---------------------------------------------------------------- F
class _FakeHass:
    def __init__(self, forecast_days: int | None) -> None:
        self.services = MagicMock()

        async def _call(*a: Any, **k: Any) -> Any:
            if forecast_days is None:
                raise RuntimeError("nessun forecast")
            items = []
            for i in range(1, forecast_days + 1):
                day = TODAY + timedelta(days=i)
                row = _climate_row(day)
                row["datetime"] = f"{day.isoformat()}T00:00:00+00:00"
                items.append(row)
            return {"weather.test": {"forecast": items}}

        self.services.async_call = _call

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)


def _detect(forecast_days: int | None) -> SeasonState | None:
    history = {TODAY - timedelta(days=i): _climate_row(TODAY - timedelta(days=i)) for i in range(330)}
    det = SeasonDetector(_FakeHass(forecast_days), SeasonDetectorConfig(forecast_provider="weather.test"))  # type: ignore[arg-type]
    return asyncio.run(det.async_detect(history, TODAY))


def test_detector_espone_il_forecast_con_pesi_e_non_altera_l_osservato() -> None:
    with_fc, without_fc = _detect(8), _detect(None)

    assert with_fc is not None and without_fc is not None
    forecast = with_fc.weather.regime_forecast
    assert forecast is not None and [d.lead_days for d in forecast] == list(range(1, 9))
    assert [d.weight for d in forecast] == [1.0] * 5 + [0.3] * 3
    assert with_fc.weather.regime_periods == without_fc.weather.regime_periods
    assert with_fc.weather.regime_spans == without_fc.weather.regime_spans


def test_detector_senza_forecast_ha_lista_vuota() -> None:
    state = _detect(None)

    assert state is not None and state.weather.regime_forecast == []
