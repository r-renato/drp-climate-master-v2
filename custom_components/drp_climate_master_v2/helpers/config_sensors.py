from __future__ import annotations

from datetime import timedelta
from typing import List

from .utils import slugify

from ..domain.models.runtime_schema import AreaConfig, ClimateConfig, SupplyUnitsConfig

from .sensor_aggregator import DerivedSpec, FilterConfig, GroupSpec, MappingConfig, SensorSpec, ZoneConfig


def _build_indoor_zones(areas: List[AreaConfig]) -> tuple[ZoneConfig, ...]:
    zones: List[ZoneConfig] = []

    indoor_temp_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        min_valid=5.0,
        max_valid=35.0,
        time_hampel_k=4.0,
        max_rate_per_min=0.6,  # °C/min
        rate_limit_mode="clip",
        ema_alpha=0.2,
        rolling_median_window=3,
    )

    indoor_rh_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=4.0,
        max_rate_per_min=8.0,  # %RH/min
        rate_limit_mode="clip",
        ema_alpha=0.25,
        rolling_median_window=3,
    )

    for area in areas:
        name = slugify(area.name)
        if area.indoor:
            zones.append(
                ZoneConfig(
                    zone=name,
                    weight=area.ceiling or 0.0,
                    variables=(
                        GroupSpec(
                            name="indoor_temperature",
                            sensors=(SensorSpec(area.sensors.temperature, weight=1.0, filters=indoor_temp_filters),),
                            method="weighted_mean",
                            cross_outlier_method="none",
                            min_sources=1,
                            clamp_min=5.0,
                            clamp_max=35.0 if area.radiant else 60.0,
                        ),
                        GroupSpec(
                            name="indoor_humidity",
                            sensors=(SensorSpec(area.sensors.humidity, weight=1.0, filters=indoor_rh_filters),),
                            method="weighted_mean",
                            cross_outlier_method="none",
                            min_sources=1,
                            clamp_min=1.0,
                            clamp_max=100.0,
                        ),
                    ),
                )
            )

    return tuple(zones)

def _build_outdoor_zones(areas: List[AreaConfig]) -> tuple[ZoneConfig, ...]:
    zones: List[ZoneConfig] = []

    outdoor_temp_filters = FilterConfig(
        max_age=timedelta(minutes=20),
        hold_last_good=timedelta(minutes=10),
        min_valid=-30.0,
        max_valid=55.0,
        time_hampel_k=6.0,
        max_rate_per_min=3.0,  # °C/min
        rate_limit_mode="clip",
        ema_alpha=0.15,
        rolling_median_window=1,
    )

    outdoor_rh_filters = FilterConfig(
        max_age=timedelta(minutes=20),
        hold_last_good=timedelta(minutes=10),
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=6.0,
        max_rate_per_min=20.0,  # %RH/min
        rate_limit_mode="clip",
        ema_alpha=0.15,
        rolling_median_window=1,
    )

    for area in areas:
        name = slugify(area.name)
        if not area.indoor:
            zones.append(
                ZoneConfig(
                    zone=name,
                    weight=1.0,
                    variables=(
                        GroupSpec(
                            name="outdoor_temperature",
                            sensors=(
                                SensorSpec(area.sensors.temperature, weight=1.0, filters=outdoor_temp_filters),
                                SensorSpec("sensor.hmi080_outdoor_temperature", weight=1.0, filters=outdoor_temp_filters),
                            ),
                            method="weighted_mean",
                            cross_outlier_method="none",
                            min_sources=1,
                            clamp_min=-30.0,
                            clamp_max=55.0,
                        ),
                        GroupSpec(
                            name="outdoor_humidity",
                            sensors=(SensorSpec(area.sensors.humidity, weight=1.0, filters=outdoor_rh_filters),),
                            method="weighted_mean",
                            cross_outlier_method="none",
                            min_sources=1,
                            clamp_min=1.0,
                            clamp_max=100.0,
                        ),
                    ),
                )
            )

    return tuple(zones)

def _build_plant_radiant(supply_units: SupplyUnitsConfig) -> tuple[ZoneConfig, ...]:
    
    radiant_water_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        min_valid=5.0,
        max_valid=60.0,
        time_hampel_k=6.0,
        max_rate_per_min=5.0,  # °C/min
        rate_limit_mode="clip",
        ema_alpha=0.2,
        rolling_median_window=1,
    )

    return (ZoneConfig(
                zone="plant_radiant",
                weight=0.0,
                variables=(
                    GroupSpec(
                        name="adj_supply_temperature",
                        sensors=(
                            SensorSpec(
                                supply_units.sensors.adjustable_temp_system_supply,
                                weight=1.0,
                                filters=radiant_water_filters,
                            ),
                        ),
                        method="weighted_mean",
                        cross_outlier_method="none",
                        min_sources=1,
                        clamp_min=5.0,
                        clamp_max=60.0,
                    ),
                    GroupSpec(
                        name="adj_return_temperature",
                        sensors=(
                            SensorSpec(
                                supply_units.sensors.adjustable_temp_system_return,
                                weight=1.0,
                                filters=radiant_water_filters,
                            ),
                        ),
                        method="weighted_mean",
                        cross_outlier_method="none",
                        min_sources=1,
                        clamp_min=5.0,
                        clamp_max=60.0,
                    ),
                ),
            ),
    )

def _build_psychro_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    psychro_derived: list[DerivedSpec] = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            psychro_derived.append(
                DerivedSpec(
                    name=f"{name}.indoor_dew_point",
                    kind="compute",
                    compute="dew_point_c",
                    inputs=(
                        (f"{name}.indoor_temperature", 1.0),
                        (f"{name}.indoor_humidity", 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-20.0,
                    clamp_max=30.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                )
            )
            psychro_derived.append(
                DerivedSpec(
                    name=f"{name}.indoor_heat_index",
                    kind="compute",
                    compute="heat_index_c",
                    inputs=(
                        (f"{name}.indoor_temperature", 1.0),
                        (f"{name}.indoor_humidity", 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-20.0,
                    clamp_max=60.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                )
            )
            psychro_derived.append(
                DerivedSpec(
                    name=f"{name}.condensation_margin",
                    kind="compute",
                    compute="condensation_margin_c",
                    inputs=(
                        ("global.radiant_mean_temperature", 1.0),
                        (f"{name}.indoor_dew_point", 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-20.0,
                    clamp_max=30.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                ),
            )

    return tuple(psychro_derived)

def _build_mrt_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    """Mean Radiant Temperature"""
    mrt_derived: list[DerivedSpec] = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            mrt_derived.append(
                DerivedSpec(
                    name=f"{name}.mrt",
                    kind="compute",
                    compute="mrt_c",
                    inputs=(
                        (f"{name}.indoor_temperature", 1.0),
                        ("global.radiant_mean_temperature", 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-10.0,
                    clamp_max=40.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                )
            )
            mrt_derived.append(
                DerivedSpec(
                    name=f"{name}.t_op",
                    kind="compute",
                    compute="t_op_c",
                    inputs=(
                        (f"{name}.indoor_temperature", 1.0),
                        (f"{name}.mrt", 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-10.0,
                    clamp_max=40.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                )
            )

    return tuple(mrt_derived)

def _build_global_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    indoor_temp_inputs = []
    indoor_rh_inputs = []
    indoor_dp_inputs = []
    indoor_hi_inputs = []
    indoor_cm_inputs = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            indoor_temp_inputs.append((f"{name}.indoor_temperature", area.ceiling))
            indoor_rh_inputs.append((f"{name}.indoor_humidity", 1.0))
            indoor_dp_inputs.append((f"{name}.indoor_dew_point", 1.0))
            indoor_hi_inputs.append((f"{name}.indoor_heat_index", 1.0))
            indoor_cm_inputs.append((f"{name}.condensation_margin", 1.0))

    return (
        DerivedSpec(
            name="global.indoor_temperature",
            kind="aggregate",
            inputs=tuple(indoor_temp_inputs),
            method="weighted_mean",
            min_sources=2,
            clamp_min=5.0,
            clamp_max=35.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name="global.indoor_humidity",
            kind="aggregate",
            inputs=tuple(indoor_rh_inputs),
            method="median",
            min_sources=2,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        # Conservative: max dew point among representative zones (exclude foyer duplication)
        DerivedSpec(
            name="global.indoor_dew_point",
            kind="aggregate",
            inputs=tuple(indoor_dp_inputs),
            method="max",
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=30.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        # Keep prior behavior: max heat index among zones (mostly meaningful in summer)
        DerivedSpec(
            name="global.indoor_heat_index",
            kind="aggregate",
            inputs=tuple(indoor_hi_inputs),
            method="max",
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name="global.outdoor_temperature",
            kind="aggregate",
            inputs=(("terrace.outdoor_temperature", 1.0),),
            method="weighted_mean",
            min_sources=1,
            clamp_min=-30.0,
            clamp_max=55.0,
            max_age=timedelta(minutes=20),
            hold_last_good=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.outdoor_humidity",
            kind="aggregate",
            inputs=(("terrace.outdoor_humidity", 1.0),),
            method="weighted_mean",
            min_sources=1,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=20),
            hold_last_good=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.outdoor_dew_point",
            kind="compute",
            compute="dew_point_c",
            inputs=(
                ("global.outdoor_temperature", 1.0),
                ("global.outdoor_humidity", 1.0),
            ),
            min_sources=2,
            max_age=timedelta(minutes=15),
            hold_last_good=timedelta(minutes=10),  # opzionale ma utile
        ),
        DerivedSpec(
            name="global.radiant_mean_temperature",
            kind="aggregate",
            inputs=(
                ("plant_radiant.adj_supply_temperature", 1.0),
                ("plant_radiant.adj_return_temperature", 1.0),
            ),
            method="weighted_mean",
            min_sources=1,
            clamp_min=5.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name="global.condensation_margin_min",
            kind="aggregate",
            inputs=tuple(indoor_cm_inputs),
            method="min",
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=30.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
    )

def build_sensor_mapping(climate: ClimateConfig) -> MappingConfig:
    """Mapping built from the provided areas configuration."""
    return MappingConfig(
        zones=_build_indoor_zones(climate.areas) \
                + _build_outdoor_zones(climate.areas) \
                + _build_plant_radiant(climate.devices.supply_units),
        derived=_build_global_derived(climate.areas) \
                + _build_psychro_derived(climate.areas) 
                + _build_mrt_derived(climate.areas)
    )