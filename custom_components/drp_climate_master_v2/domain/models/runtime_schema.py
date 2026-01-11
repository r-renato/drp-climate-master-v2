#
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, List

from homeassistant.util.unit_system import UnitSystem

# ---- Aree ---------------------------------------------------------------
@dataclass(frozen=False, slots=True)
class SensorPair:
    temperature: str
    humidity: str
    dew_point: Optional[str] = None
    heat_index: Optional[str] = None

@dataclass(frozen=True)
class AreaConfig:
    name: str
    indoor: bool
    radiant: bool
    sensors: SensorPair
    thermal_collector_valve_switch: Optional[str] = None
    mq: Optional[float] = None

    @staticmethod
    def find_area(areas: list[AreaConfig], name: str) -> Optional[AreaConfig]:
        return next((a for a in areas if a.name == name), None)
# ---- Supply units -------------------------------------------------------
@dataclass(frozen=True)
class SupplyUnitSensors:
    boiler_temp_system_supply: str
    boiler_temp_system_return: str
    adjustable_temp_system_supply: str
    adjustable_temp_system_return: str
    direct_temp_system_supply: str
    direct_temp_system_return: str

@dataclass(frozen=True)
class SupplyUnitsConfig:
    direct_supply_unit: str
    adjustable_supply_unit: str
    three_point_mixing_valve: str
    sensors: SupplyUnitSensors

# ---- Radiant ------------------------------------------------------------
@dataclass(frozen=True)
class ModeConfig:
    actuator: str
    heating: int
    cooling: int

@dataclass(frozen=True)
class SetpointConfig:
    actuator: str
    value: float

@dataclass(frozen=True)
class RadiantSensors:
    pdc_temp_water_in: str
    pdc_temp_water_out: str

@dataclass(frozen=True)
class RadiantConfig:
    fm_power: str
    power: str
    mode: ModeConfig
    heating_t_setpoint: SetpointConfig
    heating_dt_setpoint: SetpointConfig
    cooling_t_setpoint: SetpointConfig
    cooling_dt_setpoint: SetpointConfig
    sensors: RadiantSensors

# ---- VMC ----------------------------------------------------------------
@dataclass(frozen=True)
class SeasonConfig:
    actuator: str
    winter: str
    summer: str
    autumn: str
    spring: str

@dataclass(frozen=True)
class CompressorManagementConfig:
    actuator: str
    dehumidification_or_cooling: int
    dehumidification_only: int
    cooling_only: int

@dataclass(frozen=True)
class CoolingManagementConfig:
    actuator: str
    compressor_only: int
    water_only: int
    first_water_then_compressor: int

@dataclass(frozen=True)
class VMCRequestsConfig:
    water: str
    dehumidification: str
    heating: str
    cooling: str

@dataclass(frozen=True)
class VMCSensorsConfig:
    t_ambient: str
    h_ambient: str
    t_water: str
    t_outdoor: str
    power_on_night: str
    power_on_today: str

@dataclass(frozen=True)
class VMCAlarmsConfig:
    high_pressure: str
    dew_point: str
    low_water_temp: str
    high_water_temp: str
    alarm: str

@dataclass(frozen=True)
class VMCConfig:
    power: str
    t_setpoint: str
    h_setpoint: str
    t_dew_point_setpoint: str
    delta_t_dew_point_setpoint: str
    spare_setpoint: str
    vent_recirculation: str
    force_heating: str
    force_cooling: str
    force_free_cooling: str
    season: SeasonConfig
    compressor_management: CompressorManagementConfig
    cooling_management: CoolingManagementConfig
    requests: VMCRequestsConfig
    sensors: VMCSensorsConfig
    alarms: VMCAlarmsConfig

# ---- Dispositivi e scenari ----------------------------------------------
@dataclass(frozen=True)
class DevicesConfig:
    supply_units: SupplyUnitsConfig
    radiant: Optional[RadiantConfig] = None
    vmc: Optional[VMCConfig] = None

@dataclass(frozen=True)
class ScenariosConfig:
    vacation: str
    nobodysin: str

@dataclass(frozen=True)
class WindowsConfig:
    closed_state: str

@dataclass(frozen=True, slots=True)
class ForecastDataConfig:
    # Per HA: entity_id weather.* (es. "weather.home_rome")
    provider: str

@dataclass(frozen=True, slots=True)
class HistoricalDataConfig:
    provider: str
    token: str
    latitude: float
    longitude: float

@dataclass(frozen=True, slots=True)
class InfluxdbHistoricalDataConfig:
    organization: str
    bucket: str
    token: str
    url: str

@dataclass(frozen=True)
class WeatherConfig:
    forecast_data: ForecastDataConfig
    historical_data: HistoricalDataConfig

@dataclass(frozen=True)
class ClimateConfig:
    name: str
    unique_id: str
    units: str
    areas: List[AreaConfig]
    devices: DevicesConfig
    windows: Optional[WindowsConfig]
    weather: WeatherConfig
    historical_data: InfluxdbHistoricalDataConfig
    scenarios: ScenariosConfig
    mean_apt: SensorPair
    unit_system: UnitSystem

# ======================================================
# Runtime
# ======================================================

@dataclass(frozen=True)
class PlantCapabilities:
    supports_heating: bool = False
    supports_cooling: bool = False
    supports_dehumidifying: bool = False
    supports_ventilation: bool = False
    setpoint_step_c: float = 0.5

@dataclass(frozen=True)
class RuntimeConfig:
    update_interval: timedelta
    capabilities: PlantCapabilities
    manual_override_minutes: int
    climate: ClimateConfig
