"""Test unitari del livello "cool" del regime dinamico (patch #17, SHADOW).

Problema risolto
----------------
Il regime a tre livelli (cold ≤ 8, mild, hot ≥ 22) lasciava in "mild" tutta la
stagione di riscaldamento (8-14 °C): ~150 giorni/anno a Roma (zona D, 166 gg legali).
Il livello "cool" (RMOT ≤ 14 °C, esce sopra 15 °C) separa l'inverno dalla vera mezza
stagione. È solo diagnostico: ``legacy_regime`` (che guida l'impianto) non lo produce.

Copertura
---------
A. Soglie con isteresi e conferma: ingresso a 14, uscita sopra 15, nessuna oscillazione.
B. Scala completa: mild → cool → cold → cool → mild, e salto diretto hot ↔ cold.
C. Primo giorno: classificazione diretta a quattro livelli.
D. Degradazione: RMOT None → regime None, stato invariato.
E. Impianto invariato: ``legacy_regime`` non produce mai "cool"; confronto cool≡mild.
F. Periodi: cool compare nei periodi e i giorni totali tornano.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.drp_climate_master_v2.helpers.season import thermal_regime as tr

D0 = date(2026, 11, 1)


def _regimes(values: list[float | None], confirm_days: int = 2) -> list[str | None]:
    rmot = {D0 + timedelta(days=i): v for i, v in enumerate(values)}
    out = tr.classify_regimes(rmot, confirm_days=confirm_days)
    return [out[d] for d in sorted(out)]


# ---------------------------------------------------------------- A
def test_soglie_cool_costanti_con_isteresi() -> None:
    assert tr.REGIME_COOL_ENTER_MAX_C == 14.0
    assert tr.REGIME_COOL_EXIT_MIN_C == tr.REGIME_COOL_ENTER_MAX_C + tr.REGIME_HYSTERESIS_C == 15.0
    assert tr.REGIME_COLD_ENTER_MAX_C < tr.REGIME_COOL_ENTER_MAX_C < tr.REGIME_HOT_ENTER_MIN_C


def test_entra_in_cool_a_14_dopo_la_conferma() -> None:
    values = [16.0, 14.0, 14.0, 14.0]

    assert _regimes(values) == ["mild", "mild", "cool", "cool"]


def test_esce_da_cool_solo_sopra_15() -> None:
    regimes = _regimes([13.0, 14.9, 15.0, 15.0, 15.1, 15.1])

    assert regimes[:4] == ["cool"] * 4  # 15.0 è ancora dentro la banda di isteresi
    assert regimes[-1] == "mild"  # 15.1 per due giorni consecutivi conferma l'uscita


def test_nessuna_oscillazione_intorno_a_14_e_15() -> None:
    values = [14.4, 14.6, 14.4, 14.6, 14.4, 14.6, 14.4]
    regimes = _regimes(values)

    assert len(set(regimes[1:])) == 1, f"regime instabile: {regimes}"


def test_un_giorno_anomalo_non_cambia_regime() -> None:
    assert _regimes([18.0, 18.0, 12.0, 18.0, 18.0]) == ["mild"] * 5


# ---------------------------------------------------------------- B
def test_discesa_e_risalita_attraversano_cool() -> None:
    down = _regimes([20.0] * 3 + [12.0] * 3 + [7.0] * 3 + [11.0] * 3 + [17.0] * 3)

    assert [r for i, r in enumerate(down) if i == 0 or r != down[i - 1]] == ["mild", "cool", "cold", "cool", "mild"]


def test_cold_esce_a_9_verso_cool_non_verso_mild() -> None:
    regimes = _regimes([7.0, 7.0, 9.5, 9.5, 9.5])

    assert regimes[-1] == "cool"
    assert "mild" not in regimes


def test_salto_diretto_hot_a_cold_e_cold_a_hot() -> None:
    assert _regimes([23.0, 23.0, 6.0, 6.0])[-1] == "cold"
    assert _regimes([6.0, 6.0, 24.0, 24.0])[-1] == "hot"


def test_hot_scende_a_mild_non_a_cool_a_20() -> None:
    assert _regimes([23.0, 23.0, 20.0, 20.0])[-1] == "mild"


# ---------------------------------------------------------------- C
@pytest.mark.parametrize(
    "value, expected",
    [(5.0, "cold"), (8.0, "cold"), (8.1, "cool"), (14.0, "cool"), (14.1, "mild"), (21.9, "mild"), (22.0, "hot")],
)
def test_primo_giorno_classificazione_diretta(value: float, expected: str) -> None:
    assert _regimes([value])[0] == expected


# ---------------------------------------------------------------- D
def test_rmot_none_regime_none_e_stato_invariato() -> None:
    regimes = _regimes([12.0, 12.0, None, 12.0])

    assert regimes == ["cool", "cool", None, "cool"]


# ---------------------------------------------------------------- E
@pytest.mark.parametrize("value", [8.1, 10.0, 14.0, 15.0, 21.9])
def test_legacy_regime_non_produce_mai_cool(value: float) -> None:
    assert tr.legacy_regime(value) == "mild"


def test_legacy_regime_invariato_sulle_soglie_storiche() -> None:
    assert [tr.legacy_regime(v) for v in (8.0, 22.0, None)] == ["cold", "hot", "mild"]


def test_plant_regime_of_cool_equivale_a_mild() -> None:
    assert [tr.plant_regime_of(r) for r in ("cold", "cool", "mild", "hot")] == ["cold", "mild", "mild", "hot"]


# ---------------------------------------------------------------- F
def test_periodi_includono_cool_e_i_giorni_tornano() -> None:
    series = {D0 + timedelta(days=i): r for i, r in enumerate(["mild"] * 3 + ["cool"] * 5 + ["cold"] * 4 + ["cool"] * 2)}

    periods = tr.regime_periods(series)

    assert [p.regime for p in periods] == ["mild", "cool", "cold", "cool"]
    assert sum(p.days for p in periods) == len(series)
