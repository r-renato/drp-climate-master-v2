from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.enums import HVACOperatingProfile

@dataclass(slots=True)
class PlantPlannerConfig:
    """Configurazione per la pianificazione impianto.

    ATTENZIONE: i valori di default sono *conservativi* e vanno tarati con dati reali
    (commissioning). In questa fase il planner non è integrato nel Supervisor.
    """

    # ---- Comfort band usage (°C)
    # Soglia per considerare "richiesta sensibile" in heating/cooling
    heat_on_deficit_c: float = 0.3     # accendi se T_op < T_min - soglia
    cool_on_surplus_c: float = 0.3     # accendi se T_op > T_max + soglia

    # ---- Profile-aware multi-zone gating (dimensionless)
    # In modalità "energy saving" evita di accendere la PDC per micro-sforamenti in una sola zona.
    # COMFORT/BOOST: quorum=0 => basta anche una singola zona.
    # ECO/SLEEP/AWAY/VACATION: richiede una frazione minima (coverage) di "area/weight" fuori banda,
    # salvo override per errori grandi (override_factor).
    quorum_cov_eco: float = 0.25
    quorum_cov_sleep: float = 0.20
    quorum_cov_away: float = 0.40
    quorum_cov_vacation: float = 0.50

    # Se il deficit/surplus massimo supera (thr * override_factor), accendi comunque anche se la coverage è bassa
    # (es. una stanza molto fuori comfort).
    demand_override_factor: float = 2.0

    # Se la media pesata del deficit/surplus supera (thr * mean_factor), accendi anche se la coverage è appena sotto quorum
    # (utile quando tante zone sono poco fuori banda).
    demand_mean_factor: float = 0.60

    # ---- Dew point guard (cooling) (°C)
    dp_margin_c: float = 2.0           # margine igrometrico sopra DP_max
    delta_surface_water_c: float = 1.0 # Δ(superficie↔acqua) conservativo

    # ---- PDC setpoints (°C)
    # Curva climatica *semplice* (lineare) per heating: T_wot = base + k*(t_ref - t_out)
    heat_curve_base_c: float = 35.0
    heat_curve_k_c_per_c: float = 0.7
    heat_curve_ref_outdoor_c: float = 15.0
    heat_wot_min_c: float = 28.0
    heat_wot_max_c: float = 50.0
    heat_dt_c: float = 2.0            # default ΔT

    # ---- Heating curve enhancements (profile offset + indoor feedback + anti-hunting)
    # Nota: chiavi allineate a HVACOperatingProfile.value ("Comfort", "Eco", ...)
    heat_profile_offset_c: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.COMFORT:  0.0,
        HVACOperatingProfile.ECO:      -1.5,
        HVACOperatingProfile.BOOST:    +3.0,
        HVACOperatingProfile.SLEEP:    -1.0,
        HVACOperatingProfile.AWAY:     -3.0,
        HVACOperatingProfile.VACATION: -4.0,
    })

    # Indoor feedback (quanto alzare WOT per ogni °C di deficit medio)
    heat_feedback_gain_c_per_c: float = 0.8
    heat_feedback_max_up_c: float = 6.0
    heat_feedback_max_down_c: float = 2.0

    # Extra boost se una zona è molto fuori banda (worst-case)
    heat_kick_on_max_def_c: float = 4.0
    heat_kick_extra_c: float = 2.0

    # Anti-hunting: limita variazione setpoint nel tempo
    heat_wot_rate_limit_c_per_min: float = 0.5
    heat_wot_deadband_c: float = 0.2

    cool_profile_offset_c: dict[HVACOperatingProfile, float] = field(default_factory=lambda: {
        HVACOperatingProfile.COMFORT:  0.0,     # baseline
        HVACOperatingProfile.ECO:      +1.0,    # meno spinta: acqua più calda
        HVACOperatingProfile.BOOST:    -1.5,    # più spinta: acqua più fredda
        HVACOperatingProfile.SLEEP:    +1.5,    # più “morbido” e silenzioso
        HVACOperatingProfile.AWAY:     +3.0,    # quasi niente cooling sensibile
        HVACOperatingProfile.VACATION: +4.0,
    })
    
    # Cooling setpoint (flat, da evolvere)
    cool_wot_min_c: float = 7.0
    cool_wot_max_c: float = 18.0
    cool_wot_default_c: float = 12.0
    cool_dt_c: float = 7.0

    # ---- Radiante (secondary) targets (°C)
    # In heating la mandata radiante tipica è più bassa del primario (mixing).
    heat_rad_supply_offset_c: float = 5.0
    heat_rad_supply_min_c: float = 25.0
    heat_rad_supply_max_c: float = 40.0

    # In cooling il vincolo anticondensa domina: T_sup_rad >= DP_max + margin + Δ
    cool_rad_supply_min_c: float = 16.0
    cool_rad_supply_max_c: float = 22.0

    # ---- VMC defaults (se vuoi guidarla dal planner)
    vmc_mode_winter: str = "winter"
    vmc_mode_summer: str = "summer"
    vmc_setpoint_t_c: float = 24.0
    vmc_setpoint_rh_pct: float = 51.0
    vmc_setpoint_dp_c: float = 15.1
    vmc_setpoint_ddp_c: float = 0.0
