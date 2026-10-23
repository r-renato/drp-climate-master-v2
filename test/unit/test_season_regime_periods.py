"""Test unitari per i periodi dei regimi dinamici e le fasce della timeline (patch #16).

Problema risolto
----------------
La patch #15 sintetizzava hot/cold per anno (primo inizio, ultima fine). La
timeline di dashboard richiede tutti i periodi cold/mild/hot in ordine
cronologico, le soglie per la legenda, l'ultimo giorno osservato e le fasce
astronomiche dell'anno meteo corrente. Il mild è il residuo tra le transizioni:
compare come periodo ma non ha soglie proprie.

Copertura
---------
A. ``regime_periods``: sequenza, giorni, periodo in corso, inizio ignoto, buchi
   ``None``, cutoff ``until``, vuoto; coerenza con ``regime_spans_by_year``.
B. Modello: curva sintetica di due anni → sequenza mild/cold/mild/hot/mild.
C. Attributi: forma, soglie dalle costanti, ``as of``, JSON, degradazione.
D. ``astronomical_bands_of``: continuità sull'asse 1/12→1/12, date coerenti con
   ``astronomical_season_for``, fuori intervallo → lista vuota.
E. Detector: i periodi arrivano nello stato e non superano ``today``.
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
    RegimePeriod,
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season import season_attributes
from custom_components.drp_climate_master_v2.helpers.season.season_astronomical import (
    astronomical_season_for,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import (
    astronomical_bands_of,
    build_regime_periods_attributes,
    build_season_attributes,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)
from custom_components.drp_climate_master_v2.helpers.season.thermal_regime import (
    REGIME_COLD_ENTER_MAX_C,
    REGIME_COLD_EXIT_MIN_C,
    REGIME_COOL_ENTER_MAX_C,
    REGIME_COOL_EXIT_MIN_C,
    REGIME_HOT_ENTER_MIN_C,
    REGIME_HOT_EXIT_MAX_C,
    REGIME_TORRID_ENTER_MIN_C,
    REGIME_TORRID_EXIT_MAX_C,
    regime_periods,
    regime_spans_by_year,
)

D = date


def _series(start: date, spec: list[tuple[int, str | None]]) -> dict[date, str | None]:
    out: dict[date, str | None] = {}
    cursor = start
    for n, regime in spec:
        for _ in range(n):
            out[cursor] = regime
            cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------- A
def test_sequenza_cronologica_con_giorni_e_fine() -> None:
    series = _series(D(2026, 1, 1), [(10, "mild"), (9, "cold"), (30, "mild"), (20, "hot"), (5, "mild")])

    periods = regime_periods(series)

    assert [(p.regime, p.start, p.end, p.days) for p in periods] == [
        ("mild", D(2026, 1, 1), D(2026, 1, 11), 10),
        ("cold", D(2026, 1, 11), D(2026, 1, 20), 9),
        ("mild", D(2026, 1, 20), D(2026, 2, 19), 30),
        ("hot", D(2026, 2, 19), D(2026, 3, 11), 20),
        ("mild", D(2026, 3, 11), None, 5),
    ]


def test_primo_periodo_ha_inizio_non_noto_gli_altri_si() -> None:
    periods = regime_periods(_series(D(2026, 8, 1), [(10, "hot"), (10, "mild")]))

    assert [p.start_known for p in periods] == [False, True]


def test_periodi_contigui_senza_buchi() -> None:
    periods = regime_periods(_series(D(2026, 1, 1), [(7, "mild"), (7, "hot"), (7, "mild")]))

    for prev, nxt in zip(periods, periods[1:]):
        assert prev.end == nxt.start


def test_giorni_senza_regime_non_spezzano_un_periodo() -> None:
    series = _series(D(2026, 6, 1), [(5, "mild"), (10, "hot"), (4, None), (10, "hot"), (5, "mild")])

    periods = regime_periods(series)

    assert [(p.regime, p.days) for p in periods] == [("mild", 5), ("hot", 20), ("mild", 5)]
    assert periods[1].end == D(2026, 6, 30)  # primo giorno noto in un regime diverso


def test_cutoff_until_e_regime_in_corso() -> None:
    series = _series(D(2026, 8, 1), [(10, "mild"), (61, "hot"), (10, "mild")])  # hot 11/8 → 10/10

    assert regime_periods(series, until=D(2026, 10, 5))[-1].end is None
    assert regime_periods(series, until=D(2026, 10, 15))[-2].end == D(2026, 10, 11)


def test_serie_vuota_o_solo_ignoti() -> None:
    assert regime_periods({}) == []
    assert regime_periods(_series(D(2026, 1, 1), [(10, None)])) == []


def test_i_giorni_dei_periodi_sommano_ai_giorni_noti() -> None:
    series = _series(D(2026, 1, 1), [(10, "mild"), (3, None), (9, "cold"), (30, "mild")])

    assert sum(p.days for p in regime_periods(series)) == sum(1 for v in series.values() if v is not None)


def test_coerenza_con_la_sintesi_per_anno_della_patch_15() -> None:
    series = _series(D(2026, 1, 1), [(10, "mild"), (9, "cold"), (110, "mild"), (80, "hot"), (3, "mild"), (20, "hot"), (10, "mild")])

    spans = regime_spans_by_year(series)[2026]
    hot = [p for p in regime_periods(series) if p.regime == "hot"]

    assert spans["hot"].start == hot[0].start and spans["hot"].end == hot[-1].end
    assert spans["hot"].days == sum(p.days for p in hot) and spans["hot"].periods == len(hot)
    assert "mild" not in spans  # la sintesi per anno resta hot/cold


# ---------------------------------------------------------------- B
def _climate_row(d: date) -> dict[str, Any]:
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


def _model_with_signals(end: date, days: int = 730) -> MeteoContiguousSeasonModel:
    daily = {end - timedelta(days=i): _climate_row(end - timedelta(days=i)) for i in range(days)}
    model = MeteoContiguousSeasonModel(MeteoSeasonConfig())
    model._signals = model._compute_signals(daily)
    return model


def test_modello_sequenza_mild_cold_mild_hot_mild_per_ogni_anno() -> None:
    model = _model_with_signals(D(2026, 10, 17))

    periods = model.regime_periods(until=D(2026, 10, 17))

    seq = [p.regime for p in periods]
    # inverno: mild → cool → cold → cool → mild (si scende e si risale passando per "cool"), poi hot
    assert seq == ["mild", "cool", "cold", "cool", "mild", "hot", "mild",
                   "cool", "cold", "cool", "mild", "hot", "mild"]
    assert periods[0].start_known is False and all(p.start_known for p in periods[1:])
    assert periods[-1].end is None  # ottobre 2026: mild in corso
    assert all(p.end is None or p.end <= D(2026, 10, 17) for p in periods)


def test_modello_senza_segnali_non_solleva() -> None:
    assert MeteoContiguousSeasonModel(MeteoSeasonConfig()).regime_periods() == []


# ---------------------------------------------------------------- C
PERIODS = [
    RegimePeriod("mild", D(2025, 9, 30), D(2026, 1, 10), True, 102),
    RegimePeriod("cold", D(2026, 1, 10), D(2026, 1, 19), True, 9),
    RegimePeriod("hot", D(2026, 5, 29), None, True, 140),
]


def _state(periods: list[RegimePeriod] | None, as_of: date = D(2026, 10, 17)) -> SeasonState:
    sig = WeatherDaySignals(
        d=as_of, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.5, cloud=0.31,
        t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0,
    )
    return SeasonState(
        window=SeasonWindow(Seasons.AUTUMN, D(2026, 9, 1), D(2026, 11, 30)),
        weather=WeatherSeason(
            season=Seasons.AUTUMN, anomaly=False, anomaly_score=1.28, reason="infer",
            regime_hint="mild", weather_day_signals=sig, regime_periods=periods,
        ),
        as_of=as_of,
        detect_model="inferred",
    )


def test_attributi_forma_soglie_e_as_of() -> None:
    attrs = build_regime_periods_attributes(_state(PERIODS))

    assert attrs["dynamic regime periods"][1] == {
        "regime": "cold", "start": "2026-01-10", "end": "2026-01-19", "start known": True, "days": 9,
    }
    assert attrs["dynamic regime periods"][2]["end"] is None
    assert attrs["dynamic regime periods as of"] == "2026-10-17"
    assert attrs["dynamic regime thresholds"] == {
        "cold enter max": REGIME_COLD_ENTER_MAX_C,
        "cold exit min": REGIME_COLD_EXIT_MIN_C,
        "cool enter max": REGIME_COOL_ENTER_MAX_C,
        "cool exit min": REGIME_COOL_EXIT_MIN_C,
        "hot enter min": REGIME_HOT_ENTER_MIN_C,
        "hot exit max": REGIME_HOT_EXIT_MAX_C,
        "torrid enter min": REGIME_TORRID_ENTER_MIN_C,
        "torrid exit max": REGIME_TORRID_EXIT_MAX_C,
    }
    assert (REGIME_COLD_ENTER_MAX_C, REGIME_HOT_ENTER_MIN_C) == (8.0, 22.0)


@pytest.mark.parametrize("periods", [None, []])
def test_attributi_senza_periodi_degradano_a_vuoto(periods: list[RegimePeriod] | None) -> None:
    attrs = build_regime_periods_attributes(_state(periods))

    assert attrs["dynamic regime periods"] == []
    assert attrs["astronomical bands"]  # le fasce dipendono solo dalla data


def test_attributi_serializzabili_in_json_e_integrati_senza_regressioni() -> None:
    attrs = build_season_attributes(_state(PERIODS))

    for key in ("dynamic windows", "dynamic hot regime end", "dynamic regime spans", "seasons", "weather signals flat"):
        assert key in attrs, key
    assert json.loads(json.dumps(attrs)) == attrs


# ---------------------------------------------------------------- D
@pytest.mark.parametrize("as_of", [D(2026, 10, 17), D(2026, 12, 5), D(2025, 5, 1), D(2028, 2, 29), D(2026, 11, 30), D(2026, 12, 1)])
def test_fasce_astronomiche_coprono_l_asse_senza_buchi(as_of: date) -> None:
    bands = astronomical_bands_of(as_of)

    year = as_of.year + 1 if as_of.month == 12 else as_of.year
    assert bands[0]["start"] == date(year - 1, 12, 1).isoformat()
    assert bands[-1]["end"] == date(year, 12, 1).isoformat()
    for prev, nxt in zip(bands, bands[1:]):
        assert prev["end"] == nxt["start"]
        assert prev["season"] != nxt["season"]  # nessun tratto frammentato
    assert [b["season"] for b in bands] == ["autumn", "winter", "spring", "summer", "autumn"]


def test_fasce_astronomiche_coerenti_con_la_finestra_del_tile() -> None:
    bands = astronomical_bands_of(D(2026, 10, 17))
    window = astronomical_season_for(D(2026, 10, 17))

    assert bands[-1] == {"season": "autumn", "start": window.start.date().isoformat(), "end": "2026-12-01"}
    assert window.end.date() == D(2026, 12, 21)  # il tile mostra 2026-09-23 - 2026-12-21


def test_fasce_astronomiche_fuori_intervallo_degradano_a_vuoto(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_: date) -> None:
        raise ValueError("anno fuori intervallo")

    monkeypatch.setattr(season_attributes, "astronomical_season_for", _boom)

    assert astronomical_bands_of(D(2026, 10, 17)) == []


# ---------------------------------------------------------------- E
TODAY = D(2026, 10, 15)


class _FakeHass:
    def __init__(self) -> None:
        self.services = MagicMock()

        async def _call(*a: Any, **k: Any) -> Any:
            raise RuntimeError("nessun forecast")

        self.services.async_call = _call

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)


def test_detector_espone_i_periodi_e_non_supera_today() -> None:
    history = {TODAY - timedelta(days=i): _climate_row(TODAY - timedelta(days=i)) for i in range(330)}
    det = SeasonDetector(_FakeHass(), SeasonDetectorConfig(forecast_provider="weather.test"))  # type: ignore[arg-type]

    state = asyncio.run(det.async_detect(history, TODAY))

    assert state is not None and state.weather.regime_periods
    periods = state.weather.regime_periods
    assert periods[-1].end is None and periods[-1].regime == "mild"
    assert all(p.end is None or p.end <= TODAY for p in periods)
    assert [p.regime for p in periods if p.regime != "mild"] == ["cool", "cold", "cool", "hot"]
