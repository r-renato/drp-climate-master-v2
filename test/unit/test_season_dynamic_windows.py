"""Test unitari per ``build_dynamic_windows_attributes`` (patch #12, finestre ML passate).

Problema risolto
----------------
Il modello ML calcola già le finestre stagionali degli anni meteo passati
(``WeatherSeason.windows``) ma nessun attributo le mostrava: non si poteva vedere
quando il modello ha collocato l'inizio dell'autunno né quanto si discosta dal
calendario. Questa patch le espone, solo come diagnostica.

Due limiti del modello sono resi espliciti, non nascosti:

- i bordi 1/12 e 30/11 sono imposti dall'anno meteo (``start fixed`` / ``end fixed``):
  per l'autunno solo l'inizio è informazione del modello;
- con meno di ``DYNAMIC_TYPICAL_MIN_YEARS`` anni non esiste una finestra "tipica".

Copertura
---------
A. Scenario reale: un solo anno meteo (2025), autunno con inizio dinamico +11 giorni.
B. Due anni → finestra tipica (mediana degli scostamenti).
C. Nessuna finestra (``None`` / lista vuota) → degradazione senza eccezioni.
D. Attribuzione all'anno meteo (dicembre appartiene all'anno successivo).
E. Mediana deterministica sulle metà (arrotondamento per eccesso).
F. ``_calendar_window_north`` non deriva da ``CalendarSeason`` (anni bisestili inclusi).
G. Attributi serializzabili in JSON (requisito degli attributi di stato HA).
H. Integrazione in ``build_season_attributes``: chiavi nuove, preesistenti invariate.
I. Inverno (finestra di calendario a cavallo d'anno) con tipica su due anni.
"""
from __future__ import annotations

import json
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
    DYNAMIC_TYPICAL_MIN_YEARS,
    _calendar_window_north,
    _median_days,
    build_dynamic_windows_attributes,
    build_season_attributes,
)
from custom_components.drp_climate_master_v2.helpers.season.season_weather_calendar import (
    CalendarSeason,
)

Window = tuple[date, date, Seasons]

# Anno meteo 2025 (dic 2024 → nov 2025): autunno dinamico che inizia 11 giorni dopo il 1/9.
WINDOWS_2025: list[Window] = [
    (date(2024, 12, 1), date(2025, 3, 4), Seasons.WINTER),
    (date(2025, 3, 5), date(2025, 6, 2), Seasons.SPRING),
    (date(2025, 6, 3), date(2025, 9, 11), Seasons.SUMMER),
    (date(2025, 9, 12), date(2025, 11, 30), Seasons.AUTUMN),
]
# Anno meteo 2026 (dic 2025 → nov 2026): autunno dinamico +1 giorno.
WINDOWS_2026: list[Window] = [
    (date(2025, 12, 1), date(2026, 3, 1), Seasons.WINTER),
    (date(2026, 3, 2), date(2026, 5, 30), Seasons.SPRING),
    (date(2026, 5, 31), date(2026, 9, 1), Seasons.SUMMER),
    (date(2026, 9, 2), date(2026, 11, 30), Seasons.AUTUMN),
]


def _state(
    windows: list[Window] | None,
    *,
    calendar: Seasons = Seasons.AUTUMN,
    window: tuple[date, date] = (date(2026, 9, 1), date(2026, 11, 30)),
    as_of: date = date(2026, 10, 17),
) -> SeasonState:
    sig = WeatherDaySignals(
        d=as_of, t_mean=19.2, t_low=14.4, dew=14.3, wind=8.57, cloud=0.31,
        t_smooth=21.39, dew_smooth=13.07, trend=-6.6, t_max=24.0,
    )
    return SeasonState(
        window=SeasonWindow(calendar, window[0], window[1]),
        weather=WeatherSeason(
            season=Seasons.AUTUMN,
            anomaly=False,
            anomaly_score=1.28,
            reason="infer",
            regime_hint="mild",
            weather_day_signals=sig,
            windows=windows,
        ),
        as_of=as_of,
        detect_model="inferred",
    )


# ---------------------------------------------------------------- A
def test_scenario_reale_un_solo_anno_autunno_inizia_11_giorni_dopo_il_calendario() -> None:
    attrs = build_dynamic_windows_attributes(_state(WINDOWS_2025))

    autumn = attrs["dynamic windows"]["2025"]["autumn"]
    assert autumn["window"] == "2025-09-12 - 2025-11-30"
    assert autumn["days"] == 80
    assert autumn["start offset days"] == 11
    assert autumn["end offset days"] == 0
    assert autumn["start fixed"] is False  # inizio deciso dal modello
    assert autumn["end fixed"] is True     # 30/11: bordo dell'anno meteo
    assert attrs["dynamic windows"]["2025"]["winter"]["start fixed"] is True
    assert attrs["dynamic windows years"] == 1


def test_un_solo_anno_non_produce_finestra_tipica() -> None:
    assert DYNAMIC_TYPICAL_MIN_YEARS == 2
    assert build_dynamic_windows_attributes(_state(WINDOWS_2025))["dynamic season typical"] is None


# ---------------------------------------------------------------- B
def test_due_anni_producono_finestra_tipica_con_mediana_degli_scostamenti() -> None:
    attrs = build_dynamic_windows_attributes(_state(WINDOWS_2025 + WINDOWS_2026))

    assert attrs["dynamic windows years"] == 2
    assert list(attrs["dynamic windows"]) == ["2025", "2026"]
    typical = attrs["dynamic season typical"]
    # scostamenti di inizio autunno: +11 (2025) e +1 (2026) → mediana 6
    assert typical["start offset days"] == 6
    assert typical["end offset days"] == 0
    assert typical["window"] == "2026-09-07 - 2026-11-30"  # applicata alla finestra di calendario corrente
    assert typical["years"] == 2
    assert typical["end fixed"] is True


# ---------------------------------------------------------------- C
@pytest.mark.parametrize("windows", [None, []])
def test_nessuna_finestra_degrada_senza_eccezioni(windows: list[Window] | None) -> None:
    attrs = build_dynamic_windows_attributes(_state(windows))

    assert attrs == {
        "dynamic windows": {},
        "dynamic windows years": 0,
        "dynamic season typical": None,
    }


def test_stagione_corrente_assente_dalle_finestre_non_produce_tipica() -> None:
    only_winter = [w for w in WINDOWS_2025 + WINDOWS_2026 if w[2] == Seasons.WINTER]

    attrs = build_dynamic_windows_attributes(_state(only_winter))  # stagione corrente = autumn

    assert attrs["dynamic windows years"] == 2
    assert attrs["dynamic season typical"] is None


# ---------------------------------------------------------------- D
def test_la_finestra_invernale_di_dicembre_appartiene_all_anno_meteo_successivo() -> None:
    attrs = build_dynamic_windows_attributes(_state(WINDOWS_2025[:1]))

    assert list(attrs["dynamic windows"]) == ["2025"]  # 1/12/2024 → anno meteo 2025
    assert attrs["dynamic windows"]["2025"]["winter"]["start offset days"] == 0


# ---------------------------------------------------------------- E
@pytest.mark.parametrize(
    ("values", "expected"),
    [([11, 1], 6), ([1, 2], 2), ([-3, -2], -2), ([5], 5), ([0, 0, 7], 0)],
)
def test_mediana_deterministica_sulle_meta(values: list[int], expected: int) -> None:
    assert _median_days(values) == expected


# ---------------------------------------------------------------- F
@pytest.mark.parametrize("year", range(2023, 2031))  # include 2024 e 2028 bisestili
def test_finestre_di_calendario_replicate_senza_deriva_da_calendarseason(year: int) -> None:
    official = CalendarSeason(year).windows()

    for season in Seasons:
        assert _calendar_window_north(season, year) == (official[season].start, official[season].end)


# ---------------------------------------------------------------- G
def test_attributi_serializzabili_in_json() -> None:
    attrs = build_dynamic_windows_attributes(_state(WINDOWS_2025 + WINDOWS_2026))

    assert json.loads(json.dumps(attrs)) == attrs  # niente date/enum negli attributi di stato


# ---------------------------------------------------------------- H
def test_integrazione_in_build_season_attributes() -> None:
    attrs = build_season_attributes(_state(WINDOWS_2025))

    assert attrs["dynamic windows"]["2025"]["autumn"]["start offset days"] == 11
    assert attrs["dynamic windows years"] == 1
    assert attrs["dynamic season typical"] is None
    for key in ("calendar season", "seasons", "weather regime dynamic", "weather signals flat"):
        assert key in attrs, key  # chiavi preesistenti invariate


def test_attributi_integrati_restano_serializzabili_in_json() -> None:
    json.dumps(build_season_attributes(_state(WINDOWS_2025 + WINDOWS_2026)))


# ---------------------------------------------------------------- I
def test_inverno_con_calendario_a_cavallo_d_anno() -> None:
    state = _state(
        WINDOWS_2025 + WINDOWS_2026,
        calendar=Seasons.WINTER,
        window=(date(2026, 12, 1), date(2027, 2, 28)),
        as_of=date(2026, 12, 15),
    )

    typical = build_dynamic_windows_attributes(state)["dynamic season typical"]

    # fine inverno: +4 giorni (2025: 4/3 vs 28/2) e +1 (2026: 1/3 vs 28/2) → mediana 2,5 → 3
    assert typical["start offset days"] == 0
    assert typical["end offset days"] == 3
    assert typical["window"] == "2026-12-01 - 2027-03-03"
