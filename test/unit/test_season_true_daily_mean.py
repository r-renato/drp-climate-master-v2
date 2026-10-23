"""Test unitari per la temperatura media giornaliera vera (patch #4).

Problema risolto
----------------
``native_temperature`` del provider è la Tmax del giorno, ma veniva usata come
temperatura *media*: t_smooth (28 gg) risultava sovrastimata di ~3-5 °C, il regime
restava "hot" da maggio a ottobre e il gating ``regime_hot`` bloccava il preriscaldo.
Ora ``t_mean = (Tmax + Tmin) / 2``; un giorno senza Tmin è escluso.

Copertura
---------
A. ``t_mean`` = (Tmax+Tmin)/2; ``t_low`` invariata.
B. ``t_smooth`` = media mobile delle medie vere.
C. Giorno senza Tmin: escluso (flag on), usa la Tmax (flag off, rollback).
D. Flag off = comportamento precedente, identico.
E. Regime a metà ottobre su clima normale di Roma: "hot" (vecchio) → "mild" (nuovo).
F. Fit completo con media vera: l'inferenza resta coerente col calendario.
G. Giorni falliti o senza Tmax restano esclusi (nessuna regressione).
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

# Escursione diurna tipica (Tmax - Tmin) in un clima mediterraneo, °C.
DIURNAL_RANGE_C = 8.0
HALF_RANGE_C = DIURNAL_RANGE_C / 2.0
ROLLING_DAYS = 28


def _climate_mean(d: date, *, autumn_delta_c: float = 0.0) -> float:
    """Temperatura media giornaliera sintetica (Roma), deterministica."""
    doy = d.timetuple().tm_yday
    phase = math.cos(2.0 * math.pi * (doy - 206) / 365.0)
    return 16.0 + 9.5 * phase + autumn_delta_c


def _row(d: date, mean_c: float, *, with_tmin: bool = True) -> dict[str, object]:
    return {
        "datetime": d.isoformat(),
        "native_temperature": mean_c + HALF_RANGE_C,  # Tmax
        "native_templow": (mean_c - HALF_RANGE_C) if with_tmin else None,
        "native_dew_point": mean_c - 7.0,
        "native_wind_speed": 8.0,
        "cloud_coverage": 0.3,
    }


def _daily(end: date, days: int = 90, **kw) -> dict[date, dict[str, object]]:
    return {
        end - timedelta(days=i): _row(end - timedelta(days=i), _climate_mean(end - timedelta(days=i)), **kw)
        for i in range(days)
    }


def _model(**cfg) -> MeteoContiguousSeasonModel:
    return MeteoContiguousSeasonModel(MeteoSeasonConfig(**cfg))


# ---------------------------------------------------------------- A
def test_t_mean_e_la_media_di_tmax_e_tmin() -> None:
    end = date(2026, 10, 15)
    daily = {end: _row(end, 19.5)}  # Tmax 23.5, Tmin 15.5

    sig = _model()._compute_signals(daily)[end]

    assert sig.t_mean == pytest.approx(19.5)
    assert sig.t_low == pytest.approx(15.5)


# ---------------------------------------------------------------- B
def test_t_smooth_e_media_mobile_delle_medie_vere() -> None:
    end = date(2026, 10, 15)
    daily = _daily(end)

    sig = _model()._compute_signals(daily)[end]

    expected = sum(_climate_mean(end - timedelta(days=i)) for i in range(ROLLING_DAYS)) / ROLLING_DAYS
    assert sig.t_smooth == pytest.approx(expected)
    legacy = _model(use_true_daily_mean=False)._compute_signals(daily)[end]
    assert legacy.t_smooth - sig.t_smooth == pytest.approx(HALF_RANGE_C)


# ---------------------------------------------------------------- C
def test_giorno_senza_tmin_escluso_con_media_vera_e_tmax_in_rollback() -> None:
    end = date(2026, 10, 15)
    daily = _daily(end)
    daily[end] = _row(end, 19.5, with_tmin=False)

    on = _model()._compute_signals(daily)
    off = _model(use_true_daily_mean=False)._compute_signals(daily)

    assert end not in on, "senza Tmin non esiste la media vera"
    assert end - timedelta(days=1) in on
    assert off[end].t_mean == pytest.approx(19.5 + HALF_RANGE_C)  # legacy: Tmax
    assert off[end].t_low is None


def test_pochi_giorni_senza_tmin_non_azzerano_t_smooth() -> None:
    end = date(2026, 10, 15)
    daily = _daily(end)
    for i in (3, 9, 15):  # 3 giorni su 28 senza Tmin: sotto il 15% tollerato
        d = end - timedelta(days=i)
        daily[d] = _row(d, _climate_mean(d), with_tmin=False)

    sig = _model()._compute_signals(daily)[end]

    assert sig.t_smooth is not None


# ---------------------------------------------------------------- C2
def test_t_max_e_la_massima_del_provider_ed_e_indipendente_da_t_mean() -> None:
    end = date(2026, 10, 15)
    daily = {end: _row(end, 19.5)}  # Tmax 23.5, Tmin 15.5

    sig = _model()._compute_signals(daily)[end]

    assert sig.t_max == pytest.approx(19.5 + HALF_RANGE_C)
    assert sig.t_mean == pytest.approx(19.5)  # la media non è la massima
    assert sig.t_max >= sig.t_mean >= sig.t_low


def test_t_max_disponibile_anche_con_flag_legacy() -> None:
    end = date(2026, 10, 15)
    daily = {end: _row(end, 19.5)}

    sig = _model(use_true_daily_mean=False)._compute_signals(daily)[end]

    assert sig.t_max == pytest.approx(19.5 + HALF_RANGE_C)


# ---------------------------------------------------------------- D
def test_flag_disattivato_e_il_comportamento_precedente() -> None:
    end = date(2026, 10, 15)
    daily = _daily(end)

    sig = _model(use_true_daily_mean=False)._compute_signals(daily)[end]

    assert sig.t_mean == pytest.approx(_climate_mean(end) + HALF_RANGE_C)  # = Tmax
    expected = sum(_climate_mean(end - timedelta(days=i)) + HALF_RANGE_C for i in range(ROLLING_DAYS)) / ROLLING_DAYS
    assert sig.t_smooth == pytest.approx(expected)


# ---------------------------------------------------------------- E
@pytest.mark.parametrize(
    ("end", "legacy_regime", "new_regime"),
    [
        (date(2026, 10, 15), "hot", "mild"),   # caso reale: preriscaldo bloccato da regime_hot
        (date(2026, 7, 20), "hot", "hot"),     # estate: invariato
        (date(2026, 4, 15), "mild", "mild"),   # primavera: invariato
    ],
)
def test_regime_con_clima_normale_di_roma(end: date, legacy_regime: str, new_regime: str) -> None:
    daily = _daily(end, days=60)
    # clima di Roma con escursione maggiore in ottobre/estate: Tmax = media + 5, Tmin = media - 5
    for d in daily:
        daily[d] = {**daily[d], "native_temperature": _climate_mean(d) + 5.0, "native_templow": _climate_mean(d) - 5.0}

    old = _model(use_true_daily_mean=False)
    new = _model()

    assert old._regime_hint(old._compute_signals(daily)[end]) == legacy_regime
    assert new._regime_hint(new._compute_signals(daily)[end]) == new_regime


# ---------------------------------------------------------------- F
def test_fit_completo_con_media_vera_resta_coerente_col_calendario() -> None:
    end = date(2026, 10, 21)
    history = {k.isoformat(): v for k, v in _daily(end, days=420).items()}

    model = MeteoContiguousSeasonModel().fit(history=history)
    info = model.infer(date(2026, 10, 15), expected_season=Seasons.AUTUMN)

    assert info is not None
    assert info.season == Seasons.AUTUMN
    assert info.weather_day_signals.t_smooth is not None


# ---------------------------------------------------------------- G
def test_giorni_falliti_o_senza_tmax_restano_esclusi() -> None:
    end = date(2026, 10, 15)
    daily = _daily(end)
    daily[end] = {**_row(end, 19.5), "failed": True}
    d2 = end - timedelta(days=1)
    daily[d2] = {**_row(d2, 19.0), "native_temperature": None}

    out = _model()._compute_signals(daily)

    assert end not in out and d2 not in out
    assert end - timedelta(days=2) in out
