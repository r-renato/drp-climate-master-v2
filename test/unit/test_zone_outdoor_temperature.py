"""Test unitari per ``resolve_outdoor_temperature`` e l'uso nel planner zone.

Problema risolto
----------------
Il fallback "media giornaliera forecast" del planner leggeva
``snapshot.season.weather_signals``: attributo che non esiste (i segnali sono in
``SeasonState.weather.weather_day_signals``). Il ramo era codice morto, quindi con
sensore esterno privo di valore l'unico esito possibile era abortire il piano.
Inoltre il warning ``outdoor_temperature_unreliable`` era emesso solo per sorgente
``global_outdoor_temperature`` *e* flag stale/insufficient, condizione mai vera
(il sensore stale/insufficiente non viene mai scelto come sorgente primaria).

Copertura
---------
A. Sensore fresco → sorgente primaria.
B. Sensore stale/insufficiente con valore → resta la lettura (nessuna regressione
   rispetto al comportamento precedente), anche con forecast disponibile.
C. Sensore senza valore → media giornaliera ``t_mean`` del forecast (ramo prima morto).
D. Percorso reale ``SeasonState.weather.weather_day_signals`` (non ``weather_signals``).
E. ``t_smooth`` mai usata come temperatura esterna.
F. Nessuna sorgente / valori non finiti → ``missing``.
G. Planner: warning ``outdoor_temperature_unreliable`` per sorgente degradata,
   assente per sensore fresco; piano abortito se ``missing``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.drp_climate_master_v2.domain.models.season import (
    SeasonState,
    SeasonWindow,
    Seasons,
    WeatherDaySignals,
    WeatherSeason,
)
from custom_components.drp_climate_master_v2.plant.decision.zone.config import (
    ControlConfig,
    MpcConfig,
    RcZoneParams,
)
from custom_components.drp_climate_master_v2.plant.decision.zone.outdoor_temperature import (
    OUTDOOR_SRC_FORECAST_MEAN,
    OUTDOOR_SRC_MISSING,
    OUTDOOR_SRC_SENSOR,
    OUTDOOR_SRC_SENSOR_STALE,
    resolve_outdoor_temperature,
)
from custom_components.drp_climate_master_v2.plant.decision.zone.planner import ZoneDecisionPlanner

SENSOR_C = 7.4
FORECAST_T_MEAN_C = 12.0
FORECAST_T_SMOOTH_C = 22.7  # non deve mai essere usata


def _av(value: Any, *, stale: bool = False, insufficient: bool = False) -> SimpleNamespace:
    return SimpleNamespace(value=value, is_stale=stale, is_insufficient=insufficient)


def _season(t_mean: float | None = FORECAST_T_MEAN_C) -> SeasonState:
    signals = WeatherDaySignals(
        d=date(2026, 12, 10),
        t_mean=t_mean,
        t_low=None,
        dew=None,
        wind=None,
        cloud=None,
        t_smooth=FORECAST_T_SMOOTH_C,
        dew_smooth=None,
        trend=None,
    )
    return SeasonState(
        window=SeasonWindow(Seasons.WINTER, date(2026, 12, 1), date(2027, 2, 28)),
        weather=WeatherSeason(
            season=Seasons.WINTER,
            anomaly=False,
            anomaly_score=0.0,
            reason="test",
            regime_hint="cold",
            weather_day_signals=signals,
        ),
        as_of=date(2026, 12, 10),
        detect_model="inferred",
    )


def _snapshot(av: Any, season: Any = None) -> SimpleNamespace:
    return SimpleNamespace(global_outdoor_temperature=av, season=season)


# ---------------------------------------------------------------- A
def test_sensore_fresco_e_sorgente_primaria() -> None:
    value, src = resolve_outdoor_temperature(_snapshot(_av(SENSOR_C), _season()))

    assert (value, src) == (SENSOR_C, OUTDOOR_SRC_SENSOR)


# ---------------------------------------------------------------- B
@pytest.mark.parametrize("flags", [{"stale": True}, {"insufficient": True}])
def test_sensore_stale_con_valore_resta_preferito_al_forecast(flags: dict[str, bool]) -> None:
    value, src = resolve_outdoor_temperature(_snapshot(_av(SENSOR_C, **flags), _season()))

    assert (value, src) == (SENSOR_C, OUTDOOR_SRC_SENSOR_STALE)


# ---------------------------------------------------------------- C
@pytest.mark.parametrize("av", [None, _av(None), _av(None, stale=True)])
def test_sensore_senza_valore_usa_media_giornaliera_forecast(av: Any) -> None:
    value, src = resolve_outdoor_temperature(_snapshot(av, _season()))

    assert (value, src) == (FORECAST_T_MEAN_C, OUTDOOR_SRC_FORECAST_MEAN)


# ---------------------------------------------------------------- D
def test_percorso_reale_weather_day_signals_non_weather_signals() -> None:
    # il vecchio percorso inesistente non deve mai produrre un valore
    legacy = SimpleNamespace(weather_signals=SimpleNamespace(t_mean=FORECAST_T_MEAN_C))

    assert resolve_outdoor_temperature(_snapshot(None, legacy)) == (None, OUTDOOR_SRC_MISSING)


# ---------------------------------------------------------------- E
def test_t_smooth_mai_usata_come_temperatura_esterna() -> None:
    value, _ = resolve_outdoor_temperature(_snapshot(None, _season()))

    assert value == FORECAST_T_MEAN_C
    assert value != FORECAST_T_SMOOTH_C


# ---------------------------------------------------------------- F
@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(None, None),
        _snapshot(_av(None), _season(t_mean=None)),
        _snapshot(_av(float("nan")), _season(t_mean=float("inf"))),
        SimpleNamespace(),  # snapshot senza alcun attributo
    ],
)
def test_nessuna_sorgente_utilizzabile_e_missing(snapshot: Any) -> None:
    assert resolve_outdoor_temperature(snapshot) == (None, OUTDOOR_SRC_MISSING)


def test_sensore_non_finito_con_forecast_valido_usa_forecast() -> None:
    value, src = resolve_outdoor_temperature(_snapshot(_av(float("nan")), _season()))

    assert (value, src) == (FORECAST_T_MEAN_C, OUTDOOR_SRC_FORECAST_MEAN)


# ---------------------------------------------------------------- G
_MPC_FAST = MpcConfig(
    dt_minutes=10,
    horizon_steps=6,
    w_comfort=10.0,
    w_energy=0.3,
    w_switch=1.5,
    comfort_slack_c=0.1,
    min_switch_minutes=0,
    degenerate_retry_enabled=False,
)
_PLANNER = ZoneDecisionPlanner(cfg=ControlConfig(mpc=_MPC_FAST, rc_default=RcZoneParams()))
_BAND_C = (20.0, 22.0)


def _planner_snapshot(av: Any, season: Any) -> MagicMock:
    """PlantSnapshot minimale con una zona 'living' fredda (come nei test del planner)."""
    snap = MagicMock()
    snap.timestamp = datetime(2026, 12, 10, 18, 0, 0, tzinfo=timezone.utc)
    snap.windows_closed = True
    snap.presence_vacation = False
    snap.climate_preset_mode = None
    snap.global_outdoor_temperature = av
    snap.season = season

    zone = MagicMock()
    t_op = MagicMock()
    t_op.value = 18.0
    zone.t_op = t_op
    zone.temperature = t_op
    valve = MagicMock()
    valve.value = 0.0
    zone.radiant_valve = valve
    snap.indoor_zones = {"living": zone}
    return snap


def _plan(snap: Any) -> Any:
    band = MagicMock()
    band.t_op_min, band.t_op_max = _BAND_C
    band.ok = True
    return _PLANNER.plan(snapshot=snap, reason="test", comfort_bands_by_zone={"living": band})


def test_planner_nessun_warning_con_sensore_fresco() -> None:
    plan = _plan(_planner_snapshot(_av(SENSOR_C), None))

    assert "outdoor_temperature_unreliable" not in plan.warnings
    assert plan.meta["t_out_mpc_src"] == OUTDOOR_SRC_SENSOR


def test_planner_warning_con_sensore_stale() -> None:
    plan = _plan(_planner_snapshot(_av(SENSOR_C, stale=True), None))

    assert "outdoor_temperature_unreliable" in plan.warnings
    assert plan.meta["t_out_mpc_src"] == OUTDOOR_SRC_SENSOR_STALE
    assert plan.meta["t_out_mpc_c"] == SENSOR_C


def test_planner_warning_e_sorgente_forecast_senza_sensore() -> None:
    plan = _plan(_planner_snapshot(_av(None), _season()))

    assert "outdoor_temperature_unreliable" in plan.warnings
    assert "missing_outdoor_temperature" not in plan.warnings
    assert plan.meta["t_out_mpc_src"] == OUTDOOR_SRC_FORECAST_MEAN
    assert plan.meta["t_out_mpc_c"] == FORECAST_T_MEAN_C


def test_planner_aborta_se_nessuna_sorgente() -> None:
    plan = _plan(_planner_snapshot(_av(None), None))

    assert "missing_outdoor_temperature" in plan.warnings
    assert "t_out_mpc_c" not in plan.meta
