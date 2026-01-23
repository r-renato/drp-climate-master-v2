# custom_components/drp_climate_master_v2/domain/models.py
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

# -------------------- High-level operating enums -------------------- #

class HVACOperatingProfile(Enum):
    """Preset/Profilo operativo esposto nel Climate."""

    COMFORT = "Comfort"
    ECO = "Eco"
    BOOST = "Boost"
    SLEEP = "Sleep"
    AWAY = "Away"
    VACATION = "Vacation"

    @classmethod
    def values(cls) -> list[str]:
        """Restituisce i valori come lista di stringhe (es. ['comfort', 'eco', ...])."""
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, value: Any, *, default: Optional["HVACOperatingProfile"] = None) -> Optional["HVACOperatingProfile"]:
        """
        Converte `value` in un `HVACOperatingProfile`.

        Accetta:
        - un `HVACOperatingProfile` (ritorna invariato)
        - una stringa (case-insensitive), gestendo sia:
          - i *values* esposti (es. "comfort", "eco", "boost"...) => match su `m.value.lower()`
          - i nomi enum (es. "COMFORT", "eco") => match su `m.name`

        Args:
            value: str oppure HVACOperatingProfile (o altro tipo)
            default: valore di fallback se la conversione fallisce (default: None)

        Returns:
            HVACOperatingProfile se convertibile, altrimenti `default`.
        """
        if value is None:
            return default

        if isinstance(value, cls):
            return value

        if isinstance(value, str):
            v = value.strip()
            if not v:
                return default

            key = v.lower()

            # match sul value esposto (Comfort -> "comfort")
            for m in cls:
                if m.value.lower() == key:
                    return m

            # match sul nome enum (COMFORT)
            name_key = v.strip().upper()
            try:
                return cls[name_key]
            except KeyError:
                return default

        return default

