"""season_attributes.py — Attributi diagnostici e riga di log dello stato stagionale.

Funzioni pure (nessuna dipendenza da Home Assistant): a partire da ``SeasonState``
costruiscono il dizionario di attributi del sensore stagione e la riga di log
compatta per il commissioning.

Le tre "stagioni" che convivono in ``SeasonState`` sono distinte esplicitamente:

- **calendario** (``state.season``): finestra DJF/MAM/JJA/SON. È la stagione che
  guida la decisione (resolver, VMC, comfort band).
- **astronomica** (``astronomical_season_for``): equinozi e solstizi. Solo
  diagnostica, calcolata sulla data ``state.as_of``.
- **meteo** (``state.weather.season``): etichetta del modello ML. Solo diagnostica:
  può divergere dal calendario (es. anno meteo incompleto).
- **operativa**: bucket winter/summer/shoulder derivato dal calendario.
"""
from __future__ import annotations

import calendar
import math
import statistics
from datetime import date, timedelta
from typing import Any, Final

from ...domain.models.season import OperativeSeason, SeasonState, Seasons
from ..formatter import fnum
from .season_astronomical import AstroSeasonWindow, astronomical_season_for
from .thermal_regime import (
    FORECAST_RELIABLE_DAYS,
    METEO_YEAR_START_MONTH,
    REGIME_COLD_ENTER_MAX_C,
    REGIME_COLD_EXIT_MIN_C,
    REGIME_COOL_ENTER_MAX_C,
    REGIME_COOL_EXIT_MIN_C,
    REGIME_HOT_ENTER_MIN_C,
    REGIME_HOT_EXIT_MAX_C,
    REGIME_TORRID_ENTER_MIN_C,
    REGIME_TORRID_EXIT_MAX_C,
    meteo_year_of,
    plant_regime_of,
)

LOG_REASON_MAX_CHARS: Final[int] = 160
"""Lunghezza massima del motivo del modello nella riga di log (evita dump enormi)."""

SIGNAL_TEMP_DECIMALS: Final[int] = 2
"""Decimali delle temperature giornaliere esposte come attributo.

I provider danno 1 decimale: la media (Tmax+Tmin)/2 ne ha al più 2 (es. 19.45).
L'arrotondamento elimina solo il rumore in virgola mobile (19.450000000000003) e
vale per la sola presentazione: il modello continua a usare il valore pieno.
"""


def _round_temp(value: float | None) -> float | None:
    """Arrotonda una temperatura per la presentazione; ``None`` resta ``None``."""
    return None if value is None else round(float(value), SIGNAL_TEMP_DECIMALS)


def operative_season_of(state: SeasonState) -> OperativeSeason:
    """Bucket operativo (winter/summer/shoulder) derivato dalla stagione di calendario.

    Stessa mappatura usata dal resolver: spring/autumn → SHOULDER.
    """
    return OperativeSeason.from_value(state.season.value)


def astronomical_window_of(state: SeasonState) -> AstroSeasonWindow:
    """Finestra astronomica della data ``state.as_of`` (funzione pura, in cache)."""
    return astronomical_season_for(state.as_of)


def build_astronomical_attributes(state: SeasonState) -> dict[str, Any]:
    """Attributi della stagione astronomica, affiancabili a quelli di calendario.

    Giorni conteggiati su date locali: ``passed`` = giorni dalla data di inizio,
    ``days`` = giorni tra la data di inizio e quella di fine (evento successivo),
    ``day`` = numerazione 1-based. La percentuale è limitata a [0, 100].
    """
    window = astronomical_window_of(state)
    start_date = window.start.date()
    end_date = window.end.date()
    days = (end_date - start_date).days
    passed = (state.as_of - start_date).days
    progress = min(100.0, max(0.0, passed / days * 100.0)) if days > 0 else 0.0
    return {
        "astronomical season": window.season.value,
        "astronomical window": f"{start_date.isoformat()} - {end_date.isoformat()}",
        "astronomical start": window.start.isoformat(timespec="minutes"),
        "astronomical end": window.end.isoformat(timespec="minutes"),
        "astronomical days": days,
        "astronomical days passed": passed,
        "astronomical days remaining": days - passed,
        "astronomical day": passed + 1,
        "astronomical progress pct": round(progress, 1),
        "astronomical mismatch": window.season != state.season,
    }


def dynamic_source_of(state: SeasonState) -> str:
    """Sorgente della stagione dinamica, senza la data variabile del fallback.

    Es. ``fallback_last_known(2026-10-14)`` → ``fallback_last_known``.
    """
    return state.detect_model.split("(", 1)[0]


def build_three_seasons_attributes(state: SeasonState) -> dict[str, Any]:
    """Vista affiancata delle tre stagioni: astronomica, meteorologica, dinamica.

    - **meteorologica**: finestra di calendario (mesi interi), quella che guida
      la decisione.
    - **astronomica**: equinozi e solstizi.
    - **dinamica**: etichetta del modello ML su storico e previsioni. Solo
      diagnostica (shadow): non influenza l'impianto.

    ``seasons diverging`` elenca le stagioni diverse da quella meteorologica
    (vuota se tutte concordano), utile per un badge di divergenza in dashboard.
    """
    meteorological = state.season.value
    seasons = {
        "astronomical": astronomical_window_of(state).season.value,
        "meteorological": meteorological,
        "dynamic": state.weather.season.value,
    }
    return {
        "meteorological season": meteorological,
        "dynamic season": seasons["dynamic"],
        "dynamic source": dynamic_source_of(state),
        "seasons": seasons,
        "seasons agree": len(set(seasons.values())) == 1,
        "seasons diverging": [
            name for name in ("astronomical", "dynamic") if seasons[name] != meteorological
        ],
    }


DYNAMIC_TYPICAL_MIN_YEARS: Final[int] = 2
"""Anni meteo segmentati minimi per esporre una finestra "tipica".

Con un solo anno la finestra dinamica è un campione singolo, non una tendenza:
meglio ``None`` che una falsa precisione.
"""

METEO_YEAR_END_MONTH: Final[int] = 11
METEO_YEAR_END_DAY: Final[int] = 30
"""L'anno meteo va dal 1/12 al 30/11: i due bordi sono vincolati dalla definizione
di anno, non dedotti dai dati (la segmentazione DP impone 4 segmenti contigui)."""


def _calendar_window_north(season: Seasons, meteo_year: int) -> tuple[date, date]:
    """Finestra di calendario DJF/MAM/JJA/SON (emisfero nord) di un anno meteo.

    Replica ``CalendarSeason._north_windows`` senza importarla (quel modulo dipende
    da Home Assistant, questo no); un test ne blocca la deriva.
    """
    last_feb = calendar.monthrange(meteo_year, 2)[1]
    windows = {
        Seasons.WINTER: (date(meteo_year - 1, 12, 1), date(meteo_year, 2, last_feb)),
        Seasons.SPRING: (date(meteo_year, 3, 1), date(meteo_year, 5, 31)),
        Seasons.SUMMER: (date(meteo_year, 6, 1), date(meteo_year, 8, 31)),
        Seasons.AUTUMN: (date(meteo_year, 9, 1), date(meteo_year, 11, 30)),
    }
    return windows[season]


def _median_days(values: list[int]) -> int:
    """Mediana in giorni, arrotondata per eccesso sulle metà (deterministica)."""
    return int(math.floor(statistics.median(values) + 0.5))


def build_dynamic_windows_attributes(state: SeasonState) -> dict[str, Any]:
    """Finestre stagionali dedotte dal modello ML sugli anni passati (solo diagnostica).

    Legge ``state.weather.windows`` (già calcolato dal rilevatore) e per ogni anno
    meteo segmentato espone, per stagione: finestra ``yyyy-mm-dd - yyyy-mm-dd``,
    durata e scostamento in giorni da quella di calendario.

    ``start fixed`` / ``end fixed`` sono True quando il bordo coincide con 1/12 o
    30/11: in quel caso è imposto dall'anno meteo, non una decisione del modello
    (per l'autunno solo l'inizio è informazione ML).

    L'anno meteo in corso non è segmentato (incompleto): non compare qui.

    ``dynamic season typical`` riguarda la stagione di calendario corrente ed è
    ``None`` con meno di ``DYNAMIC_TYPICAL_MIN_YEARS`` anni.
    """
    per_year: dict[int, dict[str, dict[str, Any]]] = {}
    for start, end, season in state.weather.windows or []:
        year = meteo_year_of(start)
        cal_start, cal_end = _calendar_window_north(season, year)
        per_year.setdefault(year, {})[season.value] = {
            "window": f"{start.isoformat()} - {end.isoformat()}",
            "days": (end - start).days + 1,
            "start offset days": (start - cal_start).days,
            "end offset days": (end - cal_end).days,
            "start fixed": start == date(year - 1, METEO_YEAR_START_MONTH, 1),
            "end fixed": end == date(year, METEO_YEAR_END_MONTH, METEO_YEAR_END_DAY),
        }

    current = state.season
    samples = [
        year_windows[current.value]
        for _, year_windows in sorted(per_year.items())
        if current.value in year_windows
    ]
    typical: dict[str, Any] | None = None
    if len(samples) >= DYNAMIC_TYPICAL_MIN_YEARS:
        start_off = _median_days([s["start offset days"] for s in samples])
        end_off = _median_days([s["end offset days"] for s in samples])
        cal = state.window
        typical = {
            "window": (
                f"{(cal.start + timedelta(days=start_off)).isoformat()} - "
                f"{(cal.end + timedelta(days=end_off)).isoformat()}"
            ),
            "start offset days": start_off,
            "end offset days": end_off,
            "years": len(samples),
            "end fixed": all(s["end fixed"] for s in samples),
        }

    return {
        "dynamic windows": {str(y): per_year[y] for y in sorted(per_year)},
        "dynamic windows years": len(per_year),
        "dynamic season typical": typical,
    }


def build_hot_regime_end_attributes(state: SeasonState) -> dict[str, Any]:
    """Fine del regime dinamico "hot" (RMOT) per anno meteo, a confronto con il ML.

    Per ogni anno: ``date`` (primo giorno non-hot dopo l'ultima ondata di caldo) e
    ``offset days`` rispetto all'inizio d'autunno di calendario (1/9). Se il
    segmentatore ha collocato l'inizio dell'autunno in quell'anno, aggiunge
    ``ml autumn start`` e ``ml lag days`` (ML meno la data RMOT: positivo = il ML
    arriva dopo, effetto delle medie a 28 giorni).

    Anno assente = nessun "hot" osservato o ancora in corso. Solo diagnostica.
    """
    ends = state.weather.hot_regime_ends or {}
    ml_autumn_start = {
        meteo_year_of(start): start
        for start, _end, season in (state.weather.windows or [])
        if season == Seasons.AUTUMN
    }
    out: dict[str, dict[str, Any]] = {}
    for year in sorted(ends):
        end = ends[year]
        entry: dict[str, Any] = {
            "date": end.isoformat(),
            "offset days": (end - _calendar_window_north(Seasons.AUTUMN, year)[0]).days,
        }
        ml_start = ml_autumn_start.get(year)
        if ml_start is not None:
            entry["ml autumn start"] = ml_start.isoformat()
            entry["ml lag days"] = (ml_start - end).days
        out[str(year)] = entry
    return {"dynamic hot regime end": out}


def build_regime_spans_attributes(state: SeasonState) -> dict[str, Any]:
    """Inizio e fine dei regimi dinamici hot/cold (RMOT) per anno meteo.

    Per anno e regime: ``start`` e ``end`` (ISO; ``end`` è ``None`` se il regime è
    in corso), ``start known`` (``False`` = lo storico inizia dentro il regime),
    ``ongoing``, ``periods`` (ondate separate da pause) e ``days`` in regime.
    Il regime "mild" non ha confini propri: è il residuo tra le transizioni.

    Anno o regime assente = mai verificato nello storico disponibile. Solo
    diagnostica: non guida l'impianto.
    """
    spans = state.weather.regime_spans or {}
    out: dict[str, dict[str, Any]] = {}
    for year in sorted(spans):
        out[str(year)] = {
            regime: {
                "start": span.start.isoformat(),
                "end": span.end.isoformat() if span.end is not None else None,
                "start known": span.start_known,
                "ongoing": span.end is None,
                "periods": span.periods,
                "days": span.days,
            }
            for regime, span in sorted(spans[year].items())
        }
    return {"dynamic regime spans": out}


def astronomical_bands_of(as_of: date) -> list[dict[str, str]]:
    """Fasce astronomiche sull'asse dell'anno meteo che contiene ``as_of`` (1/12 → 30/11).

    Ogni fascia: ``season``, ``start`` (incluso) e ``end`` (escluso), date ISO.
    Il primo tratto può essere l'autunno dell'anno precedente (fino al solstizio
    d'inverno). Date = giorno locale dell'evento, come ``astronomical window``. Fuori dall'intervallo valido dell'algoritmo: lista vuota.
    """
    year = meteo_year_of(as_of)
    axis_start = date(year - 1, METEO_YEAR_START_MONTH, 1)
    axis_end = date(year, METEO_YEAR_START_MONTH, 1)  # esclusivo: 1/12 = giorno dopo il 30/11
    bands: list[dict[str, str]] = []
    sample = axis_start
    try:
        while True:
            window = astronomical_season_for(sample)
            start = max(window.start.date(), axis_start)
            end = min(window.end.date(), axis_end)
            bands.append({"season": window.season.value, "start": start.isoformat(), "end": end.isoformat()})
            if window.end.date() >= axis_end:
                break
            # il giorno dopo l'evento è sicuramente nella stagione successiva (campionata a mezzogiorno)
            sample = window.end.date() + timedelta(days=1)
    except (ValueError, RuntimeError):
        return []
    return bands


def build_regime_periods_attributes(state: SeasonState) -> dict[str, Any]:
    """Dati per la timeline dei regimi termici dinamici (RMOT), tutti gli anni disponibili.

    - ``dynamic regime periods``: periodi cold/cool/mild/hot in ordine cronologico
      (``regime``, ``start``, ``end`` = ``None`` se in corso, ``start known``, ``days``);
    - ``dynamic regime periods as of``: ultimo giorno osservato (oltre: non osservato);
    - ``dynamic regime thresholds``: soglie RMOT °C di ingresso/uscita (legenda);
    - ``astronomical bands``: fasce astronomiche dell'anno meteo corrente.

    Solo diagnostica: non guida l'impianto. Senza dati: lista vuota.
    """
    periods = state.weather.regime_periods or []
    return {
        "dynamic regime periods": [
            {
                "regime": p.regime,
                "start": p.start.isoformat(),
                "end": p.end.isoformat() if p.end is not None else None,
                "start known": p.start_known,
                "days": p.days,
            }
            for p in periods
        ],
        "dynamic regime periods as of": state.as_of.isoformat(),
        "dynamic regime thresholds": {
            "cold enter max": REGIME_COLD_ENTER_MAX_C,
            "cold exit min": REGIME_COLD_EXIT_MIN_C,
            "cool enter max": REGIME_COOL_ENTER_MAX_C,
            "cool exit min": REGIME_COOL_EXIT_MIN_C,
            "hot enter min": REGIME_HOT_ENTER_MIN_C,
            "hot exit max": REGIME_HOT_EXIT_MAX_C,
            "torrid enter min": REGIME_TORRID_ENTER_MIN_C,
            "torrid exit max": REGIME_TORRID_EXIT_MAX_C,
        },
        "astronomical bands": astronomical_bands_of(state.as_of),
    }


def build_regime_forecast_attributes(state: SeasonState) -> dict[str, Any]:
    """Regime dinamico *previsto* (forecast) per i giorni futuri, con peso di affidabilità.

    - ``dynamic regime forecast``: per giorno ``day``, ``regime``, ``lead days`` (1 = domani)
      e ``weight`` (1.0 = affidabile, minore = informativo);
    - ``dynamic regime forecast until``: ultimo giorno previsto (``None`` senza forecast);
    - ``dynamic regime forecast reliable days``: quanti giorni sono considerati affidabili.

    Senza forecast: lista vuota e ``until`` ``None`` (la dashboard omette lo zoom).
    I giorni previsti non entrano mai nei periodi osservati. Solo diagnostica.
    """
    days = state.weather.regime_forecast or []
    return {
        "dynamic regime forecast": [
            {"day": d.day.isoformat(), "regime": d.regime, "lead days": d.lead_days, "weight": d.weight}
            for d in days
        ],
        "dynamic regime forecast until": days[-1].day.isoformat() if days else None,
        "dynamic regime forecast reliable days": FORECAST_RELIABLE_DAYS,
    }


def build_season_attributes(state: SeasonState) -> dict[str, Any]:
    """Attributi di ``extra_state_attributes`` del sensore stagione.

    Compatibilità: le chiavi preesistenti mantengono nome e forma. In particolare
    ``weather signals`` resta un dict di liste a un elemento (HA serializzava già
    il set precedente come lista), così i template di dashboard esistenti non
    cambiano. I valori scalari sono esposti in ``weather signals flat``.

    Fallback: ogni segnale assente (None) produce ``"-"`` nelle liste legacy e
    ``None`` nei valori scalari; nessuna eccezione.
    """
    weather = state.weather
    sig = weather.weather_day_signals
    cloud_pct = sig.cloud * 100.0 if sig.cloud is not None else None

    attributes: dict[str, Any] = {
        # --- stagioni (calendario / operativa / meteo) ---
        "calendar season": state.season.value,
        "operative season": operative_season_of(state).value,
        "weather season": weather.season.value,
        "season mismatch": state.season != weather.season,
        # --- calendario (chiavi preesistenti: stringhe) ---
        "calendar window": f"{state.start.isoformat()} - {state.end.isoformat()}",
        "calendar days": f"{state.days}",
        "calendar days passed": f"{state.passed}",
        "calendar days remaining": f"{state.remaining}",
        # --- calendario (nuovo: numerazione 1-based e % coerenti con il modello) ---
        "calendar day": state.passed + 1,
        "calendar progress pct": state.progress,
        # --- meteo ---
        "weather reason": {
            "detect_model": state.weather_detect_model,
            "anomaly": weather.anomaly,
            "score": round(weather.anomaly_score, 2),
            "cold_snap": weather.cold_snap,
            "cold_snap_score": round(weather.cold_snap_score, 2),
            # Con segno (+ più freddo del normale, − più caldo): permette alla dashboard di mostrare il lato caldo.
            "cold_snap_score_signed": round(weather.cold_snap_score_signed, 2),
            "reason": weather.reason,
        },
        "weather regime": weather.regime_hint,
        # Regime dinamico (RMOT + isteresi + conferma): SHADOW, non guida l'impianto.
        "weather regime dynamic": sig.regime_dynamic,
        "weather regime agree": (
            None if sig.regime_dynamic is None else plant_regime_of(sig.regime_dynamic) == weather.regime_hint
        ),
        "weather signals": {
            "t_low": [_round_temp(sig.t_low)],
            "t_mean": [_round_temp(sig.t_mean)],
            "t_max": [_round_temp(sig.t_max)],
            "dewp": [sig.dew],
            "wind": [sig.wind],
            "cloud": [fnum(cloud_pct)],
        },
        "weather signals flat": {
            "t_low": _round_temp(sig.t_low),
            "t_mean": _round_temp(sig.t_mean),
            "t_max": _round_temp(sig.t_max),
            "dewp": sig.dew,
            "wind": sig.wind,
            "cloud_pct": round(cloud_pct, 1) if cloud_pct is not None else None,
        },
        "weather signals eng.": {
            "smooth": round(sig.t_smooth, 2) if sig.t_smooth is not None else None,
            "dewp": round(sig.dew_smooth, 2) if sig.dew_smooth is not None else None,
            "trend": round(sig.trend, 2) if sig.trend is not None else None,
            "rmot": round(sig.t_rmot, 2) if sig.t_rmot is not None else None,
        },
    }
    # --- astronomica (nuove chiavi, nessuna preesistente modificata) ---
    attributes.update(build_astronomical_attributes(state))
    # --- vista affiancata delle tre stagioni (nuove chiavi) ---
    attributes.update(build_three_seasons_attributes(state))
    # --- finestre dinamiche (ML) degli anni passati: solo diagnostica ---
    attributes.update(build_dynamic_windows_attributes(state))
    attributes.update(build_hot_regime_end_attributes(state))
    attributes.update(build_regime_spans_attributes(state))
    attributes.update(build_regime_periods_attributes(state))
    attributes.update(build_regime_forecast_attributes(state))
    return attributes


def _seasons_agree(state: SeasonState) -> bool:
    """True se astronomica, meteorologica e dinamica coincidono."""
    return not build_three_seasons_attributes(state)["seasons diverging"]


def season_log_signature(state: SeasonState) -> tuple[str, str, str]:
    """Firma (calendario, meteo, sorgente) per emettere il log a livello INFO solo al cambio.

    La sorgente è normalizzata (``fallback_last_known(<data>)`` → ``fallback_last_known``)
    perché la data varierebbe ogni giorno.
    """
    return (state.season.value, state.weather.season.value, dynamic_source_of(state))


def format_season_log_line(state: SeasonState) -> str:
    """Riga di log compatta (una sola) dello stato stagionale, per commissioning."""
    weather = state.weather
    sig = weather.weather_day_signals
    reason = weather.reason
    if len(reason) > LOG_REASON_MAX_CHARS:
        reason = reason[: LOG_REASON_MAX_CHARS - 1] + "…"
    return (
        f"season: calendar={state.season.value} "
        f"operative={operative_season_of(state).value} "
        f"weather={weather.season.value} "
        f"astro={astronomical_window_of(state).season.value} "
        f"mismatch={state.season != weather.season} "
        f"agree={_seasons_agree(state)} "
        f"src={state.detect_model} "
        f"window={state.start.isoformat()}..{state.end.isoformat()} "
        f"day={state.passed + 1}/{state.days} "
        f"regime={weather.regime_hint} "
        f"regime_dyn={sig.regime_dynamic or '-'} "
        f"t_smooth={fnum(sig.t_smooth)} trend={fnum(sig.trend)} rmot={fnum(sig.t_rmot)} "
        f"anomaly={weather.anomaly_score:.2f}/{weather.anomaly} "
        f"why=[{reason}]"
    )
