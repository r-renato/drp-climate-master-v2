from __future__ import annotations

import math
import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple, Literal, cast

from homeassistant.util import dt as dt_util

from ..domain.models.season import SeasonState

from ..helpers.logger import log_debug

from ..weather.provider import WeatherForecastProvider, WeatherHistoricalProvider

from ..domain.enums import Seasons

_LOGGER = logging.getLogger(__name__)

# Ordine canonico delle stagioni (emisfero nord): utile per ordinamenti coerenti
_SEASON_ORDER = Seasons.ordered()


class CalendarSeason:
    """
    Calendarizzatore di stagioni **meteorologiche** (DJF, MAM, JJA, SON)
    per anno ed emisfero, con gestione robusta degli anni bisestili e degli
    attraversamenti di anno (Winter a cavallo Dicembre→Febbraio).

    Convenzione sull'anno:
        `year` è l'anno in cui l'INVERNO termina (Febbraio di `year`).
        Esempio: CalendarSeason(2025) → Winter: 2024-12-01 .. 2025-02-28/29.

    Emisferi supportati:
        - "north":  WINTER=Dec-Feb, SPRING=Mar-May, SUMMER=Jun-Aug, AUTUMN=Sep-Nov
        - "south":  SUMMER=Dec-Feb, AUTUMN=Mar-May, WINTER=Jun-Aug, SPRING=Sep-Nov

    API principali:
        - `windows()`     → Dict[Seasons, SeasonWindow]
        - `as_dict()`     → Dict[Seasons, Tuple[date, date]]
        - `season_for(d)` → Seasons (gestione corretta DJF)
        - helper fluenti: `with_year(y)`, `with_hemisphere(h)`

    Note:
        - Le finestre sono **inclusive** [start, end].
        - L’istanza è leggera; nessun caching necessario nella maggior parte dei casi.
    """

    @dataclass(frozen=True, slots=True)
    class SeasonWindow:
        """
        Finestra stagionale inclusiva [start, end] per una determinata `season`.
        """
        season: Seasons
        start: date  # inclusive
        end: date    # inclusive

        def contains(self, d: date) -> bool:
            """True se `start <= d <= end`."""
            return self.start <= d <= self.end

    def __init__(
        self,
        year: Optional[int] = None,
        *,
        hemisphere: Literal["north", "south"] = "north",
    ) -> None:
        """
        Inizializza il calendario stagionale per un `year` logico e un `hemisphere`.

        Args:
            year: Anno logico in cui termina l'inverno (Febbraio di `year`).
                  Se None, viene usato l'anno corrente (`date.today().year`).
            hemisphere: Emisfero di riferimento ("north" o "south").

        Raises:
            ValueError: se `hemisphere` non è tra {"north","south"}.
        """
        self._year: int = year if year is not None else date.today().year
        hemi = (hemisphere or "north").lower()
        if hemi not in ("north", "south"):
            raise ValueError(f"Invalid hemisphere: {hemisphere}")
        self._hemisphere: Literal["north", "south"] = cast(Literal["north", "south"], hemi)

    # ---- fluent helpers ------------------------------------------------------
    def with_year(self, year: int) -> "CalendarSeason":
        """Ritorna una nuova istanza con stesso emisfero ma anno diverso."""
        return CalendarSeason(year, hemisphere=self._hemisphere)

    def with_hemisphere(self, hemisphere: Literal["north", "south"]) -> "CalendarSeason":
        """Ritorna una nuova istanza con stesso anno ma emisfero diverso."""
        return CalendarSeason(self._year, hemisphere=hemisphere)

    # ---- API -----------------------------------------------------------------
    def windows(self) -> Dict[Seasons, "CalendarSeason.SeasonWindow"]:
        """
        Restituisce le finestre stagionali meteorologiche per l'anno/emisfero correnti.
        """
        if self._hemisphere == "south":
            return self._south_windows(self._year)
        return self._north_windows(self._year)

    def as_dict(self) -> Dict[Seasons, Tuple[date, date]]:
        """Retro-compat: mapping stagione → (start, end)."""
        wins = self.windows()
        return {s: (w.start, w.end) for s, w in wins.items()}

    def season_for(self, d: Optional[date] = None) -> Seasons:
        """
        Determina la stagione meteorologica di una data, gestendo DJF a cavallo anno.

        Args:
            d: Data da classificare. Se None, `date.today()`.

        Returns:
            Seasons: stagione corrispondente.
        """
        d = d or date.today()
        # Considera anno precedente/corrente/successivo per coprire DJF
        for y in (d.year - 1, d.year, d.year + 1):
            for w in self.with_year(y).windows().values():
                if w.contains(d):
                    return w.season
        # Non dovrebbe accadere
        _LOGGER.warning("CalendarSeason: date %s not in any window (unexpected).", d)
        return Seasons.SUMMER

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _eom(y: int, m: int) -> int:
        """Ultimo giorno del mese `m` per l'anno `y` (gestisce anni bisestili)."""
        return calendar.monthrange(y, m)[1]

    @classmethod
    def _north_windows(cls, year: int) -> Dict[Seasons, "CalendarSeason.SeasonWindow"]:
        """Finestre stagionali per emisfero nord (anno logico `year`)."""
        winter = cls.SeasonWindow(
            Seasons.WINTER, date(year - 1, 12, 1), date(year, 2, cls._eom(year, 2))
        )
        spring = cls.SeasonWindow(Seasons.SPRING, date(year, 3, 1), date(year, 5, 31))
        summer = cls.SeasonWindow(Seasons.SUMMER, date(year, 6, 1), date(year, 8, 31))
        autumn = cls.SeasonWindow(Seasons.AUTUMN, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
        }

    @classmethod
    def _south_windows(cls, year: int) -> Dict[Seasons, "CalendarSeason.SeasonWindow"]:
        """Finestre stagionali per emisfero sud (anno logico `year`)."""
        summer = cls.SeasonWindow(
            Seasons.SUMMER, date(year - 1, 12, 1), date(year, 2, cls._eom(year, 2))
        )
        autumn = cls.SeasonWindow(Seasons.AUTUMN, date(year, 3, 1), date(year, 5, 31))
        winter = cls.SeasonWindow(Seasons.WINTER, date(year, 6, 1), date(year, 8, 31))
        spring = cls.SeasonWindow(Seasons.SPRING, date(year, 9, 1), date(year, 11, 30))
        return {
            Seasons.SUMMER: summer,
            Seasons.AUTUMN: autumn,
            Seasons.WINTER: winter,
            Seasons.SPRING: spring,
        }

class WeatherSeason:
    """
    Rilevamento stagione basato su:
      1) Prior da calendario (DJF/MAM/JJA/SON) con boost gaussiano sul cuore stagione.
      2) Storico recente (T media e dew point) con medie **pesate esponenzialmente**.
      3) Climatologia stagionale (medie anno precedente per ciascuna stagione).

    Flusso:
      - Costruisce il prior (calendario).
      - Calcola medie pesate su N giorni recenti (default 21).
      - Estrae climatologia stagionale sull'anno precedente.
      - Scoring per stagione con RBF (distanza di T/DP da climatologia).
      - Combina prior ⊙ storico, seleziona best e confidenza (margine best-second).

    Note:
      - `weatherForecast` è opzionale e attualmente non usato (estensioni future).
      - Le unità attese per T e dew sono °C: normalizzare a monte nei provider.
    """

    @dataclass(slots=True, frozen=True)
    class _ScoreParams:
        """
        Parametri di scoring:
        - day_decay/half-life sono gestiti internamente con decadimento esponenziale.
        - boost gaussiano sul prior al centro della finestra stagionale.
        - sigma per RBF di scarto dalla climatologia (°C).
        """
        # boost gaussiano sul prior in prossimità del "cuore" della stagione
        boost_sigma: float = 0.20
        boost_amp: float = 0.20
        # dispersioni (°C) per le RBF di scarto dalla climatologia
        sigma_temp: float = 3.0
        sigma_dew: float = 2.0

    def __init__(
        self,
        weatherHistorical: WeatherHistoricalProvider,
        calendar: CalendarSeason = CalendarSeason(),
        *,
        history_days: int = 21,
        params: Optional[_ScoreParams] = None,
        provider_id: str = "historical",
        weatherForecast: Optional[WeatherForecastProvider] = None,  # non usato
        last_available_offset_days: int = 2,
    ) -> None:
        """
        Args:
            weatherHistorical: provider storico (deve esporre `daily_range` o `daily`).
            calendar: istanza di CalendarSeason (emisfero/anno baseline).
            history_days: giorni di storico da pesare (min 7).
            params: parametri di scoring.
            provider_id: id diagnostico del provider.
            weatherForecast: opzionale (non usato).
            last_available_offset_days: quanti giorni sottrarre a "oggi" per la data
                                        storica massima disponibile (es. provider con T-48h).
        """
        self._calendar = calendar
        self._weather_historical = weatherHistorical
        self._weather_forecast = weatherForecast
        self._history_days = max(7, int(history_days))
        self._p = params or WeatherSeason._ScoreParams()
        self._provider_id = provider_id
        self._last_offset = max(0, int(last_available_offset_days))

    # ------------------------------- API -------------------------------------
    async def detect(self, target_date: Optional[date] = None) -> SeasonState:
        """
        Esegue la detection per `target_date` (default: oggi).

        Returns:
            SeasonState (se disponibile) o dict equivalente con:
              - season: Seasons
              - confidence: float
              - scores: Dict[str, float]
              - baseline: Seasons
              - details: diagnostica estesa
        """
        today = target_date or date.today()

        # 1) prior da calendario (+ boost gaussiano sul cuore stagione)
        baseline = self._calendar.season_for(today)
        prior = self._build_prior(self._calendar, today, baseline)

        # 2) storico recente → medie pesate
        start_hist = date.fromordinal(today.toordinal() - (self._history_days - 1))
        log_debug(_LOGGER, "Fetching history from %s to %s", start_hist, today)
        hist_recent = await self._fetch_history_range(start=start_hist, end=today)
        mean_t, mean_dew = self._weighted_recent_means(hist_recent)

        # 3) climatologia (finestre anno precedente rispetto al calendario)
        clim = await self._season_climatology_from_prev_year(today)

        # 4) scoring da climatologia (RBF su scarto T/dew)
        score_hist = self._scores_from_climatology(
            mean_t,
            mean_dew,
            clim,
            sigma_t=self._p.sigma_temp,
            sigma_d=self._p.sigma_dew,
        )

        # 5) combinazione prior ⊙ storico
        combined = self._combine_prior_and_hist(prior, score_hist)

        # 6) decisione con confidenza
        season, confidence, ordered = self._decide(combined)

        details = {
            "date": today.isoformat(),
            "provider_id": self._provider_id,
            "baseline": getattr(baseline, "value", str(baseline)),
            "prior": {s.value: v for s, v in prior.items()},
            "scores_hist": {s.value: v for s, v in score_hist.items()},
            "combined": {s.value: v for s, v in combined.items()},
            "recent_mean_t": mean_t,
            "recent_mean_dew": mean_dew,
            "climatology_t": {s.value: clim[s].get("tavg") for s in _SEASON_ORDER},
            "climatology_dew": {s.value: clim[s].get("dew") for s in _SEASON_ORDER},
            "ordered": [{s.value : sc} for s, sc in ordered],
            "confidence": confidence,
            "method": "calendar_prior + historical_anomaly",
        }

        # log_debug(_LOGGER, "Season detected %s (%s) with confidence %s using %s ", season.value, today, confidence, self._provider_id)
                  
        state: SeasonState = self._build_state(
            today=today,
            baseline=baseline,
            selected=season,
            scores=combined,      # NOTE: chiavi = Seasons, NON s.value
        )

        # Opzione A: restituisci solo l'oggetto tipizzato + diagnostics a parte
        log_debug(_LOGGER, "\n%s", details)

        return state

    # ---------------------------- internals ----------------------------------
    def _build_prior(
        self, cal: CalendarSeason, d: date, baseline: Seasons
    ) -> Dict[Seasons, float]:
        """Costruisce il prior da calendario con boost gaussiano al centro finestra."""

        def _adjacent(s: Seasons) -> Tuple[Seasons, Seasons]:
            i = _SEASON_ORDER.index(s)
            return (_SEASON_ORDER[(i - 1) % 4], _SEASON_ORDER[(i + 1) % 4])

        def _opposite(s: Seasons) -> Seasons:
            i = _SEASON_ORDER.index(s)
            return _SEASON_ORDER[(i + 2) % 4]

        base_w, adj_w, opp_w = 0.55, 0.20, 0.05
        s_adj_l, s_adj_r = _adjacent(baseline)
        prior = {
            baseline: base_w,
            s_adj_l: adj_w,
            s_adj_r: adj_w,
            _opposite(baseline): opp_w,
        }

        base_win = cal.windows()[baseline]
        x = self._relative_pos_in_window(base_win, d)  # 0..1
        boost = 1.0 + self._p.boost_amp * self._gauss(x, mu=0.5, sigma=self._p.boost_sigma)
        prior[baseline] *= boost
        return self._normalize(prior)

    @staticmethod
    def _relative_pos_in_window(win: CalendarSeason.SeasonWindow, d: date) -> float:
        """Posizione relativa 0..1 della data `d` nella finestra stagionale `win`."""
        span = (win.end - win.start).days or 1
        pos = max(0, min(span, (d - win.start).days))
        return pos / span

    async def _fetch_history_range(self, start: date, end: date) -> List[Mapping[str, Any]]:
        """
        Recupera lo storico preferendo l'API canonica `daily_range(start, end)`.
        Accetta sia Forecast HA-like (con 'datetime') che record custom.

        - Clampa `end` a (oggi - last_available_offset_days).
        - Se `start > end` dopo il clamping, ritorna [].
        - Fallback a `daily(d)` se non disponibile `daily_range`.
        """
        p = self._weather_historical

        today = dt_util.now().date()
        last_available = today - timedelta(days=self._last_offset)

        if end > last_available:
            _LOGGER.debug(
                "Clamping end date from %s to last available %s (today=%s, -%dd).",
                end, last_available, today, self._last_offset
            )
            end = last_available

        if start > end:
            _LOGGER.debug("History window empty after clamping: %s > %s", start, end)
            return []

        _LOGGER.debug("_fetch_history_range start %s - end %s", start, end)

        # 1) API canonica async
        if hasattr(p, "daily_range"):
            try:
                try:
                    res = await p.daily_range(start, end)  # type: ignore[misc]
                except TypeError:
                    res = await p.daily_range(start=start, end=end)  # type: ignore[misc]
                return list(res or [])
            except Exception as e:
                _LOGGER.warning("history daily_range(%s..%s) failed: %s", start, end, e)

        # 2) API per-giorno: `daily(d)`
        rows: List[Mapping[str, Any]] = []
        if hasattr(p, "daily"):
            d_ = start
            while d_ <= end:
                try:
                    r = await p.daily(d_)  # type: ignore[misc]
                    if isinstance(r, Mapping):
                        rows.append(r)
                except Exception as e:
                    _LOGGER.debug("history daily(%s) failed: %s", d_, e)
                d_ += timedelta(days=1)
            return rows

        _LOGGER.warning("WeatherSeasonDetector: provider storico senza API note; ritorno []")
        return []

    def _weighted_recent_means(
        self, series: List[Mapping[str, Any]]
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Media pesata **esponenziale** di T media (tavg) e dew point sugli ultimi N giorni.
        Supporta chiavi data: 'date' | 'day' | 'time' | 'datetime' (ISO o datetime).
        """
        rows = [r for r in series if self._parse_date(r) is not None]
        rows.sort(key=lambda r: cast(date, self._parse_date(r)))  # type: ignore[arg-type]
        if not rows:
            return None, None

        rows = rows[-self._history_days :]
        n = len(rows)

        # i=0 sarà il più recente (iteriamo su reversed(rows))
        # Half-life ~ history_days/2: i campioni a ~N/2 giorni valgono ~50%
        half_life = max(1.0, self._history_days / 2.0)
        lam = math.log(2.0) / half_life
        weights = [math.exp(-lam * i) for i in range(n)]
        ws = sum(weights) or 1.0
        weights = [w / ws for w in weights]

        t_vals: List[float] = []
        d_vals: List[float] = []
        for i, r in enumerate(reversed(rows)):  # i=0 = più recente
            w = weights[i]
            t = self._tavg_of(r)
            d = self._dew_of(r)
            if isinstance(t, (int, float)):
                t_vals.append(w * float(t))
            if isinstance(d, (int, float)):
                d_vals.append(w * float(d))

        t_mean = sum(t_vals) if t_vals else None
        d_mean = sum(d_vals) if d_vals else None
        return t_mean, d_mean

    async def _season_climatology_from_prev_year(
        self, ref_day: date
    ) -> Dict[Seasons, Dict[str, Optional[float]]]:
        """
        Climatologia stagionale sull'anno precedente alle finestre DJF/MAM/JJA/SON.
        Ritorna: {season: {"tavg": float|None, "dew": float|None}}
        """
        cal_prev = self._calendar.with_year(ref_day.year - 1)
        wins_prev = cal_prev.windows()
        start = wins_prev[Seasons.WINTER].start
        end = wins_prev[Seasons.AUTUMN].end

        series = await self._fetch_history_range(start, end)

        by_season_t: Dict[Seasons, List[float]] = {s: [] for s in _SEASON_ORDER}
        by_season_d: Dict[Seasons, List[float]] = {s: [] for s in _SEASON_ORDER}

        for row in series:
            d = self._parse_date(row)
            if not d:
                continue
            s = cal_prev.season_for(d)
            t = self._tavg_of(row)
            if isinstance(t, (int, float)):
                by_season_t[s].append(float(t))
            dp = self._dew_of(row)
            if isinstance(dp, (int, float)):
                by_season_d[s].append(float(dp))

        def _mean(vals: List[float]) -> Optional[float]:
            return (sum(vals) / len(vals)) if vals else None

        return {
            s: {"tavg": _mean(by_season_t[s]), "dew": _mean(by_season_d[s])}
            for s in _SEASON_ORDER
        }

    def _scores_from_climatology(
        self,
        mean_t: Optional[float],
        mean_dew: Optional[float],
        clim: Dict[Seasons, Dict[str, Optional[float]]],
        sigma_t: float,
        sigma_d: float,
    ) -> Dict[Seasons, float]:
        """
        Calcola punteggi per stagione sommando due RBF:
          - exp(- (ΔT)^2 / (2 σ_t^2))
          - exp(- (ΔDP)^2 / (2 σ_d^2))
        Normalizza i punteggi in [0..1] con somma = 1.
        """
        scores: Dict[Seasons, float] = {s: 0.0 for s in _SEASON_ORDER}
        if mean_t is None and mean_dew is None:
            return scores

        for s in scores.keys():
            sc = 0.0
            c = clim.get(s, {})
            if mean_t is not None and isinstance(c.get("tavg"), (int, float)):
                dt_ = abs(mean_t - cast(float, c["tavg"]))
                sc += math.exp(-(dt_ * dt_) / (2.0 * sigma_t * sigma_t))
            if mean_dew is not None and isinstance(c.get("dew"), (int, float)):
                dd_ = abs(mean_dew - cast(float, c["dew"]))
                sc += math.exp(-(dd_ * dd_) / (2.0 * sigma_d * sigma_d))
            scores[s] = sc
        return self._normalize(scores)

    def _combine_prior_and_hist(
        self,
        prior: Dict[Seasons, float],
        hist: Dict[Seasons, float],
    ) -> Dict[Seasons, float]:
        """
        Combina prior e storico con prodotto punto-punto e normalizza.
        Se `hist` è vuoto o nullo, ritorna `prior`.
        """
        if not hist or sum(hist.values()) == 0:
            return prior
        combined = {s: prior.get(s, 0.0) * hist.get(s, 0.0) for s in prior.keys()}
        return self._normalize(combined)

    @staticmethod
    def _decide(
        scores: Dict[Seasons, float]
    ) -> Tuple[Seasons, float, List[Tuple[Seasons, float]]]:
        """Ordina, sceglie il migliore e calcola la confidenza (best - second)."""
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_s, best_v = ordered[0]
        second_v = ordered[1][1] if len(ordered) > 1 else 0.0
        confidence = max(0.0, best_v - second_v)
        return best_s, confidence, ordered

    # --------------------------- utilità locali -------------------------------
    @staticmethod
    def _gauss(x: float, mu: float, sigma: float) -> float:
        """Valore di una gaussiana univariata N(mu, sigma^2) in x (non normalizzata)."""
        if sigma <= 0:
            return 0.0
        z = (x - mu) / sigma
        return math.exp(-0.5 * z * z)

    @staticmethod
    def _normalize(d: Dict[Seasons, float]) -> Dict[Seasons, float]:
        """Normalizza i valori ≥0 in modo che sommino a 1 (uniforme se somma ≤ 0)."""
        s = sum(max(0.0, v) for v in d.values())
        if s <= 0:
            n = len(d) or 1
            return {k: 1.0 / n for k in d.keys()}
        return {k: max(0.0, v) / s for k, v in d.items()}

    def _season_window_metrics(
        self,
        win: CalendarSeason.SeasonWindow,
        today: date,
    ) -> tuple[int, int, int]:
        """Ritorna (days, passed, remaining) per finestra inclusiva [start, end]."""
        days = (win.end - win.start).days + 1  # inclusiva
        if today <= win.start:
            passed = 0
            remaining = days - 1
        elif today >= win.end:
            passed = days - 1
            remaining = 0
        else:
            passed = (today - win.start).days
            remaining = (win.end - today).days
        return days, passed, remaining


    def _build_state(
        self,
        *,
        today: date,
        baseline: Seasons,
        selected: Seasons,
        scores: Mapping[Seasons, float],
    ) -> SeasonState:
        """Costruisce un SeasonState coerente con il modello."""
        # Finestra della stagione baseline (di calendario)
        win = self._calendar.windows()[baseline]
        days, passed, remaining = self._season_window_metrics(win, today)

        # Gli score sono già normalizzati in [0..1]; le probabilità sono in percentuale.
        season_scores = dict(scores)
        season_probabilities = {s: v * 100.0 for s, v in scores.items()}

        return SeasonState(
            season=baseline,                       # baseline di calendario
            days=days,
            passed=passed,
            remaining=remaining,
            overridden=selected,                   # stagione "scelta"
            weather_anomaly=(baseline != selected),
            season_scores=season_scores,
            season_probabilities=season_probabilities,
        )


    # --------------------------- helper DRY -----------------------------------
    def _parse_date(self, item: Mapping[str, Any]) -> Optional[date]:
        """
        Estrae una `date` da un record supportando più chiavi e formati:
        - 'date' | 'day' | 'time' | 'datetime'
        - str ISO con timezone → convertita in locale
        - fallback 'YYYY-MM-DD'
        """
        d_ = item.get("date") or item.get("day") or item.get("time") or item.get("datetime")
        if isinstance(d_, date):
            return d_
        if isinstance(d_, datetime):
            return dt_util.as_local(d_).date()
        if isinstance(d_, str):
            dtp = dt_util.parse_datetime(d_)
            if isinstance(dtp, datetime):
                return dt_util.as_local(dtp).date()
            try:
                y, m, dd = map(int, d_.split("-"))
                return date(y, m, dd)
            except Exception:
                return None
        return None

    def _tavg_of(self, item: Mapping[str, Any]) -> Optional[float]:
        """Ritorna T media come (tmax+tmin)/2 quando possibile; altrimenti tmax."""
        tmax = item.get("temp_max") or item.get("tmax")
        tmin = item.get("temp_min") or item.get("tmin")
        if isinstance(tmax, (int, float)) and isinstance(tmin, (int, float)):
            return (float(tmax) + float(tmin)) / 2.0
        if isinstance(tmax, (int, float)):
            return float(tmax)
        return None

    def _dew_of(self, item: Mapping[str, Any]) -> Optional[float]:
        """Ritorna dew point (°C) se presente in 'dewpoint' o 'dew_point'."""
        dp = item.get("dew_point") or item.get("dew_point")
        return float(dp) if isinstance(dp, (int, float)) else None
