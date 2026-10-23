"""Test unitari per la fine del regime "hot" (RMOT) per anno meteo (patch #13).

Problema risolto
----------------
Il segmentatore ML colloca l'inizio dell'autunno usando feature a 28 giorni
(``t_smooth``, ``trend``, ``dew_smooth``): una media così lunga conferma il
cambio di regime con ritardo (nel caso reale 2025-09-29, +28 giorni dal
calendario). Per quantificare il ritardo serve una data termica a memoria breve:
la RMOT (~5 giorni) con isteresi e conferma, già calcolata in shadow.

Questa patch espone, per ogni anno meteo, il primo giorno dopo l'ultimo periodo
di regime dinamico "hot" e lo confronta con l'inizio autunno del ML
(``ml lag days``). Solo diagnostica: nessuna decisione cambia.

Copertura
---------
A. ``regime_end_by_year``: casi base, ondate di caldo multiple, buchi, regime in
   corso, mai verificato, cutoff ``until``, anno meteo di dicembre, regime "cold".
B. ``meteo_year_of``.
C. Modello: serie sintetica di due anni → fine "hot" a fine settembre, coerente
   tra gli anni; il cutoff esclude l'anno non ancora concluso.
D. Attributi: offset da calendario, ``ml lag days`` solo se l'inizio ML esiste.
E. Degradazione: nessun dato → ``{}`` senza eccezioni; JSON-serializzabile.
F. Detector: la fine "hot" arriva nello stato e non supera mai ``today``.
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
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import (
    build_hot_regime_end_attributes,
    build_season_attributes,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)
from custom_components.drp_climate_master_v2.helpers.season.thermal_regime import (
    meteo_year_of,
    regime_end_by_year,
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
def test_fine_hot_e_il_primo_giorno_dopo_l_ultimo_hot() -> None:
    series = _series(D(2025, 6, 1), [(103, "hot"), (30, "mild")])  # hot fino all'11/9

    assert regime_end_by_year(series) == {2025: D(2025, 9, 12)}


def test_ondate_di_caldo_multiple_contano_solo_l_ultima() -> None:
    series = _series(D(2025, 6, 1), [(20, "hot"), (5, "mild"), (40, "hot"), (3, "mild"), (10, "hot"), (20, "mild")])

    # ultimo hot = giorno 20+5+40+3+10 = 78 → fine il 79° giorno
    assert regime_end_by_year(series) == {2025: D(2025, 6, 1) + timedelta(days=78)}


def test_giorni_senza_regime_sono_saltati() -> None:
    series = _series(D(2025, 8, 1), [(30, "hot"), (3, None), (10, "mild")])  # hot 1-30/8, ignoti 31/8-2/9

    assert regime_end_by_year(series) == {2025: D(2025, 9, 3)}  # primo giorno noto non-hot


def test_hot_ancora_in_corso_non_produce_una_fine() -> None:
    assert regime_end_by_year(_series(D(2026, 6, 1), [(60, "hot")])) == {}


def test_hot_seguito_solo_da_giorni_ignoti_e_ancora_in_corso() -> None:
    assert regime_end_by_year(_series(D(2026, 6, 1), [(30, "hot"), (5, None)])) == {}


def test_mai_hot_nessuna_fine() -> None:
    assert regime_end_by_year(_series(D(2026, 1, 1), [(120, "mild")])) == {}
    assert regime_end_by_year({}) == {}


def test_cutoff_until_esclude_i_giorni_del_forecast() -> None:
    # hot fino al 10/10, mild dall'11/10 (forecast): oggi è il 5/10 → ancora hot
    series = _series(D(2026, 8, 1), [(71, "hot"), (10, "mild")])

    assert regime_end_by_year(series, until=D(2026, 10, 5)) == {}
    assert regime_end_by_year(series, until=D(2026, 10, 15)) == {2026: D(2026, 10, 11)}


def test_fine_a_dicembre_appartiene_all_anno_meteo_successivo() -> None:
    series = _series(D(2026, 11, 20), [(10, "hot"), (10, "mild")])  # hot fino al 29/11 → fine 30/11

    assert regime_end_by_year(series) == {2026: D(2026, 11, 30)}
    series = _series(D(2026, 11, 25), [(8, "hot"), (5, "mild")])  # hot fino al 2/12 → fine 3/12
    assert regime_end_by_year(series) == {2027: D(2026, 12, 3)}


def test_anni_meteo_separati() -> None:
    series = {
        **_series(D(2025, 7, 1), [(60, "hot"), (30, "mild")]),
        **_series(D(2026, 7, 1), [(60, "hot"), (30, "mild")]),
    }

    assert regime_end_by_year(series) == {2025: D(2025, 8, 30), 2026: D(2026, 8, 30)}


def test_regime_cold_parametrizzabile() -> None:
    series = _series(D(2026, 1, 1), [(50, "cold"), (30, "mild")])

    assert regime_end_by_year(series, regime="cold") == {2026: D(2026, 2, 20)}


# ---------------------------------------------------------------- B
@pytest.mark.parametrize(
    ("day", "expected"),
    [(D(2025, 11, 30), 2025), (D(2025, 12, 1), 2026), (D(2026, 1, 15), 2026), (D(2026, 9, 1), 2026)],
)
def test_anno_meteo(day: date, expected: int) -> None:
    assert meteo_year_of(day) == expected


# ---------------------------------------------------------------- C
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


def test_modello_fine_hot_a_fine_settembre_e_coerente_tra_anni() -> None:
    model = _model_with_signals(D(2026, 10, 17))

    ends = model.hot_regime_ends(until=D(2026, 10, 17))

    assert set(ends) == {2025, 2026}
    for end in ends.values():
        # T media ≥ 21 °C fino al ~25/9 sulla curva sintetica; RMOT in ritardo ~3 giorni + 2 di conferma
        assert D(end.year, 9, 22) <= end <= D(end.year, 10, 4), end
    assert abs((ends[2026] - D(2026, 1, 1)).days - (ends[2025] - D(2025, 1, 1)).days) <= 1


def test_modello_cutoff_esclude_l_anno_non_ancora_concluso() -> None:
    model = _model_with_signals(D(2026, 10, 17))

    assert set(model.hot_regime_ends(until=D(2026, 9, 10))) == {2025}  # a settembre 2026 è ancora hot


def test_modello_senza_segnali_non_solleva() -> None:
    assert MeteoContiguousSeasonModel(MeteoSeasonConfig()).hot_regime_ends() == {}


# ---------------------------------------------------------------- D
def _state(
    *,
    ends: dict[int, date] | None,
    windows: list[tuple[date, date, Seasons]] | None = None,
) -> SeasonState:
    as_of = D(2026, 10, 17)
    sig = WeatherDaySignals(
        d=as_of, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.5, cloud=0.31,
        t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0,
    )
    return SeasonState(
        window=SeasonWindow(Seasons.AUTUMN, D(2026, 9, 1), D(2026, 11, 30)),
        weather=WeatherSeason(
            season=Seasons.AUTUMN, anomaly=False, anomaly_score=1.28, reason="infer",
            regime_hint="mild", weather_day_signals=sig, windows=windows, hot_regime_ends=ends,
        ),
        as_of=as_of,
        detect_model="inferred",
    )


def test_attributi_offset_dal_calendario_e_lag_del_ml() -> None:
    state = _state(
        ends={2025: D(2025, 9, 12), 2026: D(2026, 10, 3)},
        windows=[(D(2025, 9, 29), D(2025, 11, 30), Seasons.AUTUMN)],  # ML: +28 d dal calendario
    )

    attrs = build_hot_regime_end_attributes(state)["dynamic hot regime end"]

    assert attrs["2025"] == {
        "date": "2025-09-12",
        "offset days": 11,
        "ml autumn start": "2025-09-29",
        "ml lag days": 17,  # il ML conferma l'autunno 17 giorni dopo il regime termico
    }
    # 2026: nessun inizio ML (anno meteo non segmentato) → niente campi di confronto
    assert attrs["2026"] == {"date": "2026-10-03", "offset days": 32}


def test_attributi_senza_dati_degradano_a_vuoto() -> None:
    assert build_hot_regime_end_attributes(_state(ends=None)) == {"dynamic hot regime end": {}}
    assert build_hot_regime_end_attributes(_state(ends={})) == {"dynamic hot regime end": {}}


def test_inizio_autunno_ml_di_un_altro_anno_non_viene_confrontato() -> None:
    state = _state(
        ends={2026: D(2026, 10, 3)},
        windows=[(D(2025, 9, 29), D(2025, 11, 30), Seasons.AUTUMN)],
    )

    assert "ml lag days" not in build_hot_regime_end_attributes(state)["dynamic hot regime end"]["2026"]


# ---------------------------------------------------------------- E
def test_attributi_serializzabili_in_json_e_integrati() -> None:
    state = _state(
        ends={2025: D(2025, 9, 12)},
        windows=[(D(2025, 9, 29), D(2025, 11, 30), Seasons.AUTUMN)],
    )

    attrs = build_season_attributes(state)

    assert attrs["dynamic hot regime end"]["2025"]["ml lag days"] == 17
    assert "dynamic windows" in attrs and "seasons" in attrs  # chiavi precedenti invariate
    json.dumps(attrs)


# ---------------------------------------------------------------- F
TODAY = D(2026, 10, 15)


class _FakeHass:
    def __init__(self) -> None:
        self.services = MagicMock()

        async def _call(*a: Any, **k: Any) -> Any:
            raise RuntimeError("nessun forecast")

        self.services.async_call = _call

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)


def test_detector_espone_la_fine_hot_e_non_supera_today() -> None:
    history = {TODAY - timedelta(days=i): _climate_row(TODAY - timedelta(days=i)) for i in range(330)}
    det = SeasonDetector(_FakeHass(), SeasonDetectorConfig(forecast_provider="weather.test"))  # type: ignore[arg-type]

    state = asyncio.run(det.async_detect(history, TODAY))

    assert state is not None
    ends = state.weather.hot_regime_ends
    assert ends is not None and 2026 in ends
    assert all(end <= TODAY for end in ends.values())
