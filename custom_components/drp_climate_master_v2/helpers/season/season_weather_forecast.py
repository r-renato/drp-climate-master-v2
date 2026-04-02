from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union, cast
import logging
import math

from ...domain.models.season import RegimeHint, WeatherSeason, WeatherDaySignals

from ...domain.models.weather import Forecast, Historical

from ..logger import log_warning
from ...domain.models.season import Seasons
from .season_weather_calendar import CalendarSeason

_LOGGER = logging.getLogger(__name__)

DateLike = Union[date, datetime]

# Forecast = il tuo TypedDict

# Mappa più comune tra payload "presentational/legacy" e payload "native"
_LEGACY_TO_NATIVE: dict[str, str] = {
    "precipitation": "native_precipitation",
    "pressure": "native_pressure",
    "temperature": "native_temperature",
    "templow": "native_templow",
    "wind_speed": "native_wind_speed",
    # spesso compare anche nei payload (anche se non nel tuo TypedDict legacy)
    "wind_gust_speed": "native_wind_gust_speed",
    "apparent_temperature": "native_apparent_temperature",
    "dew_point": "native_dew_point",
}

_LEGACY_KEYS = set(_LEGACY_TO_NATIVE.keys())


def forecast_legacy_to_native(
    item: Mapping[str, Any],
    *,
    drop_legacy: bool = False,
) -> Forecast:
    """Normalize a forecast entry to `native_*` keys.

    Name mapping only (no unit conversion).
    """
    out: dict[str, Any] = dict(item)

    for legacy_key, native_key in _LEGACY_TO_NATIVE.items():
        native_val = out.get(native_key)
        legacy_val = out.get(legacy_key)

        if (native_val is None) and (legacy_val is not None):
            out[native_key] = legacy_val

        if drop_legacy and legacy_key in out:
            out.pop(legacy_key, None)

    return cast(Forecast, out)


# ------------------------------- config --------------------------------------


@dataclass(frozen=True, slots=True)
class MeteoSeasonConfig:
    """Modello orientato a *stagioni meteorologiche* (regimi lenti).

    - Stagioni = segmenti lunghi e contigui.
    - Anomalie = eventi brevi dentro al regime.
    - Supporta inferenza "live" su giorni non etichettati (es. inizio anno / solo forecast).
    """

    # smoothing di regime
    rolling_days: int = 28
    rolling_min_frac: float = 0.85

    # trend (delta tra medie smussate distanti)
    trend_days: int = 28

    # segmentazione annuale DP: limiti di sicurezza
    max_days_per_year: int = 370

    # durata minima per stagione meteorologica
    min_season_days: int = 60

    # pesi del costo in z-space robusto
    w_temp: float = 1.0
    w_trend: float = 0.35
    w_dew: float = 0.15

    # anomalie: distanza robusta dal regime del segmento (storico) o dal prototipo (live)
    anomaly_z: float = 2.8

    # cold_snap: deviazione intra-stagionale firmata di t_smooth rispetto al prototipo.
    #
    # Formula: (μ_season[t] - z_t) / σ_season[t]
    #   Positivo = t_smooth è SOTTO il prototipo stagionale → giorno freddo per la stagione.
    #   Negativo = t_smooth è SOPRA il prototipo → giorno caldo per la stagione.
    #
    # Soglia 0.0  → metà fredda della distribuzione stagionale (50° percentile dal lato freddo).
    # Soglia 0.5  → top ~25% giorni freddi della stagione (evento moderato).
    # Soglia 1.0  → top ~5–10% (cold snap chiaro, come una settimana di Marzo con T<10°C a Roma).
    # Soglia 1.5  → solo eventi severi (~2% dei giorni stagionali).
    #
    # NOTA: cold_snap_z è molto più bassa di anomaly_z (2.8) perché misura
    # un concetto diverso: non "siamo in una stagione sbagliata" ma
    # "siamo nel lato freddo della stagione corretta".
    # Calibrazione consigliata su dati storici reali con calibrate_cold_snap.py.
    cold_snap_z: float = 0.0

    # se anno troppo incompleto, viene ignorato dalla segmentazione (stabilità)
    min_days_per_year: int = 300

    # inferenza live: prior meteorologico e isteresi (ex-getattr con fallback hardcoded)
    prior_penalty: float = 0.75
    expected_margin: float = 0.35
    switch_gap_min: float = 0.25

    def __post_init__(self) -> None:
        if self.prior_penalty < 0:
            raise ValueError("prior_penalty must be >= 0")
        if self.expected_margin < 0:
            raise ValueError("expected_margin must be >= 0")
        if self.switch_gap_min < 0:
            raise ValueError("switch_gap_min must be >= 0")
        # cold_snap_z può essere negativa (cattura anche il lato caldo, utile per debug),
        # ma valori < -3.0 sarebbero semanticamente privi di senso.
        if self.cold_snap_z < -3.0:
            raise ValueError("cold_snap_z must be >= -3.0")

# ----------------------------- main model ------------------------------------


class MeteoContiguousSeasonModel:
    """Segmentazione stagioni meteorologiche per anno + inferenza live.

    Segmentazione (storico):
      - per ogni anno sufficientemente completo: 4 segmenti contigui con 3 breakpoints
      - durata minima per segmento
      - etichette determinate da statistiche di segmento (winter=min, summer=max)

    Inferenza live:
      - se un giorno non è presente in output segmentato (tipico a inizio anno),
        prova a inferire una stagione usando prototipi robusti imparati dallo storico.
      - per giorni successivi all'ultimo etichettato: limita i candidati a "stessa stagione" o "next" nel ciclo
        per evitare salti implausibili.
    """

    _CYCLE: Tuple[Seasons, ...] = (
        Seasons.WINTER,
        Seasons.SPRING,
        Seasons.SUMMER,
        Seasons.AUTUMN,
    )

    def __init__(self, cfg: MeteoSeasonConfig = MeteoSeasonConfig()) -> None:
        self._cfg = cfg
        self._signals: Dict[date, WeatherDaySignals] = {}
        self._out: Dict[date, WeatherSeason] = {}

        # robust scale globale per features [t_smooth, trend, dew_smooth]
        self._global_med: List[float] = []
        self._global_iqr: List[float] = []

        # prototipi per inferenza live (per stagione, in z-space)
        self._infer_mu: Dict[Seasons, List[float]] = {}
        self._infer_sig: Dict[Seasons, List[float]] = {}

    # -------------------------- public API -----------------------------------

    def fit(self, history: dict[str, Historical]) -> "MeteoContiguousSeasonModel":
        daily = self._parse_history(history)
        if len(daily) < self._cfg.rolling_days + self._cfg.trend_days + 120:
            raise ValueError("Not enough daily data to fit reliably")

        self._signals = self._compute_signals(daily)
        self._fit_global_scale()
        self._out = self._segment_all_years()

        # prototipi per inferenza live (usa i giorni già etichettati)
        self._fit_infer_prototypes()

        # post-pass: calcola cold_snap per tutti i giorni segmentati.
        # Deve essere DOPO _fit_infer_prototypes() perché richiede _infer_mu/_infer_sig.
        self._out = self._apply_cold_snap(self._out)
        return self

    def day(self, d: DateLike) -> Optional[WeatherSeason]:
        dd = self._to_date(d)
        return self._out.get(dd)

    def infer(
        self,
        d: DateLike,
        *,
        expected_season: Optional[Seasons] = None,
    ) -> Optional[WeatherSeason]:
        """
        Inferenza stagionale per giorni fuori copertura segmentata.

        - Usa costo robusto dei prototipi (raw_cost)
        - Applica un prior meteorologico debole (prior_penalty) se la stagione != expected_season
        - Tie-break: se expected è entro expected_margin dal best, scegli expected
        - Isteresi: evita flip-flop SOLO quando expected coincide con prev (non blocca transizioni attese)
        """
        dd = self._to_date(d)

        # Se già segmentato, ritorna quello
        if dd in self._out:
            return self._out[dd]

        sig = self._signals.get(dd)
        if sig is None:
            return None

        # ---------------- expected season ----------------
        # Se non fornita, delega a CalendarSeason (DJF/MAM/JJA/SON, corretta per Dicembre)
        if expected_season is None:
            expected_season = CalendarSeason.for_date(dd).season_for(dd)

        # ---------------- candidates ----------------
        candidates: List[Seasons] = list(self._CYCLE)
        prev_season: Optional[Seasons] = None

        if self._out:
            last = max(self._out)
            if dd > last:
                prev_season = self._out[last].season
                candidates = [prev_season, self._next_in_cycle(prev_season)]

                # IMPORTANT: se expected NON è tra i candidati, aggiungilo comunque
                # (risolve casi tipo: prev=Autumn ma siamo in pieno inverno meteorologico)
                if expected_season not in candidates:
                    candidates.append(expected_season)

        # ---------------- tunables ----------------
        prior_penalty = self._cfg.prior_penalty
        expected_margin = self._cfg.expected_margin
        switch_gap_min = self._cfg.switch_gap_min

        # ---------------- score ----------------
        # total_cost = raw_cost + prior (prior = 0 se season==expected)
        scored: List[Tuple[float, float, Seasons]] = []  # (total_cost, raw_cost, season)
        for s in candidates:
            raw = float(self._infer_cost(s, sig))
            prior = 0.0 if s == expected_season else prior_penalty
            total = raw + prior
            scored.append((total, raw, s))

        scored.sort(key=lambda x: x[0])
        best_total, best_raw, best_season = scored[0]

        # tie-break pro-expected (molto utile a Gennaio nel tuo caso)
        exp_tuple = next((t for t in scored if t[2] == expected_season), None)
        if exp_tuple is not None:
            exp_total, exp_raw, _ = exp_tuple
            if (exp_total - best_total) <= expected_margin:
                best_total, best_raw, best_season = exp_total, exp_raw, expected_season

        # isteresi: NON bloccare il passaggio verso expected quando expected != prev
        if prev_season is not None and best_season != prev_season:
            # applica isteresi solo se expected coincide col prev (caso flip-flop interno stagione)
            if expected_season == prev_season:
                # gap totale tra best e secondo best
                second_total = scored[1][0] if len(scored) > 1 else float("inf")
                gap_tot = float(second_total - best_total) if math.isfinite(second_total) else 0.0
                if gap_tot < switch_gap_min:
                    # resta su prev
                    prev_raw = float(self._infer_cost(prev_season, sig))
                    best_season = prev_season
                    best_raw = prev_raw
                    best_total = prev_raw + (0.0 if prev_season == expected_season else prior_penalty)

        # anomaly: usa raw cost (non total)
        anomaly_score = float(best_raw)
        anomaly = anomaly_score >= self._cfg.anomaly_z

        regime_hint = self._regime_hint(sig)

        # cold_snap: deviazione intra-stagionale rispetto al prototipo della stagione scelta.
        # Calcolato dopo aver scelto best_season, non prima (per coerenza).
        cs_score = self._cold_snap_score(best_season, sig)
        cold_snap = cs_score >= self._cfg.cold_snap_z

        cand_str = ", ".join(
            f"{(s.value if hasattr(s,'value') else s)}:raw={raw:.2f},tot={tot:.2f}"
            for (tot, raw, s) in scored
        )
        reason = (
            f"infer: expected={expected_season} chosen={best_season} raw={best_raw:.2f} tot={best_total:.2f} "
            f"candidates=[{cand_str}]"
            + ("; anomaly" if anomaly else "")
            + (f"; cold_snap={cs_score:+.2f}" if cold_snap else f"; cs={cs_score:+.2f}")
        )

        return WeatherSeason(
            season=best_season,
            anomaly=anomaly,
            anomaly_score=anomaly_score,
            reason=reason,
            regime_hint=regime_hint,
            weather_day_signals=sig,
            cold_snap=cold_snap,
            cold_snap_score=max(0.0, cs_score),
        )


    def season_for(self, d: DateLike) -> Optional[Seasons]:
        x = self.day(d)
        return x.season if x else None

    def all(self) -> Mapping[date, WeatherSeason]:
        return self._out

    def windows(self) -> List[Tuple[date, date, Seasons]]:
        if not self._out:
            return []
        days = sorted(self._out)
        start = days[0]
        cur = self._out[start].season
        prev = start
        out: List[Tuple[date, date, Seasons]] = []
        for dd in days[1:]:
            s = self._out[dd].season
            if s != cur:
                out.append((start, prev, cur))
                start = dd
                cur = s
            prev = dd
        out.append((start, prev, cur))
        return out

    def coverage(self) -> Tuple[Optional[date], Optional[date], int]:
        """Copertura dell'output segmentato."""
        if not self._out:
            return None, None, 0
        days = sorted(self._out)
        return days[0], days[-1], len(days)

    def signals_coverage(self) -> Tuple[Optional[date], Optional[date], int]:
        """Copertura dei segnali calcolati."""
        if not self._signals:
            return None, None, 0
        days = sorted(self._signals)
        return days[0], days[-1], len(days)

    # --------------------------- parsing -------------------------------------

    @staticmethod
    def _to_date(d: DateLike) -> date:
        return d.date() if isinstance(d, datetime) else d

    @staticmethod
    def _parse_date_key(key: str) -> date:
        return date.fromisoformat(key)

    def _parse_history(self, history: dict[str, Historical]) -> Dict[date, Dict[str, Any]]:
        out: Dict[date, Dict[str, Any]] = {}
        for k, v in history.items():
            try:
                dd = self._parse_date_key(k)
            except Exception:
                dt_str = str(v.get("datetime"))
                try:
                    dd = datetime.fromisoformat(dt_str).date()
                except Exception:
                    log_warning(_LOGGER, "Skipping invalid day key=%s datetime=%s", k, dt_str)
                    continue
            out[dd] = dict(v)
        return dict(sorted(out.items(), key=lambda x: x[0]))

    # ------------------------- engineering -----------------------------------

    @staticmethod
    def _is_failed_row(row: Mapping[str, Any]) -> bool:
        failed = row.get("failed")
        if failed is None:
            return False
        if isinstance(failed, bool):
            return failed
        if isinstance(failed, (int, float)):
            return bool(failed)
        if isinstance(failed, str):
            s = failed.strip().lower()
            if s in ("", "0", "false", "no", "none", "null"):
                return False
            return True
        return bool(failed)

    def _compute_signals(self, daily: Mapping[date, Dict[str, Any]]) -> Dict[date, WeatherDaySignals]:
        days = sorted(daily)

        T: Dict[date, float] = {}
        Tlow: Dict[date, Optional[float]] = {}
        Dew: Dict[date, Optional[float]] = {}
        Wind: Dict[date, Optional[float]] = {}
        Cloud: Dict[date, Optional[float]] = {}

        for dd in days:
            row = daily[dd]
            if self._is_failed_row(row):
                continue
            if row.get("native_temperature") is None:
                # per meteo-model, senza temperatura non possiamo fare nulla
                continue

            T[dd] = float(row["native_temperature"])
            Tlow[dd] = float(row["native_templow"]) if row.get("native_templow") is not None else None
            Dew[dd] = float(row["native_dew_point"]) if row.get("native_dew_point") is not None else None
            Wind[dd] = float(row["native_wind_speed"]) if row.get("native_wind_speed") is not None else None

            cc = row.get("cloud_coverage")
            if cc is None:
                Cloud[dd] = None
            else:
                ccf = float(cc)
                Cloud[dd] = (ccf / 100.0) if ccf > 1.0 else ccf

        def _min_required(n: int, frac: float) -> int:
            frac = max(0.0, min(1.0, frac))
            return max(1, int(math.ceil(n * frac)))

        def rolling_mean(series: Mapping[date, float], end: date, n: int, min_frac: float) -> Optional[float]:
            vals: List[float] = []
            for i in range(n):
                di = end - timedelta(days=(n - 1 - i))
                v = series.get(di)
                if v is None:
                    continue
                vals.append(float(v))
            if len(vals) < _min_required(n, min_frac):
                return None
            return sum(vals) / len(vals)

        def rolling_mean_opt(series: Mapping[date, Optional[float]], end: date, n: int, min_frac: float) -> Optional[float]:
            vals: List[float] = []
            for i in range(n):
                di = end - timedelta(days=(n - 1 - i))
                if di not in series:
                    continue
                v = series[di]
                if v is None:
                    continue
                vals.append(float(v))
            if len(vals) < _min_required(n, min_frac):
                return None
            return sum(vals) / len(vals)

        out: Dict[date, WeatherDaySignals] = {}
        for dd in days:
            row = daily[dd]
            if self._is_failed_row(row):
                continue
            if dd not in T:
                continue

            t_sm = rolling_mean(T, dd, self._cfg.rolling_days, self._cfg.rolling_min_frac)
            dew_sm = rolling_mean_opt(Dew, dd, self._cfg.rolling_days, self._cfg.rolling_min_frac)

            trend: Optional[float] = None
            if t_sm is not None:
                prev = dd - timedelta(days=self._cfg.trend_days)
                prev_sm = rolling_mean(T, prev, self._cfg.rolling_days, self._cfg.rolling_min_frac)
                if prev_sm is not None:
                    trend = t_sm - prev_sm

            out[dd] = WeatherDaySignals(
                d=dd,
                t_mean=T[dd],
                t_low=Tlow.get(dd),
                dew=Dew.get(dd),
                wind=Wind.get(dd),
                cloud=Cloud.get(dd),
                t_smooth=t_sm,
                dew_smooth=dew_sm,
                trend=trend,
            )

        return out

    # ------------------------- robust stats ----------------------------------

    @staticmethod
    def _median(xs: Sequence[float]) -> float:
        ys = sorted(xs)
        n = len(ys)
        if n == 0:
            raise ValueError("empty")
        mid = n // 2
        if n % 2:
            return ys[mid]
        return 0.5 * (ys[mid - 1] + ys[mid])

    @staticmethod
    def _quantile(xs: Sequence[float], q: float) -> float:
        ys = sorted(xs)
        n = len(ys)
        if n == 0:
            raise ValueError("empty")
        if q <= 0:
            return ys[0]
        if q >= 1:
            return ys[-1]
        pos = (n - 1) * q
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return ys[lo]
        w = pos - lo
        return ys[lo] * (1 - w) + ys[hi] * w

    def _robust_scale_optional(self, values: Sequence[List[Optional[float]]]) -> Tuple[List[float], List[float]]:
        if not values:
            raise ValueError("no values")
        d = len(values[0])
        cols: List[List[float]] = [[] for _ in range(d)]
        for v in values:
            if len(v) != d:
                raise ValueError("inconsistent vector length")
            for i, x in enumerate(v):
                if x is None:
                    continue
                if isinstance(x, float) and math.isnan(x):
                    continue
                cols[i].append(float(x))

        med: List[float] = []
        iqr: List[float] = []
        for c in cols:
            if not c:
                med.append(0.0)
                iqr.append(1.0)
                continue
            m = self._median(c)
            q25 = self._quantile(c, 0.25)
            q75 = self._quantile(c, 0.75)
            med.append(m)
            iqr.append(max(1e-6, q75 - q25))
        return med, iqr

    # ------------------------- feature vector --------------------------------

    def _vec(self, s: WeatherDaySignals) -> Optional[List[Optional[float]]]:
        # meteo vector: [T_smooth, Trend?, Dew_smooth?]
        if s.t_smooth is None:
            return None
        return [
            float(s.t_smooth),
            (float(s.trend) if s.trend is not None else None),
            (float(s.dew_smooth) if s.dew_smooth is not None else None),
        ]

    def _fit_global_scale(self) -> None:
        vecs: List[List[Optional[float]]] = []
        for sig in self._signals.values():
            v = self._vec(sig)
            if v is None:
                continue
            vecs.append(v)
        if len(vecs) < 200:
            raise ValueError("Insufficient feature vectors")
        self._global_med, self._global_iqr = self._robust_scale_optional(vecs)

    def _z(self, v: List[Optional[float]]) -> List[float]:
        out: List[float] = []
        for i, x in enumerate(v):
            if x is None:
                out.append(float("nan"))
            else:
                out.append((float(x) - self._global_med[i]) / self._global_iqr[i])
        return out

    def _regime_hint(self, sig: WeatherDaySignals) -> RegimeHint:
        regime_hint = "mild"
        if sig.t_smooth is not None:
            if sig.t_smooth <= 8.0:
                regime_hint = "cold"
            elif sig.t_smooth >= 22.0:
                regime_hint = "hot"
        return regime_hint

    # ------------------------- infer prototypes ------------------------------

    def _next_in_cycle(self, s: Seasons) -> Seasons:
        cycle = list(self._CYCLE)
        i = cycle.index(s)
        return cycle[(i + 1) % len(cycle)]

    @staticmethod
    def _djf_buckets_for_year(
        y: int,
        signals: "Dict[date, WeatherDaySignals]",
    ) -> "Dict[Seasons, List[date]]":
        """Giorni del meteo-year y assegnati ai confini DJF canonici (emisfero nord).

        I confini sono fissi e non dipendono dalla segmentazione DP:
          WINTER : [1 Dic (y-1) .. ultimo Feb (y)]
          SPRING : [1 Mar (y)   .. 31 Mag (y)]
          SUMMER : [1 Giu (y)   .. 31 Ago (y)]
          AUTUMN : [1 Set (y)   .. 30 Nov (y)]

        Usato da _fit_infer_prototypes per ancorare i prototipi live ai regimi
        climatologici DJF indipendentemente da dove il DP ha posizionato i breakpoints.
        """
        import calendar as _cal
        feb_last = _cal.monthrange(y, 2)[1]
        windows_djf: Dict[Seasons, Tuple[date, date]] = {
            Seasons.WINTER: (date(y - 1, 12, 1), date(y, 2, feb_last)),
            Seasons.SPRING: (date(y, 3, 1),       date(y, 5, 31)),
            Seasons.SUMMER: (date(y, 6, 1),       date(y, 8, 31)),
            Seasons.AUTUMN: (date(y, 9, 1),       date(y, 11, 30)),
        }
        out: Dict[Seasons, List[date]] = {s: [] for s in Seasons}
        for s, (lo, hi) in windows_djf.items():
            out[s] = [d for d in signals if lo <= d <= hi]
        return out

    def _fit_infer_prototypes(self) -> None:
        """Prototipi per inferenza live ancorati ai confini DJF/MAM/JJA/SON canonici.

        Usa i segnali nei confini stagionali fissi (DJF/MAM/JJA/SON) di ogni
        meteo-year segmentato, ignorando dove il DP ha posizionato i breakpoints.
        Questo rende i prototipi stabili anche quando il DP allarga il segmento
        WINTER oltre Febbraio (es. Dicembre mite + Marzo fresco sullo stesso cluster).

        Fallback: se un bucket DJF ha meno di 20 giorni di segnale, si usa il
        bucket etichettato dal DP (comportamento precedente).
        """
        # --- bucket DJF canonici da tutti i meteo-year segmentati ---
        buckets: Dict[Seasons, List[List[Optional[float]]]] = {s: [] for s in self._CYCLE}

        meteo_years = self._meteo_years_present()
        djf_used = False
        for y in meteo_years:
            lo, hi = self._meteo_year_bounds(y)
            # solo anni effettivamente segmentati (presenti in self._out)
            if not any(lo <= dd <= hi for dd in self._out):
                continue
            day_buckets = self._djf_buckets_for_year(y, self._signals)
            for s, days in day_buckets.items():
                for dd in days:
                    if dd not in self._signals:
                        continue
                    v = self._vec(self._signals[dd])
                    if v is not None:
                        buckets[s].append(v)
                        djf_used = True

        # fallback: se nessun dato DJF disponibile, usa etichette DP (caso degenere)
        if not djf_used:
            log_warning(
                _LOGGER,
                "_fit_infer_prototypes: nessun dato DJF disponibile, fallback su etichette DP.",
            )
            for dd, ds in self._out.items():
                v = self._vec(ds.weather_day_signals)
                if v is not None:
                    buckets[ds.season].append(v)

        for season in self._CYCLE:
            vs = buckets[season]
            if len(vs) < 20:
                # fallback neutro
                self._infer_mu[season] = [0.0, 0.0, 0.0]
                self._infer_sig[season] = [1.0, 1.0, 1.0]
                continue

            zs = [self._z(v) for v in vs]
            d = len(zs[0])
            mu: List[float] = []
            sigma: List[float] = []

            for i in range(d):
                col = [p[i] for p in zs if not math.isnan(p[i])]
                if len(col) < 10:
                    mu.append(0.0)
                    sigma.append(1.0)
                    continue
                m = self._median(col)
                q25 = self._quantile(col, 0.25)
                q75 = self._quantile(col, 0.75)
                sdev = max(0.15, (q75 - q25) / 1.349)
                mu.append(m)
                sigma.append(sdev)

            self._infer_mu[season] = mu
            self._infer_sig[season] = sigma

    def _infer_cost(self, season: Seasons, sig: WeatherDaySignals) -> float:
        v = self._vec(sig)
        if v is None:
            return 1e6
        vz = self._z(v)
        mu = self._infer_mu.get(season)
        sd = self._infer_sig.get(season)
        if mu is None or sd is None:
            return 1e6

        w = [self._cfg.w_temp, self._cfg.w_trend, self._cfg.w_dew]
        cost = 0.0
        for i, x in enumerate(vz):
            if math.isnan(x):
                continue
            cost += w[i] * abs((x - mu[i]) / sd[i])
        return cost

    def _cold_snap_score(self, season: Seasons, sig: WeatherDaySignals) -> float:
        """Deviazione firmata di t_smooth rispetto al prototipo stagionale.

        Ritorna:
          Positivo  → t_smooth è SOTTO il prototipo → cold snap.
          Negativo  → t_smooth è SOPRA il prototipo → giorno caldo per la stagione.
          0.0       → prototipi non disponibili (fail-safe conservativo).

        Formula: (μ_season[t] - z_t) / σ_season[t]
          μ_season[t]: mediana z-space di t_smooth per la stagione (in z-space globale).
          z_t:         z-score globale di t_smooth del giorno in esame.
          σ_season[t]: scala robusta intra-stagionale di t_smooth.

        IMPORTANTE: richiede che _fit_infer_prototypes() sia già stato eseguito.
        Non chiamare durante _segment_all_years() (prototipi non ancora disponibili).
        """
        v = self._vec(sig)
        if v is None:
            return 0.0
        vz = self._z(v)
        z_t = vz[0]  # dimensione 0 = t_smooth
        if math.isnan(z_t):
            return 0.0
        mu = self._infer_mu.get(season)
        sd = self._infer_sig.get(season)
        if not mu or not sd:
            return 0.0
        sig_t = max(sd[0], 0.1)  # clamp: evita divisione per valori degeneri
        # positivo → z_t < μ_season → t_smooth sotto il prototipo → più freddo del tipico
        return float((mu[0] - z_t) / sig_t)

    def _apply_cold_snap(
        self,
        out: "Dict[date, WeatherSeason]",
    ) -> "Dict[date, WeatherSeason]":
        """Post-pass: arricchisce ogni WeatherSeason segmentato con cold_snap.

        Chiamato da fit() DOPO _fit_infer_prototypes() perché richiede i prototipi.
        Usa dataclasses.replace() per preservare l'immutabilità.

        Semantica cold_snap_score:
          - Positivo → il giorno è più freddo del prototipo stagionale.
          - Negativo → il giorno è più caldo del prototipo.
          cold_snap = True  iff  cold_snap_score >= cold_snap_z  (default 0.0).

        cold_snap_score nel WeatherSeason è sempre >= 0 (saturato a zero se negativo)
        per evitare confusione nella lettura del campo: un valore "assente" è 0.0,
        non un negativo che potrebbe essere frainteso come errore.
        Il segno informativo rimane accessibile solo attraverso il reason string.
        """
        from dataclasses import replace as _dc_replace

        result: Dict[date, WeatherSeason] = {}
        for dd, ws in out.items():
            cs = self._cold_snap_score(ws.season, ws.weather_day_signals)
            cold_snap = cs >= self._cfg.cold_snap_z
            # Aggiorna reason solo se cold_snap è True (evita verbosità inutile)
            reason = ws.reason
            if cold_snap:
                reason = f"{reason}; cold_snap={cs:+.2f}"
            elif cs < 0:
                reason = f"{reason}; cs={cs:+.2f}"
            result[dd] = _dc_replace(
                ws,
                cold_snap=cold_snap,
                cold_snap_score=max(0.0, cs),
                reason=reason,
            )
        return result

    # ------------------------- segmentation ----------------------------------

    @staticmethod
    def _meteo_year_bounds(y: int) -> Tuple[date, date]:
        """Anno meteorologico y: da Dicembre (y-1) a Novembre (y) inclusi."""
        return date(y - 1, 12, 1), date(y, 11, 30)

    def _meteo_years_present(self) -> List[int]:
        """Anni meteorologici con almeno un giorno di segnali.

        Convenzione: Dicembre del giorno d appartiene all'anno meteorologico d.year+1.
        """
        if not self._signals:
            return []
        meteo_years: set[int] = set()
        for d in self._signals:
            meteo_years.add(d.year + 1 if d.month == 12 else d.year)
        return sorted(meteo_years)

    def _meteo_year_days(self, y: int) -> List[date]:
        """Giorni di segnale nell'anno meteorologico y (Dic Y-1 → Nov Y)."""
        lo, hi = self._meteo_year_bounds(y)
        return sorted(d for d in self._signals if lo <= d <= hi)

    def _segment_all_years(self) -> Dict[date, WeatherSeason]:
        out: Dict[date, WeatherSeason] = {}
        for y in self._meteo_years_present():
            year_days = self._meteo_year_days(y)
            if len(year_days) < self._cfg.min_days_per_year:
                # anno meteorologico troppo incompleto: inferenza live coprirà.
                continue
            if len(year_days) > self._cfg.max_days_per_year:
                year_days = year_days[: self._cfg.max_days_per_year]

            try:
                seg = self._segment_one_year(year_days, meteo_year=y)
            except Exception as e:  # noqa: BLE001
                log_warning(_LOGGER, "Year segmentation failed for meteo year %s: %r", y, e)
                continue
            out.update(seg)
        return out

    def _segment_one_year(self, days: List[date], *, meteo_year: Optional[int] = None) -> Dict[date, WeatherSeason]:
        """Trova 3 breakpoints che minimizzano il costo intra-segmento.

        Vincoli:
          - 4 segmenti contigui
          - ciascun segmento >= min_season_days

        DP O(N^2) con precomputo di cost(i,j).
        """
        n = len(days)
        mnd = self._cfg.min_season_days
        if n < 4 * mnd:
            raise ValueError(f"Year has only {n} days, need at least {4*mnd}")

        # feature z per ogni giorno
        X: List[List[float]] = []
        for d in days:
            v = self._vec(self._signals[d])
            if v is None:
                X.append([float("nan"), float("nan"), float("nan")])
            else:
                X.append(self._z(v))

        w = [self._cfg.w_temp, self._cfg.w_trend, self._cfg.w_dew]

        def seg_cost(i: int, j: int) -> float:
            # costo di descrivere i..j con un "regime" costante = L1 rispetto alla mediana per-feature
            cols: List[List[float]] = [[], [], []]
            for k in range(i, j + 1):
                x = X[k]
                for t in range(3):
                    if not math.isnan(x[t]):
                        cols[t].append(x[t])

            # se troppi missing, penalizza: segmento poco informativo
            if len(cols[0]) < int(0.7 * (j - i + 1)):
                return 1e3

            med = [self._median(c) if c else 0.0 for c in cols]
            cst = 0.0
            for k in range(i, j + 1):
                x = X[k]
                for t in range(3):
                    if math.isnan(x[t]):
                        continue
                    cst += w[t] * abs(x[t] - med[t])
            return cst

        # precompute cost matrix
        cost: List[List[float]] = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i, n):
                cost[i][j] = seg_cost(i, j)

        INF = 1e18
        dp = [[INF] * n for _ in range(5)]
        back = [[-1] * n for _ in range(5)]

        # 1 segmento: 0..j
        for j in range(mnd - 1, n):
            dp[1][j] = cost[0][j]

        # 2..4 segmenti
        for s in range(2, 5):
            for j in range(s * mnd - 1, n):
                best = INF
                best_i = -1
                for i in range((s - 1) * mnd, j - mnd + 2):
                    c = dp[s - 1][i - 1] + cost[i][j]
                    if c < best:
                        best = c
                        best_i = i
                dp[s][j] = best
                back[s][j] = best_i

        end_j = n - 1
        if dp[4][end_j] >= INF / 2:
            raise ValueError("DP failed to find a valid 4-segmentation")

        b3 = back[4][end_j]
        b2 = back[3][b3 - 1]
        b1 = back[2][b2 - 1]

        seg_idx = [(0, b1 - 1), (b1, b2 - 1), (b2, b3 - 1), (b3, n - 1)]

        # stats per segmento per assegnare etichette
        seg_stats: List[Dict[str, float]] = []
        for a, b in seg_idx:
            ts: List[float] = []
            tr: List[float] = []
            for k in range(a, b + 1):
                sig = self._signals[days[k]]
                if sig.t_smooth is not None:
                    ts.append(sig.t_smooth)
                if sig.trend is not None:
                    tr.append(sig.trend)
            t_mean = sum(ts) / len(ts) if ts else float("nan")
            tr_mean = sum(tr) / len(tr) if tr else 0.0
            seg_stats.append({"t_mean": t_mean, "tr_mean": tr_mean})

        # winter/summer = estremi termici
        t_means = [s["t_mean"] for s in seg_stats]
        winter_i = min(range(4), key=lambda i: t_means[i])
        summer_i = max(range(4), key=lambda i: t_means[i])

        other = [i for i in range(4) if i not in (winter_i, summer_i)]
        other_sorted = sorted(other)
        first, second = other_sorted[0], other_sorted[1]

        # spring = segmento con trend mediamente più "in salita"
        if seg_stats[first]["tr_mean"] >= seg_stats[second]["tr_mean"]:
            spring_i = first
            autumn_i = second
        else:
            spring_i = second
            autumn_i = first

        label_map = {
            winter_i: Seasons.WINTER,
            spring_i: Seasons.SPRING,
            summer_i: Seasons.SUMMER,
            autumn_i: Seasons.AUTUMN,
        }

        # Post-labeling consistency check: in anno meteorologico Nord (Dic→Nov),
        # l'ordine atteso è approssimativamente WINTER < SPRING < SUMMER < AUTUMN.
        # Un SUMMER prima di WINTER o un WINTER dopo SUMMER suggerisce inversione.
        season_positions = {label_map[i]: i for i in range(4)}
        if season_positions[Seasons.SUMMER] < season_positions[Seasons.WINTER]:
            log_warning(
                _LOGGER,
                "Segmentation consistency warning (meteo_year=%s): SUMMER (seg %d) before WINTER (seg %d) – check input data.",
                meteo_year,
                season_positions[Seasons.SUMMER],
                season_positions[Seasons.WINTER],
            )

        # regime mediano per segmento (in z-space)
        seg_regime_med: List[List[float]] = []
        for a, b in seg_idx:
            cols = [[], [], []]
            for k in range(a, b + 1):
                x = X[k]
                for t in range(3):
                    if not math.isnan(x[t]):
                        cols[t].append(x[t])
            seg_regime_med.append([self._median(c) if c else 0.0 for c in cols])

        out: Dict[date, WeatherSeason] = {}
        for seg_no, (a, b) in enumerate(seg_idx):
            season = label_map[seg_no]
            reg = seg_regime_med[seg_no]

            for k in range(a, b + 1):
                dd = days[k]
                sig = self._signals[dd]
                v = self._vec(sig)
                if v is None:
                    continue
                vz = self._z(v)

                # anomaly score: distanza L1 pesata dal regime del segmento
                score = 0.0
                dim_ok = 0
                for t in range(3):
                    if math.isnan(vz[t]):
                        continue
                    dim_ok += 1
                    score += w[t] * abs(vz[t] - reg[t])

                if dim_ok <= 1:
                    score += 1.0

                anomaly = score >= self._cfg.anomaly_z
                regime_hint = self._regime_hint(sig)

                my_label = f"meteo_year={meteo_year}" if meteo_year is not None else f"year={dd.year}"
                reason = (
                    f"{my_label} seg={seg_no} [{days[a]}..{days[b]}] "
                    f"t_mean_seg={seg_stats[seg_no]['t_mean']:.2f} tr_mean_seg={seg_stats[seg_no]['tr_mean']:.2f} "
                    f"score={score:.2f}{'; anomaly' if anomaly else ''}"
                )

                out[dd] = WeatherSeason(
                    # d=dd,
                    season=season,
                    anomaly=anomaly,
                    anomaly_score=score,
                    reason=reason,
                    regime_hint=regime_hint,
                    weather_day_signals=sig,
                )

        return out
