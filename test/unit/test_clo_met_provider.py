"""Test suite per il modulo CloMetProvider."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile
from custom_components.drp_climate_master_v2.domain.models.season import OperativeSeason
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.clo_provider import (
    CloProvider,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.config import (
    CloMetConfig,
    CloS4Config,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.met_provider import (
    MetProvider,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.model import (
    RoomType,
    TimeOfDay,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.provider import (
    ComfortParameterProvider,
)


@pytest.fixture
def cfg() -> CloMetConfig:
    return CloMetConfig(climate_zone="D")


@pytest.fixture
def clo(cfg: CloMetConfig) -> CloProvider:
    return CloProvider(cfg)


@pytest.fixture
def met(cfg: CloMetConfig) -> MetProvider:
    return MetProvider(cfg)


def _clo(
    provider: CloProvider,
    season: OperativeSeason,
    progress: float | None = None,
    direction: str | None = None,
    profile: HVACOperatingProfile = HVACOperatingProfile.ECO,
    tod: TimeOfDay = TimeOfDay.DAYTIME,
    cold_snap: bool = False,
    t_rm: float | None = None,
    t_op: float | None = None,
):
    return provider.compute(
        operative_season=season,
        season_progress=progress,
        shoulder_direction=direction,
        profile=profile,
        time_of_day=tod,
        cold_snap=cold_snap,
        t_op_running_mean=t_rm,
        t_op_current=t_op,
    )


def _met(
    provider: MetProvider,
    room_type: RoomType,
    profile: HVACOperatingProfile = HVACOperatingProfile.ECO,
    tod: TimeOfDay = TimeOfDay.DAYTIME,
):
    return provider.compute(room_type=room_type, profile=profile, time_of_day=tod)


class TestCloSeasonBase:
    def test_summer_base(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SUMMER)
        assert r.season_base == pytest.approx(0.50, abs=0.01)
        assert r.s4_delta == pytest.approx(0.0, abs=0.001)

    def test_winter_base_zone_d(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER)
        assert r.season_base == pytest.approx(1.05, abs=0.01)

    def test_shoulder_base_no_progress(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER)
        assert r.season_base == pytest.approx(0.82, abs=0.01)
        assert r.s4_delta == pytest.approx(0.0, abs=0.001)

    def test_s4_spring_progress_90pct(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER, progress=90.1, direction="spring")
        assert r.s4_delta < 0
        assert r.value < r.season_base
        assert r.value == pytest.approx(0.82 + r.s4_delta, abs=0.01)

    def test_s4_spring_progress_below_ramp_start(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER, progress=30.0, direction="spring")
        assert r.s4_delta == pytest.approx(0.0, abs=0.001)

    def test_s4_autumn_raises_clo(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER, progress=80.0, direction="autumn")
        assert r.s4_delta > 0

    def test_s4_disabled(self) -> None:
        cfg_no_s4 = CloMetConfig(s4=CloS4Config(enabled=False), climate_zone="D")
        prov = CloProvider(cfg_no_s4)
        r = _clo(prov, OperativeSeason.SHOULDER, progress=90.0, direction="spring")
        assert r.s4_delta == pytest.approx(0.0, abs=0.001)


class TestCloColdSnap:
    def test_cold_snap_raises_shoulder_clo(self, clo: CloProvider) -> None:
        r_normal = _clo(clo, OperativeSeason.SHOULDER, cold_snap=False)
        r_snap = _clo(clo, OperativeSeason.SHOULDER, cold_snap=True)
        assert r_snap.cold_snap_delta > 0
        assert r_snap.value > r_normal.value

    def test_cold_snap_no_effect_in_summer(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SUMMER, cold_snap=True)
        assert r.cold_snap_delta == pytest.approx(0.0, abs=0.001)

    def test_cold_snap_no_effect_in_winter(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, cold_snap=True)
        assert r.cold_snap_delta == pytest.approx(0.0, abs=0.001)


class TestCloSleepProfile:
    def test_sleep_winter_adds_delta(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, profile=HVACOperatingProfile.SLEEP)
        assert r.profile_delta == pytest.approx(0.95, abs=0.01)
        assert r.value == pytest.approx(min(2.40, 1.05 + 0.95), abs=0.05)

    def test_sleep_shoulder_adds_delta(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER, profile=HVACOperatingProfile.SLEEP)
        assert r.profile_delta == pytest.approx(0.70, abs=0.01)

    def test_sleep_summer_no_delta(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SUMMER, profile=HVACOperatingProfile.SLEEP)
        assert r.profile_delta == pytest.approx(0.0, abs=0.001)

    def test_sleep_s3_suppressed(self, clo: CloProvider) -> None:
        r = _clo(
            clo,
            OperativeSeason.WINTER,
            profile=HVACOperatingProfile.SLEEP,
            t_rm=15.0,
            t_op=20.0,
        )
        assert r.s3_delta == pytest.approx(0.0, abs=0.001)


class TestCloS3Adaptive:
    def test_s3_cold_house_raises_clo(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, t_rm=18.0, t_op=20.0)
        assert r.s3_delta > 0

    def test_s3_warm_house_lowers_clo(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.SHOULDER, t_rm=25.0, t_op=26.0)
        assert r.s3_delta < 0

    def test_s3_suppressed_for_deviation_too_large(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, t_rm=14.0, t_op=21.0)
        assert r.s3_delta == pytest.approx(0.0, abs=0.001)

    def test_s3_suppressed_if_t_rm_none(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, t_rm=None, t_op=21.0)
        assert r.s3_delta == pytest.approx(0.0, abs=0.001)

    def test_s3_cap_applied(self, clo: CloProvider) -> None:
        r = _clo(clo, OperativeSeason.WINTER, t_rm=10.0, t_op=13.0)
        assert r.s3_delta == pytest.approx(0.25, abs=0.001)


class TestCloTimeOfDay:
    def test_evening_lower_than_daytime(self, clo: CloProvider) -> None:
        r_day = _clo(clo, OperativeSeason.SHOULDER, tod=TimeOfDay.DAYTIME)
        r_eve = _clo(clo, OperativeSeason.SHOULDER, tod=TimeOfDay.EVENING)
        assert r_eve.value < r_day.value

    def test_morning_higher_than_daytime(self, clo: CloProvider) -> None:
        r_day = _clo(clo, OperativeSeason.SHOULDER, tod=TimeOfDay.DAYTIME)
        r_mor = _clo(clo, OperativeSeason.SHOULDER, tod=TimeOfDay.MORNING)
        assert r_mor.value > r_day.value


class TestMetRoomType:
    def test_kitchen_higher_than_living(self, met: MetProvider) -> None:
        r_kit = _met(met, RoomType.KITCHEN)
        r_liv = _met(met, RoomType.LIVING)
        assert r_kit.value > r_liv.value

    def test_bathroom_higher_than_bedroom(self, met: MetProvider) -> None:
        r_bath = _met(met, RoomType.BATHROOM)
        r_bed = _met(met, RoomType.BEDROOM)
        assert r_bath.value > r_bed.value

    def test_kitchen_daytime_delta_applied(self, met: MetProvider) -> None:
        r_day = _met(met, RoomType.KITCHEN, tod=TimeOfDay.DAYTIME)
        r_eve = _met(met, RoomType.KITCHEN, tod=TimeOfDay.EVENING)
        assert r_day.value > r_eve.value


class TestMetProfileOverride:
    def test_sleep_override_is_070(self, met: MetProvider) -> None:
        for room in RoomType:
            r = _met(met, room, profile=HVACOperatingProfile.SLEEP)
            assert r.value == pytest.approx(0.70, abs=0.01)
            assert r.profile_override == pytest.approx(0.70, abs=0.01)

    def test_away_override_is_100(self, met: MetProvider) -> None:
        r = _met(met, RoomType.KITCHEN, profile=HVACOperatingProfile.AWAY)
        assert r.value == pytest.approx(1.00, abs=0.01)


class TestGlobalWeightedMean:
    def _provider(self) -> ComfortParameterProvider:
        cfg = CloMetConfig(climate_zone="D")
        room_map = {
            "living": RoomType.LIVING,
            "kitchen": RoomType.KITCHEN,
            "bedroom": RoomType.BEDROOM,
        }
        return ComfortParameterProvider(cfg=cfg, room_type_map=room_map)

    def test_global_clo_is_weighted_mean(self) -> None:
        prov = self._provider()
        zone_ids = ["living", "kitchen", "bedroom"]
        weights = {"living": 28.0, "kitchen": 12.0, "bedroom": 20.0}
        all_params = prov.compute_all(
            zone_ids=zone_ids,
            operative_season=OperativeSeason.SHOULDER,
            season_progress=90.1,
            shoulder_direction="spring",
            profile=HVACOperatingProfile.ECO,
            time_of_day=TimeOfDay.EVENING,
            cold_snap=False,
            zone_weights=weights,
        )
        total_w = 28.0 + 12.0 + 20.0
        expected_clo = sum(all_params[z].clo.value * weights[z] for z in zone_ids) / total_w
        assert all_params["global"].clo.value == pytest.approx(expected_clo, abs=0.001)

    def test_global_clo_differs_from_fixed_shoulder_seed(self) -> None:
        prov = self._provider()
        all_params = prov.compute_all(
            zone_ids=["living", "kitchen", "bedroom"],
            operative_season=OperativeSeason.SHOULDER,
            season_progress=90.1,
            shoulder_direction="spring",
            profile=HVACOperatingProfile.ECO,
            time_of_day=TimeOfDay.DAYTIME,
            cold_snap=False,
            zone_weights={"living": 28.0, "kitchen": 12.0, "bedroom": 20.0},
        )
        assert all_params["global"].clo.value < 0.70


class TestComputeAll:
    def test_all_zones_and_global_present(self) -> None:
        cfg = CloMetConfig(climate_zone="D")
        room_map = {
            z: RoomType.LIVING
            for z in [
                "living",
                "kitchen",
                "bedroom",
                "guest_bedroom",
                "master_bathroom",
                "main_bathroom",
            ]
        }
        prov = ComfortParameterProvider(cfg=cfg, room_type_map=room_map)
        result = prov.compute_all(
            zone_ids=list(room_map.keys()),
            operative_season=OperativeSeason.SHOULDER,
            season_progress=90.1,
            shoulder_direction="spring",
            profile=HVACOperatingProfile.ECO,
            time_of_day=TimeOfDay.from_hour(9),
            cold_snap=False,
            zone_weights={z: 15.0 for z in room_map},
        )
        assert "global" in result
        assert len(result) == len(room_map) + 1
        for params in result.values():
            assert 0.20 <= params.clo.value <= 2.40
            assert 0.50 <= params.met.value <= 4.00
