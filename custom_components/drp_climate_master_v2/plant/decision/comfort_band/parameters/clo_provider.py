"""CloProvider - stima adattiva del CLO per una zona."""

from __future__ import annotations

from typing import Optional

from .....domain.enums import HVACOperatingProfile
from .....domain.models.season import OperativeSeason
from .config import CloMetConfig
from .model import CloEstimate, TimeOfDay

_S3_SKIP_PROFILES: frozenset[HVACOperatingProfile] = frozenset(
    {
        HVACOperatingProfile.SLEEP,
        HVACOperatingProfile.AWAY,
        HVACOperatingProfile.VACATION,
    }
)


class CloProvider:
    """Calcola la stima CLO per una zona."""

    def __init__(self, cfg: CloMetConfig) -> None:
        self._cfg = cfg

    def compute(
        self,
        *,
        operative_season: OperativeSeason,
        season_progress: Optional[float],
        shoulder_direction: Optional[str],
        profile: HVACOperatingProfile,
        time_of_day: TimeOfDay,
        cold_snap: bool,
        t_op_running_mean: Optional[float],
        t_op_current: Optional[float],
    ) -> CloEstimate:
        """Calcola il CLO per una zona con il contesto fornito."""
        cfg = self._cfg
        season_key = operative_season.value.lower()

        season_base, s4_delta = self._season_base_with_s4(
            operative_season=operative_season,
            season_progress=season_progress,
            shoulder_direction=shoulder_direction,
        )
        clo = season_base + s4_delta

        cold_snap_delta = 0.0
        if cold_snap and operative_season == OperativeSeason.SHOULDER:
            clo_winter = cfg.season_seeds.winter(cfg.climate_zone)
            cold_snap_delta = cfg.cold_snap.fraction * (clo_winter - clo)
            clo += cold_snap_delta

        profile_delta = 0.0
        profile_override: Optional[float] = None
        if profile == HVACOperatingProfile.SLEEP:
            if season_key == "winter":
                profile_delta = cfg.profile_deltas.sleep_winter_delta
            elif season_key == "shoulder":
                profile_delta = cfg.profile_deltas.sleep_shoulder_delta
            clo += profile_delta
        elif profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            if season_key == "winter":
                before = clo
                clo = max(
                    cfg.profile_deltas.away_vacation_winter_floor,
                    clo + cfg.profile_deltas.away_vacation_winter_delta,
                )
                profile_delta = clo - before

        s3_delta = 0.0
        if profile not in _S3_SKIP_PROFILES and t_op_running_mean is not None:
            deviation = (
                abs(t_op_current - t_op_running_mean)
                if t_op_current is not None
                else 0.0
            )
            if deviation <= cfg.s3.max_deviation_suppress_c:
                neutral = cfg.s3.neutral_by_season.get(
                    season_key,
                    cfg.s3.neutral_by_season["shoulder"],
                )
                raw_delta = (
                    (neutral - t_op_running_mean) * cfg.s3.sensitivity_clo_per_degc
                )
                s3_delta = max(
                    -cfg.s3.cap_delta_clo,
                    min(cfg.s3.cap_delta_clo, raw_delta),
                )
                if abs(s3_delta) >= 0.005:
                    clo += s3_delta
                else:
                    s3_delta = 0.0

        tod_delta = self._tod_delta(time_of_day)
        clo += tod_delta

        cap = (
            cfg.profile_deltas.cap_sleep
            if profile == HVACOperatingProfile.SLEEP
            else cfg.clo_max_default
        )
        effective_floor = (
            cfg.clo_min_shoulder
            if operative_season == OperativeSeason.SHOULDER
            else cfg.clo_min
        )
        clo = max(effective_floor, min(cap, clo))

        source = self._build_source(
            season_key,
            profile,
            cold_snap,
            s3_delta,
            s4_delta,
            tod_delta,
            operative_season=operative_season,
            t_op_running_mean=t_op_running_mean,
            deviation=(
                abs(t_op_current - t_op_running_mean)
                if t_op_current is not None and t_op_running_mean is not None
                else None
            ),
            profile_skips_s3=profile in _S3_SKIP_PROFILES,
        )

        return CloEstimate(
            value=round(clo, 3),
            season_base=round(season_base, 3),
            s3_delta=round(s3_delta, 3),
            s4_delta=round(s4_delta, 3),
            profile_delta=round(profile_delta, 3),
            cold_snap_delta=round(cold_snap_delta, 3),
            tod_delta=round(tod_delta, 3),
            profile_override=profile_override,
            source=source,
        )

    def _season_base_with_s4(
        self,
        *,
        operative_season: OperativeSeason,
        season_progress: Optional[float],
        shoulder_direction: Optional[str],
    ) -> tuple[float, float]:
        """Restituisce il seed stagionale e il delta della ramp S4."""
        cfg = self._cfg
        s4_delta = 0.0

        if operative_season == OperativeSeason.SUMMER:
            return cfg.season_seeds.summer, s4_delta

        if operative_season == OperativeSeason.WINTER:
            return cfg.season_seeds.winter(cfg.climate_zone), s4_delta

        base = cfg.season_seeds.shoulder
        if cfg.s4.enabled and season_progress is not None and shoulder_direction is not None:
            progress = float(season_progress)
            ramp_start = cfg.s4.ramp_start_pct
            if progress > ramp_start:
                blend = (progress - ramp_start) / max(1e-6, 100.0 - ramp_start)
                blend = min(1.0, blend)
                if shoulder_direction == "spring":
                    target = cfg.season_seeds.summer
                else:
                    target = cfg.season_seeds.winter(cfg.climate_zone)
                s4_delta = blend * (target - base)

        return base, s4_delta

    def _tod_delta(self, time_of_day: TimeOfDay) -> float:
        """Delta CLO per fascia oraria."""
        d = self._cfg.tod_deltas
        if time_of_day == TimeOfDay.MORNING:
            return d.morning
        if time_of_day == TimeOfDay.EVENING:
            return d.evening
        if time_of_day == TimeOfDay.NIGHT:
            return d.night
        return d.daytime

    @staticmethod
    def _build_source(
        season_key: str,
        profile: HVACOperatingProfile,
        cold_snap: bool,
        s3_delta: float,
        s4_delta: float,
        tod_delta: float,
        operative_season: OperativeSeason,
        t_op_running_mean: Optional[float],
        deviation: Optional[float],
        profile_skips_s3: bool,
    ) -> str:
        """Costruisce la stringa source per commissioning e debug."""
        parts = [f"season:{season_key}", f"profile:{profile.value}"]
        if cold_snap:
            parts.append("cold_snap")
        if abs(s4_delta) >= 0.005:
            parts.append(f"s4:{s4_delta:+.3f}")
        if profile_skips_s3:
            parts.append("s3:suppresso_profilo")
        elif t_op_running_mean is None:
            parts.append("s3:suppresso_warmup")
        elif (
            deviation is not None
            and deviation > CloMetConfig().s3.max_deviation_suppress_c
        ):
            parts.append(f"s3:suppresso_deviazione={deviation:.1f}c")
        elif abs(s3_delta) < 0.005:
            parts.append("s3:zero")
        else:
            parts.append(f"s3:{s3_delta:+.3f}")
        if abs(tod_delta) >= 0.005:
            parts.append(f"tod:{tod_delta:+.3f}")
        return "|".join(parts)
