"""Test unitari del ``cold_snap_score_signed`` (patch #18, solo diagnostica).

Problema risolto
----------------
``cold_snap_score`` è saturato a zero: un giorno più caldo del normale e un giorno
normale mostrano entrambi 0.000, e la dashboard non può distinguerli (la metà
"warmer" della barra non si riempiva mai). ``cold_snap_score_signed`` espone la
stessa deviazione con segno. L'impianto continua a usare ``cold_snap`` e
``cold_snap_score`` invariati.

Copertura
---------
A. Modello: giorno caldo → signed < 0, score = 0, cold_snap False; giorno freddo → signed = score > 0.
B. Invariante su 90 giorni inferiti: score == max(0, signed).
C. Percorso infer (giorni fuori copertura) coerente con quello segmentato.
D. Attributo esposto (``weather reason``) arrotondato a 2 decimali, anche negativo.
E. Default del campo (0.0) e dati mancanti: punteggio 0.0 (fail-safe invariato).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from custom_components.drp_climate_master_v2.domain.models.season import (
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import build_season_attributes
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import MeteoContiguousSeasonModel

END = date(2026, 10, 21)
ANOMALY_DAYS = 28   # = finestra di t_smooth: lo scostamento si riflette per intero sulla media mobile
ANOMALY_C = 7.0
HALF_RANGE_C = 5.0


def _mean(d: date) -> float:
    return 16.0 + 9.5 * math.cos(2.0 * math.pi * (d.timetuple().tm_yday - 206) / 365.0)


def _history(delta_c: float) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for i in range(420):
        d = END - timedelta(days=i)
        m = _mean(d) + (delta_c if i < ANOMALY_DAYS else 0.0)
        out[d.isoformat()] = {
            "datetime": d.isoformat(), "native_temperature": m + HALF_RANGE_C, "native_templow": m - HALF_RANGE_C,
            "native_dew_point": m - 7.0, "native_wind_speed": 8.0, "cloud_coverage": 0.3,
        }
    return out


@pytest.fixture(scope="module")
def warm() -> MeteoContiguousSeasonModel:
    return MeteoContiguousSeasonModel().fit(history=_history(+ANOMALY_C))


@pytest.fixture(scope="module")
def cold() -> MeteoContiguousSeasonModel:
    return MeteoContiguousSeasonModel().fit(history=_history(-ANOMALY_C))


def _day(model: MeteoContiguousSeasonModel) -> WeatherSeason:
    info = model.infer(END, expected_season=Seasons.AUTUMN)
    assert info is not None
    return info


# ---------------------------------------------------------------- A / C
def test_giorno_caldo_signed_negativo_e_score_saturato_a_zero(warm: MeteoContiguousSeasonModel) -> None:
    info = _day(warm)

    assert info.cold_snap_score_signed < -0.5
    assert info.cold_snap_score == 0.0
    assert info.cold_snap is False


def test_giorno_freddo_signed_positivo_uguale_allo_score(cold: MeteoContiguousSeasonModel) -> None:
    info = _day(cold)

    assert info.cold_snap_score_signed > 0.5
    assert info.cold_snap_score == pytest.approx(info.cold_snap_score_signed)
    assert info.cold_snap is True


# ---------------------------------------------------------------- B
@pytest.mark.parametrize("fixture", ["warm", "cold"])
def test_invariante_score_uguale_max_zero_signed_su_tutti_i_giorni(fixture: str, request: pytest.FixtureRequest) -> None:
    model: MeteoContiguousSeasonModel = request.getfixturevalue(fixture)

    days = [END - timedelta(days=i) for i in range(90)]
    checked = 0
    for d in days:
        ws = model.infer(d, expected_season=Seasons.AUTUMN) if d >= END - timedelta(days=40) else model.infer(d)
        if ws is None:
            continue
        checked += 1
        assert ws.cold_snap_score == pytest.approx(max(0.0, ws.cold_snap_score_signed)), d
        assert ws.cold_snap == (ws.cold_snap_score_signed >= model._cfg.cold_snap_z), d
    assert checked >= 60


# ---------------------------------------------------------------- D
def _state(weather: WeatherSeason) -> SeasonState:
    return SeasonState(
        window=SeasonWindow(Seasons.AUTUMN, date(2026, 9, 1), date(2026, 11, 30)),
        weather=weather, as_of=END, detect_model="inferred",
    )


def _ws(**kw: object) -> WeatherSeason:
    sig = WeatherDaySignals(d=END, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.5, cloud=0.31,
                            t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0)
    return WeatherSeason(season=Seasons.AUTUMN, anomaly=False, anomaly_score=1.28, reason="infer",
                         regime_hint="mild", weather_day_signals=sig, **kw)  # type: ignore[arg-type]


def test_attributo_signed_negativo_arrotondato_a_due_decimali() -> None:
    attrs = build_season_attributes(_state(_ws(cold_snap=False, cold_snap_score=0.0, cold_snap_score_signed=-1.4349)))

    wr = attrs["weather reason"]
    assert wr["cold_snap_score_signed"] == -1.43
    assert wr["cold_snap_score"] == 0.0 and wr["cold_snap"] is False


def test_attributo_signed_positivo() -> None:
    attrs = build_season_attributes(_state(_ws(cold_snap=True, cold_snap_score=1.2651, cold_snap_score_signed=1.2651)))

    assert attrs["weather reason"]["cold_snap_score_signed"] == 1.27
    assert attrs["weather reason"]["cold_snap_score"] == 1.27


# ---------------------------------------------------------------- E
def test_default_del_campo_e_zero() -> None:
    assert _ws().cold_snap_score_signed == 0.0
    assert build_season_attributes(_state(_ws()))["weather reason"]["cold_snap_score_signed"] == 0.0


def test_dati_mancanti_punteggio_zero_fail_safe_invariato(warm: MeteoContiguousSeasonModel) -> None:
    from dataclasses import replace

    sig = replace(warm._signals[END], t_smooth=None)

    assert warm._cold_snap_score(Seasons.AUTUMN, sig, END) == 0.0
