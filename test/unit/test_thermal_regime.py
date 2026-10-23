"""Test unitari per ``thermal_regime`` (RMOT + isteresi + conferma, patch #5 fase 1).

Problema risolto
----------------
Il regime (cold/mild/hot) derivava da ``t_smooth`` (media piatta a 28 giorni):
dopo una fine settembre calda a metà ottobre restava "hot" e bloccava il
preriscaldo (``regime_hot``). La RMOT della EN 16798-1 (media esponenziale,
α = 0.8, memoria ~5 giorni) segue il tempo recente; isteresi e conferma su N
giorni evitano oscillazioni. In questa fase è solo SHADOW: l'impianto continua a
usare il regime storico.

Copertura
---------
A. RMOT: serie costante, gradino (convergenza ~5 giorni), pesi esatti.
B. RMOT: buchi (rinormalizzazione), copertura insufficiente → None.
C. RMOT causale: non dipende dal giorno stesso né dai successivi.
D. Regime: ingresso/uscita con isteresi (nessuna oscillazione a ±0.1 °C dalla soglia).
E. Conferma su N giorni: un solo giorno anomalo non cambia regime.
F. RMOT None → regime None, stato invariato; primo giorno classificato direttamente.
G. Deterministico e causale: troncare la serie non cambia i giorni precedenti.
H. Regime storico (``legacy_regime``) identico alle vecchie soglie 8/22.
I. Integrazione col modello: dopo una fine settembre calda il regime storico resta "hot"; il dinamico segue il tempo recente (con la latenza della conferma).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from custom_components.drp_climate_master_v2.helpers.season import thermal_regime as tr
from custom_components.drp_climate_master_v2.helpers.season.season_weather_forecast import (
    MeteoContiguousSeasonModel,
    MeteoSeasonConfig,
)

D0 = date(2026, 9, 1)


def _series(values: list[float], start: date = D0) -> dict[date, float]:
    return {start + timedelta(days=i): v for i, v in enumerate(values)}


# ---------------------------------------------------------------- A
def test_rmot_serie_costante_vale_la_costante() -> None:
    series = _series([18.0] * 40)

    assert tr.compute_rmot(series, D0 + timedelta(days=39)) == pytest.approx(18.0)


def test_rmot_pesi_esatti_ieri_e_il_giorno_prima() -> None:
    day = D0 + timedelta(days=3)
    series = {day - timedelta(days=1): 10.0, day - timedelta(days=2): 20.0}
    # solo k=1 e k=2 disponibili: w1=0.2, w2=0.16 → coperture 0.36/1 < 0.7
    assert tr.compute_rmot(series, day) is None
    # con copertura minima abbassata il valore è la media pesata rinormalizzata
    rmot = tr.compute_rmot(series, day, min_weight_coverage=0.3)
    assert rmot == pytest.approx((0.2 * 10.0 + 0.16 * 20.0) / (0.2 + 0.16))


def test_rmot_gradino_converge_in_circa_cinque_giorni() -> None:
    # 40 giorni a 26 °C poi 20 a 14 °C: la RMOT scende seguendo e^(-t/5)
    series = _series([26.0] * 40 + [14.0] * 20)
    step_day = D0 + timedelta(days=40)
    gap = 26.0 - 14.0

    after_5 = tr.compute_rmot(series, step_day + timedelta(days=5))
    after_15 = tr.compute_rmot(series, step_day + timedelta(days=15))

    assert after_5 is not None and after_15 is not None
    assert 26.0 - after_5 == pytest.approx(gap * (1 - 0.8**5), abs=0.15)
    assert after_15 < 14.0 + 0.05 * gap, "dopo 15 giorni il 95% del salto è assorbito"
    # la media piatta a 28 giorni a +15 giorni non ha ancora finito
    flat28 = sum(series[step_day + timedelta(days=15) - timedelta(days=i)] for i in range(28)) / 28
    assert flat28 > after_15 + 3.0


# ---------------------------------------------------------------- B
def test_rmot_con_un_buco_rinormalizza() -> None:
    series = _series([20.0] * 40)
    day = D0 + timedelta(days=39)
    del series[day - timedelta(days=1)]  # manca ieri (20% del peso)

    assert tr.compute_rmot(series, day) == pytest.approx(20.0)


def test_rmot_con_copertura_insufficiente_e_none() -> None:
    series = _series([20.0] * 40)
    day = D0 + timedelta(days=39)
    for k in (1, 2, 3):  # mancano ieri, ieri-1, ieri-2: 0.2+0.16+0.128 = 49% del peso
        del series[day - timedelta(days=k)]

    assert tr.compute_rmot(series, day) is None


# ---------------------------------------------------------------- C
def test_rmot_e_causale_non_usa_il_giorno_stesso_ne_i_successivi() -> None:
    series = _series([20.0] * 40)
    day = D0 + timedelta(days=30)
    base = tr.compute_rmot(series, day)
    series[day] = 99.0
    series[day + timedelta(days=1)] = -50.0

    assert tr.compute_rmot(series, day) == base


# ---------------------------------------------------------------- D
def _regimes(values: list[float | None], confirm_days: int = 1) -> list[str | None]:
    series = {D0 + timedelta(days=i): v for i, v in enumerate(values)}
    out = tr.classify_regimes(series, confirm_days=confirm_days)
    return [out[d] for d in sorted(out)]


def test_isteresi_hot_entra_a_22_esce_sotto_21() -> None:
    values = [20.0, 22.0, 21.5, 21.1, 21.0, 20.9, 22.0]

    assert _regimes(values) == ["mild", "hot", "hot", "hot", "hot", "mild", "hot"]


def test_isteresi_cold_entra_a_8_esce_sopra_9() -> None:
    values = [10.0, 8.0, 8.5, 9.0, 9.1, 8.0]

    assert _regimes(values) == ["cool", "cold", "cold", "cold", "cool", "cold"]


def test_nessuna_oscillazione_a_piu_o_meno_un_decimo_dalla_soglia() -> None:
    values = [21.9, 22.1, 21.9, 22.1, 21.9, 22.1, 21.9]
    regimes = _regimes(values)

    assert len({r for r in regimes[1:]}) == 1, f"regime instabile: {regimes}"


# ---------------------------------------------------------------- E
def test_conferma_un_giorno_anomalo_non_cambia_regime() -> None:
    values = [18.0, 18.0, 23.0, 18.0, 18.0]

    assert _regimes(values, confirm_days=2) == ["mild"] * 5


def test_conferma_due_giorni_consecutivi_cambiano_regime() -> None:
    values = [18.0, 18.0, 23.0, 23.0, 23.0]

    assert _regimes(values, confirm_days=2) == ["mild", "mild", "mild", "hot", "hot"]


def test_conferma_si_azzera_se_la_condizione_si_interrompe() -> None:
    values = [18.0, 23.0, 18.0, 23.0, 18.0]  # mai due giorni di fila

    assert _regimes(values, confirm_days=2) == ["mild"] * 5


def test_default_conferma_e_due_giorni() -> None:
    assert tr.REGIME_CONFIRM_DAYS == 2


# ---------------------------------------------------------------- F
def test_rmot_none_regime_none_e_stato_invariato() -> None:
    values = [23.0, None, None, 23.0]

    assert _regimes(values) == ["hot", None, None, "hot"]


def test_primo_giorno_classificato_direttamente() -> None:
    assert _regimes([5.0]) == ["cold"]
    assert _regimes([15.0]) == ["mild"]
    assert _regimes([25.0]) == ["hot"]


# ---------------------------------------------------------------- G
def test_causale_troncare_la_serie_non_cambia_i_giorni_precedenti() -> None:
    values = [10.0, 12.0, 23.0, 24.0, 18.0, 7.0, 6.0, 12.0, 25.0, 25.0]
    full = _regimes(values, confirm_days=2)
    for cut in range(1, len(values)):
        assert _regimes(values[:cut], confirm_days=2) == full[:cut]


# ---------------------------------------------------------------- H
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "mild"), (-5.0, "cold"), (8.0, "cold"), (8.01, "mild"), (21.99, "mild"), (22.0, "hot"), (30.0, "hot")],
)
def test_regime_storico_invariato(value: float | None, expected: str) -> None:
    assert tr.legacy_regime(value) == expected


def test_costanti_coerenti() -> None:
    assert tr.REGIME_COLD_EXIT_MIN_C == tr.REGIME_COLD_ENTER_MAX_C + tr.REGIME_HYSTERESIS_C
    assert tr.REGIME_HOT_EXIT_MAX_C == tr.REGIME_HOT_ENTER_MIN_C - tr.REGIME_HYSTERESIS_C
    assert 0.0 < tr.RMOT_ALPHA < 1.0


# ---------------------------------------------------------------- I
def _row(d: date, mean_c: float) -> dict[str, object]:
    return {
        "datetime": d.isoformat(),
        "native_temperature": mean_c + 4.0,
        "native_templow": mean_c - 4.0,
        "native_dew_point": mean_c - 7.0,
        "native_wind_speed": 8.0,
        "cloud_coverage": 0.3,
    }


def _october_after_hot_september(cool_days: int) -> dict[date, dict[str, object]]:
    """Fine settembre molto calda (media 26 °C), poi ``cool_days`` giorni a 19 °C, fino al 15/10."""
    end = date(2026, 10, 15)
    daily = {}
    for i in range(120):
        d = end - timedelta(days=i)
        mean = 19.0 if i < cool_days else (26.0 if i < 35 else 16.0 + 6.0 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 206) / 365.0))
        daily[d] = _row(d, mean)
    return daily


@pytest.mark.parametrize(
    ("cool_days", "expected_dynamic"),
    [
        (10, "mild"),  # 10 giorni a 19 °C: RMOT < 21 da più di 2 giorni → regime uscito da "hot"
        (7, "hot"),    # RMOT scende sotto 21 solo ieri-oggi: la conferma (2 giorni) trattiene "hot"
    ],
)
def test_integrazione_ottobre_storico_hot_dinamico_segue_il_tempo_recente(
    cool_days: int, expected_dynamic: str
) -> None:
    model = MeteoContiguousSeasonModel(MeteoSeasonConfig(use_true_daily_mean=True))
    sig = model._compute_signals(_october_after_hot_september(cool_days))[date(2026, 10, 15)]

    assert sig.t_smooth is not None and sig.t_smooth >= 22.0
    assert model._regime_hint(sig) == "hot", "il regime che guida l'impianto resta quello storico"
    assert sig.t_rmot is not None and sig.t_rmot < 21.0
    assert sig.regime_dynamic == expected_dynamic


def test_integrazione_giorni_senza_storia_sufficiente_hanno_rmot_none() -> None:
    model = MeteoContiguousSeasonModel()
    first = date(2026, 10, 1)
    daily = {first + timedelta(days=i): _row(first + timedelta(days=i), 18.0) for i in range(3)}

    out = model._compute_signals(daily)

    assert all(s.t_rmot is None and s.regime_dynamic is None for s in out.values())
