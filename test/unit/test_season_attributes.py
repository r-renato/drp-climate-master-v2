"""Test unitari per ``season_attributes`` (diagnostica stagionale, patch #1).

Problema risolto
----------------
Il sensore stagione mostrava ``weather.season`` (etichetta ML, es. "summer")
accanto alla finestra di *calendario* (1/9–30/11 = autunno) senza dichiarare che
sono due sorgenti diverse. La decisione usa il calendario (→ shoulder).

Copertura
---------
A. Scenario reale 15/10/2026: calendario=autumn, meteo=summer, mismatch=True,
   operativa=shoulder, giorno 45/91 (1-based).
B. Compatibilità: ``weather signals`` resta dict di liste a un elemento.
C. Fallback: segnali None → nessuna eccezione, "-" nelle liste legacy, None nei flat.
D. Bug cloud=0.0 (cielo sereno) non deve diventare "-".
E. Mappatura operativa winter/summer/shoulder dal calendario.
F. Riga di log: una sola riga, compatta, con i campi di commissioning.
G. Firma log: stabile al variare della data in ``fallback_last_known(<data>)``.
H. Stagione astronomica: chiavi, conteggio giorni, mismatch e riga di log.
I. Vista affiancata delle tre stagioni (astronomica / meteorologica / dinamica).
J. Regime dinamico (RMOT) in shadow: attributi e riga di log.
K. Temperature giornaliere: niente rumore in virgola mobile e ``t_max`` esposta.
"""
from __future__ import annotations

from datetime import date

import pytest

from custom_components.drp_climate_master_v2.domain.models.season import (
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.helpers.season.season_attributes import (
    LOG_REASON_MAX_CHARS,
    build_season_attributes,
    format_season_log_line,
    operative_season_of,
    season_log_signature,
)


def _state(
    *,
    calendar: Seasons = Seasons.AUTUMN,
    weather: Seasons = Seasons.SUMMER,
    window: tuple[date, date] = (date(2026, 9, 1), date(2026, 11, 30)),
    as_of: date = date(2026, 10, 15),
    detect_model: str = "segmented",
    reason: str = "meteo_year=2026 seg=3",
    signals: WeatherDaySignals | None = None,
) -> SeasonState:
    sig = signals or WeatherDaySignals(
        d=as_of,
        t_mean=23.5,
        t_low=15.1,
        dew=12.9,
        wind=6.26,
        cloud=0.13,
        t_smooth=26.85,
        dew_smooth=13.58,
        trend=-7.71,
    )
    return SeasonState(
        window=SeasonWindow(calendar, window[0], window[1]),
        weather=WeatherSeason(
            season=weather,
            anomaly=False,
            anomaly_score=1.14,
            reason=reason,
            regime_hint="hot",
            weather_day_signals=sig,
        ),
        as_of=as_of,
        detect_model=detect_model,
    )


# ---------------------------------------------------------------- A
def test_scenario_reale_calendario_autunno_meteo_summer() -> None:
    attrs = build_season_attributes(_state())

    assert attrs["calendar season"] == "autumn"
    assert attrs["weather season"] == "summer"
    assert attrs["operative season"] == "shoulder"
    assert attrs["season mismatch"] is True
    assert attrs["calendar window"] == "2026-09-01 - 2026-11-30"
    assert attrs["calendar days"] == "91"
    assert attrs["calendar days passed"] == "44"
    assert attrs["calendar days remaining"] == "46"
    assert attrs["calendar day"] == 45  # 1-based: 15/10 è il 45° giorno
    assert attrs["weather regime"] == "hot"
    assert attrs["weather reason"]["detect_model"] == "segmented"
    assert attrs["weather reason"]["reason"] == "meteo_year=2026 seg=3"
    assert attrs["weather signals eng."] == {"smooth": 26.85, "dewp": 13.58, "trend": -7.71, "rmot": None}


def test_nessun_mismatch_se_stagioni_coincidono() -> None:
    attrs = build_season_attributes(_state(weather=Seasons.AUTUMN))
    assert attrs["season mismatch"] is False


# ---------------------------------------------------------------- B
def test_weather_signals_legacy_liste_a_un_elemento() -> None:
    sig = build_season_attributes(_state())["weather signals"]

    assert sig == {
        "t_low": [15.1],
        "t_mean": [23.5],
        "t_max": [None],  # nuova chiave: None se il provider non la fornisce
        "dewp": [12.9],
        "wind": [6.26],
        "cloud": ["13.0"],
    }
    flat = build_season_attributes(_state())["weather signals flat"]
    assert flat["t_low"] == 15.1 and flat["cloud_pct"] == 13.0


# ---------------------------------------------------------------- C
def test_fallback_segnali_assenti_senza_eccezioni() -> None:
    empty = WeatherDaySignals(
        d=date(2026, 10, 15),
        t_mean=20.0,
        t_low=None,
        dew=None,
        wind=None,
        cloud=None,
        t_smooth=None,
        dew_smooth=None,
        trend=None,
    )
    state = _state(signals=empty)
    attrs = build_season_attributes(state)

    assert attrs["weather signals"]["t_low"] == [None]
    assert attrs["weather signals"]["cloud"] == ["-"]
    assert attrs["weather signals flat"]["cloud_pct"] is None
    assert attrs["weather signals eng."] == {"smooth": None, "dewp": None, "trend": None, "rmot": None}
    line = format_season_log_line(state)
    assert "t_smooth=- trend=-" in line


# ---------------------------------------------------------------- D
def test_cloud_zero_non_diventa_trattino() -> None:
    clear = WeatherDaySignals(
        d=date(2026, 10, 15), t_mean=20.0, t_low=10.0, dew=5.0, wind=1.0,
        cloud=0.0, t_smooth=18.0, dew_smooth=6.0, trend=-1.0,
    )
    attrs = build_season_attributes(_state(signals=clear))
    assert attrs["weather signals"]["cloud"] == ["0.0"]
    assert attrs["weather signals flat"]["cloud_pct"] == 0.0


# ---------------------------------------------------------------- E
@pytest.mark.parametrize(
    ("calendar", "expected"),
    [
        (Seasons.WINTER, "winter"),
        (Seasons.SUMMER, "summer"),
        (Seasons.SPRING, "shoulder"),
        (Seasons.AUTUMN, "shoulder"),
    ],
)
def test_mappatura_operativa_da_calendario(calendar: Seasons, expected: str) -> None:
    assert operative_season_of(_state(calendar=calendar)).value == expected


# ---------------------------------------------------------------- F
def test_riga_di_log_compatta_con_campi_commissioning() -> None:
    line = format_season_log_line(_state())

    assert "\n" not in line
    for token in (
        "calendar=autumn",
        "operative=shoulder",
        "weather=summer",
        "mismatch=True",
        "src=segmented",
        "window=2026-09-01..2026-11-30",
        "day=45/91",
        "regime=hot",
        "t_smooth=26.9",  # fnum(26.85, 1) → arrotondamento a 1 decimale
        "anomaly=1.14/False",
    ):
        assert token in line, token


def test_riga_di_log_tronca_motivo_lungo() -> None:
    line = format_season_log_line(_state(reason="x" * 1000))
    why = line.split("why=[", 1)[1].rstrip("]")
    assert len(why) == LOG_REASON_MAX_CHARS


# ---------------------------------------------------------------- G
def test_firma_log_ignora_data_nel_fallback() -> None:
    a = season_log_signature(_state(detect_model="fallback_last_known(2026-10-14)"))
    b = season_log_signature(_state(detect_model="fallback_last_known(2026-10-15)"))
    c = season_log_signature(_state(detect_model="segmented"))

    assert a == b
    assert a != c
    assert c == ("autumn", "summer", "segmented")


# ---------------------------------------------------------------- H
def test_attributi_astronomici_scenario_reale() -> None:
    attrs = build_season_attributes(_state())  # as_of 15/10/2026

    assert attrs["astronomical season"] == "autumn"
    assert attrs["astronomical window"] == "2026-09-23 - 2026-12-21"
    assert attrs["astronomical start"].startswith("2026-09-23T02:05")
    assert attrs["astronomical end"].startswith("2026-12-21T21:50")
    assert attrs["astronomical days"] == 89
    assert attrs["astronomical days passed"] == 22
    assert attrs["astronomical days remaining"] == 67
    assert attrs["astronomical day"] == 23
    assert attrs["astronomical progress pct"] == 24.7
    assert attrs["astronomical mismatch"] is False


def test_astronomica_diverge_dal_calendario_a_inizio_autunno() -> None:
    # 10/9/2026: calendario = autunno (dal 1/9), astronomica = ancora estate (fino al 23/9)
    attrs = build_season_attributes(_state(as_of=date(2026, 9, 10)))

    assert attrs["calendar season"] == "autumn"
    assert attrs["astronomical season"] == "summer"
    assert attrs["astronomical mismatch"] is True
    assert attrs["astronomical window"] == "2026-06-21 - 2026-09-23"


@pytest.mark.parametrize("as_of", [date(2026, 6, 21), date(2026, 9, 22), date(2026, 12, 21)])
def test_progresso_astronomico_sempre_in_0_100(as_of: date) -> None:
    attrs = build_season_attributes(_state(as_of=as_of))

    assert 0.0 <= attrs["astronomical progress pct"] <= 100.0
    assert attrs["astronomical days remaining"] >= 0
    assert 1 <= attrs["astronomical day"] <= attrs["astronomical days"] + 1


def test_chiavi_preesistenti_invariate_dopo_aggiunta_astronomica() -> None:
    attrs = build_season_attributes(_state())

    for key in (
        "calendar season", "operative season", "weather season", "season mismatch",
        "calendar window", "calendar days", "calendar days passed",
        "calendar days remaining", "weather reason", "weather regime",
        "weather signals", "weather signals eng.",
    ):
        assert key in attrs, key
    assert attrs["calendar days passed"] == "44"  # stringa legacy invariata


def test_riga_di_log_contiene_stagione_astronomica() -> None:
    line = format_season_log_line(_state())

    assert "astro=autumn" in line
    assert "\n" not in line


# ---------------------------------------------------------------- I
def test_tre_stagioni_scenario_reale_15_ottobre() -> None:
    # calendario=autumn, astronomica=autumn, dinamica (ML)=summer
    attrs = build_season_attributes(_state())

    assert attrs["seasons"] == {
        "astronomical": "autumn",
        "meteorological": "autumn",
        "dynamic": "summer",
    }
    assert attrs["meteorological season"] == "autumn"
    assert attrs["dynamic season"] == "summer"
    assert attrs["dynamic source"] == "segmented"
    assert attrs["seasons agree"] is False
    assert attrs["seasons diverging"] == ["dynamic"]


def test_tre_stagioni_concordi() -> None:
    attrs = build_season_attributes(_state(weather=Seasons.AUTUMN))

    assert attrs["seasons agree"] is True
    assert attrs["seasons diverging"] == []


def test_tre_stagioni_divergenti_a_inizio_autunno() -> None:
    # 10/9/2026: meteorologica=autumn, astronomica=summer, dinamica=summer
    attrs = build_season_attributes(_state(as_of=date(2026, 9, 10)))

    assert attrs["seasons"]["astronomical"] == "summer"
    assert attrs["seasons diverging"] == ["astronomical", "dynamic"]
    assert attrs["seasons agree"] is False


def test_sorgente_dinamica_normalizzata_senza_data() -> None:
    attrs = build_season_attributes(_state(detect_model="fallback_last_known(2026-10-14)"))
    assert attrs["dynamic source"] == "fallback_last_known"


def test_riga_di_log_con_accordo_tra_stagioni() -> None:
    assert "agree=False" in format_season_log_line(_state())
    assert "agree=True" in format_season_log_line(_state(weather=Seasons.AUTUMN))


# ---------------------------------------------------------------- J
def _signals_with_rmot(rmot: float | None, dynamic: str | None) -> WeatherDaySignals:
    return WeatherDaySignals(
        d=date(2026, 10, 15), t_mean=19.5, t_low=15.1, dew=12.9, wind=6.26, cloud=0.13,
        t_smooth=26.85, dew_smooth=13.58, trend=-7.71, t_rmot=rmot, regime_dynamic=dynamic,
    )


def test_regime_dinamico_in_shadow_diverge_dallo_storico() -> None:
    attrs = build_season_attributes(_state(signals=_signals_with_rmot(19.84, "mild")))

    assert attrs["weather regime"] == "hot"  # storico: guida l'impianto
    assert attrs["weather regime dynamic"] == "mild"
    assert attrs["weather regime agree"] is False
    assert attrs["weather signals eng."]["rmot"] == 19.84
    assert attrs["weather signals eng."]["smooth"] == 26.85  # chiavi esistenti invariate


def test_regime_dinamico_concorde() -> None:
    attrs = build_season_attributes(_state(signals=_signals_with_rmot(23.0, "hot")))

    assert attrs["weather regime agree"] is True


def test_regime_dinamico_assente_degrada_senza_eccezioni() -> None:
    state = _state(signals=_signals_with_rmot(None, None))
    attrs = build_season_attributes(state)

    assert attrs["weather regime dynamic"] is None
    assert attrs["weather regime agree"] is None
    assert attrs["weather signals eng."]["rmot"] is None
    line = format_season_log_line(state)
    assert "regime_dyn=-" in line and "rmot=-" in line


def test_riga_di_log_con_regime_dinamico_e_rmot() -> None:
    line = format_season_log_line(_state(signals=_signals_with_rmot(19.84, "mild")))

    assert "regime=hot regime_dyn=mild" in line
    assert "rmot=19.8" in line


# ---------------------------------------------------------------- K
def _signals_with_temps(t_mean: float, t_max: float | None, t_low: float | None = 16.6) -> WeatherDaySignals:
    return WeatherDaySignals(
        d=date(2026, 10, 17), t_mean=t_mean, t_low=t_low, dew=13.8, wind=6.73, cloud=0.31,
        t_smooth=21.57, dew_smooth=13.24, trend=-6.57, t_max=t_max,
    )


def test_t_mean_senza_rumore_in_virgola_mobile() -> None:
    noisy = (22.3 + 16.6) / 2  # 19.450000000000003 in virgola mobile
    assert noisy != 19.45, "il caso reale deve produrre davvero il rumore"

    attrs = build_season_attributes(_state(signals=_signals_with_temps(noisy, 22.3)))

    assert attrs["weather signals"]["t_mean"] == [19.45]
    assert attrs["weather signals flat"]["t_mean"] == 19.45
    assert repr(attrs["weather signals"]["t_mean"][0]) == "19.45"


def test_t_max_esposta_in_entrambe_le_viste_dei_segnali() -> None:
    attrs = build_season_attributes(_state(signals=_signals_with_temps(19.45, 22.3)))

    assert attrs["weather signals"]["t_max"] == [22.3]  # stessa forma di t_low/t_mean
    assert attrs["weather signals flat"]["t_max"] == 22.3
    assert attrs["weather signals"]["t_low"] == [16.6]  # chiavi esistenti invariate


def test_t_max_assente_degrada_a_none_senza_eccezioni() -> None:
    attrs = build_season_attributes(_state(signals=_signals_with_temps(19.45, None)))

    assert attrs["weather signals"]["t_max"] == [None]
    assert attrs["weather signals flat"]["t_max"] is None


def test_arrotondamento_solo_di_presentazione_il_segnale_resta_pieno() -> None:
    sig = _signals_with_temps((22.3 + 16.6) / 2, 22.3)
    build_season_attributes(_state(signals=sig))

    assert sig.t_mean == (22.3 + 16.6) / 2, "il dataclass non è toccato dall'arrotondamento"
