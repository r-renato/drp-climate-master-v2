#
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, List, Tuple

from homeassistant.util.unit_system import UnitSystem

# ---- Aree ---------------------------------------------------------------
@dataclass(frozen=False, slots=True)
class SensorPair:
    temperature: str
    humidity: str
    dew_point: Optional[str] = None
    heat_index: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RadiantSurface:
    """Una singola superficie radiante con la propria valvola di zona.

    Una AreaConfig può avere zero, una o più RadiantSurface.
    Ogni superficie ha la propria elettrovalvola (entity_id dello switch HA)
    e la propria estensione in m².

    Esempio: Living + Foyer condividono i sensori T/RH (stessa AreaConfig)
    ma hanno due circuiti radianti indipendenti — si modellano come due
    RadiantSurface distinte nella stessa area.
    """
    valve_switch: str       # entity_id dello switch HA (elettrovalvola)
    surface_m2: float = 0.0  # superficie in m² (0.0 se non nota)


@dataclass(frozen=True)
class AreaConfig:
    """Configurazione di un'area ambientale (zona fisica).

    Un'area ha un set di sensori T/RH condivisi e zero o più superfici
    radianti indipendenti (RadiantSurface). Questo modello rimpiazza il
    vecchio schema flat (thermal_collector_valve_switch + radiant_surface)
    che permetteva una sola valvola per area.

    Retrocompatibilità YAML
    -----------------------
    Il parser (config_entries.py) converte automaticamente il vecchio formato:
        thermal_collector_valve_switch: switch.xxx
        radiant_surface: 9.0
    nel nuovo:
        radiant_surfaces:
          - valve_switch: switch.xxx
            surface_m2: 9.0

    ceiling : Optional[float]
        Peso dell'area negli aggregati globali ponderati (global.indoor_temperature,
        global.mrt). Tipicamente uguale alla somma delle superfici radianti oppure
        alla superficie planimetrica della zona. None = area esclusa dagli aggregati
        globali (non entra nel pipeline radiante).
        ceiling=0.0 → area partecipa al pipeline (ha valvole da comandare,
        condensation_margin calcolato) ma non contribuisce ai global aggregate.
    """
    name: str
    indoor: bool
    radiant: bool
    sensors: SensorPair
    radiant_surfaces: Tuple[RadiantSurface, ...] = ()
    ceiling: Optional[float] = None

    @staticmethod
    def find_area(areas: list[AreaConfig], name: str) -> Optional[AreaConfig]:
        return next((a for a in areas if a.name == name), None)

    def valve_switches(self) -> Tuple[str, ...]:
        """Restituisce tutti gli entity_id delle valvole di questa area."""
        return tuple(s.valve_switch for s in self.radiant_surfaces)

    def total_surface_m2(self) -> float:
        """Superficie radiante totale dell'area in m²."""
        return sum(s.surface_m2 for s in self.radiant_surfaces)


def is_active_radiant_zone(area: AreaConfig) -> bool:
    """Vero se l'area partecipa alla pipeline radiante.

    Un'area è attiva se è indoor, ha superfici radianti configurate (ceiling
    is not None) ed è marcata come radiant. Usare questo helper al posto del
    guard inline ``area.indoor and area.radiant and area.ceiling is not None``
    per centralizzare il contratto: quando il modello cambia, basta aggiornare
    qui.

    ceiling=0.0 è considerato attivo: l'area ha valvole da comandare e superfici
    da proteggere dalla condensa, anche se non contribuisce agli aggregati globali.
    """
    return area.indoor and area.radiant and area.ceiling is not None


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
    pdc_temp_outdoor: Optional[str] = None
    pdc_compressor_state: Optional[str] = None

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
