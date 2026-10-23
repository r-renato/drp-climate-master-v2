"""Test unitari per ``season_astronomical`` (stagione astronomica, patch #1 tre stagioni).

Problema risolto
----------------
Il vecchio modulo dipendeva da ``skyfield`` (non dichiarata nel manifest),
scaricava ``de421.bsp`` a runtime con I/O di rete sincrono e percorso relativo
alla cwd di Home Assistant, e non era usato da nessuno. Ora è una funzione pura
(algoritmo di Meeus), senza dipendenze né I/O.

Copertura
---------
A. Istanti di equinozi/solstizi entro tolleranza dai riferimenti ``ephem``.
B. Confini: stagione prima/dopo ogni evento 2026.
C. Continuità: le finestre di due anni consecutivi sono contigue e in ordine.
D. Ingressi: ``date`` (mezzogiorno locale), datetime naive, aware in altro fuso.
E. Durate plausibili (88–94 giorni).
F. Anno fuori dall'intervallo di validità → ``ValueError``.
G. Cache: il calcolo degli eventi di un anno non si ripete.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.drp_climate_master_v2.domain.models.season import Seasons
from custom_components.drp_climate_master_v2.helpers.season import season_astronomical as astro
from custom_components.drp_climate_master_v2.helpers.season.season_astronomical import (
    astronomical_season_for,
)

ROME = ZoneInfo("Europe/Rome")
TOLERANCE = timedelta(minutes=3)

# Riferimenti UTC calcolati con ``ephem`` (pyephem), arrotondati al secondo.
REFERENCE_EVENTS_UTC: dict[int, tuple[datetime, ...]] = {
    2024: (
        datetime(2024, 3, 20, 3, 6, 22, tzinfo=timezone.utc),
        datetime(2024, 6, 20, 20, 51, 3, tzinfo=timezone.utc),
        datetime(2024, 9, 22, 12, 43, 32, tzinfo=timezone.utc),
        datetime(2024, 12, 21, 9, 20, 21, tzinfo=timezone.utc),
    ),
    2026: (
        datetime(2026, 3, 20, 14, 45, 53, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 8, 24, 31, tzinfo=timezone.utc),
        datetime(2026, 9, 23, 0, 5, 9, tzinfo=timezone.utc),
        datetime(2026, 12, 21, 20, 50, 0, tzinfo=timezone.utc),
    ),
    2030: (
        datetime(2030, 3, 20, 13, 51, 47, tzinfo=timezone.utc),
        datetime(2030, 6, 21, 7, 31, 18, tzinfo=timezone.utc),
        datetime(2030, 9, 22, 23, 26, 50, tzinfo=timezone.utc),
        datetime(2030, 12, 21, 20, 9, 18, tzinfo=timezone.utc),
    ),
}


# ---------------------------------------------------------------- A
@pytest.mark.parametrize("year", sorted(REFERENCE_EVENTS_UTC))
def test_eventi_coerenti_con_ephem(year: int) -> None:
    computed = astro._year_events(year)

    assert len(computed) == 4
    for got, expected in zip(computed, REFERENCE_EVENTS_UTC[year]):
        assert abs(got - expected) <= TOLERANCE, (got, expected)


# ---------------------------------------------------------------- B
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 3, 20, 15, 0, tzinfo=ROME), Seasons.WINTER),  # equinozio 15:45 locali
        (datetime(2026, 3, 20, 17, 0, tzinfo=ROME), Seasons.SPRING),
        (datetime(2026, 6, 21, 9, 0, tzinfo=ROME), Seasons.SPRING),  # solstizio 10:24 locali
        (datetime(2026, 6, 21, 11, 0, tzinfo=ROME), Seasons.SUMMER),
        (datetime(2026, 9, 22, 23, 0, tzinfo=ROME), Seasons.SUMMER),  # equinozio 02:05 locali
        (datetime(2026, 9, 23, 3, 0, tzinfo=ROME), Seasons.AUTUMN),
        (datetime(2026, 12, 21, 21, 0, tzinfo=ROME), Seasons.AUTUMN),  # solstizio 21:50 locali
        (datetime(2026, 12, 21, 23, 0, tzinfo=ROME), Seasons.WINTER),
        (datetime(2026, 1, 15, 12, 0, tzinfo=ROME), Seasons.WINTER),  # inverno a cavallo d'anno
        (datetime(2027, 1, 15, 12, 0, tzinfo=ROME), Seasons.WINTER),
    ],
)
def test_stagione_ai_confini(moment: datetime, expected: Seasons) -> None:
    assert astronomical_season_for(moment).season == expected


def test_caso_reale_15_ottobre_2026() -> None:
    window = astronomical_season_for(date(2026, 10, 15))

    assert window.season == Seasons.AUTUMN
    assert window.start.date() == date(2026, 9, 23)
    assert window.end.date() == date(2026, 12, 21)
    assert window.start.tzinfo is not None and window.start.utcoffset() == timedelta(hours=2)
    assert window.end.utcoffset() == timedelta(hours=1)  # ora solare a dicembre


# ---------------------------------------------------------------- C
def test_finestre_contigue_e_in_ordine_su_due_anni() -> None:
    cursor = datetime(2026, 1, 1, 12, tzinfo=ROME)
    previous = astronomical_season_for(cursor)
    order = [Seasons.WINTER, Seasons.SPRING, Seasons.SUMMER, Seasons.AUTUMN]
    seen = [previous.season]

    for _ in range(2 * 366):
        cursor += timedelta(days=1)
        current = astronomical_season_for(cursor)
        if current.season != previous.season:
            assert current.start == previous.end, "finestre contigue"
            assert order[(order.index(previous.season) + 1) % 4] == current.season
            seen.append(current.season)
        assert current.start <= cursor < current.end
        previous = current

    assert len(seen) == 9, "da inverno 2026 a inverno 2027/28: 8 cambi"


# ---------------------------------------------------------------- D
def test_date_usa_mezzogiorno_locale() -> None:
    # 21/12/2026: solstizio alle 21:50 locali → a mezzogiorno è ancora autunno
    assert astronomical_season_for(date(2026, 12, 21)).season == Seasons.AUTUMN
    assert astronomical_season_for(date(2026, 12, 22)).season == Seasons.WINTER
    # 20/3/2026: equinozio alle 15:45 locali → a mezzogiorno è ancora inverno
    assert astronomical_season_for(date(2026, 3, 20)).season == Seasons.WINTER
    assert astronomical_season_for(date(2026, 3, 21)).season == Seasons.SPRING


def test_datetime_naive_interpretato_nel_fuso_locale_e_aware_convertito() -> None:
    naive = astronomical_season_for(datetime(2026, 9, 23, 1, 0))  # 01:00 Roma, prima dell'equinozio
    aware_utc = astronomical_season_for(datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc))  # 01:00 Roma

    assert naive.season == aware_utc.season == Seasons.SUMMER
    assert naive == aware_utc


# ---------------------------------------------------------------- E
def test_durate_plausibili() -> None:
    day = date(2025, 1, 1)
    for _ in range(0, 365 * 3, 30):
        window = astronomical_season_for(day)
        assert 88 <= (window.end - window.start).days <= 94
        day += timedelta(days=30)


# ---------------------------------------------------------------- F
def test_anno_fuori_intervallo_solleva_value_error() -> None:
    with pytest.raises(ValueError):
        astronomical_season_for(date(900, 6, 1))
    with pytest.raises(ValueError):
        astronomical_season_for(date(3000, 6, 1))  # serve anche l'anno successivo (3001)


# ---------------------------------------------------------------- G
def test_eventi_dell_anno_in_cache() -> None:
    astro._year_events.cache_clear()
    astronomical_season_for(date(2026, 10, 15))
    first = astro._year_events.cache_info()
    astronomical_season_for(date(2026, 11, 1))
    second = astro._year_events.cache_info()

    assert second.misses == first.misses, "nessun nuovo calcolo per gli stessi anni"
    assert second.hits > first.hits
