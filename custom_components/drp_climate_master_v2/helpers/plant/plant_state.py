from __future__ import annotations

import logging
from typing import List, Optional
from datetime import datetime

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant, State

from ...domain.enums import HVACOperatingProfile
from ..confort.confort_band import ComfortBandResult
from ..sensor_aggregator import AggregatedValue, SensorAggregator

from ..logger import log_debug, log_exception, log_warning

from ..ha import get_entity_value, async_time_in_states

from ...domain.models.plant import (
    PDCSnapshot,
    PlantSnapshot,
    SupplyUnitSnapshot,
    VMCSnapshot,
    ZoneSnapshot,
)
from ...domain.models.runtime_schema import (
    AreaConfig,
    RadiantConfig,
    RuntimeConfig,
    SensorPair,
    SupplyUnitSensors,
    SupplyUnitsConfig,
    VMCConfig,
)
from ...domain.models.season import SeasonState
from ..utils import as_bool, as_float, as_int, make_class, slugify

_LOGGER = logging.getLogger(__name__)

async def async_take_plant_snapshot(
    hass: HomeAssistant,
    runtime_config: RuntimeConfig,
    season: SeasonState,
    sensor_aggr: SensorAggregator,
    confort_bands: dict[str, ComfortBandResult],
    entities_state: dict,
    climate_hvac_mode: HVACMode,
    climate_preset_mode: HVACOperatingProfile,
    timestamp: datetime,
) -> PlantSnapshot | None:
    """Build a plant snapshot collecting HA entity states."""

    def _build_indoor_zones_snapshot(
            runtime_config: RuntimeConfig, 
            ts: datetime,
            sensor_aggr: SensorAggregator,
            confort_bands: dict[str, ComfortBandResult],
    ) -> dict[str, ZoneSnapshot] | None:
        zone_snapshots: dict[str, ZoneSnapshot] = {}

        areas: List[AreaConfig] = runtime_config.climate.areas

        try:
            for area in areas:
                timestamp=ts
                name=slugify(area.name)

                if area.indoor and area.radiant and area.ceiling:
                    condensation_margin: AggregatedValue = sensor_aggr.get(name=f"{name}.condensation_margin")
                    indoor_dew_point: AggregatedValue = sensor_aggr.get(name=f"{name}.indoor_dew_point")
                    indoor_heat_index: AggregatedValue = sensor_aggr.get(name=f"{name}.indoor_heat_index")
                    indoor_humidity: AggregatedValue = sensor_aggr.get(name=f"{name}.indoor_humidity")
                    indoor_temperature: AggregatedValue = sensor_aggr.get(name=f"{name}.indoor_temperature")
                    mrt: AggregatedValue = sensor_aggr.get(name=f"{name}.mrt")
                    plant_active: AggregatedValue = sensor_aggr.get(name=f"{name}.plant_active")
                    radiant_valve_open: AggregatedValue = sensor_aggr.get(name=f"{name}.radiant_valve_open")
                    t_op: AggregatedValue = sensor_aggr.get(name=f"{name}.t_op")

                    confort_band: ComfortBandResult | None = confort_bands.get(name)
                else:
                    continue

                zone_snapshot: ZoneSnapshot = make_class(
                                    ZoneSnapshot,
                                    timestamp=timestamp,
                                    name=name,
                                    temperature=indoor_temperature,
                                    humidity=indoor_humidity,
                                    heat_index=indoor_heat_index,
                                    dew_point=indoor_dew_point,
                                    t_op=t_op,
                                    mrt=mrt,
                                    condensation_margin=condensation_margin,
                                    radiant_valve=radiant_valve_open,
                                    confort_band=confort_band,
                )

                zone_snapshots[name] = zone_snapshot

            return zone_snapshots
        except TypeError as ex:
            # Parametri mancanti/extra o mismatch firma costruttore
            log_warning(_LOGGER, "Error creating indoor ZoneSnapshot %s", ex, exc_info=True)
            return None
        except Exception as ex:
            # Qualsiasi altro errore inaspettato
            log_exception(_LOGGER, "Unexpected error creating indoor ZoneSnapshot %s", ex)
            return None

    def _build_outdoor_zones_snapshot(
            runtime_config: RuntimeConfig,
            ts: datetime, 
            sensor_aggr: SensorAggregator
    ) -> dict[str, ZoneSnapshot] | None:
        zone_snapshots: dict[str, ZoneSnapshot] = {}

        areas: List[AreaConfig] = runtime_config.climate.areas

        try:
            for area in areas:
                timestamp=ts
                name=slugify(area.name)

                if area.indoor:
                    continue

                outdoor_temperature: AggregatedValue = sensor_aggr.get(name=f"{name}.outdoor_temperature")
                outdoor_humidity: AggregatedValue = sensor_aggr.get(name=f"{name}.outdoor_humidity")
                outdoor_dew_point: AggregatedValue = sensor_aggr.get(name=f"{name}.outdoor_dew_point")

                zone_snapshot: ZoneSnapshot = make_class(
                                    ZoneSnapshot,
                                    timestamp=timestamp,
                                    name=name,
                                    temperature=outdoor_temperature,
                                    humidity=outdoor_humidity,
                                    dew_point=outdoor_dew_point,
                )

                zone_snapshots[name] = zone_snapshot

            return zone_snapshots
        except TypeError as ex:
            # Parametri mancanti/extra o mismatch firma costruttore
            log_warning(_LOGGER, "Error creating outdoor ZoneSnapshot %s", ex, exc_info=True)
            return None
        except Exception as ex:
            # Qualsiasi altro errore inaspettato
            log_exception(_LOGGER, "Unexpected error creating outdoor ZoneSnapshot %s", ex)
            return None


    def _build_pdc_snapshot(
        runtime_config: RuntimeConfig,
        ts: datetime,
    ) -> PDCSnapshot | None:

        radiant: RadiantConfig | None = runtime_config.climate.devices.radiant
        if radiant is None:
            return None

        try:
            power_state = entities_state.get(radiant.power) if radiant.power else None

            power_on = False
            last_changed = None

            if power_state is not None:
                # Se hai già lo State, evita un secondo lookup con get_entity_value
                power_on = as_bool(power_state.state) or False

                # Preferisci last_changed, fallback last_updated
                last_changed = power_state.last_changed or power_state.last_updated

            minutes_power_on = None
            minutes_power_off = None

            if last_changed is not None:
                minutes = max(0.0, (ts - last_changed).total_seconds() / 60.0)
                if power_on:
                    minutes_power_on = minutes
                else:
                    minutes_power_off = minutes

            pdc_snapshot: PDCSnapshot = make_class(
                PDCSnapshot,
                timestamp=ts,
                fm_power_on=as_bool(get_entity_value(entities_state, radiant.fm_power)) or False,
                power_on=power_on,

                device_mode=as_int(get_entity_value(entities_state, radiant.mode.actuator)),
                wot_heat=as_float(get_entity_value(entities_state, radiant.heating_t_setpoint.actuator)),
                delta_t_heat=as_float(get_entity_value(entities_state, radiant.heating_dt_setpoint.actuator)),
                wot_cool=as_float(get_entity_value(entities_state, radiant.cooling_t_setpoint.actuator)),
                delta_t_cool=as_float(get_entity_value(entities_state, radiant.cooling_dt_setpoint.actuator)),
                
                sensor_t_water_in_pe=as_float(get_entity_value(entities_state, radiant.sensors.pdc_temp_water_in)),
                sensor_t_water_out_pe=as_float(get_entity_value(entities_state, radiant.sensors.pdc_temp_water_out)),

                sensor_compressor_state=as_bool(get_entity_value(entities_state, radiant.sensors.pdc_compressor_state)) or False,
                minutes_power_on=minutes_power_on,
                minutes_power_off=minutes_power_off,
            )

            return pdc_snapshot
        except TypeError as ex:
            # Parametri mancanti/extra o mismatch firma costruttore
            log_warning(_LOGGER, "Error creating PDCSnapshot %s", ex, exc_info=True)
            return None
        except Exception as ex:
            # Qualsiasi altro errore inaspettato
            log_exception(_LOGGER, "Unexpected error creating PDCSnapshot %s", ex)
            return None

    async def _async_build_vmc_snapshot(
            runtime_config: RuntimeConfig, 
            ts: datetime
        ) -> VMCSnapshot | None:

        def _key_vmc_level(st: State) -> Optional[int]:
            if st.state in ("unknown", "unavailable", None):
                return None
            try:
                v = int(float(st.state))
            except (TypeError, ValueError):
                return None
            return v if 0 <= v <= 5 else None

        vmc: VMCConfig | None = runtime_config.climate.devices.vmc
        if vmc is None:
            return None

        try:
            power_state = entities_state.get(vmc.power) if vmc.power else None

            power_on = False
            last_changed = None

            if power_state is not None:
                # Se hai già lo State, evita un secondo lookup con get_entity_value
                power_on = as_bool(power_state.state) or False

                # Preferisci last_changed, fallback last_updated
                last_changed = power_state.last_changed or power_state.last_updated

            minutes_power_on = None
            minutes_power_off = None

            if last_changed is not None:
                minutes = max(0.0, (ts - last_changed).total_seconds() / 60.0)
                if power_on:
                    minutes_power_on = minutes
                else:
                    minutes_power_off = minutes

            spare_time_in_state_stats = await async_time_in_states(
                hass,
                vmc.spare_setpoint,
                ts,                 # UTC aware
                window="last_24h",
                key_fn=_key_vmc_level,
                include_unknown=False,
            )
            
            vmc_snapshot: VMCSnapshot = make_class(
                VMCSnapshot,
                timestamp=ts,
                power_on=as_bool(get_entity_value(entities_state, vmc.power)) or False,
                t_setpoint=as_float(get_entity_value(entities_state, vmc.t_setpoint)),
                rh_setpoint=as_float(get_entity_value(entities_state, vmc.h_setpoint)),
                t_dew_point_setpoint=as_float(get_entity_value(entities_state, vmc.t_dew_point_setpoint)),
                delta_t_dew_point_setpoint=as_float(get_entity_value(entities_state, vmc.delta_t_dew_point_setpoint)),
                spare_setpoint=as_int(get_entity_value(entities_state, vmc.spare_setpoint)),
                
                act_vent_recirculation=as_bool(get_entity_value(entities_state, vmc.vent_recirculation)) or False,
                act_force_heating=as_bool(get_entity_value(entities_state, vmc.force_heating)) or False,
                act_force_cooling=as_bool(get_entity_value(entities_state, vmc.force_cooling)) or False,
                act_force_free_cooling=as_bool(get_entity_value(entities_state, vmc.force_free_cooling)) or False,

                processing_mode=get_entity_value(entities_state, vmc.season.actuator),
                compressor_management=as_int(get_entity_value(entities_state, vmc.compressor_management.actuator)),
                cooling_management=as_int(get_entity_value(entities_state, vmc.cooling_management.actuator)),

                request_water=as_bool(get_entity_value(entities_state, vmc.requests.water)) or False,
                request_dehumidification=as_bool(get_entity_value(entities_state, vmc.requests.dehumidification)) or False,
                request_heating=as_bool(get_entity_value(entities_state, vmc.requests.heating)) or False,
                request_cooling=as_bool(get_entity_value(entities_state, vmc.requests.cooling)) or False,
                
                sensor_t_ambient=as_float(get_entity_value(entities_state, vmc.sensors.t_ambient)),
                sensor_h_ambient=as_float(get_entity_value(entities_state, vmc.sensors.h_ambient)),
                sensor_t_water=as_float(get_entity_value(entities_state, vmc.sensors.t_water)),
                sensor_t_outdoor=as_float(get_entity_value(entities_state, vmc.sensors.t_outdoor)),
                sensor_power_on_night=None,
                sensor_power_on_today=None,

                minutes_power_on=minutes_power_on,
                minutes_power_off=minutes_power_off,
                spare_time_in_state_stats=spare_time_in_state_stats,
                
                alarm_high_pressure=as_bool(get_entity_value(entities_state, vmc.alarms.high_pressure)) or False,
                alarm_dew_point=as_bool(get_entity_value(entities_state, vmc.alarms.dew_point)) or False,
                alarm_low_water_temp=as_bool(get_entity_value(entities_state, vmc.alarms.low_water_temp)) or False,
                alarm_high_water_temp=as_bool(get_entity_value(entities_state, vmc.alarms.high_water_temp)) or False,
                alarm_alarm=as_bool(get_entity_value(entities_state, vmc.alarms.alarm)) or False,
            )
            return vmc_snapshot
        except TypeError as ex:
            # Parametri mancanti/extra o mismatch firma costruttore
            log_warning(_LOGGER, "Error creating VMCSnapshot %s", ex, exc_info=True)
            return None
        except Exception as ex:
            # Qualsiasi altro errore inaspettato
            log_exception(_LOGGER, "Unexpected error creating VMCSnapshot %s", ex)
            return None

    def _build_supply_unit_snapshot(runtime_config: RuntimeConfig, ts: datetime) -> SupplyUnitSnapshot | None:

        supply_unit: SupplyUnitsConfig | None = runtime_config.climate.devices.supply_units
        if supply_unit is None:
            return None

        try:
            supply_unic_snapshot: SupplyUnitSnapshot = make_class(
                SupplyUnitSnapshot,
                timestamp=ts,
                direct_su_power_on=as_bool(get_entity_value(entities_state, supply_unit.direct_supply_unit)) or False,
                adjustable_su_power_on=as_bool(get_entity_value(entities_state, supply_unit.adjustable_supply_unit)) or False,
                three_point_mixing_valve=as_int(get_entity_value(entities_state, supply_unit.three_point_mixing_valve)),
                sensor_boiler_temp_system_supply=as_float(get_entity_value(entities_state, supply_unit.sensors.boiler_temp_system_supply)),
                sensor_boiler_temp_system_return=as_float(get_entity_value(entities_state, supply_unit.sensors.boiler_temp_system_return)),
                sensor_adjustable_temp_system_supply=as_float(get_entity_value(entities_state, supply_unit.sensors.adjustable_temp_system_supply)),
                sensor_adjustable_temp_system_return=as_float(get_entity_value(entities_state, supply_unit.sensors.adjustable_temp_system_return)),
                sensor_direct_temp_system_supply=as_float(get_entity_value(entities_state, supply_unit.sensors.direct_temp_system_supply)),
                sensor_direct_temp_system_return=as_float(get_entity_value(entities_state, supply_unit.sensors.direct_temp_system_return)),
            )
            return supply_unic_snapshot
        except TypeError as ex:
            # Parametri mancanti/extra o mismatch firma costruttore
            log_warning(_LOGGER, "Error creating SupplyUnitSnapshot %s", ex, exc_info=True)
            return None
        except Exception as ex:
            # Qualsiasi altro errore inaspettato
            log_exception(_LOGGER, "Unexpected error creating SupplyUnitSnapshot %s", ex)
            return None

# -------------------------------------------------------- 
# Main logic
# --------------------------------------------------------
    try:
        indoor_zones_snapshot = _build_indoor_zones_snapshot(runtime_config, timestamp, sensor_aggr, confort_bands)
        outdoor_zones_snapshot = _build_outdoor_zones_snapshot(runtime_config, timestamp, sensor_aggr)

        pdc = _build_pdc_snapshot(runtime_config, timestamp)
        vmc = await _async_build_vmc_snapshot(runtime_config, timestamp)
        supply_unit: SupplyUnitSnapshot | None = _build_supply_unit_snapshot(runtime_config, timestamp)

        runtime_windows=runtime_config.climate.windows
        if runtime_windows:
            windows_close_state=as_bool(get_entity_value(entities_state, runtime_windows.closed_state)) or False
        else:
            windows_close_state=False
        presence_vacation=as_bool(get_entity_value(entities_state, runtime_config.climate.scenarios.vacation)) or False
        presence_nobodysin=as_bool(get_entity_value(entities_state, runtime_config.climate.scenarios.nobodysin)) or False
        
        global_indoor_zone_snapshot = make_class(
            ZoneSnapshot,
            timestamp=timestamp,
            name="global_indoor",
            temperature=sensor_aggr.get("global.indoor_temperature"),
            humidity=sensor_aggr.get("global.indoor_humidity"),
            heat_index=sensor_aggr.get("global.indoor_heat_index"),
            dew_point=sensor_aggr.get("global.indoor_dew_point"),

            t_op=sensor_aggr.get("global.t_op"),
            mrt=sensor_aggr.get("global.mrt"),
            condensation_margin=sensor_aggr.get("global.condensation_margin_min"),

            confort_band=confort_bands.get("global_indoor"),
            
            flow_t=supply_unit.sensor_adjustable_temp_system_supply if supply_unit else None,
            return_t=supply_unit.sensor_adjustable_temp_system_return if supply_unit else None
        )

        return make_class(
            PlantSnapshot,
            timestamp=timestamp,
            season=season,

            indoor_zones=indoor_zones_snapshot,
            outdoor_zones=outdoor_zones_snapshot,
            
            pdc=pdc,
            vmc=vmc,
            supply_unit=supply_unit,

            windows_close_state=windows_close_state,
            presence_vacation=presence_vacation,
            presence_nobodysin=presence_nobodysin,

            global_indoor_zone=global_indoor_zone_snapshot,

            # global_indoor_dew_point=sensor_aggr.get("global.indoor_dew_point"),
            # global_indoor_heat_index=sensor_aggr.get("global.indoor_heat_index"),
            # global_indoor_humidity=sensor_aggr.get("global.indoor_humidity"),
            # global_indoor_temperature=sensor_aggr.get("global.indoor_temperature"),

            global_outdoor_dew_point=sensor_aggr.get("global.outdoor_dew_point"),
            global_outdoor_humidity=sensor_aggr.get("global.outdoor_humidity"),
            global_outdoor_temperature=sensor_aggr.get("global.outdoor_temperature"),

            # global_condensation_margin_min=sensor_aggr.get("global.condensation_margin_min"),
            # global_radiant_mean_temperature=sensor_aggr.get("global.radiant_mean_temperature"),
            climate_hvac_mode=climate_hvac_mode,
            climate_preset_mode=climate_preset_mode,
        )
    except TypeError as ex:
        # Parametri mancanti/extra o mismatch firma costruttore
        log_warning( _LOGGER, "Error creating PlantSnapshot %s", ex, exc_info=True)
        return None
    except Exception as ex:
        # Qualsiasi altro errore inaspettato
        log_exception(_LOGGER, "Unexpected error creating PlantSnapshot %s", ex)
        return None




 