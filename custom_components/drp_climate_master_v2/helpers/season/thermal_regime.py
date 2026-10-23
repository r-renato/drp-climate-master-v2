"""thermal_regime.py — Regime termico dinamico (RMOT + isteresi + conferma).

Funzioni pure (nessun I/O, nessuna dipendenza da Home Assistant).

Il regime (cold/cool/mild/hot/torrid) descrive *quanto è stata fredda o calda* la settimana
appena trascorsa, perché è ciò che l'edificio "sente" (inerzia termica). Si basa
sulla **Running Mean Outdoor Temperature** (RMOT) della EN 16798-1: media
esponenziale con memoria effettiva di circa 5 giorni (``1 / (1 - alpha)``),
molto più reattiva della media piatta a 28 giorni (``t_smooth``), che resta per
la classificazione ML (scala lenta).

Per non oscillare intorno alle soglie:

- **isteresi**: si entra in "hot"/"cold" a una soglia e si esce a una più
  interna (banda ``REGIME_HYSTERESIS_C``);
- **conferma**: il cambio di regime avviene solo se la condizione persiste per
  ``REGIME_CONFIRM_DAYS`` giorni consecutivi.

Il regime di un giorno dipende solo da quel giorno e dai precedenti (causale):
non è influenzato dal forecast dei giorni successivi.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Final, Mapping, Optional

from ...domain.models.season import DynamicRegime, RegimeForecastDay, RegimeHint, RegimePeriod, RegimeSpan

# ---- RMOT (EN 16798-1) --------------------------------------------------------

RMOT_ALPHA: Final[float] = 0.8
"""Coefficiente di decadimento (EN 16798-1). Memoria effettiva ≈ 1/(1-α) = 5 giorni."""

RMOT_LOOKBACK_DAYS: Final[int] = 30
"""Giorni precedenti considerati: α^30 ≈ 0.001, il resto è trascurabile."""

RMOT_MIN_WEIGHT_COVERAGE: Final[float] = 0.7
"""Frazione minima del peso totale che deve avere dati.

Mancare "ieri" costa il 20% del peso: tollerato. Sotto il 70% la RMOT è None e il
chiamante ricade sul regime da ``t_smooth`` (degradazione controllata).
"""

# ---- Soglie di regime ---------------------------------------------------------

REGIME_COLD_ENTER_MAX_C: Final[float] = 8.0
"""RMOT (o t_smooth) ≤ soglia → regime "cold"."""

REGIME_COOL_ENTER_MAX_C: Final[float] = 14.0
"""RMOT ≤ soglia → regime "cool" (fresco: stagione di riscaldamento, zona D ≈ 150 gg/anno).

Solo regime dinamico (shadow): ``legacy_regime`` e l'impianto non lo usano.
"""

REGIME_HOT_ENTER_MIN_C: Final[float] = 22.0
"""RMOT (o t_smooth) ≥ soglia → regime "hot"."""

REGIME_TORRID_ENTER_MIN_C: Final[float] = 28.0
"""RMOT ≥ soglia → regime "torrid" (ondata di calore, sopra "hot").

Solo regime dinamico (shadow). A Roma (medie mensili lug-ago 26-29 °C) la soglia dà
5-20 giorni/anno negli anni normali e circa 40 nell'estate 2026; ≥ 27 °C sarebbe un
secondo "hot", ≥ 30 °C quasi mai raggiunta. Da rivedere ogni 2-3 anni (riscaldamento climatico).
"""

REGIME_HYSTERESIS_C: Final[float] = 1.0
"""Banda di isteresi: si esce da "cold" sopra soglia+banda e da "hot" sotto soglia-banda."""

REGIME_COLD_EXIT_MIN_C: Final[float] = REGIME_COLD_ENTER_MAX_C + REGIME_HYSTERESIS_C
REGIME_COOL_EXIT_MIN_C: Final[float] = REGIME_COOL_ENTER_MAX_C + REGIME_HYSTERESIS_C
REGIME_HOT_EXIT_MAX_C: Final[float] = REGIME_HOT_ENTER_MIN_C - REGIME_HYSTERESIS_C
REGIME_TORRID_EXIT_MAX_C: Final[float] = REGIME_TORRID_ENTER_MIN_C - REGIME_HYSTERESIS_C

_REGIME_ORDER: Final[tuple[DynamicRegime, ...]] = ("cold", "cool", "mild", "hot", "torrid")
"""Scala termica crescente: l'indice è il livello usato dall'isteresi."""

REGIME_CONFIRM_DAYS: Final[int] = 2
"""Giorni consecutivi richiesti per confermare un cambio di regime."""

METEO_YEAR_START_MONTH: Final[int] = 12
"""L'anno meteo va dal 1/12 al 30/11: dicembre appartiene all'anno successivo."""


def legacy_regime(value: Optional[float]) -> RegimeHint:
    """Regime senza isteresi né conferma (comportamento storico, su ``t_smooth``)."""
    regime: RegimeHint = "mild"
    if value is not None:
        if value <= REGIME_COLD_ENTER_MAX_C:
            regime = "cold"
        elif value >= REGIME_HOT_ENTER_MIN_C:
            regime = "hot"
    return regime


def compute_rmot(
    daily_mean: Mapping[date, float],
    day: date,
    *,
    alpha: float = RMOT_ALPHA,
    lookback_days: int = RMOT_LOOKBACK_DAYS,
    min_weight_coverage: float = RMOT_MIN_WEIGHT_COVERAGE,
) -> Optional[float]:
    """RMOT del giorno ``day``: media esponenziale dei giorni *precedenti*.

    ``RMOT(d) = Σ_k w_k · T(d-k) / Σ_k w_k`` con ``w_k = (1-α)·α^(k-1)``, k = 1..N
    (k = 1 è ieri), calcolata solo sui giorni disponibili: un buco non azzera la
    media, ne rinormalizza i pesi. Se i giorni disponibili coprono meno di
    ``min_weight_coverage`` del peso totale ritorna ``None``.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    available_weight = 0.0
    for k in range(1, lookback_days + 1):
        weight = (1.0 - alpha) * alpha ** (k - 1)
        total_weight += weight
        value = daily_mean.get(day - timedelta(days=k))
        if value is None:
            continue
        available_weight += weight
        weighted_sum += weight * float(value)
    if total_weight <= 0.0 or available_weight / total_weight < min_weight_coverage:
        return None
    return weighted_sum / available_weight


def plant_regime_of(regime: DynamicRegime) -> RegimeHint:
    """Equivalente a 3 livelli ("cool" → "mild", "torrid" → "hot") per il confronto con l'impianto."""
    if regime == "cool":
        return "mild"
    return "hot" if regime == "torrid" else regime


def _target_regime(state: DynamicRegime, value: float) -> DynamicRegime:
    """Regime "desiderato" dato lo stato corrente e la RMOT, con isteresi.

    Quattro confini ordinati: cold|cool, cool|mild (zona bassa: si entra a ``≤ ENTER``
    e si resta fino a ``≤ EXIT``), mild|hot e hot|torrid (zona alta: si entra a
    ``≥ ENTER`` e si resta fino a ``≥ EXIT``). Il livello è il numero di confini
    superati, quindi un salto di più livelli (es. torrid → cold) è possibile in un passo.
    """
    level = _REGIME_ORDER.index(state)
    below_cold = value <= (REGIME_COLD_EXIT_MIN_C if level == 0 else REGIME_COLD_ENTER_MAX_C)
    below_cool = value <= (REGIME_COOL_EXIT_MIN_C if level <= 1 else REGIME_COOL_ENTER_MAX_C)
    above_hot = value >= (REGIME_HOT_EXIT_MAX_C if level >= 3 else REGIME_HOT_ENTER_MIN_C)
    above_torrid = value >= (REGIME_TORRID_EXIT_MAX_C if level == 4 else REGIME_TORRID_ENTER_MIN_C)
    return _REGIME_ORDER[(not below_cold) + (not below_cool) + above_hot + above_torrid]


def _initial_regime(value: float) -> DynamicRegime:
    """Regime del primo giorno con RMOT valida (nessuna storia, nessuna isteresi)."""
    return _REGIME_ORDER[
        (value > REGIME_COLD_ENTER_MAX_C) + (value > REGIME_COOL_ENTER_MAX_C) + (value >= REGIME_HOT_ENTER_MIN_C)
        + (value >= REGIME_TORRID_ENTER_MIN_C)
    ]


def classify_regimes(
    rmot_by_day: Mapping[date, Optional[float]],
    *,
    confirm_days: int = REGIME_CONFIRM_DAYS,
) -> dict[date, Optional[DynamicRegime]]:
    """Regime dinamico per ogni giorno, ripercorrendo la serie in ordine cronologico.

    - Primo giorno con RMOT valida: classificazione diretta (nessuna storia).
    - Poi: il regime cambia solo dopo ``confirm_days`` giorni consecutivi in cui
      il regime desiderato (con isteresi) è lo stesso e diverso da quello corrente.
    - Giorno con RMOT ``None``: regime ``None`` (indisponibile), stato e conferma
      in corso invariati.

    Deterministico e senza stato persistente: la serie intera ricostruisce
    sempre lo stesso risultato, anche dopo un riavvio.
    """
    out: dict[date, Optional[DynamicRegime]] = {}
    state: Optional[DynamicRegime] = None
    pending: Optional[DynamicRegime] = None
    pending_days = 0

    for day in sorted(rmot_by_day):
        value = rmot_by_day[day]
        if value is None:
            out[day] = None
            continue
        if state is None:
            state = _initial_regime(float(value))
        else:
            target = _target_regime(state, float(value))
            if target == state:
                pending, pending_days = None, 0
            elif target == pending:
                pending_days += 1
            else:
                pending, pending_days = target, 1
            if pending is not None and pending_days >= confirm_days:
                state, pending, pending_days = pending, None, 0
        out[day] = state
    return out


def meteo_year_of(day: date) -> int:
    """Anno meteo (dic→nov) di una data: dicembre appartiene all'anno successivo."""
    return day.year + 1 if day.month == METEO_YEAR_START_MONTH else day.year


def regime_end_by_year(
    regime_by_day: Mapping[date, Optional[DynamicRegime]],
    *,
    regime: DynamicRegime = "hot",
    until: Optional[date] = None,
) -> dict[int, date]:
    """Primo giorno dopo l'ultimo periodo in ``regime``, per anno meteo.

    Per ogni anno meteo si cerca l'ultimo giorno nel regime dato (default "hot") e
    si restituisce il primo giorno successivo con regime noto e diverso: è la data
    in cui quel regime è davvero finito, anche dopo più ondate di calore.

    Un anno è assente se il regime non si è mai verificato, oppure è ancora in
    corso all'ultimo giorno noto (nessuna data di fine è ancora osservata).

    ``until`` esclude i giorni successivi (di norma oggi): i giorni del forecast
    non devono produrre una "fine" che non è ancora avvenuta.
    """
    by_year: dict[int, list[tuple[date, DynamicRegime]]] = {}
    for day in sorted(regime_by_day):
        value = regime_by_day[day]
        if value is None or (until is not None and day > until):
            continue
        if regime == "hot" and value == "torrid":
            value = "hot"  # "torrid" è un sottolivello di "hot": non interrompe la stagione calda
        by_year.setdefault(meteo_year_of(day), []).append((day, value))

    out: dict[int, date] = {}
    for year, series in by_year.items():
        last_idx = max((i for i, (_, r) in enumerate(series) if r == regime), default=None)
        if last_idx is None or last_idx == len(series) - 1:
            continue
        out[year] = series[last_idx + 1][0]
    return out


FORECAST_RELIABLE_DAYS: Final[int] = 5
"""Giorni di forecast considerati affidabili (primi N): candidati alla gestione futura."""

FORECAST_WEIGHT_RELIABLE: Final[float] = 1.0
"""Peso dei giorni di forecast affidabili."""

FORECAST_WEIGHT_INFORMATIVE: Final[float] = 0.3
"""Peso dei giorni di forecast oltre ``FORECAST_RELIABLE_DAYS``: solo informativi."""

REGIME_SPAN_TRACKED: Final[tuple[DynamicRegime, ...]] = ("hot", "cold")
"""Regimi con confini propri. "mild" è il residuo tra le transizioni: nessun confine."""


def regime_periods(
    regime_by_day: Mapping[date, Optional[DynamicRegime]],
    *,
    until: Optional[date] = None,
) -> list[RegimePeriod]:
    """Periodi continui in ciascun regime (cold/cool/mild/hot/torrid), in ordine cronologico.

    Sequenze di giorni consecutivi con lo stesso regime; i giorni con regime
    ``None`` sono ignorati (non spezzano un periodo). ``end`` è il primo giorno
    successivo in un regime diverso, ``None`` se il periodo è in corso all'ultimo
    giorno noto. ``start_known`` è ``False`` per il primo periodo della serie: lo
    storico può iniziare dentro il regime. ``until`` esclude i giorni successivi
    (di norma oggi): il forecast non deve produrre periodi non ancora avvenuti.
    """
    series: list[tuple[date, DynamicRegime]] = []
    for day in sorted(regime_by_day):
        value = regime_by_day[day]
        if value is not None and (until is None or day <= until):
            series.append((day, value))

    out: list[RegimePeriod] = []
    i = 0
    while i < len(series):
        j = i
        while j + 1 < len(series) and series[j + 1][1] == series[i][1]:
            j += 1
        out.append(
            RegimePeriod(
                regime=series[i][1],
                start=series[i][0],
                end=series[j + 1][0] if j + 1 < len(series) else None,
                start_known=i > 0,
                days=j - i + 1,
            )
        )
        i = j + 1
    return out


def regime_spans_by_year(
    regime_by_day: Mapping[date, Optional[DynamicRegime]],
    *,
    until: Optional[date] = None,
    regimes: tuple[DynamicRegime, ...] = REGIME_SPAN_TRACKED,
) -> dict[int, dict[str, RegimeSpan]]:
    """Inizio e fine dei periodi hot/cold per anno meteo.

    Sintesi di ``regime_periods``: ogni periodo è attribuito all'anno meteo del suo
    **inizio** (le date di fine possono cadere nell'anno successivo). Per anno e
    regime: inizio del primo periodo, fine dell'ultimo (``None`` se in corso),
    numero di periodi e giorni totali. Regimi mai verificati: assenti.
    """
    grouped: dict[int, dict[str, list[RegimePeriod]]] = {}
    if "torrid" not in regimes:  # "torrid" è un sottolivello di "hot": non frammenta i periodi hot
        regime_by_day = {d: ("hot" if v == "torrid" else v) for d, v in regime_by_day.items()}
    for period in regime_periods(regime_by_day, until=until):
        if period.regime in regimes:
            grouped.setdefault(meteo_year_of(period.start), {}).setdefault(period.regime, []).append(period)

    return {
        year: {
            regime: RegimeSpan(
                start=items[0].start,
                end=items[-1].end,
                start_known=items[0].start_known,
                periods=len(items),
                days=sum(it.days for it in items),
            )
            for regime, items in by_regime.items()
        }
        for year, by_regime in grouped.items()
    }


def forecast_weight(lead_days: int) -> float:
    """Peso di affidabilità di un giorno di forecast in base all'anticipo (1 = domani)."""
    return FORECAST_WEIGHT_RELIABLE if lead_days <= FORECAST_RELIABLE_DAYS else FORECAST_WEIGHT_INFORMATIVE


def regime_forecast_days(
    regime_by_day: Mapping[date, Optional[DynamicRegime]],
    *,
    today: date,
) -> list[RegimeForecastDay]:
    """Regime dinamico dei giorni successivi a ``today``, con anticipo e peso.

    Usa lo stesso regime causale dei giorni osservati (RMOT + isteresi + conferma)
    calcolato sulle temperature previste. Giorni senza regime noto (forecast
    incompleto) sono saltati: nessuna eccezione, la lista può essere vuota.
    Solo diagnostica: questi giorni non entrano mai nei periodi osservati.
    """
    out: list[RegimeForecastDay] = []
    for day in sorted(regime_by_day):
        value = regime_by_day[day]
        if day <= today or value is None:
            continue
        lead = (day - today).days
        out.append(RegimeForecastDay(day=day, regime=value, lead_days=lead, weight=forecast_weight(lead)))
    return out
