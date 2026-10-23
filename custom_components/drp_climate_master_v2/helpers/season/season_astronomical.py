"""season_astronomical.py — Stagione astronomica (emisfero nord), funzione pura.

Equinozi e solstizi sono calcolati con l'algoritmo di Meeus (*Astronomical
Algorithms*, cap. 27): nessuna dipendenza esterna, nessun download di
effemeridi, nessun I/O. Scarto verificato contro ``ephem``: ~30 s negli anni
2020, < 3 min su 1950–2100 (ΔT fissato, vedi ``DELTA_T_SECONDS``), più che
sufficiente per una stagione usata a livello di giorno.

Le tre "stagioni" che il sistema espone sono distinte:

- **astronomica** (questo modulo): delimitata da equinozi/solstizi.
- **meteorologica** (calendario): mesi interi, 1/12–28(29)/2, 1/3–31/5, ...
- **dinamica**: dedotta da storico e previsioni (modello ML / regime termico).

Questo modulo è solo diagnostico: non partecipa alle decisioni.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

from ...domain.models.season import Seasons

ASTRO_DEFAULT_TIMEZONE: Final[str] = "Europe/Rome"
"""Fuso orario in cui sono espresse finestre e date locali."""

ASTRO_REFERENCE_HOUR_LOCAL: Final[int] = 12
"""Ora locale usata per classificare una *data* senza orario (mezzogiorno).

Un giorno che contiene un equinozio/solstizio è così assegnato alla stagione
vigente a metà giornata, in modo deterministico.
"""

MEEUS_MIN_YEAR: Final[int] = 1000
MEEUS_MAX_YEAR: Final[int] = 3000
"""Intervallo di validità della tabella di Meeus (cap. 27)."""

DELTA_T_SECONDS: Final[float] = 69.0
"""TT − UT approssimato (anni 2020). Un errore di ±1 s è irrilevante qui."""

_JDE_J2000: Final[float] = 2451545.0
_JULIAN_CENTURY_DAYS: Final[float] = 36525.0
_JD_UNIX_EPOCH: Final[float] = 2440587.5
_SECONDS_PER_DAY: Final[float] = 86400.0

# Coefficienti (JDE0 medio) per Y = (anno − 2000) / 1000, cap. 27 tab. 27.c.
# Ordine: equinozio di marzo, solstizio di giugno, equinozio di settembre,
# solstizio di dicembre.
_JDE0_COEFFS: Final[tuple[tuple[float, float, float, float, float], ...]] = (
    (2451623.80984, 365242.37404, 0.05169, -0.00411, -0.00057),
    (2451716.56767, 365241.62603, 0.00325, 0.00888, -0.00030),
    (2451810.21715, 365242.01767, -0.11575, 0.00337, 0.00078),
    (2451900.05952, 365242.74049, -0.06223, -0.00823, 0.00032),
)

# Stagione che INIZIA con ciascun evento (stesso ordine dei coefficienti).
_SEASON_STARTED_BY_EVENT: Final[tuple[Seasons, ...]] = (
    Seasons.SPRING,
    Seasons.SUMMER,
    Seasons.AUTUMN,
    Seasons.WINTER,
)

# Termini periodici (A, B in gradi, C in gradi/secolo), cap. 27 tab. 27.b.
_PERIODIC_TERMS: Final[tuple[tuple[int, float, float], ...]] = (
    (485, 324.96, 1934.136),
    (203, 337.23, 32964.467),
    (199, 342.08, 20.186),
    (182, 27.85, 445267.112),
    (156, 73.14, 45036.886),
    (136, 171.52, 22518.443),
    (77, 222.54, 65928.934),
    (74, 296.72, 3034.906),
    (70, 243.58, 9037.513),
    (58, 119.81, 33718.147),
    (52, 297.17, 150.678),
    (50, 21.02, 2281.226),
    (45, 247.54, 29929.562),
    (44, 325.15, 31555.956),
    (29, 60.93, 4443.417),
    (18, 155.12, 67555.328),
    (17, 288.79, 4562.452),
    (16, 198.04, 62894.029),
    (14, 199.76, 31436.921),
    (12, 95.39, 14577.848),
    (12, 287.11, 31931.756),
    (12, 320.81, 34777.259),
    (9, 227.73, 1222.114),
    (8, 15.45, 16859.074),
)


@dataclass(frozen=True, slots=True)
class AstroSeasonWindow:
    """Finestra di una stagione astronomica, in fuso locale.

    ``start`` è inclusivo, ``end`` esclusivo (istante dell'evento successivo).
    """

    season: Seasons
    start: datetime
    end: datetime


def _event_utc(year: int, event: int) -> datetime:
    """Istante UTC (timezone-aware) dell'evento ``event`` (0..3) dell'anno ``year``."""
    y = (year - 2000) / 1000.0
    c0, c1, c2, c3, c4 = _JDE0_COEFFS[event]
    jde0 = c0 + y * (c1 + y * (c2 + y * (c3 + y * c4)))

    t = (jde0 - _JDE_J2000) / _JULIAN_CENTURY_DAYS
    w = math.radians(35999.373 * t - 2.47)
    d_lambda = 1.0 + 0.0334 * math.cos(w) + 0.0007 * math.cos(2.0 * w)
    s = sum(a * math.cos(math.radians(b + c * t)) for a, b, c in _PERIODIC_TERMS)
    jde = jde0 + 0.00001 * s / d_lambda

    jd_ut = jde - DELTA_T_SECONDS / _SECONDS_PER_DAY
    unix_seconds = (jd_ut - _JD_UNIX_EPOCH) * _SECONDS_PER_DAY
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(seconds=round(unix_seconds))


@lru_cache(maxsize=16)
def _year_events(year: int) -> tuple[datetime, ...]:
    """I quattro eventi dell'anno (UTC), in ordine cronologico. Risultato in cache."""
    if not MEEUS_MIN_YEAR <= year <= MEEUS_MAX_YEAR:
        raise ValueError(
            f"anno {year} fuori dall'intervallo valido "
            f"[{MEEUS_MIN_YEAR}, {MEEUS_MAX_YEAR}]"
        )
    return tuple(_event_utc(year, event) for event in range(4))


def astronomical_season_for(
    moment: datetime | date,
    *,
    tz_name: str = ASTRO_DEFAULT_TIMEZONE,
) -> AstroSeasonWindow:
    """Stagione astronomica (emisfero nord) che contiene ``moment``.

    - ``datetime`` naive: interpretato nel fuso ``tz_name``.
    - ``datetime`` aware: convertito in ``tz_name``.
    - ``date``: classificato a ``ASTRO_REFERENCE_HOUR_LOCAL`` (mezzogiorno locale).

    Ritorna la finestra ``[start, end)`` in ``tz_name``. Solleva ``ValueError``
    se l'anno è fuori dall'intervallo valido dell'algoritmo.
    """
    tz = ZoneInfo(tz_name)
    if isinstance(moment, datetime):
        local = moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
    else:
        local = datetime.combine(moment, time(ASTRO_REFERENCE_HOUR_LOCAL), tzinfo=tz)
    instant = local.astimezone(timezone.utc)

    # Un anno prima e uno dopo coprono l'inizio e la fine di ogni stagione.
    # L'evento a indice i inizia la stagione _SEASON_STARTED_BY_EVENT[i % 4].
    events: list[tuple[datetime, Seasons]] = []
    for year in (local.year - 1, local.year, local.year + 1):
        for index, when in enumerate(_year_events(year)):
            events.append((when, _SEASON_STARTED_BY_EVENT[index]))

    for (start, season), (end, _) in zip(events, events[1:]):
        if start <= instant < end:
            return AstroSeasonWindow(
                season=season,
                start=start.astimezone(tz),
                end=end.astimezone(tz),
            )
    raise RuntimeError(f"stagione astronomica non trovata per {instant.isoformat()}")
