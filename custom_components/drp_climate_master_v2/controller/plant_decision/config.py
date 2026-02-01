from __future__ import annotations

from dataclasses import dataclass


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
