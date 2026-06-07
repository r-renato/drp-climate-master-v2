"""MetProvider - stima del tasso metabolico (MET) per una zona."""

from __future__ import annotations

from .....domain.enums import HVACOperatingProfile
from .config import CloMetConfig
from .model import MetEstimate, RoomType, TimeOfDay

_MET_ISO_MIN: float = 0.50
_MET_ISO_MAX: float = 4.00


class MetProvider:
    """Calcola la stima MET per una zona dato il tipo di stanza e il profilo."""

    def __init__(self, cfg: CloMetConfig) -> None:
        self._cfg = cfg

    def compute(
        self,
        *,
        room_type: RoomType,
        profile: HVACOperatingProfile,
        time_of_day: TimeOfDay,
    ) -> MetEstimate:
        """Calcola il MET per una zona."""
        seeds = self._cfg.met_seeds
        profile_override = None
        tod_delta = 0.0

        if profile == HVACOperatingProfile.SLEEP:
            profile_override = seeds.bedroom_sleep
            met = profile_override
            source = f"sleep_override:{met:.2f}"
        elif profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            profile_override = 1.00
            met = profile_override
            source = f"away_override:{met:.2f}"
        else:
            met = self._room_base(room_type)
            room_base = met
            tod_delta = self._tod_delta(room_type, time_of_day)
            met += tod_delta
            met = max(_MET_ISO_MIN, min(_MET_ISO_MAX, met))
            source = f"room:{room_type.value}:{room_base:.2f}"
            if abs(tod_delta) >= 0.005:
                source += f"|tod:{tod_delta:+.2f}"

        room_base_val = self._room_base(room_type)
        return MetEstimate(
            value=round(met, 3),
            room_base=round(room_base_val, 3),
            profile_override=profile_override,
            tod_delta=round(tod_delta, 3),
            source=source,
        )

    def _room_base(self, room_type: RoomType) -> float:
        s = self._cfg.met_seeds
        return {
            RoomType.BEDROOM: s.bedroom_awake,
            RoomType.BATHROOM: s.bathroom,
            RoomType.KITCHEN: s.kitchen,
            RoomType.LIVING: s.living,
            RoomType.HALLWAY: s.hallway,
            RoomType.GLOBAL: s.global_zone,
            RoomType.OTHER: s.other,
        }.get(room_type, s.other)

    def _tod_delta(self, room_type: RoomType, time_of_day: TimeOfDay) -> float:
        """Correzione MET per fascia oraria."""
        d = self._cfg.met_tod
        if room_type == RoomType.BEDROOM and time_of_day == TimeOfDay.NIGHT:
            return d.bedroom_night_delta
        if room_type == RoomType.KITCHEN and time_of_day == TimeOfDay.DAYTIME:
            return d.kitchen_daytime_delta
        if room_type == RoomType.BATHROOM and time_of_day == TimeOfDay.NIGHT:
            return d.bathroom_night_delta
        return 0.0
