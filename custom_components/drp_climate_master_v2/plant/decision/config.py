from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Optional

from ...domain.enums import HVACOperatingProfile
from .confort_band.policy_layer import ClimateZoneIT, ComplianceMode, ConfortPolicyConfig

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

    ctrl_aggr_by_profile:
        Aggressività di controllo per profilo (adimensionale, 1.0 = COMFORT nominale).
        Scala tutte le soglie di attivazione e i pesi MPC: valori > 1.0 rendono il
        controllo più reattivo (BOOST), valori < 1.0 lo rendono più conservativo
        (ECO/SLEEP/AWAY). Unica fonte di verità: usato da ``gating.py`` e da
        ``zone/planner.py`` tramite ``ZoneDecisionPlanner.gating_cfg``.

    Methods
    -------
    quorum_cov(profile):
        Returns the quorum coverage required for the given profile.
    ctrl_aggr(profile):
        Returns the control aggressiveness factor for the given profile.
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
    """Fattore override worst-zone (fallback fisso).
    Usato da ``effective_override_factor()`` quando T_ext non è disponibile.
    Valore conservativo invernale: intervento tempestivo anche senza quorum."""

    demand_override_factor_cold: float = 2.0
    """Fattore override a bassa T_ext (<= t_override_cold_c).
    In inverno il deficit peggiora rapidamente: soglia bassa per intervento
    tempestivo anche senza quorum."""

    demand_override_factor_mild: float = 4.0
    """Fattore override ad alta T_ext (>= t_override_mild_c).
    In mezza stagione/estate le variazioni termiche sono spesso spontanee e
    transitorie: soglia alta per evitare avvii inutili della PDC."""

    t_override_cold_c: float = 5.0
    """T_ext (degC) sotto la quale si applica demand_override_factor_cold senza interpolazione."""

    t_override_mild_c: float = 18.0
    """T_ext (degC) sopra la quale si applica demand_override_factor_mild senza interpolazione."""

    demand_mean_factor: float = 0.60

    shoulder_heat_suppress_t_ext_c: float = 12.0
    """T_ext (°C) sopra la quale il guard shoulder sopprime la domanda di
    riscaldamento quando il deficit medio pesato e' sotto la soglia minima.
    Fisica: a T_ext >= 12°C in mezza stagione (Roma), i deficit lievi tendono
    a recuperarsi spontaneamente per apporti interni e solari. Il guard evita
    avvii di PDC per micro-domanda transitoria in primavera/autunno tardiva.
    Disabilita il guard: impostare a 0.0 (mai soppresso) o a un valore molto alto."""

    shoulder_heat_min_wmean_c: float = 0.30
    """Soglia minima (°C) per heat_def_wmean sotto la quale il guard shoulder
    sopprime heat_sensible. Se la media pesata del deficit e' sotto questa soglia
    con T_ext >= shoulder_heat_suppress_t_ext_c, l'impianto non viene avviato.
    Fisica: wmean bassa significa che solo una/poche zone sono fuori banda; con
    T_ext alta il recupero spontaneo e' probabile. Default 0.30°C (ECO: ~85% del
    deficit medio recuperato da apporti interni in ~30 min a 15°C outdoor)."""

    ctrl_aggr_by_profile: dict[HVACOperatingProfile, float] = field(
        default_factory=lambda: {
            HVACOperatingProfile.COMFORT: 1.00,
            HVACOperatingProfile.BOOST: 1.35,
            HVACOperatingProfile.ECO: 0.85,
            HVACOperatingProfile.SLEEP: 0.75,
            HVACOperatingProfile.AWAY: 0.50,
            HVACOperatingProfile.VACATION: 0.50,
        }
    )
    """Fattore di aggressività per profilo (adimensionale).
    Modula soglie e pesi MPC: > 1.0 = più reattivo, < 1.0 = più conservativo.
    Valore di fallback (profilo sconosciuto): 1.0 (COMFORT nominale).
    """

    def _alpha(self, t_ext: float) -> float:
        """Interpolazione lineare normalizzata [0..1] tra t_override_cold_c e t_override_mild_c."""
        span = self.t_override_mild_c - self.t_override_cold_c
        if span <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (float(t_ext) - self.t_override_cold_c) / span))

    def effective_heat_override_factor(self, t_ext: float | None) -> float:
        """Fattore override per riscaldamento, interpolato in funzione di T_ext.

        Fisica riscaldamento: dispersione proporzionale a (T_int - T_ext).
        A T_ext bassa il deficit di riscaldamento peggiora rapidamente:
        il fattore e' basso (soglia vicina a heat_thr, intervento tempestivo).
        A T_ext alta il deficit si recupera spesso spontaneamente:
        il fattore e' alto (soglia lontana, evita avvii inutili).

        Comportamento ai limiti:
        - T_ext <= t_override_cold_c -> demand_override_factor_cold   (inverno)
        - T_ext >= t_override_mild_c -> demand_override_factor_mild   (estate)
        - T_ext = None               -> demand_override_factor        (fallback invernale)

        Args:
            t_ext: temperatura esterna in degC, o None se non disponibile.

        Returns:
            Fattore adimensionale >= 1.0.
        """
        if t_ext is None:
            return float(self.demand_override_factor)
        alpha = self._alpha(t_ext)
        return float(self.demand_override_factor_cold) + alpha * (
            float(self.demand_override_factor_mild) - float(self.demand_override_factor_cold)
        )

    def effective_cool_override_factor(self, t_ext: float | None) -> float:
        """Fattore override per raffrescamento, interpolato in funzione di T_ext.

        Fisica raffrescamento: a T_ext alta il calore entra dall'esterno
        continuamente, il surplus di cooling peggiora rapidamente e non
        si recupera da solo, quindi il fattore e' basso (intervento tempestivo).
        A T_ext bassa il raffreddamento estivo e' raro e i surplus sono
        transitori, quindi il fattore e' alto (soglia lontana).

        Curva inversa rispetto a effective_heat_override_factor:
        - T_ext <= t_override_cold_c -> demand_override_factor_mild   (inverno: cooling raro)
        - T_ext >= t_override_mild_c -> demand_override_factor_cold   (estate: cooling urgente)
        - T_ext = None               -> demand_override_factor        (fallback invernale)

        Args:
            t_ext: temperatura esterna in degC, o None se non disponibile.

        Returns:
            Fattore adimensionale >= 1.0.
        """
        if t_ext is None:
            return float(self.demand_override_factor)
        alpha = self._alpha(t_ext)
        return float(self.demand_override_factor_mild) - alpha * (
            float(self.demand_override_factor_mild) - float(self.demand_override_factor_cold)
        )

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

    def ctrl_aggr(self, profile: HVACOperatingProfile) -> float:
        """Restituisce il fattore di aggressività di controllo per il profilo dato.

        Args:
            profile: profilo operativo HVAC attivo.

        Returns:
            Fattore adimensionale (0.2..1.35). Fallback 1.0 per profili sconosciuti.
        """
        return float(self.ctrl_aggr_by_profile.get(profile, 1.0))


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

    # Segnale ML: delta WOT addizionale in riscaldamento basato su regime meteorologico.
    # regime_hint="cold" → giornata fredda in assoluto (t_smooth bassa su scala annuale).
    # cold_snap=True     → giornata fredda *per la stagione* (sotto il prototipo stagionale ML).
    # I due delta si escludono: si applica il maggiore (cold_hint ha priorità su cold_snap).
    regime_cold_delta_c: float = 2.0       # delta WOT (°C) per regime_hint="cold"
    regime_cold_snap_delta_c: float = 1.0  # delta WOT (°C) per cold_snap=True in shoulder

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

    setpoint_dp_c: float = 16.0
    setpoint_ddp_c: float = 1.0
    ddp_device_step_c: float = 1.0
    hysteresis_c: float = 0.2

    dp_control_percentile: float = 0.8
    dp_sp_min_c: float = 7.0
    dp_sp_max_c: float = 15.0
    dp_setpoint_from_psychrometrics: bool = True

    water_on_for_dehumid: bool = False
    outdoor_dp_headroom_c: float = 0.2

    # ------------------------------------------------------------------
    # Soglie gate deumidifica (condizioni fisicamente motivate).
    # Calibrate su: attico romano, vetri doppio/triplo, nessun ponte
    # termico critico, VMC RER020i efficace in 1-2h.
    # ------------------------------------------------------------------

    rh_dehum_absolute_threshold_pct: float = 67.0
    """Condizione B: soglia UR indoor max (%) oltre la quale la deumidifica
    e' autorizzata indipendentemente dallo stato del cooling.
    Fisica: UR > 67% causa disagio percepito (Fanger ISO 7730) e favorisce
    muffe su superfici parzialmente fredde.
    Default 67%: con VMC efficace in 1-2h garantisce ritorno sotto 63%."""

    dp_dehum_critical_threshold_c: float = 16.5
    """Condizione C: soglia DP indoor max (degC) oltre la quale la deumidifica
    e' autorizzata come guardrail preventivo su superfici passive.
    Fisica: DP > 16.5 degC -> superfici a <=16 degC (vetri notturna,
    evaporatori aperti) possono andare in condensa. Per attico romano
    e' anche il guardrail pre-avvio estivo del cooling.
    Default 16.5 degC ~= UR 61% a 26 degC (coerente con soglia B)."""

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

    speed_iaq_min:
        Velocita minima VMC in modalita IAQ_ONLY (appartamento occupato, nessuna
        domanda termica). Garantisce il ricambio d'aria minimo indipendentemente
        dal DP. Tipicamente 1 (minimo dispositivo attivo).

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
    speed_iaq_min: int = 1

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
    mode_shoulder: str = "shoulder"

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

    # Soglia finestre aperte per disabilitare l'impianto [minuti].
    # Se le finestre risultano aperte da piu di questo tempo, il modo
    # scende a OFF anche con appartamento occupato (nessun senso climatizzare
    # con dispersione attiva). Default 30 min - coerente con inerzia radiante.
    windows_open_off_minutes: float = 30.0

    # Configurazione fisica del comfort engine (ISO 7730 PMV/PPD).
    # Il default replica il comportamento precedente hardcoded in builder.py
    # (zona climatica D, met=1.10, clo standard).
    # Per Roma bordo D/E, considerare climate_zone=ClimateZoneIT.E (clo_winter=1.15).
    comfort_policy: ConfortPolicyConfig = field(
        default_factory=lambda: ConfortPolicyConfig(
            climate_zone=ClimateZoneIT.D,
            base_met=1.10,
            base_clo_summer=0.50,
            base_clo_shoulder=0.70,
            default_clo_winter=1.00,
            zone_clo_delta_enabled=True,
            non_living_high_speed_hi_scale=0.90,
            living_high_speed_hi_scale=1.05,
            compliance_mode=ComplianceMode.OFF,
            heating_allowed_from=time(5, 30),
            heating_allowed_to=time(23, 30),
            cooling_allowed_from=time(8, 0),
            cooling_allowed_to=time(22, 30),
        )
    )
