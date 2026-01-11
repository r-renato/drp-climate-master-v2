"""Plant-aware HVAC regime inference + threshold fitting (season-aware).

This refactor integrates:
- season_windows (weather-based contiguous season segmentation) for *training-time* partitioning.
- SeasonState (computed daily for the current day) for *runtime* selection of the best config.
- Local-day aggregation (Europe/Rome by default) to match Home Assistant semantics.

Policy implemented (as requested):
- FIT:
  - Days covered by season_windows are used for *season-specific* fits.
  - Days NOT covered by season_windows ("holes") are used ONLY for the *global* fit.
- RUNTIME:
  - Prefer SeasonState.weather_season when available, otherwise SeasonState.season.

What this code does NOT do (by design):
- It does not try to reconstruct historical SeasonState per day.
- It does not force any behavior based on weather_anomaly; you can add that as a policy.

Expected domain objects (from your integration):
- Seasons: StrEnum with WINTER/SPRING/SUMMER/AUTUMN and .ordered()
- SeasonState: holds .season (calendar) and .weather_season (meteo) and other diagnostics.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, cast

import math
import logging

import pandas as pd
from pandas import DatetimeIndex
from influxdb_client import InfluxDBClient

from ..domain.influx import InfluxConfig

from ..domain.enums import Seasons
from ..domain.models.season import SeasonState

# -----------------------------------------------------------------------------
# Domain imports (adapt paths to your project)
# -----------------------------------------------------------------------------
# from ..domain.enums import Seasons
# from ..domain.models.season import SeasonState

# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------


# @dataclass(frozen=True, slots=True)
# class InfluxConfig:
#     url: str
#     org: str
#     token: str
#     bucket: str

_LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class PlantEntities:
    """Entity id mapping (Influx `entity_id` tag values)."""

    outdoor_temp: str
    compressor_state: str
    device_mode: str
    vmc_pump_switch: str
    radiant_pump_switch: str
    active_power: str


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    tau_days: float
    heating_on: float
    heating_off: float
    cooling_on: float
    cooling_off: float


@dataclass(frozen=True, slots=True)
class CandidateScore:
    cfg: RegimeConfig
    loss: float
    err: float
    pen: float
    switches: int
    days: int


@dataclass(frozen=True, slots=True)
class RegimeSearchResult:
    best: CandidateScore
    top10: List[CandidateScore]
    obs_counts: pd.Series
    common_days: int


@dataclass(frozen=True, slots=True)
class SeasonalRegimeSearchResult:
    """A global fit + (optional) per-season fits."""

    global_result: RegimeSearchResult
    per_season_result: Dict[Any, RegimeSearchResult]  # key is Seasons

    # diagnostics
    season_coverage_days: int
    season_hole_days: int


# -----------------------------------------------------------------------------
# Influx reader
# -----------------------------------------------------------------------------


class InfluxSeriesReader:
    """InfluxDB v2 reader specialized for HA Influx schema."""

    def __init__(self, cfg: InfluxConfig):
        self._cfg = cfg
        self._client = InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org)
        self._query_api = self._client.query_api()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    @staticmethod
    def _flux_to_dataframe(query_api, flux: str) -> pd.DataFrame:
        """Run a Flux query and return a DataFrame (robust)."""
        tables = query_api.query(flux)
        rows: List[Dict[str, Any]] = []

        for table in tables:
            for record in table.records:
                vals = record.values or {}

                try:
                    t = record.get_time()
                except Exception:
                    t = vals.get("_time")

                try:
                    v = record.get_value()
                except Exception:
                    v = vals.get("_value")

                rows.append(
                    {
                        "_time": t,
                        "_field": vals.get("_field"),
                        "_value": v,
                        "entity_id": vals.get("entity_id"),
                        "_measurement": vals.get("_measurement"),
                    }
                )

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["_time"] = pd.to_datetime(df["_time"], utc=True, errors="coerce")
        df = df.dropna(subset=["_time"]).sort_values("_time")
        return df

    def fetch_entity_df(
        self,
        entity_id: str,
        start: str,
        stop: str,
        *,
        every: str = "5m",
        field: Optional[str] = "value",
        agg_fn: str = "last",
    ) -> pd.DataFrame:
        if agg_fn not in {"last", "mean", "min", "max"}:
            raise ValueError(f"Unsupported agg_fn={agg_fn}")

        field_filter = f'  |> filter(fn: (r) => r["_field"] == "{field}")\n' if field else ""

        flux = f"""
from(bucket: \"{self._cfg.bucket}\")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r[\"entity_id\"] == \"{entity_id}\")
{field_filter}  |> aggregateWindow(every: {every}, fn: {agg_fn}, createEmpty: false)
  |> keep(columns: [\"_time\",\"_field\",\"_value\",\"entity_id\",\"_measurement\"])
""".strip()

        df = self._flux_to_dataframe(self._query_api, flux)
        if df.empty:
            return pd.DataFrame(
                columns=["_field", "_value", "entity_id", "_measurement"],
                index=pd.DatetimeIndex([], tz="UTC"),
            )

        return df.set_index("_time").sort_index()

    @staticmethod
    def df_to_value_series(df: pd.DataFrame) -> pd.Series:
        if df is None or df.empty:
            return pd.Series(dtype=object)
        return df["_value"].copy()


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------


def _as_bool_switch(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return pd.Series(dtype=bool)

    num = pd.to_numeric(series, errors="coerce")
    if num.notna().any():
        return num.fillna(0).astype(int).astype(bool)

    s = series.astype(str).str.lower()
    return s.isin(["on", "true", "1", "yes"])


def _as_bool_binary(series: pd.Series) -> pd.Series:
    return _as_bool_switch(series)


def _to_float(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce")


def _ensure_utc_index(s: pd.Series) -> pd.Series:
    if s is None or s.empty:
        return s

    idx = s.index
    if not isinstance(idx, DatetimeIndex):
        raise TypeError(f"Expected DatetimeIndex, got {type(idx).__name__}")

    if idx.tz is None:
        s.index = idx.tz_localize("UTC")
    else:
        s.index = idx.tz_convert("UTC")
    return s


def _tz_convert_series(s: pd.Series, tz: str) -> pd.Series:
    """Convert a tz-aware series to a target timezone."""
    if s is None or s.empty:
        return s
    s = _ensure_utc_index(s)
    s.index = cast(DatetimeIndex, s.index).tz_convert(tz)
    return s

def daily_local_mean(s: pd.Series, tz: str) -> pd.Series:
    return _daily_local_mean(s, tz)

def _daily_local_mean(s: pd.Series, tz: str) -> pd.Series:
    """Resample a UTC series to local-day mean and return an index of python `date`."""
    if s is None or s.empty:
        return pd.Series(dtype=float)
    sl = _tz_convert_series(_to_float(s), tz)
    # local midnight boundaries
    daily = sl.resample("1D").mean()
    daily.index = pd.Index([ts.date() for ts in daily.index], dtype=object)
    return daily


# -----------------------------------------------------------------------------
# Season windows index
# -----------------------------------------------------------------------------


class SeasonWindowsIndex:
    """Fast date->season lookup based on contiguous season windows."""

    def __init__(self, windows: List[Tuple[date, date, Any]]):
        self._windows = list(windows or [])
        self._map: Dict[date, Any] = {}
        self._min: Optional[date] = None
        self._max: Optional[date] = None
        self._build()

    def _build(self) -> None:
        if not self._windows:
            return
        self._windows.sort(key=lambda w: w[0])
        for start, end, season in self._windows:
            if start > end:
                continue
            if self._min is None or start < self._min:
                self._min = start
            if self._max is None or end > self._max:
                self._max = end
            d = start
            while d <= end:
                self._map[d] = season
                d += timedelta(days=1)

    def season_for(self, d: date) -> Optional[Any]:
        return self._map.get(d)

    def coverage(self) -> Tuple[Optional[date], Optional[date], int]:
        return self._min, self._max, len(self._map)


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------


class PlantRegimePipeline:
    """End-to-end plant-aware regime pipeline (season-aware)."""

    def __init__(
        self,
        reader: InfluxSeriesReader,
        entities: PlantEntities,
        season_state: SeasonState,  # SeasonState
        season_windows: List[Tuple[date, date, Seasons]],  # List[(date,date,Seasons)]
        *,
        local_tz: str = "Europe/Rome",
        start: str = "-730d",
        stop: str = "now()",
        power_threshold_w: float = 250.0,
        duty_min: float = 0.20,
        shoulder_duty_max: float = 0.02,
        cool_vmc_duty_min: Optional[float] = None,
    ):
        self.reader = reader
        self.entities = entities

        self.season_state = season_state
        self.season_windows = season_windows
        self._season_index = SeasonWindowsIndex(season_windows)

        self.local_tz = str(local_tz)

        self.start = start
        self.stop = stop

        self.power_threshold_w = float(power_threshold_w)
        self.duty_min = float(duty_min)
        self.shoulder_duty_max = float(shoulder_duty_max)
        self.cool_vmc_duty_min = float(cool_vmc_duty_min) if cool_vmc_duty_min is not None else float(duty_min)

    # --------------------- data acquisition ---------------------

    def load_raw(self) -> Dict[str, pd.Series]:
        out_df = self.reader.fetch_entity_df(
            self.entities.outdoor_temp,
            self.start,
            self.stop,
            every="30m",
            field="value",
            agg_fn="mean",
        )

        comp_df = self.reader.fetch_entity_df(
            self.entities.compressor_state,
            self.start,
            self.stop,
            every="5m",
            field="value",
            agg_fn="last",
        )

        mode_df = self.reader.fetch_entity_df(
            self.entities.device_mode,
            self.start,
            self.stop,
            every="5m",
            field="value",
            agg_fn="last",
        )

        vmc_df = self.reader.fetch_entity_df(
            self.entities.vmc_pump_switch,
            self.start,
            self.stop,
            every="5m",
            field="value",
            agg_fn="last",
        )

        rad_df = self.reader.fetch_entity_df(
            self.entities.radiant_pump_switch,
            self.start,
            self.stop,
            every="5m",
            field="value",
            agg_fn="last",
        )

        pwr_df = self.reader.fetch_entity_df(
            self.entities.active_power,
            self.start,
            self.stop,
            every="5m",
            field="value",
            agg_fn="mean",
        )

        raw = {
            "T_out": self.reader.df_to_value_series(out_df),
            "compressor": self.reader.df_to_value_series(comp_df),
            "device_mode": self.reader.df_to_value_series(mode_df),
            "vmc_pump": self.reader.df_to_value_series(vmc_df),
            "radiant_pump": self.reader.df_to_value_series(rad_df),
            "power": self.reader.df_to_value_series(pwr_df),
        }

        for k, s in raw.items():
            raw[k] = _ensure_utc_index(s)

        return raw

    def normalize(self, raw: Dict[str, pd.Series]) -> Dict[str, pd.Series]:
        T_out = _to_float(raw["T_out"])  # °C
        compressor_on = _as_bool_binary(raw["compressor"])
        device_mode = _to_float(raw["device_mode"])  # 1/5
        vmc_pump_on = _as_bool_switch(raw["vmc_pump"])
        radiant_pump_on = _as_bool_switch(raw["radiant_pump"])
        power_w = _to_float(raw["power"])

        return {
            "T_out": T_out,
            "compressor_on": compressor_on,
            "device_mode": device_mode,
            "vmc_pump_on": vmc_pump_on,
            "radiant_pump_on": radiant_pump_on,
            "active_power_w": power_w,
        }

    # --------------------- observed regimes ---------------------

    def build_observed_daily_regime(
        self, norm: Dict[str, pd.Series]
    ) -> Tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
        compressor_on = _ensure_utc_index(norm["compressor_on"])
        device_mode = _ensure_utc_index(norm["device_mode"])
        vmc_pump_on = _ensure_utc_index(norm["vmc_pump_on"])
        radiant_pump_on = _ensure_utc_index(norm["radiant_pump_on"])
        pwr = _ensure_utc_index(norm["active_power_w"])

        indices = [
            s.index
            for s in [compressor_on, device_mode, vmc_pump_on, radiant_pump_on, pwr]
            if s is not None and not s.empty
        ]
        if not indices:
            return pd.Series(dtype=object), pd.DataFrame(), pd.DataFrame()

        idx = indices[0]
        for other in indices[1:]:
            idx = idx.union(other)
        idx = idx.sort_values()

        df = pd.DataFrame(index=idx)

        df["compressor_on"] = compressor_on.reindex(idx).ffill().infer_objects(copy=False)
        df["device_mode"] = device_mode.reindex(idx).ffill()
        df["vmc_pump_on"] = vmc_pump_on.reindex(idx).ffill().infer_objects(copy=False)
        df["radiant_pump_on"] = radiant_pump_on.reindex(idx).ffill().infer_objects(copy=False)
        df["active_power_w"] = pwr.reindex(idx).ffill()

        df["device_mode"] = pd.to_numeric(df["device_mode"], errors="coerce")
        df["active_power_w"] = pd.to_numeric(df["active_power_w"], errors="coerce")

        mode_heat = df["device_mode"] == 1
        mode_cool = df["device_mode"] == 5

        pwr_ok = df["active_power_w"] >= self.power_threshold_w

        cool_active = mode_cool & df["compressor_on"].astype(bool) & pwr_ok
        heat_active = mode_heat & df["compressor_on"].astype(bool) & pwr_ok

        cool_radiant = cool_active & df["radiant_pump_on"].astype(bool)
        cool_vmc = cool_active & df["vmc_pump_on"].astype(bool) & (~df["radiant_pump_on"].astype(bool))
        heat_radiant = heat_active & df["radiant_pump_on"].astype(bool)

        df["cool_active"] = cool_active
        df["heat_active"] = heat_active
        df["cool_radiant"] = cool_radiant
        df["cool_vmc"] = cool_vmc
        df["heat_radiant"] = heat_radiant

        # IMPORTANT: resample on LOCAL day boundaries
        df_local = df.copy()
        df_local.index = cast(DatetimeIndex, df_local.index).tz_convert(self.local_tz)

        duty = (
            pd.DataFrame(
                {
                    "heat_radiant": df_local["heat_radiant"].astype(bool),
                    "cool_radiant": df_local["cool_radiant"].astype(bool),
                    "cool_vmc": df_local["cool_vmc"].astype(bool),
                    "heat_active": df_local["heat_active"].astype(bool),
                    "cool_active": df_local["cool_active"].astype(bool),
                },
                index=df_local.index,
            )
            .resample("1D")
            .mean()
        )

        # convert daily index to python date for easy season mapping
        duty.index = pd.Index([ts.date() for ts in duty.index], dtype=object)

        obs = pd.Series(index=duty.index, dtype=object)
        for day, hr, cr, cv, ha, ca in zip(
            duty.index,
            duty["heat_radiant"].fillna(0.0),
            duty["cool_radiant"].fillna(0.0),
            duty["cool_vmc"].fillna(0.0),
            duty["heat_active"].fillna(0.0),
            duty["cool_active"].fillna(0.0),
        ):
            if hr >= self.duty_min:
                obs.at[day] = "heating_radiant"
            elif cr >= self.duty_min:
                obs.at[day] = "cooling_radiant"
            elif cv >= self.cool_vmc_duty_min:
                obs.at[day] = "dehumid_vmc"
            else:
                obs.at[day] = "shoulder" if max(float(ha), float(ca)) <= self.shoulder_duty_max else None

        return obs, duty, df

    # --------------------- regime prediction model ---------------------

    @staticmethod
    def _smooth_outdoor(T_out_daily: pd.Series, tau_days: float) -> pd.Series:
        s = T_out_daily.copy().sort_index()
        return s.ewm(span=max(1.0, float(tau_days)), adjust=False).mean()

    @staticmethod
    def _predict_regime_from_outdoor(T_s: pd.Series, cfg: RegimeConfig) -> pd.Series:
        state: str = "shoulder"
        vals: list[object] = []

        for _, t in T_s.items():
            if pd.isna(t):
                vals.append(None)
                continue

            tt = float(t)
            if state == "shoulder":
                if tt <= cfg.heating_on:
                    state = "heating"
                elif tt >= cfg.cooling_on:
                    state = "cooling"
            elif state == "heating":
                if tt >= cfg.heating_off:
                    state = "shoulder"
            elif state == "cooling":
                if tt <= cfg.cooling_off:
                    state = "shoulder"

            vals.append(state)

        return pd.Series(vals, index=T_s.index, dtype=object)

    @staticmethod
    def _map_observed_to_macro(obs: pd.Series) -> pd.Series:
        mapping = {
            "heating_radiant": "heating",
            "cooling_radiant": "cooling",
            "dehumid_vmc": "cooling",
            "shoulder": "shoulder",
        }
        return obs.map(mapping)

    @staticmethod
    def _count_switches(series: pd.Series) -> int:
        s = series.dropna()
        if s.empty:
            return 0
        prev = None
        switches = 0
        for v in s.values:
            if prev is None:
                prev = v
                continue
            if v != prev:
                switches += 1
                prev = v
        return switches

    @staticmethod
    def _accuracy(pred: pd.Series, obs: pd.Series) -> Tuple[float, int]:
        common = pred.index.intersection(obs.index)
        if len(common) == 0:
            return float("nan"), 0

        p = pred.loc[common]
        o = obs.loc[common]

        mask = p.notna() & o.notna()
        if int(mask.sum()) == 0:
            return float("nan"), 0

        correct = int((p[mask] == o[mask]).sum())
        total = int(mask.sum())
        return float(correct / total), total

    # --------------------- grid search ---------------------

    def grid_search_regime(
        self,
        T_out_daily: pd.Series,
        obs_regime_daily: pd.Series,
        *,
        tau_grid: Sequence[float] = (3.0, 5.0, 7.0),
        heating_on_grid: Sequence[float] = tuple(x / 2 for x in range(24, 33)),
        heating_hyst: float = 1.0,
        cooling_on_grid: Sequence[float] = tuple(x / 2 for x in range(44, 53)),
        cooling_hyst: float = 1.0,
        switch_penalty_weight: float = 0.01,
        top_k: int = 10,
    ) -> RegimeSearchResult:
        obs_macro = self._map_observed_to_macro(obs_regime_daily).dropna()

        T_out_daily = _to_float(T_out_daily).dropna().sort_index()

        common_days = int(T_out_daily.index.intersection(obs_macro.index).shape[0])

        candidates: List[CandidateScore] = []

        for tau in tau_grid:
            T_s = self._smooth_outdoor(T_out_daily, tau)
            for hon in heating_on_grid:
                hoff = float(hon + heating_hyst)
                for con in cooling_on_grid:
                    coff = float(con - cooling_hyst)

                    cfg = RegimeConfig(
                        tau_days=float(tau),
                        heating_on=float(hon),
                        heating_off=float(hoff),
                        cooling_on=float(con),
                        cooling_off=float(coff),
                    )

                    pred = self._predict_regime_from_outdoor(T_s, cfg)
                    acc, n = self._accuracy(pred, obs_macro)
                    if n == 0 or math.isnan(acc):
                        continue

                    err = 1.0 - acc
                    switches = self._count_switches(pred)
                    pen = float(switch_penalty_weight) * (switches / max(1, n))
                    loss = float(err + pen)

                    candidates.append(
                        CandidateScore(
                            cfg=cfg,
                            loss=loss,
                            err=float(err),
                            pen=float(pen),
                            switches=int(switches),
                            days=int(n),
                        )
                    )

        if not candidates:
            fallback = CandidateScore(
                cfg=RegimeConfig(tau_days=5.0, heating_on=13.0, heating_off=14.0, cooling_on=24.0, cooling_off=23.0),
                loss=float("nan"),
                err=float("nan"),
                pen=0.0,
                switches=0,
                days=0,
            )
            return RegimeSearchResult(
                best=fallback,
                top10=[fallback],
                obs_counts=obs_regime_daily.value_counts(dropna=False),
                common_days=common_days,
            )

        candidates.sort(key=lambda c: c.loss)
        top10 = candidates[: max(1, int(top_k))]
        best = top10[0]

        return RegimeSearchResult(
            best=best,
            top10=top10,
            obs_counts=obs_regime_daily.value_counts(dropna=False),
            common_days=best.days,
        )

    # --------------------- season-aware fitting ---------------------

    def fit_seasonal(
        self,
        *,
        tau_grid: Sequence[float] = (3.0, 5.0, 7.0),
        heating_on_grid: Sequence[float] = tuple(x / 2 for x in range(24, 33)),
        heating_hyst: float = 1.0,
        cooling_on_grid: Sequence[float] = tuple(x / 2 for x in range(44, 53)),
        cooling_hyst: float = 1.0,
        switch_penalty_weight: float = 0.01,
        top_k: int = 10,
        min_days_per_season_fit: int = 45,
    ) -> SeasonalRegimeSearchResult:
        """Fit a global config and (optionally) a config per meteorological season.

        - Season labeling for days comes from season_windows.
        - Days not covered by season_windows are excluded from season fits, but included in global.
        """
        raw = self.load_raw()
        norm = self.normalize(raw)

        obs_daily, _duty_daily, _frame = self.build_observed_daily_regime(norm)
        T_out_daily = _daily_local_mean(norm["T_out"], tz=self.local_tz)

        # --- GLOBAL ---
        global_result = self.grid_search_regime(
            T_out_daily=T_out_daily,
            obs_regime_daily=obs_daily,
            tau_grid=tau_grid,
            heating_on_grid=heating_on_grid,
            heating_hyst=heating_hyst,
            cooling_on_grid=cooling_on_grid,
            cooling_hyst=cooling_hyst,
            switch_penalty_weight=switch_penalty_weight,
            top_k=top_k,
        )

        # --- SEASONAL ---
        per_season: Dict[Any, RegimeSearchResult] = {}

        # Build a season series for the days in the training set
        days_all: List[date] = sorted(set(obs_daily.index).union(set(T_out_daily.index)))
        season_for_day: Dict[date, Any] = {d: self._season_index.season_for(d) for d in days_all}

        covered_days = sum(1 for d, s in season_for_day.items() if s is not None)
        hole_days = sum(1 for d, s in season_for_day.items() if s is None)

        # Partition by season value
        buckets: Dict[Any, List[date]] = {}
        for d, s in season_for_day.items():
            if s is None:
                continue
            buckets.setdefault(s, []).append(d)

        for season, dlist in buckets.items():
            if len(dlist) < int(min_days_per_season_fit):
                continue

            # NOTE: gli indici daily sono Timestamp (spesso tz-aware). Convertiamo la lista di `date`
            # in un DatetimeIndex a mezzanotte *locale* per poter indicizzare in modo corretto (e soddisfare Pylance).
            didx = pd.DatetimeIndex(pd.to_datetime(list(dlist)))
            # `didx` è naive: lo localizziamo nel timezone locale usato per il resample giornaliero
            if getattr(didx, "tz", None) is None:
                didx = didx.tz_localize(self.local_tz)
            else:
                didx = didx.tz_convert(self.local_tz)
            # allineiamo a mezzanotte
            didx = cast(pd.DatetimeIndex, didx).normalize()

            # `reindex` è più robusto di `.loc` (niente KeyError se manca qualche giorno)
            T_s = T_out_daily.reindex(didx)
            O_s = obs_daily.reindex(didx)

            per_season[season] = self.grid_search_regime(
                T_out_daily=T_s,
                obs_regime_daily=O_s,
                tau_grid=tau_grid,
                heating_on_grid=heating_on_grid,
                heating_hyst=heating_hyst,
                cooling_on_grid=cooling_on_grid,
                cooling_hyst=cooling_hyst,
                switch_penalty_weight=switch_penalty_weight,
                top_k=top_k,
            )

        return SeasonalRegimeSearchResult(
            global_result=global_result,
            per_season_result=per_season,
            season_coverage_days=int(covered_days),
            season_hole_days=int(hole_days),
        )

    # --------------------- runtime selection ---------------------

    def pick_runtime_season(self) -> Any:
        """Policy: prefer meteo season when available; else calendar season."""
        ss = self.season_state
        # SeasonState exposes .weather_season and .season
        w = getattr(ss, "weather_season", None)
        if w is not None:
            return w
        return getattr(ss, "season", None)

    def pick_runtime_config(self, fit: SeasonalRegimeSearchResult) -> RegimeConfig:
        """Select the most appropriate RegimeConfig for *today* based on SeasonState."""
        season = self.pick_runtime_season()
        if season is not None:
            sr = fit.per_season_result.get(season)
            if sr is not None and sr.best.days > 0 and not math.isnan(sr.best.loss):
                return sr.best.cfg
        return fit.global_result.best.cfg


# -----------------------------------------------------------------------------
# Example usage (adapt to your HA integration)
# -----------------------------------------------------------------------------


def example_usage_in_ha(
    reader: InfluxSeriesReader,
    entities: PlantEntities,
    season_state: Any,  # SeasonState
    season_windows: List[Tuple[date, date, Any]],
) -> None:
    """Example: fit + choose runtime config.

    In HA you typically:
    - compute season_state + season_windows in WeatherCoordinator
    - run fit occasionally (not every 5 minutes)
    - store the resulting cfg(s) in a cache or storage
    """

    pipe = PlantRegimePipeline(
        reader=reader,
        entities=entities,
        season_state=season_state,
        season_windows=season_windows,
        local_tz="Europe/Rome",
        start="-730d",
        stop="now()",
    )

    fit = pipe.fit_seasonal()
    cfg_today = pipe.pick_runtime_config(fit)

    # cfg_today is the config you feed to your decider for macro regime decisions
    print("Runtime season:", pipe.pick_runtime_season())
    print("Chosen cfg:", cfg_today)
