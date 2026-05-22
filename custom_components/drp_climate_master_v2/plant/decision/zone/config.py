from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(slots=True)
class RcZoneParams:
    """Parametri RC del primo ordine per una singola zona.

    Modello a tempo continuo::

        dT/dt = (T_out - T) / tau_h + k * u

    dove ``u in {0, 1}`` è lo stato della valvola di zona (0 = chiusa, 1 = aperta)
    e ``k`` assume segno opposto a seconda della modalità operativa:

    * **Riscaldamento** (``k = k_c_per_h > 0``): la valvola aperta eroga calore,
      la temperatura della zona sale.
    * **Raffrescamento** (``k = k_cool_c_per_h < 0``): la valvola aperta assorbe
      calore, la temperatura della zona scende.

    I due guadagni sono fisicamente distinti perché i pannelli Eurotherm Leonardo 3.5
    hanno potenze specifiche diverse nelle due direzioni:

    * Riscaldamento: 69 W/m² a T_man=35°C, ΔT=4°C.
    * Raffrescamento: 52 W/m² a T_man=14°C, ΔT=4°C.

    Il rapporto 52/69 ~= 0.754 determina il default di ``k_cool_c_per_h`` rispetto
    a ``k_c_per_h``.

    La costante di tempo ``tau_h`` è indipendente dalla direzione: dipende
    dall'inerzia termica dell'edificio (massa muraria + aria), non dal pannello.

    Attributi
    ---------
    tau_h : float  [h]
        Costante di tempo termica della zona. Default 6.0 h (tipico per
        edificio in muratura, zona 50 m³). Determina la velocità con cui
        la zona si avvicina a T_out in assenza di azionamento.

    k_c_per_h : float  [°C/h]  (positivo)
        Guadagno effettivo di riscaldamento con valvola aperta. Assorbe
        implicitamente temperatura di mandata, efficienza emettitore e
        distribuzione termica. Deve essere > 0.
        Default 0.8 °C/h calibrato su pannello radiante soffitto.

    k_cool_c_per_h : float  [°C/h]  (negativo)
        Guadagno effettivo di raffrescamento con valvola aperta (segno negativo:
        la zona si raffredda). Default -0.603 °C/h = -0.8 x (52/69).
        Deve essere < 0.

    Invarianti
    ----------
    * ``tau_h > 0``
    * ``k_c_per_h > 0``
    * ``k_cool_c_per_h < 0``

    Violazioni sollevate in ``__post_init__`` con ``ValueError``.
    """

    tau_h: float = 6.0
    k_c_per_h: float = 0.8
    k_cool_c_per_h: float = -0.603
    """Guadagno raffrescamento (°C/h, negativo).

    Default -0.603 = -0.8 x (52 W/m² / 69 W/m²), derivato dal rapporto
    delle potenze specifiche del pannello Eurotherm Leonardo 3.5 nelle due
    direzioni. Può essere sovrascritto per zona tramite ``rc_by_zone`` in
    ``ControlConfig``.
    """

    def __post_init__(self) -> None:
        """Valida gli invarianti fisici dei parametri RC."""
        if self.tau_h <= 0.0:
            raise ValueError(
                f"RcZoneParams.tau_h deve essere > 0, ottenuto {self.tau_h}"
            )
        if self.k_c_per_h <= 0.0:
            raise ValueError(
                f"RcZoneParams.k_c_per_h (guadagno heating) deve essere > 0, "
                f"ottenuto {self.k_c_per_h}"
            )
        if self.k_cool_c_per_h >= 0.0:
            raise ValueError(
                f"RcZoneParams.k_cool_c_per_h (guadagno cooling) deve essere < 0, "
                f"ottenuto {self.k_cool_c_per_h}"
            )


@dataclass(slots=True)
class MpcConfig:
    """MPC-lite configuration.

    This version is a per-zone receding-horizon optimizer over a binary input u∈{0,1}
    (zone valve ON/OFF). It is intentionally small and deterministic.
    """

    dt_minutes: int = 10
    horizon_steps: int = 12

    # Cost weights
    w_comfort: float = 10.0
    w_energy: float = 0.3
    w_switch: float = 1.5

    # Comfort slack (°C)
    # Ammorbidisce la penalità comfort allargando la banda: [t_min - slack, t_max + slack].
    # Se "accetti discomfort", questa è la leva più efficace contro il comportamento "full-on" su micro-sforamenti.
    comfort_slack_c: float = 0.10

    # --- Degeneracy detection / retry (1 sola ripianificazione)
    # Trigger tipico: molte zone FULL-ON mentre tutte le zone sono già "in band".
    degenerate_full_on_pct_thr: float = 80.0
    degenerate_retry_enabled: bool = True
    degenerate_retry_only_if_all_in_band: bool = True

    # Retry config: più tolleranza comfort + più focus energia (discomfort accettato)
    degenerate_retry_comfort_slack_c: float = 0.25
    degenerate_retry_w_comfort_mult: float = 0.35
    degenerate_retry_w_energy_mult: float = 1.50
    degenerate_retry_w_switch_mult: float = 0.75

    # Practical constraints
    min_on_minutes: int = 10
    min_off_minutes: int = 10
    min_switch_minutes: int = 10


@dataclass(slots=True)
class ControlConfig:
    """Top-level control configuration.

    For the first iteration we keep this runtime-only (defaults + optional overrides).
    Later you can expose it through ConfigEntry options.
    """

    mpc: MpcConfig = field(default_factory=MpcConfig)

    # Global defaults for RC params
    rc_default: RcZoneParams = field(default_factory=RcZoneParams)

    # Optional per-zone overrides, keyed by zone slug (e.g., "living")
    rc_by_zone: Mapping[str, RcZoneParams] = field(default_factory=dict)
