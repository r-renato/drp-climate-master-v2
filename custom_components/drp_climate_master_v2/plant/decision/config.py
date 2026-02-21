from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...domain.enums import HVACOperatingProfile

# -----------------------------------------------------------------------------
# Option A: nested, domain-oriented config blocks
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class ComfortBandConfig:
    """Comfort-band thresholds (°C) used to detect sensible demand.

    This block defines the *base* thresholds used by the plant decision logic
    to treat a zone as meaningfully outside the comfort band.

    **Thermal meaning (termotecnica)**
    - These thresholds apply to *operative temperature* (T_op), which is a proxy
      for perceived comfort combining air temperature and radiant effects.
    - Small values (e.g. 0.3 °C) make the control more responsive, but can also
      cause frequent plant starts if not combined with multi-zone gating and
      anti-hunting mechanisms.

    Notes
    - The thresholds here are *base* values; they are typically modulated by the
      active HVAC operating profile (Comfort/Eco/Boost/...), via an aggressiveness
      factor implemented in the planner.

    Attributes
    ----------
    heat_on_deficit_c:
        Heating trigger threshold (°C).
        A heating demand is considered significant when::

            T_op < T_min - heat_on_deficit_c

    cool_on_surplus_c:
        Cooling trigger threshold (°C).
        A cooling demand is considered significant when::

            T_op > T_max + cool_on_surplus_c
    """

    heat_on_deficit_c: float = 0.3
    cool_on_surplus_c: float = 0.3


@dataclass(slots=True)
class DemandGatingConfig:
    """Profile-aware multi-zone gating (dimensionless).

    This block reduces unnecessary plant starts due to *micro-violations* in a
    single small zone, especially in energy-saving profiles.

    **Thermal meaning (termotecnica)**
    - In multi-zone dwellings, a single room can temporarily drift out of comfort
      (solar gains, internal gains, door opening) without requiring a full plant start.
    - Gating uses:
      1) A *coverage quorum* (fraction of the weighted building that is demanding),
      2) A *worst-case override* (if one zone is far outside comfort, force ON),
      3) A *weighted-mean factor* (if the average deviation is meaningful, allow ON).

    Attributes
    ----------
    quorum_cov_by_profile:
        Required coverage quorum per profile, in the range [0..1].
        Example: 0.25 means "require at least 25% of weighted zones to demand".
        0.0 means "a single zone can trigger".

    demand_override_factor:
        Worst-zone override factor (dimensionless).
        If::

            max_deficit >= thr * demand_override_factor

        then the plant is allowed to start regardless of quorum.

    demand_mean_factor:
        Mean-demand factor (dimensionless).
        If::

            weighted_mean_deficit >= thr * demand_mean_factor

        then the plant is allowed to start with an easier condition.

    Methods
    -------
    quorum_cov(profile):
        Returns the quorum coverage required for the given profile.
    """

    quorum_cov_by_profile: dict[HVACOperatingProfile, float] = field(
        default_factory=lambda: {
            HVACOperatingProfile.COMFORT: 0.0,
            HVACOperatingProfile.BOOST: 0.0,
            HVACOperatingProfile.ECO: 0.25,
            HVACOperatingProfile.SLEEP: 0.20,
            HVACOperatingProfile.AWAY: 0.40,
            HVACOperatingProfile.VACATION: 0.50,
        }
    )

    demand_override_factor: float = 2.0
    demand_mean_factor: float = 0.60

    def quorum_cov(self, profile: HVACOperatingProfile) -> float:
        """Return the coverage quorum (0..1) for the given profile.

        Parameters
        ----------
        profile:
            The active HVAC operating profile.

        Returns
        -------
        float
            Coverage quorum in the range [0..1].
        """

        return float(self.quorum_cov_by_profile.get(profile, 0.0))


@dataclass(slots=True)
class DewPointGuardConfig:
    """Cooling condensation guard parameters (°C).

    Radiant cooling (and, more generally, cold surfaces) can cause condensation
    when the surface temperature drops below the indoor dew point.

    **Thermal meaning (termotecnica)**
    A conservative safe water supply constraint can be approximated as::

        T_water_safe >= DP_max + dp_margin_c + delta_surface_water_c

    where:
    - DP_max is the maximum dew point among monitored zones,
    - dp_margin_c is a hygrometric safety margin,
    - delta_surface_water_c accounts for the difference between water temperature
      and the actual surface temperature (heat transfer resistance).

    Attributes
    ----------
    dp_margin_c:
        Hygrometric margin above DP_max (°C).

    delta_surface_water_c:
        Conservative Δ(surface↔water) (°C).
    """

    dp_margin_c: float = 2.0
    delta_surface_water_c: float = 1.0


# -----------------------------------------------------------------------------
# Heating / cooling setpoints
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class HeatCurveConfig:
    """Linear heating curve for primary plant outlet temperature (WOT).

    A simple linear weather-compensated curve::

        WOT = base_c + k_c_per_c * (ref_outdoor_c - t_out)

    **Thermal meaning (termotecnica)**
    - WOT typically refers to the *primary* water outlet temperature produced by
      the heat pump (or boiler) before any mixing.
    - For radiant systems, a secondary mixed loop may run at lower temperatures;
      see :class:`RadiantConfig`.

    Attributes
    ----------
    base_c:
        Base WOT at t_out == ref_outdoor_c (°C).

    k_c_per_c:
        Curve slope (°C WOT per °C outdoor).

    ref_outdoor_c:
        Reference outdoor temperature (°C).

    wot_min_c, wot_max_c:
        Min/max bounds for WOT (°C), used to clamp the computed setpoint.

    dt_c:
        Nominal design ΔT (°C) of the primary circuit.
        This value can be used for sizing/diagnostics and should match hydraulic design.
    """

    base_c: float = 35.0
    k_c_per_c: float = 0.7
    ref_outdoor_c: float = 15.0

    wot_min_c: float = 28.0
    wot_max_c: float = 50.0
    dt_c: float = 2.0


@dataclass(slots=True)
class HeatEnhancementsConfig:
    """Heating curve enhancements: profile offset, indoor feedback, anti-hunting.

    This block adds pragmatic control features on top of the baseline heating curve.

    **Thermal meaning (termotecnica)**
    - Profile offsets shift the curve up/down depending on user preference.
    - Indoor feedback corrects the weather curve using observed comfort deficits.
    - Anti-hunting limits setpoint changes to avoid cycling and overshoot.

    Attributes
    ----------
    profile_offset_c:
        WOT offset (°C) per profile.

    feedback_gain_c_per_c:
        Proportional gain for indoor feedback (°C WOT per °C of weighted mean deficit).

    feedback_max_up_c, feedback_max_down_c:
        Saturations for indoor feedback (°C).

    kick_on_max_def_c:
        Threshold (°C) for worst-zone deficit that enables an extra kick.

    kick_extra_c:
        Extra WOT boost (°C) added when kick condition is met.

    wot_rate_limit_c_per_min:
        Maximum allowed change of WOT per minute (°C/min).

    wot_deadband_c:
        Deadband (°C) to ignore tiny setpoint changes.

    Methods
    -------
    offset(profile):
        Returns the configured WOT offset for a given profile.
    """

    profile_offset_c: dict[HVACOperatingProfile, float] = field(
        default_factory=lambda: {
            HVACOperatingProfile.COMFORT: 0.0,
            HVACOperatingProfile.ECO: -1.5,
            HVACOperatingProfile.BOOST: +3.0,
            HVACOperatingProfile.SLEEP: -1.0,
            HVACOperatingProfile.AWAY: -3.0,
            HVACOperatingProfile.VACATION: -4.0,
        }
    )

    feedback_gain_c_per_c: float = 0.8
    feedback_max_up_c: float = 6.0
    feedback_max_down_c: float = 2.0

    kick_on_max_def_c: float = 4.0
    kick_extra_c: float = 2.0

    wot_rate_limit_c_per_min: float = 0.5
    wot_deadband_c: float = 0.2

    def offset(self, profile: HVACOperatingProfile) -> float:
        """Return profile-specific WOT offset (°C).

        Parameters
        ----------
        profile:
            The active HVAC operating profile.

        Returns
        -------
        float
            Offset to be applied to the base curve (°C).
        """

        return float(self.profile_offset_c.get(profile, 0.0))


@dataclass(slots=True)
class HeatingConfig:
    """Heating configuration bundle.

    Attributes
    ----------
    curve:
        Baseline weather-compensated heating curve.

    enh:
        Enhancements on top of the curve (profile offset, feedback, anti-hunting).
    """

    curve: HeatCurveConfig = field(default_factory=HeatCurveConfig)
    enh: HeatEnhancementsConfig = field(default_factory=HeatEnhancementsConfig)


@dataclass(slots=True)
class CoolingConfig:
    """Cooling setpoint (flat) + profile offsets.

    This is currently a simplified model: a default WOT (water outlet temperature)
    for cooling, modulated by profile.

    **Thermal meaning (termotecnica)**
    - In radiant cooling, the *secondary* supply is typically driven by dew-point
      constraints (see :class:`DewPointGuardConfig` and :class:`RadiantConfig`).
    - The primary cooling WOT setpoint may be less relevant if mixing/secondary
      constraints dominate.

    Attributes
    ----------
    profile_offset_c:
        Cooling WOT offset (°C) per profile.

    wot_min_c, wot_max_c:
        Bounds for cooling WOT (°C).

    wot_default_c:
        Default cooling WOT (°C), before applying profile offset.

    dt_c:
        Nominal design ΔT (°C) of the cooling circuit.

    Methods
    -------
    offset(profile):
        Returns the configured cooling offset for a given profile.
    """

    profile_offset_c: dict[HVACOperatingProfile, float] = field(
        default_factory=lambda: {
            HVACOperatingProfile.COMFORT: 0.0,
            HVACOperatingProfile.ECO: +1.0,
            HVACOperatingProfile.BOOST: -1.5,
            HVACOperatingProfile.SLEEP: +1.5,
            HVACOperatingProfile.AWAY: +3.0,
            HVACOperatingProfile.VACATION: +4.0,
        }
    )

    wot_min_c: float = 7.0
    wot_max_c: float = 18.0
    wot_default_c: float = 12.0
    dt_c: float = 7.0

    def offset(self, profile: HVACOperatingProfile) -> float:
        """Return profile-specific cooling WOT offset (°C)."""

        return float(self.profile_offset_c.get(profile, 0.0))


# -----------------------------------------------------------------------------
# Secondary loops / MPC
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class RadiantConfig:
    """Radiant (secondary) loop targets and bounds (°C).

    This block describes the *secondary* loop (e.g., radiant floor/ceiling)
    typically downstream of a mixing valve.

    **Thermal meaning (termotecnica)**
    - In heating, the secondary radiant supply is often lower than primary WOT.
      The offset captures mixing/terminal requirements.
    - In cooling, dew-point constraints dominate to prevent condensation.

    Attributes
    ----------
    heat_supply_offset_c:
        Secondary supply target is typically::

            T_rad_supply ≈ WOT_primary - heat_supply_offset_c

    heat_supply_min_c, heat_supply_max_c:
        Secondary heating supply bounds (°C).

    cool_supply_min_c, cool_supply_max_c:
        Secondary cooling supply bounds (°C). Note: actual target should also be
        clamped by dew-point safety constraints.
    """

    heat_supply_offset_c: float = 5.0
    heat_supply_min_c: float = 25.0
    heat_supply_max_c: float = 40.0

    cool_supply_min_c: float = 16.0
    cool_supply_max_c: float = 22.0


@dataclass(slots=True)
class ZonesMpcConfig:
    """ZonesPlan / MPC integration knobs.

    This block configures how a zones-level controller (MPC-like) is interpreted
    by the plant-level planner.

    Design notes (termotecnica)
    --------------------------
    - The current MPC-lite (v1) is **heating-oriented** (binary radiant valve ON/OFF).
      For this reason, running it in summer is usually unnecessary unless you later
      extend it to cooling/dehumidification logic.
    - Keeping enable/season gating here allows the plant planner to remain a single
      entrypoint while still isolating the MPC implementation.

    Attributes
    ----------
    min_duty_ratio:
        Minimum duty ratio to treat a zone as meaningfully active.

    full_off_pct:
        Percentage threshold below which the MPC is considered fully off.

    full_on_pct:
        Percentage threshold above which the MPC is considered strongly on.

    preheat_headroom_c:
        Headroom (°C) for allowing MPC heating as *preheat*.
        Usually computed using::

            min_over_zones(T_meas - T_min)

        If the minimum headroom is small, preheat is accepted; otherwise ignored.
    """

    enabled: bool = True
    """Enable/disable zones MPC integration entirely."""

    run_in_winter: bool = True
    run_in_shoulder: bool = True
    run_in_summer: bool = False
    """Season gating for the MPC provider.

    Notes
    -----
    - v1 MPC-lite is heating-only, therefore default disables summer.
    - Operative season is derived as winter/summer/shoulder.
    """

    skip_if_user_off: bool = True
    """If True, do not compute a plan when the user HVAC mode is OFF."""

    swallow_exceptions: bool = True
    """If True, exceptions in zone MPC are swallowed (plant planner remains robust)."""

    propagate_warnings_to_plant: bool = True
    """If True, `ZonesDecision.warnings` are surfaced into `PlantDecision.warnings`."""

    min_duty_ratio: float = 0.05
    full_off_pct: float = 0.0
    full_on_pct: float = 50.0

    preheat_headroom_c: float = 0.4


# -----------------------------------------------------------------------------
# VMC (ventilazione/deumidifica; heating/cooling solo boost)
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class VmcBoostConfig:
    """Boost thresholds for enabling VMC heating/cooling.

    Many residential VMC units provide limited sensible heating/cooling capacity.
    This block ensures VMC sensible functions are only enabled when the dwelling
    is meaningfully outside comfort.

    Attributes
    ----------
    enabled:
        Enables/disables VMC boost features.

    heat_def_max_thr_c, heat_def_wmean_thr_c:
        Heating boost triggers based on maximum and weighted-mean deficits (°C).

    cool_sur_max_thr_c, cool_sur_wmean_thr_c:
        Cooling boost triggers based on maximum and weighted-mean surpluses (°C).

    setpoint_heat_c, setpoint_cool_c:
        Air temperature setpoints used when VMC sensible mode is active (°C).

    min_air_speed:
        Minimum fan speed to ensure effective sensible exchange.
    """

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
    """Dehumidification policy configuration.

    The policy can be driven by RH targets and/or dew-point (DP) targets.
    Dew-point control is often more stable across temperature changes.

    Attributes
    ----------
    setpoint_rh_default_pct:
        Default RH target (%).

    setpoint_rh_by_profile_pct:
        Profile-specific RH targets (%).

    setpoint_rh_by_season_pct:
        Season-specific RH targets (%). Uses strings like "winter"/"summer".

    setpoint_dp_c:
        Baseline dew point setpoint (°C).

    setpoint_ddp_c:
        Delta-dew-point target (°C) used by the *policy* layer to build the
        dehumidification ON/OFF thresholds around the DP setpoint.

    ddp_device_step_c:
        Quantization step (°C) supported by the VMC device for ΔDP.
        Example: if the device only supports integer ΔDP, set this to 1.0.
        The policy will map the *desired* ΔDP to a device-compatible command
        by adjusting the commanded DP setpoint so that the effective ON
        threshold is preserved as closely as possible.

    hysteresis_c:
        Hysteresis (°C) applied around dew point thresholds to avoid flapping.

    dp_control_percentile:
        Percentile (0..1) used when aggregating zone dew points.
        Example: 0.8 uses the 80th percentile to reduce outlier influence.

    dp_sp_min_c, dp_sp_max_c:
        Allowed bounds for computed dew point setpoint (°C).

    dp_setpoint_from_psychrometrics:
        If True, compute DP setpoints from psychrometrics rather than direct heuristics.

    water_on_for_dehumid:
        Whether to enable water coil/valve during dehumidification.

    outdoor_dp_headroom_c:
        Minimum indoor/outdoor dew point headroom (°C) to consider dehumidification feasible.
        If too small, outdoor air is too humid to help.

    Methods
    -------
    rh_target_pct(season, profile):
        Resolve RH target in priority order:
        1) profile override
        2) season override
        3) default
    """

    setpoint_rh_default_pct: float = 51.0
    setpoint_rh_by_profile_pct: dict[HVACOperatingProfile, float] = field(
        default_factory=lambda: {
            HVACOperatingProfile.SLEEP: 56.0,
        }
    )
    setpoint_rh_by_season_pct: dict[str, float] = field(
        default_factory=lambda: {
            "winter": 55.0,
            "summer": 50.0,
        }
    )

    setpoint_dp_c: float = 12.0
    setpoint_ddp_c: float = 0.3
    ddp_device_step_c: float = 1.0
    hysteresis_c: float = 0.2

    dp_control_percentile: float = 0.8
    dp_sp_min_c: float = 7.0
    dp_sp_max_c: float = 15.0
    dp_setpoint_from_psychrometrics: bool = True

    water_on_for_dehumid: bool = False
    outdoor_dp_headroom_c: float = 0.2

    def rh_target_pct(self, season: Optional[str], profile: HVACOperatingProfile) -> float:
        """Resolve target RH (%) given season and operating profile."""

        if profile in self.setpoint_rh_by_profile_pct:
            return float(self.setpoint_rh_by_profile_pct[profile])
        if season and season in self.setpoint_rh_by_season_pct:
            return float(self.setpoint_rh_by_season_pct[season])
        return float(self.setpoint_rh_default_pct)


@dataclass(slots=True)
class VmcSpeedPolicyConfig:
    """Ventilation speed policy (0..5) driven by dew-point deviation.

    Attributes
    ----------
    speed_min, speed_max:
        Absolute bounds for fan speed.

    speed_base:
        Default speed when conditions are neutral.

    speed_vacation:
        Reduced speed during vacation.

    speed_windows_open:
        Speed used when windows are detected open (often 0).

    dp_boost_step1_c, dp_boost_step2_c, dp_boost_step3_c:
        Dew-point deviation thresholds (°C) used to increase speed.
        Typical interpretation: if measured DP exceeds DP_SP by these steps,
        increment speed progressively.
    """

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
    """VMC configuration bundle.

    This bundle groups:
    - seasonal mode names
    - temperature bounds for air-side setpoints
    - recirculation preference
    - sub-policies (boost, dehumidification, speed)

    Attributes
    ----------
    mode_winter, mode_summer:
        Device-specific strings for VMC operating modes.

    setpoint_t_c:
        Default air temperature setpoint for VMC when used as a comfort assist.

    temp_neutral_deadband_c:
        Neutral deadband (°C) used to avoid toggling between heat/cool around neutrality.

    temp_min_c, temp_max_c:
        Safety bounds for air temperature setpoints.

    recirculation:
        Recirculation mode string (device-specific).

    boost:
        VMC sensible boost configuration.

    dehum:
        VMC dehumidification configuration.

    speed:
        VMC ventilation speed policy.
    """

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
    """VACATION policy knobs.

    Vacation mode typically prioritizes energy savings while still preventing
    humidity-related issues (mold/condensation) and maintaining minimum IAQ.

    Attributes
    ----------
    allows_vmc_off:
        If True, the policy may switch VMC off during vacation (subject to safety constraints).

    ddp_on_c:
        Dew-point delta threshold (°C) used to enable dehumidification actions in vacation.
        IMPORTANT: this value needs semantic definition in the VMC policy layer
        (e.g., DP_meas - DP_SP, or DP_meas - DP_baseline).

    allow_dehum_assist:
        If True, permit switching plant mode to DEHUM_ASSIST when cooling is unsafe due
        to dew-point constraints.
    """

    allows_vmc_off: bool = True
    ddp_on_c: float = 0.8
    allow_dehum_assist: bool = True


# -----------------------------------------------------------------------------
# Root config
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class PlantPlannerConfig:
    """Root configuration for the plant decision planner.

    This is the single commissioning/tuning surface for the plant-level logic.

    Commissioning notes (termotecnica)
    -------------------------------
    - Default values are conservative and should be tuned with real sensor data,
      building thermal response, and plant constraints.
    - Key tuning loops include:
      * comfort thresholds and gating (start/stop sensitivity)
      * heating curve slope/intercept (weather compensation)
      * indoor feedback gains and anti-hunting limits
      * dew-point guard margins (condensation safety)
      * VMC humidity and ventilation policies

    Attributes
    ----------
    comfort:
        Base comfort-band thresholds used to derive sensible demand signals.

    gating:
        Multi-zone gating configuration to avoid micro-starts, profile-aware.

    dp_guard:
        Dew-point safety margins for radiant cooling.

    heating:
        Heating curve and enhancements (primary circuit WOT).

    cooling:
        Cooling setpoint policy (primary circuit WOT), currently simplified.

    radiant:
        Secondary radiant loop bounds and offsets (post-mixing).

    zones_mpc:
        Integration knobs for zone-level MPC / valve plan.

    vmc:
        Ventilation/dehumidification policy configuration.

    vacation:
        Vacation mode policy knobs.
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
