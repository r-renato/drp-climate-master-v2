from __future__ import annotations

from typing import Dict, Final, Mapping, Optional, Tuple, Literal, cast
from datetime import date, datetime
import calendar
import logging

from ..timeutils import to_local_date
from ...domain.models.season import SeasonWindow
from ...domain.models.season import Seasons
from ..logger import log_warning

_LOGGER = logging.getLogger(__name__)

NORTH: Final[str] = "north"
SOUTH: Final[str] = "south"

_Hemisphere = Literal["north", "south"]

class CalendarSeason:
    """Calendarizzatore di stagioni meteorologiche (DJF/MAM/JJA/SON).

    Versione *HA-friendly*:
      - timezone-safe: nessuna assunzione su timezone; default su data esplicita.
      - output ordinato e stabile (Mapping in ordine canonico).
      - fallback sicuro (no "SUMMER" arbitrario): calcolo deterministico per mese.

    Convenzione sull'anno (immutata):
        `year` è l'anno in cui l'inverno termina (Febbraio di `year`).

    NOTE Home Assistant:
      - In HA conviene passare la data già normalizzata alla timezone dell'istanza.
        Esempio: d = dt_util.now().date() e poi season_for(d=d)
      - Questo modulo evita di chiamare date.today() di default per prevenire mismatch
        fra timezone di sistema e timezone HA.

    Finestre inclusive: [start, end]
    """

    def __init__(
        self,
        year: Optional[int] = None,
        *,
        hemisphere: _Hemisphere = NORTH,
    ) -> None:
        # ATTENZIONE: teniamo un default per compatibilità, ma in HA è meglio passare year esplicitamente.
        if year is None:
            log_warning(
                _LOGGER,
                "CalendarSeason: year non fornito, uso anno corrente come fallback. "
                "Preferisci CalendarSeason.for_date(d) per evitare ambiguità DJF.",
            )
        self._year: int = year if year is not None else date.today().year

        hemi = (hemisphere or NORTH).lower()
        if hemi not in (NORTH, SOUTH):
            raise ValueError(f"Invalid hemisphere: {hemisphere}")
        self._hemisphere: _Hemisphere = cast(_Hemisphere, hemi)

    # ---- fluent helpers ------------------------------------------------------
    def with_year(self, year: int) -> "CalendarSeason":
        return CalendarSeason(year, hemisphere=self._hemisphere)

    def with_hemisphere(self, hemisphere: _Hemisphere) -> "CalendarSeason":
        return CalendarSeason(self._year, hemisphere=hemisphere)

    @classmethod
    def for_date(cls, d: "date | datetime", *, hemisphere: _Hemisphere = NORTH) -> "CalendarSeason":
        """Factory: crea un CalendarSeason con l'anno corretto rispetto a `d`.

        Convenzione DJF (emisfero nord):
            Dicembre appartiene all'inverno dell'anno successivo (year+1).
            Tutti gli altri mesi usano d.year.
        Per l'emisfero sud la logica è analoga (dicembre → estate year+1).
        """
        dd = d.date() if isinstance(d, datetime) else d
        year = dd.year + 1 if dd.month == 12 else dd.year
        return cls(year=year, hemisphere=hemisphere)

    # ---- API -----------------------------------------------------------------
    def windows(self) -> Mapping[Seasons, "SeasonWindow"]:
        """Restituisce finestre stagionali ordinate in modo canonico."""
        wins = self._south_windows(self._year) if self._hemisphere == SOUTH else self._north_windows(self._year)
        # Ordina sempre in modo coerente: (WINTER, SPRING, SUMMER, AUTUMN)
        # In emisfero sud questo è un *ordine di etichette*, non cronologico.
        return {s: wins[s] for s in Seasons.ordered()}

    def as_dict(self) -> Dict[Seasons, Tuple[date, date]]:
        """Retro-compat: mapping stagione → (start, end)."""
        wins = self.windows()
        return {s: (w.start, w.end) for s, w in wins.items()}

    def season_for(self, d: Optional[date | datetime] = None) -> Seasons:
        """Determina la stagione per una data.

        HA-friendly: è consigliato passare `d` esplicito.
        Se `d` è None, usa date.today() (fallback di compatibilità).

        Strategia:
          1) prova match su finestre (robusto per DJF a cavallo anno)
          2) se fallisce (non dovrebbe), fallback deterministico basato su mese+emisfero
        """
        dd = to_local_date(d) or date.today()

        # Copertura DJF: considera year-1, year, year+1 come nella versione originale
        for y in (dd.year - 1, dd.year, dd.year + 1):
            for w in self.with_year(y).windows().values():
                if w.contains(dd):
                    return w.season

        # Fallback sicuro e deterministico: mese -> stagione
        log_warning(_LOGGER, "CalendarSeason: date %s not in any window (unexpected); using month fallback.", dd)
        return self._season_by_month(dd.month, self._hemisphere)

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _eom(y: int, m: int) -> int:
        return calendar.monthrange(y, m)[1]

    @classmethod
    def _north_windows(cls, year: int) -> Dict[Seasons, "SeasonWindow"]:
        winter = SeasonWindow(
            Seasons.WINTER, date(year - 1, 12, 1), date(year, 2, cls._eom(year, 2))
        )
        spring = SeasonWindow(Seasons.SPRING, date(year, 3, 1), date(year, 5, 31))
        summer = SeasonWindow(Seasons.SUMMER, date(year, 6, 1), date(year, 8, 31))
        autumn = SeasonWindow(Seasons.AUTUMN, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
        }

    @classmethod
    def _south_windows(cls, year: int) -> Dict[Seasons, "SeasonWindow"]:
        summer = SeasonWindow(
            Seasons.SUMMER, date(year - 1, 12, 1), date(year, 2, cls._eom(year, 2))
        )
        autumn = SeasonWindow(Seasons.AUTUMN, date(year, 3, 1), date(year, 5, 31))
        winter = SeasonWindow(Seasons.WINTER, date(year, 6, 1), date(year, 8, 31))
        spring = SeasonWindow(Seasons.SPRING, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
        }

    @staticmethod
    def _season_by_month(month: int, hemisphere: _Hemisphere) -> Seasons:
        """Fallback deterministico per mese (meteorologico)."""
        if hemisphere == NORTH:
            if month in (12, 1, 2):
                return Seasons.WINTER
            if month in (3, 4, 5):
                return Seasons.SPRING
            if month in (6, 7, 8):
                return Seasons.SUMMER
            return Seasons.AUTUMN

        # SOUTH
        if month in (12, 1, 2):
            return Seasons.SUMMER
        if month in (3, 4, 5):
            return Seasons.AUTUMN
        if month in (6, 7, 8):
            return Seasons.WINTER
        return Seasons.SPRING
