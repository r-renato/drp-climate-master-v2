"""Test unitari per la guardia "anno meteo completo" di MeteoContiguousSeasonModel (patch #3).

Problema risolto
----------------
Un anno meteo (Dic Y-1 → Nov Y) con >= ``min_days_per_year`` giorni ma ancora in
corso (da ~20 settembre a fine novembre) veniva segmentato in 4 segmenti forzati:
l'ultimo, allungato fino al bordo del forecast e con la media termica più alta,
veniva etichettato SUMMER (es. ``seg=3 [2026-06-19..2026-10-21]`` il 15/10/2026).
Ora l'anno in corso è lasciato a ``infer()`` (prior di calendario).

Copertura
---------
A. Anno incompleto (15/10) → nessun segmento per l'anno in corso, ``infer`` = AUTUMN.
B. Anno completo (fine novembre superata) → ancora segmentato.
C. Confine di tolleranza: ultimo giorno 26/11 → infer, 27/11 → segmentato.
D. Anno concluso con buco finale (27–30/11 mancanti) → segmentato (non escluso per sempre).
E. Nessun anno completo in storico (nuova installazione) → degrada al calendario.
F. Flag ``require_complete_year=False`` → comportamento precedente (rollback).
G. Regressione: a parità di dati, flag on/off danno output identico quando l'anno
   corrente è completo oppure < ``min_days_per_year``.
H. Validazione della configurazione.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from custom_components.drp_climate_master_v2.domain.models.season import Seasons
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)

TODAY_REAL = date(2026, 10, 15)  # caso reale: anno meteo 2026 con 319 giorni
HOT_AUTUMN_DELTA_C = 3.0  # autunno 2026 più caldo della norma (come nel caso reale)


def _row(d: date, autumn_delta_c: float) -> dict[str, float | str]:
    """Riga giornaliera sintetica (clima indicativo di Roma), deterministica."""
    doy = d.timetuple().tm_yday
    phase = math.cos(2.0 * math.pi * (doy - 206) / 365.0)
    t_mean = 16.5 + 9.5 * phase + 1.5 * math.sin(doy * 1.7 + d.year * 0.3)
    if d.year == 2026 and date(2026, 9, 1) <= d <= date(2026, 10, 31):
        t_mean += autumn_delta_c
    half_range = (6.0 + 4.0 * (0.5 + 0.5 * phase)) / 2.0
    return {
        "datetime": d.isoformat(),
        "native_temperature": t_mean + half_range,  # Tmax (come PirateWeather)
        "native_templow": t_mean - half_range,
        "native_dew_point": t_mean - 7.0 + math.sin(doy * 0.9),
        "native_wind_speed": 8.0,
        "cloud_coverage": 0.3,
    }


def _history(
    end: date,
    *,
    days_back: int = 420,
    autumn_delta_c: float = HOT_AUTUMN_DELTA_C,
    skip: set[date] | None = None,
) -> dict[str, dict[str, float | str]]:
    skip = skip or set()
    out: dict[str, dict[str, float | str]] = {}
    for i in range(days_back):
        d = end - timedelta(days=i)
        if d not in skip:
            out[d.isoformat()] = _row(d, autumn_delta_c)
    return out


def _fit(
    end: date,
    *,
    cfg: MeteoSeasonConfig | None = None,
    **kw,
) -> MeteoContiguousSeasonModel:
    return MeteoContiguousSeasonModel(cfg or MeteoSeasonConfig()).fit(history=_history(end, **kw))


# ---------------------------------------------------------------- A
def test_anno_incompleto_non_segmentato_e_infer_usa_calendario() -> None:
    # storico fino a oggi + 6 giorni di forecast (ultimo giorno = 21/10, come nel log reale)
    model = _fit(date(2026, 10, 21))

    assert model.day(TODAY_REAL) is None
    # nessuna finestra segmentata dall'1/12/2025 in poi (anno meteo 2026 escluso)
    assert all(end < date(2025, 12, 1) for _, end, _ in model.windows()), model.windows()

    info = model.infer(TODAY_REAL, expected_season=Seasons.AUTUMN)
    assert info is not None
    assert info.season == Seasons.AUTUMN
    assert info.reason.startswith("infer:")


# ---------------------------------------------------------------- B
def test_anno_completo_resta_segmentato() -> None:
    model = _fit(date(2026, 12, 5))

    info = model.day(TODAY_REAL)
    assert info is not None, "anno meteo 2026 completo: deve essere segmentato"
    assert info.reason.startswith("meteo_year=2026")
    assert model.windows()[-1][1] == date(2026, 11, 30)


# ---------------------------------------------------------------- C
@pytest.mark.parametrize(
    ("last_day", "segmented"),
    [
        (date(2026, 11, 26), False),  # 30/11 - 3gg = 27/11 → 26/11 è fuori tolleranza
        (date(2026, 11, 27), True),
        (date(2026, 11, 30), True),
    ],
)
def test_confine_tolleranza_fine_anno(last_day: date, segmented: bool) -> None:
    model = _fit(last_day)
    assert (model.day(TODAY_REAL) is not None) is segmented


# ---------------------------------------------------------------- D
def test_anno_concluso_con_buco_finale_resta_segmentato() -> None:
    gap = {date(2026, 11, d) for d in (27, 28, 29, 30)}
    model = _fit(date(2026, 12, 20), skip=gap)

    assert model.day(TODAY_REAL) is not None


# ---------------------------------------------------------------- E
def test_nessun_anno_completo_degrada_al_calendario_senza_eccezioni() -> None:
    # solo ~300 giorni di storico: nessun anno segmentabile → prototipi neutri
    model = _fit(TODAY_REAL, days_back=330)

    assert model.windows() == []
    for expected in (Seasons.AUTUMN, Seasons.WINTER, Seasons.SPRING, Seasons.SUMMER):
        info = model.infer(TODAY_REAL, expected_season=expected)
        assert info is not None
        assert info.season == expected, "senza prototipi decide il prior di calendario"


# ---------------------------------------------------------------- F
def test_flag_disattivato_ripristina_comportamento_precedente() -> None:
    cfg = MeteoSeasonConfig(require_complete_year=False)
    model = _fit(date(2026, 10, 21), cfg=cfg)

    info = model.day(TODAY_REAL)
    assert info is not None, "con il flag off l'anno incompleto torna segmentato"
    assert info.reason.startswith("meteo_year=2026")


# ---------------------------------------------------------------- G
@pytest.mark.parametrize(
    ("end", "days_back"),
    [
        (date(2026, 6, 30), 730),  # 2025 completo segmentato; 2026 corto (211 gg < min_days_per_year)
        (date(2026, 12, 5), 500),  # 2026 completo segmentato
    ],
)
def test_regressione_output_identico_se_anno_corrente_completo_o_corto(end: date, days_back: int) -> None:
    on = _fit(end, cfg=MeteoSeasonConfig(require_complete_year=True), days_back=days_back)
    off = _fit(end, cfg=MeteoSeasonConfig(require_complete_year=False), days_back=days_back)

    assert on.all(), "il confronto deve riguardare almeno un anno segmentato"
    assert {d: w.season for d, w in on.all().items()} == {d: w.season for d, w in off.all().items()}
    assert on.windows() == off.windows()


# ---------------------------------------------------------------- H
def test_validazione_tolleranza_negativa() -> None:
    with pytest.raises(ValueError):
        MeteoSeasonConfig(complete_year_end_tolerance_days=-1)
