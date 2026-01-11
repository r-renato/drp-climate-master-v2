from __future__ import annotations

from datetime import timedelta

from .sensor_aggregator import DerivedSpec, FilterConfig, GroupSpec, MappingConfig, SensorSpec, ZoneConfig

def build_sensor_mapping() -> MappingConfig:
    """Mapping built from the provided areas configuration.

    Areas included
    - Indoor zones: Kitchen, Living, Foyer, Master Bedroom, Guest Bedroom,
    Master Bathroom, Main Bathroom, Electric cabinet
    - Outdoor zone: Terrace

    Notes / critical choices
    - "Foyer" shares the SAME temperature/humidity sensors as "Living" in your config.
    If you include both in house-level aggregates, you'd double-count the same measurement.
    Here we keep the zone for completeness but set its weight to 0.0 and exclude it from
    derived globals.
    - "Electric cabinet" is indoor but typically not representative of comfort; we keep it
    as a zone but exclude it from indoor global aggregates.
    - Global indoor temperature uses area-weighted mean (mq).
    - Global indoor humidity uses median across zones (robust vs bathroom spikes).
    """

    # Indoor temperature filters (radiant floors / slow dynamics):
    # keep smoothing moderate, clamp spikes.
    indoor_temp_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        min_valid=5.0,
        max_valid=35.0,
        time_hampel_k=4.0,
        max_rate_per_min=0.6,  # °C/min (very conservative indoors)
        rate_limit_mode="clip",
        ema_alpha=0.2,
        rolling_median_window=3,
    )

    # Indoor humidity filters: RH can jump after showers; we clip extremely fast spikes but
    # still allow real humidity transients.
    indoor_rh_filters = FilterConfig(
        max_age=timedelta(minutes=10),
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=4.0,
        max_rate_per_min=8.0,  # %RH/min
        rate_limit_mode="clip",
        ema_alpha=0.25,
        rolling_median_window=3,
    )

    # Outdoor filters: larger dynamics and wider physical range.
    outdoor_temp_filters = FilterConfig(
        max_age=timedelta(minutes=20),
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
        min_valid=1.0,
        max_valid=100.0,
        time_hampel_k=6.0,
        max_rate_per_min=20.0,  # %RH/min
        rate_limit_mode="clip",
        ema_alpha=0.15,
        rolling_median_window=1,
    )

    # ---- Zones (from your list) ----


    kitchen = ZoneConfig(
        zone="kitchen",
        weight=10.8,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_kitchen_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_kitchen_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
            GroupSpec(
                name="indoor_dew_point",
                sensors=(SensorSpec("sensor.ambient_kitchen_dew_point", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=-20.0,
                clamp_max=30.0,
            ),
            GroupSpec(
                name="indoor_heat_index",
                sensors=(SensorSpec("sensor.ambient_kitchen_heat_index", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=-20.0,
                clamp_max=60.0,
            ),

        ),
    )

    living = ZoneConfig(
        zone="living",
        weight=12.6,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_living_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_living_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    foyer = ZoneConfig(
        zone="foyer",
        weight=0.0,  # IMPORTANT: shares the same sensors as living -> avoid double-counting
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_living_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_living_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    master_bedroom = ZoneConfig(
        zone="master_bedroom",
        weight=9.0,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_master_bedroom_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_master_bedroom_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    guest_bedroom = ZoneConfig(
        zone="guest_bedroom",
        weight=8.2,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_guest_bedroom_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_guest_bedroom_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    master_bathroom = ZoneConfig(
        zone="master_bathroom",
        weight=4.6,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_master_bathroom_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_master_bathroom_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    main_bathroom = ZoneConfig(
        zone="main_bathroom",
        weight=4.2,
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_main_bathroom_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=5.0,
                clamp_max=35.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_main_bathroom_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    electric_cabinet = ZoneConfig(
        zone="electric_cabinet",
        weight=0.0,  # exclude from comfort aggregates
        variables=(
            GroupSpec(
                name="indoor_temperature",
                sensors=(SensorSpec("sensor.ambient_electric_cabinet_temperature", weight=1.0, filters=indoor_temp_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=0.0,
                clamp_max=50.0,
            ),
            GroupSpec(
                name="indoor_humidity",
                sensors=(SensorSpec("sensor.ambient_electric_cabinet_humidity", weight=1.0, filters=indoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    terrace = ZoneConfig(
        zone="terrace",
        weight=1.0,
        variables=(
            GroupSpec(
                name="outdoor_temperature",
                sensors=(
                    SensorSpec("sensor.ambient_outdoor_temperature", weight=1.0, filters=outdoor_temp_filters),
                    SensorSpec("sensor.hmi080_outdoor_temperature", weight=1.0, filters=outdoor_temp_filters)
                ),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=-30.0,
                clamp_max=55.0,
            ),
            GroupSpec(
                name="outdoor_humidity",
                sensors=(SensorSpec("sensor.ambient_outdoor_humidity", weight=1.0, filters=outdoor_rh_filters),),
                method="weighted_mean",
                cross_outlier_method="none",
                min_sources=1,
                clamp_min=1.0,
                clamp_max=100.0,
            ),
        ),
    )

    zones = (
        kitchen,
        living,
        foyer,
        master_bedroom,
        guest_bedroom,
        master_bathroom,
        main_bathroom,
        # electric_cabinet, # excluded from derived globals
        terrace,
    )

    # Derived globals (indoor only; exclude foyer + electric cabinet by omission)
    indoor_temp_inputs = (
        ("kitchen.indoor_temperature", 10.8),
        ("living.indoor_temperature", 12.6),
        ("master_bedroom.indoor_temperature", 9.0),
        ("guest_bedroom.indoor_temperature", 8.2),
        ("master_bathroom.indoor_temperature", 4.6),
        ("main_bathroom.indoor_temperature", 4.2),
    )

    indoor_rh_inputs = (
        ("kitchen.indoor_humidity", 1.0),
        ("living.indoor_humidity", 1.0),
        ("master_bedroom.indoor_humidity", 1.0),
        ("guest_bedroom.indoor_humidity", 1.0),
        ("master_bathroom.indoor_humidity", 1.0),
        ("main_bathroom.indoor_humidity", 1.0),
    )

    derived = (
        DerivedSpec(
            name="global.indoor_temperature",
            kind="aggregate",
            inputs=indoor_temp_inputs,
            method="weighted_mean",
            min_sources=2,
            clamp_min=5.0,
            clamp_max=35.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_humidity",
            kind="aggregate",
            inputs=indoor_rh_inputs,
            method="median",
            min_sources=2,
            clamp_min=1.0,
            clamp_max=100.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_dew_point",
            kind="aggregate",
            inputs=(
                ("kitchen.indoor_dew_point", 1.0),
                ("living.indoor_dew_point", 1.0),
                ("master_bedroom.indoor_dew_point", 1.0),
                ("guest_bedroom.indoor_dew_point", 1.0),
                ("master_bathroom.indoor_dew_point", 1.0),
                ("main_bathroom.indoor_dew_point", 1.0),
            ),
            method="max",
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=30.0,
            max_age=timedelta(minutes=10),
        ),
        DerivedSpec(
            name="global.indoor_heat_index",
            kind="aggregate",
            inputs=(
                ("kitchen.indoor_heat_index", 1.0),
                ("living.indoor_heat_index", 1.0),
                ("master_bedroom.indoor_heat_index", 1.0),
                ("guest_bedroom.indoor_heat_index", 1.0),
                ("master_bathroom.indoor_heat_index", 1.0),
                ("main_bathroom.indoor_heat_index", 1.0),
            ),
            method="max",
            min_sources=2,
            clamp_min=-20.0,
            clamp_max=60.0,
            max_age=timedelta(minutes=10),
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
        ),
    )

    return MappingConfig(zones=zones, derived=derived)
