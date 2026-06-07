"""ComfortParameterProvider - punto di accesso principale del modulo."""

from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import Optional

from .....domain.enums import HVACOperatingProfile
from .....domain.models.season import OperativeSeason
from .clo_provider import CloProvider
from .config import CloMetConfig
from .met_provider import MetProvider
from .model import ComfortParameters, RoomType, TimeOfDay


class ComfortParameterProvider:
    """Punto d'autorita unico per CLO e MET."""

    def __init__(
        self,
        cfg: CloMetConfig,
        room_type_map: dict[str, RoomType],
    ) -> None:
        self._cfg = cfg
        self._room_map = room_type_map
        self._clo = CloProvider(cfg)
        self._met = MetProvider(cfg)

    def compute_zone(
        self,
        *,
        zone_id: str,
        operative_season: OperativeSeason,
        season_progress: Optional[float],
        shoulder_direction: Optional[str],
        profile: HVACOperatingProfile,
        time_of_day: TimeOfDay,
        cold_snap: bool,
        t_op_running_mean: Optional[float],
        t_op_current: Optional[float],
    ) -> ComfortParameters:
        """Calcola CLO+MET per una singola zona."""
        room_type = self._room_map.get(zone_id, RoomType.OTHER)
        clo = self._clo.compute(
            operative_season=operative_season,
            season_progress=season_progress,
            shoulder_direction=shoulder_direction,
            profile=profile,
            time_of_day=time_of_day,
            cold_snap=cold_snap,
            t_op_running_mean=t_op_running_mean,
            t_op_current=t_op_current,
        )
        met = self._met.compute(
            room_type=room_type,
            profile=profile,
            time_of_day=time_of_day,
        )
        return ComfortParameters(zone_id=zone_id, room_type=room_type, clo=clo, met=met)

    def compute_global(
        self,
        *,
        zone_parameters: dict[str, ComfortParameters],
        zone_weights: dict[str, float],
    ) -> ComfortParameters:
        """Calcola CLO+MET globali come media pesata delle zone."""
        total_w = sum(zone_weights.get(z, 0.0) for z in zone_parameters)
        if total_w <= 0.0:
            fallback_clo = self._clo.compute(
                operative_season=OperativeSeason.SHOULDER,
                season_progress=None,
                shoulder_direction=None,
                profile=HVACOperatingProfile.ECO,
                time_of_day=TimeOfDay.DAYTIME,
                cold_snap=False,
                t_op_running_mean=None,
                t_op_current=None,
            )
            fallback_met = self._met.compute(
                room_type=RoomType.GLOBAL,
                profile=HVACOperatingProfile.ECO,
                time_of_day=TimeOfDay.DAYTIME,
            )
            return ComfortParameters(
                zone_id="global",
                room_type=RoomType.GLOBAL,
                clo=fallback_clo,
                met=fallback_met,
            )

        clo_sum = sum(
            zone_parameters[z].clo.value * zone_weights.get(z, 0.0)
            for z in zone_parameters
        )
        met_sum = sum(
            zone_parameters[z].met.value * zone_weights.get(z, 0.0)
            for z in zone_parameters
        )
        avg_clo = clo_sum / total_w
        avg_met = met_sum / total_w

        first = next(iter(zone_parameters.values()))
        source = f"global:weighted_mean:n={len(zone_parameters)}:w={total_w:.1f}"
        global_clo = dc_replace(first.clo, value=round(avg_clo, 3), source=source)
        global_met = dc_replace(first.met, value=round(avg_met, 3), source=source)
        return ComfortParameters(
            zone_id="global",
            room_type=RoomType.GLOBAL,
            clo=global_clo,
            met=global_met,
        )

    def compute_all(
        self,
        *,
        zone_ids: list[str],
        operative_season: OperativeSeason,
        season_progress: Optional[float],
        shoulder_direction: Optional[str],
        profile: HVACOperatingProfile,
        time_of_day: TimeOfDay,
        cold_snap: bool,
        zone_weights: dict[str, float],
        t_op_rm_by_zone: Optional[dict[str, Optional[float]]] = None,
        t_op_by_zone: Optional[dict[str, Optional[float]]] = None,
    ) -> dict[str, ComfortParameters]:
        """Calcola CLO+MET per tutte le zone e per il global."""
        t_op_rm = t_op_rm_by_zone or {}
        t_op = t_op_by_zone or {}

        result: dict[str, ComfortParameters] = {}
        for zone_id in zone_ids:
            result[zone_id] = self.compute_zone(
                zone_id=zone_id,
                operative_season=operative_season,
                season_progress=season_progress,
                shoulder_direction=shoulder_direction,
                profile=profile,
                time_of_day=time_of_day,
                cold_snap=cold_snap,
                t_op_running_mean=t_op_rm.get(zone_id),
                t_op_current=t_op.get(zone_id),
            )

        result["global"] = self.compute_global(
            zone_parameters=result,
            zone_weights=zone_weights,
        )
        return result
