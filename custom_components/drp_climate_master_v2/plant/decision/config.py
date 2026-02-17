from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...domain.enums import HVACOperatingProfile

# -----------------------------------------------------------------------------
# Option A: nested, domain-oriented config blocks
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class ComfortBandConfig:
    """Comfort-band thresholds (°C).

    These are the *base* thresholds used to consider a sensible demand significant.
    They are later modulated by profile aggressiveness (see planner).
    """

    # ON if T_op < T_min - thr
    heat_on_deficit_c: float = 0.3

    # ON if T_op > T_max + thr
    cool_on_surplus_c: float = 0.3


@dataclass(slots=True)
class DemandGatingConfig:
    """Profile-aware multi-zone gating (dimensionless).

    In energy-saving profiles we avoid starting the plant for micro-violations in a
    single zone, unless:
      - the worst violation is large enough (override), or
      - the weighted mean is meaningful (mean_factor).
    """

    # Coverage quorum by profile (0..1). 0 => one zone can trigger.
    quorum_cov_by_profile: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.COMFORT: 0.0,
        HVACOperatingProfile.BOOST: 0.0,
        HVACOperatingProfile.ECO: 0.25,
        HVACOperatingProfile.SLEEP: 0.20,
        HVACOperatingProfile.AWAY: 0.40,
        HVACOperatingProfile.VACATION: 0.50,
    })

    # If max deficit/surplus >= thr * override_factor => force ON
    demand_override_factor: float = 2.0

    # If weighted mean deficit/surplus >= thr * mean_factor => easier ON
    demand_mean_factor: float = 0.60

    def quorum_cov(self, profile: HVACOperatingProfile) -> float:
        return float(self.quorum_cov_by_profile.get(profile, 0.0))


@dataclass(slots=True)
class DewPointGuardConfig:
    """Cooling condensation guard (°C)."""

    # Hygrometric margin above DP_max
    dp_margin_c: float = 2.0

    # Conservative Δ(surface↔water)
    delta_surface_water_c: float = 1.0


# -----------------------------------------------------------------------------
# Heating / cooling setpoints
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class HeatCurveConfig:
    """Linear heating curve: WOT = base + k*(t_ref - t_out)."""

    base_c: float = 35.0
    k_c_per_c: float = 0.7
    ref_outdoor_c: float = 15.0

    wot_min_c: float = 28.0
    wot_max_c: float = 50.0
    dt_c: float = 2.0


@dataclass(slots=True)
class HeatEnhancementsConfig:
    """Heating curve enhancements (profile offset + indoor feedback + anti-hunting)."""

    # Keys aligned to HVACOperatingProfile (Comfort, Eco, ...)
    profile_offset_c: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.COMFORT: 0.0,
        HVACOperatingProfile.ECO: -1.5,
        HVACOperatingProfile.BOOST: +3.0,
        HVACOperatingProfile.SLEEP: -1.0,
        HVACOperatingProfile.AWAY: -3.0,
        HVACOperatingProfile.VACATION: -4.0,
    })

    # Indoor feedback (quanto alzare WOT per ogni °C di deficit medio)
    feedback_gain_c_per_c: float = 0.8
    feedback_max_up_c: float = 6.0
    feedback_max_down_c: float = 2.0

    # Extra boost se una zona è molto fuori banda (worst-case)
    kick_on_max_def_c: float = 4.0
    kick_extra_c: float = 2.0

    # Anti-hunting: limita variazione setpoint nel tempo
    wot_rate_limit_c_per_min: float = 0.5
    wot_deadband_c: float = 0.2

    def offset(self, profile: HVACOperatingProfile) -> float:
        return float(self.profile_offset_c.get(profile, 0.0))


@dataclass(slots=True)
class HeatingConfig:
    """Heating configuration bundle."""

    curve: HeatCurveConfig = field(default_factory=HeatCurveConfig)
    enh: HeatEnhancementsConfig = field(default_factory=HeatEnhancementsConfig)


@dataclass(slots=True)
class CoolingConfig:
    """Cooling setpoint (flat, to be evolved) + profile offsets."""

    profile_offset_c: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.COMFORT: 0.0,
        HVACOperatingProfile.ECO: +1.0,
        HVACOperatingProfile.BOOST: -1.5,
        HVACOperatingProfile.SLEEP: +1.5,
        HVACOperatingProfile.AWAY: +3.0,
        HVACOperatingProfile.VACATION: +4.0,
    })

    wot_min_c: float = 7.0
    wot_max_c: float = 18.0
    wot_default_c: float = 12.0
    dt_c: float = 7.0

    def offset(self, profile: HVACOperatingProfile) -> float:
        return float(self.profile_offset_c.get(profile, 0.0))


# -----------------------------------------------------------------------------
# Secondary loops / MPC
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class RadiantConfig:
    """Radiant (secondary) targets (°C)."""

    # In heating the radiant supply is typically lower than primary (mixing).
    heat_supply_offset_c: float = 5.0
    heat_supply_min_c: float = 25.0
    heat_supply_max_c: float = 40.0

    # In cooling, condensation constraint dominates.
    cool_supply_min_c: float = 16.0
    cool_supply_max_c: float = 22.0


@dataclass(slots=True)
class ZonesMpcConfig:
    """ZonesPlan / MPC integration knobs."""

    min_duty_ratio: float = 0.05
    full_off_pct: float = 0.0
    full_on_pct: float = 50.0

    # Accept MPC heating as "preheat" only when close to lower bound.
    # Uses min(T_meas - T_min) across zones.
    preheat_headroom_c: float = 0.4


# -----------------------------------------------------------------------------
# VMC (ventilazione/deumidifica; heating/cooling solo boost)
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class VmcBoostConfig:
    """Boost thresholds: enable VMC heating/cooling only if really out of comfort."""

    enabled: bool = True
    heat_def_max_thr_c: float = 1.5
    heat_def_wmean_thr_c: float = 1.0
    cool_sur_max_thr_c: float = 1.5
    cool_sur_wmean_thr_c: float = 1.0
    setpoint_heat_c: float = 22.0
    setpoint_cool_c: float = 24.0
    min_air_speed: int = 3


@dataclass(slots=True)
class VmcDehumConfig:
    """Dehumidification policy."""

    setpoint_rh_default_pct: float = 51.0
    setpoint_rh_by_profile_pct: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.SLEEP: 56.0,
    })
    setpoint_rh_by_season_pct: dict[str, float] = field(default_factory=lambda: {
        "winter": 55.0,
        "summer": 50.0,
    })

    setpoint_dp_c: float = 12.0
    setpoint_ddp_c: float = 0.3
    hysteresis_c: float = 0.2

    dp_control_percentile: float = 0.8
    dp_sp_min_c: float = 7.0
    dp_sp_max_c: float = 15.0
    dp_setpoint_from_psychrometrics: bool = True

    water_on_for_dehumid: bool = False
    outdoor_dp_headroom_c: float = 0.2

    def rh_target_pct(self, season: Optional[str], profile: HVACOperatingProfile) -> float:
        if profile in self.setpoint_rh_by_profile_pct:
            return float(self.setpoint_rh_by_profile_pct[profile])
        if season and season in self.setpoint_rh_by_season_pct:
            return float(self.setpoint_rh_by_season_pct[season])
        return float(self.setpoint_rh_default_pct)


@dataclass(slots=True)
class VmcSpeedPolicyConfig:
    """Ventilation speed policy (0..5)."""

    speed_min: int = 0
    speed_max: int = 5
    speed_base: int = 2
    speed_vacation: int = 1
    speed_windows_open: int = 0

    dp_boost_step1_c: float = 0.5
    dp_boost_step2_c: float = 1.2
    dp_boost_step3_c: float = 1.8


@dataclass(slots=True)
class VmcConfig:
    """VMC configuration bundle."""

    mode_winter: str = "winter"
    mode_summer: str = "summer"

    setpoint_t_c: float = 20.0
    temp_neutral_deadband_c: float = 0.3
    temp_min_c: float = 16.0
    temp_max_c: float = 28.0

    recirculation: str = "off"

    boost: VmcBoostConfig = field(default_factory=VmcBoostConfig)
    dehum: VmcDehumConfig = field(default_factory=VmcDehumConfig)
    speed: VmcSpeedPolicyConfig = field(default_factory=VmcSpeedPolicyConfig)


# -----------------------------------------------------------------------------
# Vacation policy
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class VacationConfig:
    """VACATION policy knobs."""

    allows_vmc_off: bool = True
    ddp_on_c: float = 0.8
    allow_dehum_assist: bool = True


# -----------------------------------------------------------------------------
# Root config
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class PlantPlannerConfig:
    """Configurazione per la pianificazione impianto.

    ATTENZIONE: i valori di default sono *conservativi* e vanno tarati con dati reali
    (commissioning). In questa fase il planner non è integrato nel Supervisor.
    """

    comfort: ComfortBandConfig = field(default_factory=ComfortBandConfig)
    gating: DemandGatingConfig = field(default_factory=DemandGatingConfig)
    dp_guard: DewPointGuardConfig = field(default_factory=DewPointGuardConfig)

    heating: HeatingConfig = field(default_factory=HeatingConfig)
    cooling: CoolingConfig = field(default_factory=CoolingConfig)

    radiant: RadiantConfig = field(default_factory=RadiantConfig)
    zones_mpc: ZonesMpcConfig = field(default_factory=ZonesMpcConfig)

    vmc: VmcConfig = field(default_factory=VmcConfig)
    vacation: VacationConfig = field(default_factory=VacationConfig)
