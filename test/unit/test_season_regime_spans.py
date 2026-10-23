"""Test unitari per inizio e fine dei regimi dinamici hot/cold (patch #15).

Problema risolto
----------------
La patch #13 esponeva solo la *fine* del regime "hot". Per capire dove il
calendario si discosta dal clima serve il ciclo completo dei regimi termici
(RMOT + isteresi + conferma): quando iniziano e finiscono hot e cold, anno per
anno. "mild" non ha confini propri: è il residuo tra le transizioni.

Scelte di progetto verificate qui
---------------------------------
- i periodi si cercano sull'intera serie, così un periodo a cavallo del 1/12 non
  viene spezzato; ogni periodo è attribuito all'anno meteo del suo **inizio**;
- ondate separate da pause: inizio del primo, fine dell'ultimo, più conteggi;
- ``start_known=False`` se lo storico inizia già dentro il regime;
- regime in corso → ``end=None``; ``until`` esclude il forecast;
- giorni con regime ``None`` non spezzano un periodo.

Copertura
---------
A. ``regime_spans_by_year``: casi base, ondate, in corso, inizio ignoto, cavallo
   d'anno, buchi, cutoff, regimi non tracciati, vuoto.
B. Modello: serie sintetica di due anni; coerenza con ``hot_regime_ends`` (#13).
C. Attributi: forma, JSON, assenza dati, integrazione senza regressioni.
D. Detector: gli spans arrivano nello stato e non superano ``today``.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

from custom_components.drp_climate_master_v2.controller.weather.season_detector import (
    SeasonDetector,
    SeasonDetectorConfig,
)
from custom_components.drp_climate_master_v2.domain.models.season import (
    RegimeSpan,
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import (
    build_regime_spans_attributes,
    build_season_attributes,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)
from custom_components.drp_climate_master_v2.helpers.season.thermal_regime import (
    regime_spans_by_year,
)

D = date


def _series(start: date, spec: list[tuple[int, str | None]]) -> dict[date, str | None]:
    """Serie giornaliera da spec [(n_giorni, regime), ...] a partire da ``start``."""
    out: dict[date, str | None] = {}
    cursor = start
    for n, regime in spec:
        for _ in range(n):
            out[cursor] = regime
            cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------- A
def test_un_periodo_hot_ha_inizio_fine_e_giorni() -> None:
    series = _series(D(2026, 5, 1), [(30, "mild"), (100, "hot"), (30, "mild")])  # hot 31/5 → 7/9

    span = regime_spans_by_year(series)[2026]["hot"]

    assert span == RegimeSpan(start=D(2026, 5, 31), end=D(2026, 9, 8), start_known=True, periods=1, days=100)


def test_hot_e_cold_nello_stesso_anno_meteo() -> None:
    series = _series(
        D(2025, 12, 1),
        [(20, "mild"), (50, "cold"), (100, "mild"), (90, "hot"), (30, "mild")],
    )

    year = regime_spans_by_year(series)[2026]

    assert year["cold"].start == D(2025, 12, 21) and year["cold"].end == D(2026, 2, 9)
    assert year["hot"].start == D(2026, 5, 20) and year["hot"].end == D(2026, 8, 18)
    assert "mild" not in year  # il residuo non ha confini


def test_ondate_multiple_inizio_del_primo_fine_dell_ultimo() -> None:
    series = _series(D(2026, 5, 1), [(10, "mild"), (20, "hot"), (5, "mild"), (40, "hot"), (3, "mild"), (10, "hot"), (20, "mild")])

    span = regime_spans_by_year(series)[2026]["hot"]

    assert span.start == D(2026, 5, 11)
    assert span.end == D(2026, 5, 1) + timedelta(days=88)  # 10+20+5+40+3+10 = 88 → primo giorno non hot
    assert (span.periods, span.days) == (3, 70)


def test_regime_in_corso_ha_end_none() -> None:
    span = regime_spans_by_year(_series(D(2026, 6, 1), [(10, "mild"), (60, "hot")]))[2026]["hot"]

    assert span.end is None and span.start == D(2026, 6, 11) and span.days == 60


def test_storico_che_inizia_dentro_il_regime_ha_inizio_non_noto() -> None:
    span = regime_spans_by_year(_series(D(2026, 8, 1), [(40, "hot"), (10, "mild")]))[2026]["hot"]

    assert span.start_known is False
    assert span.start == D(2026, 8, 1)  # limite superiore, non l'inizio reale


def test_periodo_a_cavallo_del_1_dicembre_non_viene_spezzato() -> None:
    series = _series(D(2025, 11, 10), [(20, "mild"), (50, "cold"), (30, "mild")])  # cold 30/11 → 18/1

    spans = regime_spans_by_year(series)

    assert spans[2025]["cold"].start == D(2025, 11, 30)  # attribuito all'anno dell'inizio
    assert spans[2025]["cold"].end == D(2026, 1, 19) and spans[2025]["cold"].days == 50
    assert 2026 not in spans


def test_inizio_a_dicembre_appartiene_all_anno_meteo_successivo() -> None:
    spans = regime_spans_by_year(_series(D(2025, 11, 20), [(15, "mild"), (40, "cold"), (10, "mild")]))

    assert set(spans) == {2026} and spans[2026]["cold"].start == D(2025, 12, 5)


def test_giorni_senza_regime_non_spezzano_un_periodo() -> None:
    series = _series(D(2026, 6, 1), [(5, "mild"), (10, "hot"), (4, None), (10, "hot"), (5, "mild")])

    span = regime_spans_by_year(series)[2026]["hot"]

    assert (span.periods, span.days) == (1, 20)
    assert span.end == D(2026, 6, 30)  # primo giorno noto non-hot


def test_cutoff_until_esclude_il_forecast() -> None:
    series = _series(D(2026, 8, 1), [(10, "mild"), (61, "hot"), (10, "mild")])  # hot 11/8 → 10/10

    assert regime_spans_by_year(series, until=D(2026, 10, 5))[2026]["hot"].end is None  # ancora in corso
    assert regime_spans_by_year(series, until=D(2026, 10, 15))[2026]["hot"].end == D(2026, 10, 11)


def test_nessun_regime_tracciato_o_serie_vuota() -> None:
    assert regime_spans_by_year({}) == {}
    assert regime_spans_by_year(_series(D(2026, 1, 1), [(100, "mild")])) == {}
    assert regime_spans_by_year(_series(D(2026, 1, 1), [(10, None)])) == {}


def test_regimi_selezionabili() -> None:
    series = _series(D(2026, 1, 1), [(20, "cold"), (20, "hot"), (5, "mild")])

    assert set(regime_spans_by_year(series, regimes=("cold",))[2026]) == {"cold"}


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
    model._signals = model._compute_signals(daily)  # solo segnali: nessun fit pesante
    return model


def test_modello_ciclo_completo_hot_e_cold_coerente_tra_anni() -> None:
    model = _model_with_signals(D(2026, 10, 17))

    spans = model.regime_spans(until=D(2026, 10, 17))

    assert {2025, 2026} <= set(spans)
    for year in (2025, 2026):
        hot, cold = spans[year]["hot"], spans[year]["cold"]
        # curva sintetica: T media ≥ 22 °C da inizio giugno a metà settembre (+ ritardo RMOT)
        assert D(year, 6, 1) <= hot.start <= D(year, 6, 20), hot
        assert hot.end is not None and D(year, 9, 20) <= hot.end <= D(year, 10, 4), hot
        # cold: da dicembre dell'anno prima a febbraio
        assert D(year - 1, 12, 5) <= cold.start <= D(year, 1, 10), cold
        assert cold.end is not None and D(year, 2, 10) <= cold.end <= D(year, 3, 15), cold
        assert hot.periods == 1 and cold.periods == 1  # curva liscia: nessuna ondata spezzata


def test_modello_coerente_con_la_fine_hot_della_patch_13() -> None:
    model = _model_with_signals(D(2026, 10, 17))
    ends = model.hot_regime_ends(until=D(2026, 10, 17))
    spans = model.regime_spans(until=D(2026, 10, 17))

    for year, end in ends.items():
        assert spans[year]["hot"].end == end


def test_modello_regime_in_corso_non_ha_fine() -> None:
    model = _model_with_signals(D(2026, 10, 17))

    hot_2026 = model.regime_spans(until=D(2026, 8, 1))[2026]["hot"]

    assert hot_2026.end is None  # ad agosto è ancora hot


def test_modello_senza_segnali_non_solleva() -> None:
    assert MeteoContiguousSeasonModel(MeteoSeasonConfig()).regime_spans() == {}


# ---------------------------------------------------------------- C
def _state(spans: dict[int, dict[str, RegimeSpan]] | None) -> SeasonState:
    as_of = D(2026, 10, 17)
    sig = WeatherDaySignals(
        d=as_of, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.5, cloud=0.31,
        t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0,
    )
    return SeasonState(
        window=SeasonWindow(Seasons.AUTUMN, D(2026, 9, 1), D(2026, 11, 30)),
        weather=WeatherSeason(
            season=Seasons.AUTUMN, anomaly=False, anomaly_score=1.28, reason="infer",
            regime_hint="mild", weather_day_signals=sig, regime_spans=spans,
        ),
        as_of=as_of,
        detect_model="inferred",
    )


SPANS = {
    2025: {
        "cold": RegimeSpan(D(2024, 12, 20), D(2025, 2, 25), True, 1, 67),
        "hot": RegimeSpan(D(2025, 6, 3), D(2025, 9, 30), True, 2, 110),
    },
    2026: {
        "cold": RegimeSpan(D(2025, 12, 14), D(2026, 2, 20), False, 1, 68),
        "hot": RegimeSpan(D(2026, 5, 30), None, True, 1, 140),
    },
}


def test_attributi_forma_e_valori() -> None:
    attrs = build_regime_spans_attributes(_state(SPANS))["dynamic regime spans"]

    assert list(attrs) == ["2025", "2026"]
    assert attrs["2025"]["hot"] == {
        "start": "2025-06-03", "end": "2025-09-30", "start known": True,
        "ongoing": False, "periods": 2, "days": 110,
    }
    assert attrs["2026"]["hot"]["end"] is None and attrs["2026"]["hot"]["ongoing"] is True
    assert attrs["2026"]["cold"]["start known"] is False


def test_attributi_senza_dati_degradano_a_vuoto() -> None:
    assert build_regime_spans_attributes(_state(None)) == {"dynamic regime spans": {}}
    assert build_regime_spans_attributes(_state({})) == {"dynamic regime spans": {}}


def test_attributi_serializzabili_e_integrati_senza_regressioni() -> None:
    attrs = build_season_attributes(_state(SPANS))

    assert attrs["dynamic regime spans"]["2026"]["hot"]["days"] == 140
    for key in ("dynamic windows", "dynamic hot regime end", "seasons", "weather signals flat"):
        assert key in attrs, key
    assert json.loads(json.dumps(attrs)) == attrs


# ---------------------------------------------------------------- D
TODAY = D(2026, 10, 15)


class _FakeHass:
    def __init__(self) -> None:
        self.services = MagicMock()

        async def _call(*a: Any, **k: Any) -> Any:
            raise RuntimeError("nessun forecast")

        self.services.async_call = _call

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)


def test_detector_espone_gli_spans_e_non_supera_today() -> None:
    history = {TODAY - timedelta(days=i): _climate_row(TODAY - timedelta(days=i)) for i in range(330)}
    det = SeasonDetector(_FakeHass(), SeasonDetectorConfig(forecast_provider="weather.test"))  # type: ignore[arg-type]

    state = asyncio.run(det.async_detect(history, TODAY))

    assert state is not None and state.weather.regime_spans is not None
    year = state.weather.regime_spans[2026]
    assert year["hot"].end is not None and year["hot"].end <= TODAY
    assert year["cold"].end is not None and year["cold"].end <= TODAY
    assert all(
        span.end is None or span.end <= TODAY
        for by_regime in state.weather.regime_spans.values()
        for span in by_regime.values()
    )
