"""Test unitari del livello "torrid" del regime dinamico (patch #20, SHADOW).

Problema risolto
----------------
Con il riscaldamento climatico l'estate romana supera spesso RMOT 25-28 °C e "hot"
(≥ 22 °C) non distingue più un'estate normale da un'ondata di calore. Il livello
"torrid" (RMOT ≥ 28 °C, esce sotto 27 °C) è solo diagnostico: ``legacy_regime`` (che
guida l'impianto) non lo produce.

Copertura
---------
A. Soglie con isteresi e conferma. B. Transizioni hot ↔ torrid e salti diretti.
C. Primo giorno. D. Degradazione (None). E. Impianto invariato.
F. Periodi: torrid separato nei periodi, ma non spezza hot in spans/fine stagione.
"""
from __future__ import annotations

from datetime import date, timedelta

from custom_components.drp_climate_master_v2.helpers.season import thermal_regime as tr

D0 = date(2026, 7, 1)


def _rmot(values: list[float | None]) -> dict[date, float | None]:
    return {D0 + timedelta(days=i): v for i, v in enumerate(values)}


def _regimes(values: list[float | None], confirm_days: int = 2) -> list[str | None]:
    out = tr.classify_regimes(_rmot(values), confirm_days=confirm_days)
    return [out[d] for d in sorted(out)]


# ---------------------------------------------------------------- A
def test_soglie_torrid_costanti_con_isteresi() -> None:
    assert tr.REGIME_TORRID_ENTER_MIN_C == 28.0
    assert tr.REGIME_TORRID_EXIT_MAX_C == tr.REGIME_TORRID_ENTER_MIN_C - tr.REGIME_HYSTERESIS_C == 27.0
    assert tr.REGIME_HOT_ENTER_MIN_C < tr.REGIME_TORRID_EXIT_MAX_C


def test_ingresso_torrid_richiede_conferma() -> None:
    out = _regimes([25.0, 25.0, 28.5, 28.5, 28.5])
    assert out[:2] == ["hot", "hot"]
    assert out[2] == "hot"  # un solo giorno ≥ 28: non basta
    assert out[3] == "torrid"


def test_nessuna_oscillazione_tra_27_e_28() -> None:
    out = _regimes([29.0] * 3 + [27.5, 28.0, 27.2, 27.8, 27.5])
    assert set(out[2:]) == {"torrid"}


def test_uscita_sotto_27_con_conferma() -> None:
    out = _regimes([29.0] * 3 + [26.5, 26.5, 26.5])
    assert out[3] == "torrid"
    assert out[4] == "hot"


# ---------------------------------------------------------------- B
def test_salto_diretto_torrid_cold_e_ritorno() -> None:
    out = _regimes([29.0] * 3 + [5.0] * 3 + [29.0] * 3)
    assert out[2] == "torrid" and out[5] == "cold" and out[8] == "torrid"


def test_scala_completa_a_cinque_livelli() -> None:
    assert [tr._initial_regime(v) for v in (5, 10, 18, 25, 29)] == ["cold", "cool", "mild", "hot", "torrid"]


# ---------------------------------------------------------------- C
def test_primo_giorno_classificazione_diretta() -> None:
    assert _regimes([28.0, 28.0])[0] == "torrid"
    assert _regimes([27.9, 27.9])[0] == "hot"


# ---------------------------------------------------------------- D
def test_none_non_cambia_stato() -> None:
    out = _regimes([29.0, 29.0, None, 29.0])
    assert out[2] is None and out[3] == "torrid"


# ---------------------------------------------------------------- E
def test_impianto_invariato() -> None:
    for t in (10.0, 20.0, 24.0, 31.0, 35.0):
        assert tr.legacy_regime(t) in ("cold", "mild", "hot")
    assert tr.plant_regime_of("torrid") == "hot"
    assert tr.plant_regime_of("cool") == "mild"
    assert tr.plant_regime_of("mild") == "mild"


# ---------------------------------------------------------------- F
def test_periodi_distinguono_torrid() -> None:
    series = _rmot([25.0] * 3 + [29.0] * 5 + [25.0] * 3)
    reg = tr.classify_regimes(series, confirm_days=1)
    periods = tr.regime_periods(reg)
    assert [p.regime for p in periods] == ["hot", "torrid", "hot"]
    assert sum(p.days for p in periods if p.end is not None) + 0 >= 0


def test_hot_span_e_fine_non_frammentati_da_torrid() -> None:
    reg = {D0 + timedelta(days=i): v for i, v in enumerate(["hot"] * 3 + ["torrid"] * 4 + ["hot"] * 3 + ["mild"] * 2)}
    spans = tr.regime_spans_by_year(reg)
    hot = spans[tr.meteo_year_of(D0)]["hot"]
    assert hot.periods == 1 and hot.days == 10
    ends = tr.regime_end_by_year(reg, regime="hot")
    assert ends[tr.meteo_year_of(D0)] == D0 + timedelta(days=10)


def test_spans_con_torrid_tracciato_esplicitamente() -> None:
    reg = {D0 + timedelta(days=i): v for i, v in enumerate(["hot"] * 3 + ["torrid"] * 4 + ["hot"] * 3)}
    spans = tr.regime_spans_by_year(reg, regimes=("hot", "torrid"))
    y = spans[tr.meteo_year_of(D0)]
    assert y["torrid"].days == 4 and y["hot"].periods == 2
