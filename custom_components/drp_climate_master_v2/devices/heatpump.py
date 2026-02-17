from typing import Protocol, Optional, Any
from typing_extensions import runtime_checkable

# Tipi logici/di dominio (puoi rimappare ai tuoi)
HVAC_HEATING = "heating"
HVAC_COOLING = "cooling"
HVAC_AUTO    = "auto"

class SeasonSetpoint:
    """Esempio minimale: adatta al tuo vero tipo."""
    def __init__(self, heating_on: Optional[float], heating_off: Optional[float]) -> None:
        self.heating = type("H", (), dict(state_on=heating_on, state_off=heating_off))  # quick stub


@runtime_checkable
class HeatPumpDevice(Protocol):
    """Interfaccia per un device 'Pompa di Calore' controllabile."""

    # --- Telemetrie minime (read-only) ---
    # @property
    # def is_power_on(self) -> bool: ...
    # @property
    # def is_fm_power_on(self) -> bool: ...
    # @property
    # def is_processing_heating(self) -> bool: ...
    # @property
    # def is_processing_cooling(self) -> bool: ...

    # @property
    # def outdoor_temperature(self) -> Optional[float]: ...
    # @property
    # def boiler_supply_temperature(self) -> Optional[float]: ...
    # @property
    # def home_felt_temperature(self) -> Optional[float]: ...
    # @property
    # def defrost_required(self) -> bool: ...

    # --- Comandi atomici (write) ---
    async def async_set_power(self, *, fm_power: Optional[bool] = None, power: Optional[bool] = None) -> None: ...
    async def async_set_processing_mode(self, mode: Optional[str] = None) -> None: ...
    async def async_set_heat_setpoints(self, *, t: Optional[float] = None, dt: Optional[float] = None) -> None: ...
    async def async_set_cool_setpoints(self, *, t: Optional[float] = None, dt: Optional[float] = None) -> None: ...


