from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class PlantMode(str, Enum):
    """High-level plant operating mode (impianto).

    Nota: volutamente disaccoppiato dagli enum dei singoli device.
    """

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DEHUM_ASSIST = "dehum_assist"
    VENT_ONLY = "vent_only"


@dataclass(slots=True)
class PdcCommand:
    """Comandi desiderati per la PDC (produzione primaria).

    I campi sono volutamente 'Optional' perché questo layer NON esegue direttamente attuazioni
    (in questa fase il codice non è integrato nel Supervisor).
    """

    power: Optional[bool] = None
    mode: Optional[str] = None  # "heating" | "cooling"
    heat_wot_c: Optional[float] = None
    heat_dt_c: Optional[float] = None
    cool_wot_c: Optional[float] = None
    cool_dt_c: Optional[float] = None

    # Alcuni impianti distinguono "Workload FM power" vs "Device Power"
    fm_power: Optional[bool] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SupplyCommand:
    """Comandi desiderati per i circuiti secondari (pompe + miscelazione)."""

    # Pompe
    direct_pump_on: Optional[bool] = None   # circuito VMC (diretto)
    adj_pump_on: Optional[bool] = None      # circuito radiante (mix)

    # Miscelatrice 3-vie (0-100%)
    mix_valve_pct: Optional[float] = None

    # Target "fisici" (utile per logging/telemetria anche se non mappati 1:1 su attuatori)
    rad_supply_target_c: Optional[float] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VmcCommand:
    """Comandi desiderati per la VMC."""

    power: Optional[bool] = None
    mode: Optional[str] = None  # "winter" | "summer" | "off"
    air_speed: Optional[int] = None

    setpoint_t_c: Optional[float] = None
    setpoint_rh_pct: Optional[float] = None
    setpoint_dp_c: Optional[float] = None
    setpoint_ddp_c: Optional[float] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlantDecision:
    """Risultato della pianificazione a livello impianto."""

    ts: datetime
    mode: PlantMode
    reason: str

    pdc: PdcCommand = field(default_factory=PdcCommand)
    supply: SupplyCommand = field(default_factory=SupplyCommand)
    vmc: VmcCommand = field(default_factory=VmcCommand)

    signals: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_log_dict(self) -> Dict[str, Any]:
        """Versione 'log friendly' (serializzabile) della decisione."""
        return {
            "ts": self.ts.isoformat(),
            "mode": self.mode.value,
            "reason": self.reason,
            "pdc": self.pdc.__dict__,
            "supply": self.supply.__dict__,
            "vmc": self.vmc.__dict__,
            "signals": self.signals,
            "warnings": self.warnings,
        }
