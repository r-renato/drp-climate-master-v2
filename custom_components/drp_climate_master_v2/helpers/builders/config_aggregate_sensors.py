from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import List

from ..utils import slugify

from ...domain.models.runtime_schema import (
    AreaConfig, 
    ClimateConfig, 
    SupplyUnitsConfig
)
from ..sensor_aggregator import (
    AggregationMethod,
    ComputeFn,
    CrossOutlierMethod,
    DerivedKind,
    DerivedSpec, 
    FilterConfig, 
    GroupSpec, 
    MappingConfig, 
    RateLimitMode, 
    SensorSpec, 
    ZoneConfig
)

GLOBAL = "global"
TERRACE = "terrace"
PLANT_RADIANT = "plant_radiant"

class FieldSuffix(StrEnum):
    INDOOR_TEMPERATURE = "indoor_temperature"
    INDOOR_HUMIDITY = "indoor_humidity"
    INDOOR_DEW_POINT = "indoor_dew_point"
    INDOOR_HEAT_INDEX = "indoor_heat_index"

    RADIANT_VALVE_OPEN = "radiant_valve_open"

    OUTDOOR_TEMPERATURE = "outdoor_temperature"
    OUTDOOR_HUMIDITY = "outdoor_humidity"
    OUTDOOR_DEW_POINT = "outdoor_dew_point"
    
    CONDENSATION_MARGIN = "condensation_margin"
    MRT = "mrt"
    T_OPT = "t_op"

    ADJ_SUPPLY_TEMPERATURE = "adj_supply_temperature"
    ADJ_RETURN_TEMPERATURE = "adj_return_temperature"
    ADJ_SUPPLY_ON = "adj_supply_on"

    WATER_MEAN_TEMPERATURE = "water_mean_temperature"
    PLANT_ACTIVE = "plant_active"

    def __call__(self, prefix: str, sep: str = ".") -> str:
        return f"{prefix}{sep}{self}"


def _build_indoor_zones(areas: List[AreaConfig]) -> tuple[ZoneConfig, ...]:
    zones: List[ZoneConfig] = []

    indoor_temp_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        min_valid=5.0,
        max_valid=35.0,
        # Indoor: Hampel un filo meno aggressivo (evita falsi outlier quando MAD è molto piccolo)
        time_hampel_k=5.0,
        time_hampel_min_samples=12,
        max_rate_per_min=0.6,  # °C/min
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.2,
        rolling_median_window=3,
        raw_history_size=60,
        filtered_history_size=60,
    )

    indoor_rh_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        hold_last_good=timedelta(minutes=5),
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=None, #4.0,
        max_rate_per_min=8.0,  # %RH/min
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.25,
        rolling_median_window=3,
    )

    switch_filters = FilterConfig(
        auto_unit_convert=False,
        max_age=timedelta(minutes=5),
        # Switch: può restare invariato giorni; vogliamo solo resilienza a glitch brevi
        hold_last_good=timedelta(minutes=10),
        min_valid=0.0,
        max_valid=1.0,
        time_hampel_k=None,          # evita Hampel su switch
        max_rate_per_min=None,       # niente rate limit su switch
        ema_alpha=1.0,               # niente EMA su switch
        rolling_median_window=1,
    )

    for area in areas:
        name = slugify(area.name)
        if area.indoor:
            if area.radiant and area.thermal_collector_valve_switch:
                variables = (
                        GroupSpec(
                            name=FieldSuffix.INDOOR_TEMPERATURE,
                            sensors=(SensorSpec(area.sensors.temperature, weight=1.0, filters=indoor_temp_filters),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=5.0,
                            clamp_max=35.0 if area.radiant else 60.0,
                        ),
                        GroupSpec(
                            name=FieldSuffix.INDOOR_HUMIDITY,
                            sensors=(SensorSpec(area.sensors.humidity, weight=1.0, filters=indoor_rh_filters),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=1.0,
                            clamp_max=100.0,
                        ),
                        GroupSpec(
                            name=FieldSuffix.RADIANT_VALVE_OPEN,
                            sensors=(SensorSpec(area.thermal_collector_valve_switch,weight=1.0,filters=switch_filters,),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=0.0,
                            clamp_max=1.0,
                        ),
                )
            else:
                variables = (
                        GroupSpec(
                            name=FieldSuffix.INDOOR_TEMPERATURE.value,
                            sensors=(SensorSpec(area.sensors.temperature, weight=1.0, filters=indoor_temp_filters),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=5.0,
                            clamp_max=35.0 if area.radiant else 60.0,
                        ),
                        GroupSpec(
                            name=FieldSuffix.INDOOR_HUMIDITY.value,
                            sensors=(SensorSpec(area.sensors.humidity, weight=1.0, filters=indoor_rh_filters),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=1.0,
                            clamp_max=100.0,
                        ),
                )

            zones.append(
                ZoneConfig(
                    zone=name,
                    weight=area.ceiling or 0.0,
                    variables=variables,
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
        # Outdoor: Hampel time-series tende a fare falsi positivi (MAD piccolo) -> disabilitato.
        # Affidati a range + rate-limit + smoothing.
        time_hampel_k=None,
        max_rate_per_min=3.0,  # °C/min
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.15,
        rolling_median_window=1,
    )

    outdoor_rh_filters = FilterConfig(
        max_age=timedelta(minutes=20),
        hold_last_good=timedelta(minutes=10),
        min_valid=1.0,
        max_valid=100.0,
        # Outdoor RH: stessa motivazione dell'outdoor temp (MAD piccolo -> falsi outlier)
        time_hampel_k=None,
        max_rate_per_min=20.0,  # %RH/min
        rate_limit_mode=RateLimitMode.CLIP,
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
                            name=FieldSuffix.OUTDOOR_TEMPERATURE,
                            sensors=(
                                SensorSpec(area.sensors.temperature, weight=1.0, filters=outdoor_temp_filters),
                                SensorSpec("sensor.hmi080_outdoor_temperature", weight=1.0, filters=outdoor_temp_filters),
                            ),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
                            min_sources=1,
                            clamp_min=-30.0,
                            clamp_max=55.0,
                        ),
                        GroupSpec(
                            name=FieldSuffix.OUTDOOR_HUMIDITY,
                            sensors=(SensorSpec(area.sensors.humidity, weight=1.0, filters=outdoor_rh_filters),),
                            method=AggregationMethod.WEIGHTED_MEAN,
                            cross_outlier_method=CrossOutlierMethod.NONE,
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
        rate_limit_mode=RateLimitMode.CLIP,
        ema_alpha=0.2,
        rolling_median_window=1,
    )

    switch_filters = FilterConfig(
        auto_unit_convert=False,
        max_age=timedelta(minutes=5),
        # Pump/supply enable: resilienza a glitch; non “scade” se resta invariato
        hold_last_good=timedelta(minutes=10),
        min_valid=0.0,
        max_valid=1.0,
        time_hampel_k=None,          # evita Hampel su switch
        max_rate_per_min=None,       # niente rate limit su switch
        ema_alpha=1.0,               # niente EMA su switch
        rolling_median_window=1,
    )

    return (ZoneConfig(
                zone=PLANT_RADIANT,
                weight=0.0,
                variables=(
                    GroupSpec(
                        name=FieldSuffix.ADJ_SUPPLY_TEMPERATURE,
                        sensors=(
                            SensorSpec(
                                supply_units.sensors.adjustable_temp_system_supply,
                                weight=1.0,
                                filters=radiant_water_filters,
                            ),
                        ),
                        method=AggregationMethod.WEIGHTED_MEAN,
                        cross_outlier_method=CrossOutlierMethod.NONE,
                        min_sources=1,
                        clamp_min=5.0,
                        clamp_max=60.0,
                    ),
                    GroupSpec(
                        name=FieldSuffix.ADJ_RETURN_TEMPERATURE,
                        sensors=(
                            SensorSpec(
                                supply_units.sensors.adjustable_temp_system_return,
                                weight=1.0,
                                filters=radiant_water_filters,
                            ),
                        ),
                        method=AggregationMethod.WEIGHTED_MEAN,
                        cross_outlier_method=CrossOutlierMethod.NONE,
                        min_sources=1,
                        clamp_min=5.0,
                        clamp_max=60.0,
                    ),
                    GroupSpec(
                        name=FieldSuffix.ADJ_SUPPLY_ON,
                        sensors=(SensorSpec(supply_units.adjustable_supply_unit, weight=1.0, filters=switch_filters),),
                        method=AggregationMethod.WEIGHTED_MEAN,
                        cross_outlier_method=CrossOutlierMethod.NONE,
                        min_sources=1,
                        clamp_min=0.0,
                        clamp_max=1.0,
                    ),
                ),
            ),
    )

def _build_plant_water_derived() -> tuple[DerivedSpec, ...]:
    """
    Plant water proxy (NOT MRT):
    - water_mean_temperature is an hydraulic mean (supply/return) and must NOT be used as MRT directly.
    """
    return (
        DerivedSpec(
            name=FieldSuffix.WATER_MEAN_TEMPERATURE(PLANT_RADIANT),
            kind=DerivedKind.AGGREGATE,
            inputs=(
                (FieldSuffix.ADJ_SUPPLY_TEMPERATURE(PLANT_RADIANT), 1.0), 
                (FieldSuffix.ADJ_RETURN_TEMPERATURE(PLANT_RADIANT), 1.0),
            ),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=1,
            clamp_min=5.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
    )

def _build_psychro_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    psychro_derived: list[DerivedSpec] = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            psychro_derived.append(
                DerivedSpec(
                    name=FieldSuffix.INDOOR_DEW_POINT(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.DEW_POINT_C,
                    inputs=(
                        (FieldSuffix.INDOOR_TEMPERATURE(name), 1.0),
                        (FieldSuffix.INDOOR_HUMIDITY(name), 1.0),
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
                    name=FieldSuffix.INDOOR_HEAT_INDEX(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.HEAT_INDEX_C,
                    inputs=(
                        (FieldSuffix.INDOOR_TEMPERATURE(name), 1.0),
                        (FieldSuffix.INDOOR_HUMIDITY(name), 1.0),
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
                    name=FieldSuffix.CONDENSATION_MARGIN(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.CONDENSATION_MARGIN_C,
                    inputs=(
                        # (C) Conservative proxy: use the coldest relevant hydraulic point (supply)
                        # rather than a mean water temperature or a misnamed "radiant mean".
                        (FieldSuffix.ADJ_SUPPLY_TEMPERATURE(PLANT_RADIANT), 1.0),
                        (FieldSuffix.INDOOR_DEW_POINT(name), 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-20.0,
                    clamp_max=30.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                ),
            )
        elif not area.indoor and not area.radiant:
            psychro_derived.append(
                DerivedSpec(
                    name=FieldSuffix.OUTDOOR_DEW_POINT(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.DEW_POINT_C,
                    inputs=(
                        (FieldSuffix.OUTDOOR_TEMPERATURE(name), 1.0),
                        (FieldSuffix.OUTDOOR_HUMIDITY(name), 1.0),
                    ),
                    min_sources=2,
                    clamp_min=-30.0,
                    clamp_max=30.0,
                    max_age=timedelta(minutes=20),
                    hold_last_good=timedelta(minutes=10),
                )
            )

    return tuple(psychro_derived)

def _build_actuation_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    """Derived boolean-ish signals (0/1) about real actuation per zone."""
    out: list[DerivedSpec] = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            out.append(
                DerivedSpec(
                    name=FieldSuffix.PLANT_ACTIVE(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.AND01,
                    inputs=(
                        (FieldSuffix.ADJ_SUPPLY_ON(PLANT_RADIANT), 1.0),  # pump
                        (FieldSuffix.RADIANT_VALVE_OPEN(name), 1.0),      # valve zona
                    ),
                    min_sources=2,
                    clamp_min=0.0,
                    clamp_max=1.0,
                    max_age=timedelta(minutes=5),
                    # Evita degradi gratuiti del gating MRT per micro-buchi su switch/valvole
                    hold_last_good=timedelta(minutes=10),
                )
            )

    return tuple(out)

def _build_mrt_derived(areas: List[AreaConfig]) -> tuple[DerivedSpec, ...]:
    """Mean Radiant Temperature"""
    mrt_derived: list[DerivedSpec] = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            mrt_derived.append(
                DerivedSpec(
                    name=FieldSuffix.MRT(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.MRT_GATED_C,
                    inputs=(
                        (FieldSuffix.INDOOR_TEMPERATURE(name), 1.0),
                        (FieldSuffix.WATER_MEAN_TEMPERATURE(PLANT_RADIANT), 1.0),
                        (FieldSuffix.PLANT_ACTIVE(name), 1.0),  # <-- nuovo input
                    ),
                    min_sources=3,
                    clamp_min=-10.0,
                    clamp_max=40.0,
                    max_age=timedelta(minutes=10),
                    hold_last_good=timedelta(minutes=5),
                )
            )

            mrt_derived.append(
                DerivedSpec(
                    name=FieldSuffix.T_OPT(name),
                    kind=DerivedKind.COMPUTE,
                    compute=ComputeFn.T_OP_C,
                    inputs=(
                        (FieldSuffix.INDOOR_TEMPERATURE(name), 1.0),
                        (FieldSuffix.MRT(name), 1.0),
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
    indoor_mrt_inputs = []

    for area in areas:
        name = slugify(area.name)
        if area.indoor and area.radiant and area.ceiling:
            indoor_temp_inputs.append((FieldSuffix.INDOOR_TEMPERATURE(name), area.ceiling))
            indoor_rh_inputs.append((FieldSuffix.INDOOR_HUMIDITY(name), 1.0))
            indoor_dp_inputs.append((FieldSuffix.INDOOR_DEW_POINT(name), 1.0))
            indoor_hi_inputs.append((FieldSuffix.INDOOR_HEAT_INDEX(name), 1.0))
            indoor_cm_inputs.append((FieldSuffix.CONDENSATION_MARGIN(name), 1.0))
            indoor_mrt_inputs.append((FieldSuffix.MRT(name), area.ceiling))

    return (
        DerivedSpec(
            name=FieldSuffix.INDOOR_TEMPERATURE(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_temp_inputs),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=2,
            clamp_min=5.0,
            clamp_max=35.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name=FieldSuffix.INDOOR_HUMIDITY(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_rh_inputs),
            method=AggregationMethod.MEDIAN,
            min_sources=2,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        # Conservative: max dew point among representative zones (exclude foyer duplication)
        DerivedSpec(
            name=FieldSuffix.INDOOR_DEW_POINT(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_dp_inputs),
            method=AggregationMethod.MAX,
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=30.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        # Keep prior behavior: max heat index among zones (mostly meaningful in summer)
        DerivedSpec(
            name=FieldSuffix.INDOOR_HEAT_INDEX(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_hi_inputs),
            method=AggregationMethod.MAX,
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name=FieldSuffix.OUTDOOR_TEMPERATURE(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=((FieldSuffix.OUTDOOR_TEMPERATURE(TERRACE), 1.0),),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=1,
            clamp_min=-30.0,
            clamp_max=55.0,
            max_age=timedelta(minutes=20),
            hold_last_good=timedelta(minutes=10),
        ),
        DerivedSpec(
            name=FieldSuffix.OUTDOOR_HUMIDITY(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=((FieldSuffix.OUTDOOR_HUMIDITY(TERRACE), 1.0),),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=1,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=20),
            hold_last_good=timedelta(minutes=10),
        ),
        DerivedSpec(
            name=FieldSuffix.OUTDOOR_DEW_POINT(GLOBAL),
            kind=DerivedKind.COMPUTE,
            compute=ComputeFn.DEW_POINT_C,
            inputs=(
                (FieldSuffix.OUTDOOR_TEMPERATURE(GLOBAL), 1.0),
                (FieldSuffix.OUTDOOR_HUMIDITY(GLOBAL), 1.0),
            ),
            min_sources=2,
            max_age=timedelta(minutes=15),
            hold_last_good=timedelta(minutes=10),  # opzionale ma utile
        ),
        # (B) True global MRT: aggregate zonal MRT (already gated by plant_active)
        DerivedSpec(
            name=FieldSuffix.MRT(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_mrt_inputs),
            method=AggregationMethod.WEIGHTED_MEAN,
            min_sources=2,
            clamp_min=-10.0,
            clamp_max=40.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        # (B) Global operative temperature uses indoor air + true MRT (NOT hydraulic temps)
        DerivedSpec(
            name=FieldSuffix.T_OPT(GLOBAL),
            kind=DerivedKind.COMPUTE,
            compute=ComputeFn.T_OP_C,
            inputs=(
                (FieldSuffix.INDOOR_TEMPERATURE(GLOBAL), 1.0),
                (FieldSuffix.MRT(GLOBAL), 1.0),
            ),
            min_sources=2,
            clamp_min=-10.0,
            clamp_max=40.0,
            max_age=timedelta(minutes=10),
            hold_last_good=timedelta(minutes=5),
        ),
        DerivedSpec(
            name=FieldSuffix.CONDENSATION_MARGIN(GLOBAL),
            kind=DerivedKind.AGGREGATE,
            inputs=tuple(indoor_cm_inputs),
            method=AggregationMethod.MIN,
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
        # Order matters for readability and (if the engine is single-pass) dependency resolution:
        # - plant water proxy first
        # - psychrometrics next (dew point, heat index, condensation margin)
        # - actuation (plant_active) then MRT/T_op per-zone
        # - global aggregates last (global.mrt, global.t_op, condensation_margin_min, etc.)
        derived=_build_plant_water_derived() \
                + _build_psychro_derived(climate.areas) \
                + _build_actuation_derived(climate.areas) \
                + _build_mrt_derived(climate.areas) \
                + _build_global_derived(climate.areas)
    )
