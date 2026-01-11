from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union, cast
import logging
import math

from ...domain.models.season import RegimeHint, WeatherSeason, WeatherDaySignals

from ...domain.models.weather import Forecast, Historical

from ..logger import log_warning
from ...domain.enums import Seasons

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

    # se anno troppo incompleto, viene ignorato dalla segmentazione (stabilità)
    min_days_per_year: int = 300

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
        # Se non fornita, mapping meteorologico classico (DJF/MAM/JJA/SON)
        if expected_season is None:
            m = dd.month
            if m in (12, 1, 2):
                expected_season = Seasons.WINTER
            elif m in (3, 4, 5):
                expected_season = Seasons.SPRING
            elif m in (6, 7, 8):
                expected_season = Seasons.SUMMER
            else:
                expected_season = Seasons.AUTUMN

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
        # prior_penalty: quanto spingiamo verso expected quando i costi sono vicini
        prior_penalty = float(getattr(self._cfg, "prior_penalty", 0.75))

        # expected_margin: tie-break -> scegli expected se è quasi a pari col best
        expected_margin = float(getattr(self._cfg, "expected_margin", 0.35))

        # isteresi: evita cambio se il vantaggio è minuscolo, MA SOLO se expected==prev
        switch_gap_min = float(getattr(self._cfg, "switch_gap_min", 0.25))

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

        cand_str = ", ".join(
            f"{(s.value if hasattr(s,'value') else s)}:raw={raw:.2f},tot={tot:.2f}"
            for (tot, raw, s) in scored
        )
        reason = (
            f"infer: expected={expected_season} chosen={best_season} raw={best_raw:.2f} tot={best_total:.2f} "
            f"candidates=[{cand_str}]" + ("; anomaly" if anomaly else "")
        )

        return WeatherSeason(
            season=best_season,
            anomaly=anomaly,
            anomaly_score=anomaly_score,
            reason=reason,
            regime_hint=regime_hint,
            weather_day_signals=sig,
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

    def _fit_infer_prototypes(self) -> None:
        """Stima prototipi robusti per inferenza live usando i giorni etichettati."""
        buckets: Dict[Seasons, List[List[Optional[float]]]] = {s: [] for s in self._CYCLE}

        for dd, ds in self._out.items():
            v = self._vec(ds.weather_day_signals)
            if v is None:
                continue
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

    # ------------------------- segmentation ----------------------------------

    def _years_present(self) -> List[int]:
        if not self._signals:
            return []
        return sorted({d.year for d in self._signals})

    def _segment_all_years(self) -> Dict[date, WeatherSeason]:
        out: Dict[date, WeatherSeason] = {}
        for y in self._years_present():
            year_days = [d for d in sorted(self._signals) if d.year == y]
            if len(year_days) < self._cfg.min_days_per_year:
                # anno troppo incompleto: evitiamo etichette instabili.
                # inferenza live coprirà eventuali giorni richiesti.
                continue
            if len(year_days) > self._cfg.max_days_per_year:
                year_days = year_days[: self._cfg.max_days_per_year]

            try:
                seg = self._segment_one_year(year_days)
            except Exception as e:  # noqa: BLE001
                log_warning(_LOGGER, "Year segmentation failed for %s: %r", y, e)
                continue
            out.update(seg)
        return out

    def _segment_one_year(self, days: List[date]) -> Dict[date, WeatherSeason]:
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

                reason = (
                    f"year={dd.year} seg={seg_no} [{days[a]}..{days[b]}] "
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
