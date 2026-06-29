"""Test unitari per ZoneDecisionPlanner con supporto cooling."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.drp_climate_master_v2.plant.decision.comfort_band.mpc.provider import (
    ZonesMpcProvider,
)
from custom_components.drp_climate_master_v2.plant.decision.config import ZonesMpcConfig
from custom_components.drp_climate_master_v2.plant.decision.zone.config import (
    ControlConfig,
    MpcConfig,
    RcZoneParams,
)
from custom_components.drp_climate_master_v2.plant.decision.zone.model import ZonesDecision
from custom_components.drp_climate_master_v2.plant.decision.zone.planner import (
    ZoneDecisionPlanner,
)


def _make_snapshot(t_op: float, t_out: float, windows_closed: bool = True) -> MagicMock:
    """PlantSnapshot minimale con una zona 'living'."""
    snap = MagicMock()
    snap.timestamp = datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
    snap.windows_closed = windows_closed
    snap.presence_vacation = False
    snap.climate_preset_mode = None

    outdoor = MagicMock()
    outdoor.value = t_out
    outdoor.is_stale = False
    outdoor.is_insufficient = False
    snap.global_outdoor_temperature = outdoor
    snap.season = None

    zone = MagicMock()
    t_op_val = MagicMock()
    t_op_val.value = t_op
    zone.t_op = t_op_val
    zone.temperature = t_op_val
    valve = MagicMock()
    valve.value = 0.0
    zone.radiant_valve = valve
    snap.indoor_zones = {"living": zone}
    return snap


def _make_band(t_min: float, t_max: float) -> MagicMock:
    band = MagicMock()
    band.t_op_min = t_min
    band.t_op_max = t_max
    band.ok = True
    return band


_BAND_LIVING = _make_band(22.15, 24.68)
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
_CFG = ControlConfig(mpc=_MPC_FAST, rc_default=RcZoneParams())
_PLANNER = ZoneDecisionPlanner(cfg=_CFG)


class TestHeatingRetrocompat:
    """Il piano heating deve restare invariato."""

    def test_plan_no_cooling_param_returns_heating_plan(self):
        snap = _make_snapshot(t_op=21.0, t_out=5.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert isinstance(plan, ZonesDecision)
        assert plan.meta.get("cooling") is False

    def test_plan_cooling_false_equals_no_param(self):
        snap = _make_snapshot(t_op=21.0, t_out=5.0)
        p1 = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        p2 = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=False,
        )
        assert p1.zones.keys() == p2.zones.keys()
        for zk in p1.zones:
            assert p1.zones[zk].seq == p2.zones[zk].seq
            assert p1.zones[zk].valve_on == p2.zones[zk].valve_on

    def test_heating_cold_room_valve_on(self):
        snap = _make_snapshot(t_op=21.0, t_out=5.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan.zones["living"].valve_on is True


class TestCoolingPlan:
    """Il piano cooling deve aprire valvole per zone calde."""

    def test_plan_cooling_sets_meta_cooling_true(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.meta.get("cooling") is True

    def test_cooling_hot_room_valve_on(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.zones["living"].valve_on is True

    def test_cooling_in_band_room_valve_off(self):
        snap = _make_snapshot(t_op=23.5, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.zones["living"].valve_on is False

    def test_cooling_debug_has_cooling_flag(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.zones["living"].debug.get("cooling") is True

    def test_cooling_rc_uses_k_cool(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        k_active = plan.zones["living"].debug.get("rc", {}).get("k_active")
        assert k_active is not None
        assert k_active < 0

    def test_heating_rc_uses_k_heat(self):
        snap = _make_snapshot(t_op=21.0, t_out=5.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=False,
        )
        k_active = plan.zones["living"].debug.get("rc", {}).get("k_active")
        assert k_active is not None
        assert k_active > 0

    def test_cooling_kpi_propagated(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert "mpc_on_now_pct" in plan.meta
        assert "mpc_duty_avg_pct" in plan.meta
        assert "mpc_first_on_step" in plan.meta


class TestCoolingWindowPolicy:
    """La policy finestre per cooling deve dipendere da free_cool_feasible."""

    def test_windows_closed_cooling_allowed(self):
        snap = _make_snapshot(t_op=25.7, t_out=28.0, windows_closed=True)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.zones["living"].valve_on is True

    def test_windows_open_no_free_cool_cooling_allowed(self):
        snap = _make_snapshot(t_op=25.7, t_out=28.0, windows_closed=False)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        assert plan.zones["living"].valve_on is True

    def test_windows_open_free_cool_feasible_cooling_blocked(self):
        snap = _make_snapshot(t_op=25.7, t_out=21.0, windows_closed=False)
        cache = MagicMock()
        cache.free_cool_feasible = True
        snap._demand_signals_cache = cache
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=True,
        )
        cmd = plan.zones["living"]
        assert cmd.valve_on is False
        assert cmd.debug.get("skip") == "free_cool_feasible_windows_open"

    def test_heating_windows_open_cold_room_still_allowed(self):
        snap = _make_snapshot(t_op=20.0, t_out=5.0, windows_closed=False)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=False,
        )
        assert plan.zones["living"].valve_on is True

    def test_heating_windows_open_mild_deficit_blocked(self):
        snap = _make_snapshot(t_op=21.5, t_out=5.0, windows_closed=False)
        plan = _PLANNER.plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
            cooling=False,
        )
        assert plan.zones["living"].valve_on is False
        assert plan.zones["living"].debug.get("skip") == "windows_or_policy"


class TestProviderCoolingGating:
    """maybe_plan_cooling deve rispettare il gating stagionale."""

    def _make_provider(self, **kwargs):
        cfg = ZonesMpcConfig(**kwargs)
        return ZonesMpcProvider(cfg=cfg, planner=_PLANNER)

    def _snap_season(
        self,
        season_val: str,
        t_op: float = 25.7,
        t_out: float = 21.0,
    ) -> MagicMock:
        snap = _make_snapshot(t_op=t_op, t_out=t_out)
        season_inner = MagicMock()
        season_inner.value = season_val
        season_outer = MagicMock()
        season_outer.season = season_inner
        snap.season = season_outer
        return snap

    def test_summer_cooling_enabled_by_default(self):
        provider = self._make_provider()
        snap = self._snap_season("summer", t_op=25.7, t_out=30.0)
        plan = provider.maybe_plan_cooling(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan is not None

    def test_shoulder_cooling_enabled_by_default(self):
        provider = self._make_provider()
        snap = self._snap_season("spring", t_op=25.7, t_out=21.0)
        plan = provider.maybe_plan_cooling(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan is not None

    def test_winter_cooling_disabled_by_default(self):
        provider = self._make_provider()
        snap = self._snap_season("winter", t_op=25.0, t_out=5.0)
        plan = provider.maybe_plan_cooling(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan is None

    def test_winter_cooling_enabled_when_configured(self):
        provider = self._make_provider(run_cooling_in_winter=True)
        snap = self._snap_season("winter", t_op=25.0, t_out=5.0)
        plan = provider.maybe_plan_cooling(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan is not None

    def test_maybe_plan_still_returns_heating(self):
        provider = self._make_provider()
        snap = self._snap_season("summer", t_op=21.0, t_out=30.0)
        plan = provider.maybe_plan(
            snapshot=snap,
            reason="test",
            comfort_bands_by_zone={"living": _BAND_LIVING},
        )
        assert plan is None

    def test_disabled_provider_returns_none(self):
        provider = self._make_provider(enabled=False)
        snap = self._snap_season("summer")
        assert provider.maybe_plan_cooling(snapshot=snap, reason="test") is None
