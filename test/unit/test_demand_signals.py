"""Test unitari per compute_gating — isteresi PDC mode-aware (Patch 0016).

Problema risolto
----------------
Prima della patch, ``pdc_currently_on`` era un flag booleano mode-agnostico.
La branch "keep cooling running" scattava ogni volta che la PDC era alimentata,
indipendentemente dal fatto che fosse in heating o cooling. Con
``cool_sur_global=0.0`` e ``cool_pdc_off_margin=0.3``::

    cool_override = (0.0 > -0.3) and not free_cool_ok = True

...producendo ``cool_sensible=True`` e ``PlantMode.COOLING`` anche con
domanda fredda zero e l'unica zona fuori banda lato freddo.

Copertura
---------
A. Bug di regressione — cooling forzato con PDC in heating
   A1. PDC on in heating, cool_sur_global=0.0  → cool_sensible=False  ← il bug
   A2. PDC on in heating, cool_sur_global=0.6  → cool_sensible=True   (start path)
   A3. PDC on in cooling, cool_sur_global=0.0  → cool_sensible=True   (keep-running ok)
   A4. PDC on in cooling, cool_sur_global=-0.4 → cool_sensible=False  (rientro ok)

B. Simmetrico — heating forzato con PDC in cooling
   B1. PDC on in cooling, heat_def_global=0.0  → heat_sensible=False
   B2. PDC on in cooling, heat_def_global=0.6  → heat_sensible=True   (start path)
   B3. PDC on in heating, heat_def_global=0.0  → heat_sensible=True   (keep-running ok)
   B4. PDC on in heating, heat_def_global=-0.4 → heat_sensible=False  (rientro ok)

C. Fail-safe pdc_current_mode=None
   C1. PDC on, mode=None, cool_sur_global=0.0  → cool_sensible=False
   C2. PDC on, mode=None, heat_def_global=0.0  → heat_sensible=False

D. PDC spenta — nessuna regressione
   D1. PDC off, cool_sur_global=0.6  → cool_sensible=True  (start)
   D2. PDC off, cool_sur_global=0.2  → cool_sensible=False (sotto soglia 0.5)
   D3. PDC off, heat_def_global=0.6  → heat_sensible=True  (start)

Nota implementativa sui guard shoulder (Fase 2b/2c)
----------------------------------------------------
I guard sopprimono ``heat_sensible`` a T_ext alta (>= 12°C di default).
Per isolare il comportamento dell'isteresi PDC, tutti i test di calore usano
``t_ext=None`` (guard disabilitato) oppure un ``DemandGatingConfig`` con
``shoulder_heat_suppress_t_ext_c=0.0`` (guard esplicitamente disattivato).

Richiede la venv del progetto con homeassistant installato.
Il path del repo viene aggiunto da test/unit/conftest.py.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.drp_climate_master_v2.plant.decision.mode.gating import (
    compute_gating,
)
from custom_components.drp_climate_master_v2.plant.decision.contracts import (
    PlantDemandSignals,
)
from custom_components.drp_climate_master_v2.plant.decision.config import (
    DemandGatingConfig,
    PlantPlannerConfig,
)
from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile


# ---------------------------------------------------------------------------
# Costanti di riferimento (da PlantPlannerConfig defaults)
# ---------------------------------------------------------------------------
# heat_pdc_on_thr_c   = 0.5   → soglia accensione PDC lato caldo
# heat_pdc_off_margin = 0.3   → margine spegnimento PDC lato caldo
# cool_pdc_on_thr_c   = 0.5   → soglia accensione PDC lato freddo
# cool_pdc_off_margin = 0.3   → margine spegnimento PDC lato freddo

_COOL_ON_THR = 0.5
_COOL_OFF_MARGIN = 0.3
_HEAT_ON_THR = 0.5
_HEAT_OFF_MARGIN = 0.3

# Profilo ECO: attiva la logica isteresi PDC globale (profili COMFORT/BOOST
# usano solo heat_def/cool_sur >= heat_thr/cool_thr, senza logica globale).
_ECO = HVACOperatingProfile.ECO


# ---------------------------------------------------------------------------
# Config con guard shoulder disabilitato
# ---------------------------------------------------------------------------
# shoulder_heat_suppress_t_ext_c = 0.0 → guard mai attivo
# Questo consente di testare l'isteresi heating senza interferenze da T_ext.

def _cfg_no_shoulder_guard() -> PlantPlannerConfig:
    """PlantPlannerConfig con guard shoulder disattivato per isolare l'isteresi."""
    cfg = PlantPlannerConfig()
    gating = replace(cfg.gating, shoulder_heat_suppress_t_ext_c=0.0)
    return replace(cfg, gating=gating)


# ---------------------------------------------------------------------------
# Helper: costruisce PlantDemandSignals minimale
# ---------------------------------------------------------------------------

def _demand(
    *,
    heat_def_max_c: float = 0.0,
    cool_sur_max_c: float = 0.0,
    heat_def_global_c: float | None = None,
    cool_sur_global_c: float | None = None,
    heat_def_wmean_c: float = 0.0,
    cool_sur_wmean_c: float = 0.0,
    heat_cov: float = 0.0,
    cool_cov: float = 0.0,
    heat_headroom_min_c: float = 2.0,
    free_cool_feasible: bool = False,
) -> PlantDemandSignals:
    """Costruisce un PlantDemandSignals con soli i campi rilevanti per il gating.

    I campi VMC sono tutti False/None di default: non interferiscono con
    heat_sensible / cool_sensible nei test di isteresi PDC.
    """
    return PlantDemandSignals(
        heat_def_max_c=heat_def_max_c,
        cool_sur_max_c=cool_sur_max_c,
        heat_def_global_c=heat_def_global_c,
        cool_sur_global_c=cool_sur_global_c,
        heat_def_wmean_c=heat_def_wmean_c,
        cool_sur_wmean_c=cool_sur_wmean_c,
        heat_cov=heat_cov,
        cool_cov=cool_cov,
        heat_headroom_min_c=heat_headroom_min_c,
        free_cool_feasible=free_cool_feasible,
        vmc_req_heating=False,
        vmc_req_cooling=False,
        vmc_req_dehumidif=False,
        vmc_req_water=False,
        vmc_req_free_cooling=False,
        vmc_req_free_heating=False,
        vmc_dehum_feasible=False,
        dp_max_c=10.0,
        dp_dehum_c=10.0,
        outdoor_dp_c=6.0,
    )


# ---------------------------------------------------------------------------
# Helper: chiama compute_gating con parametri fissi per i test di isteresi
# ---------------------------------------------------------------------------

def _gate(
    demand: PlantDemandSignals,
    *,
    pdc_currently_on: bool,
    pdc_current_mode: str | None,
    cfg: PlantPlannerConfig | None = None,
    profile: HVACOperatingProfile = _ECO,
    t_ext: float | None = None,
):
    """Invoca compute_gating con i parametri minimi per isolare l'isteresi PDC."""
    cfg = cfg or _cfg_no_shoulder_guard()
    return compute_gating(
        cfg=cfg,
        demand=demand,
        profile=profile,
        zones_decision=None,
        t_ext=t_ext,
        t_smooth=None,
        regime_hint="mild",
        pdc_currently_on=pdc_currently_on,
        pdc_current_mode=pdc_current_mode,
    )


# ===========================================================================
# CLUSTER A — Bug di regressione: cooling forzato con PDC in heating
# ===========================================================================

class TestCoolingIsteresiConPdcInHeating:
    """PDC in heating: l'isteresi 'keep cooling running' NON deve scattare."""

    def test_A1_pdc_in_heating_cool_sur_zero_no_cool_sensible(self):
        """BUG-0016: PDC on in heating, cool_sur_global=0.0 → cool_sensible=False.

        Questo era il bug: con pdc_currently_on=True e cool_sur_global=0.0,
        la condizione (0.0 > -0.3) era True → cool_override=True → cool_sensible=True.
        Dopo la fix, pdc_cool_on=False perché mode="heating" → si usa il ramo
        start: (0.0 >= 0.5) = False → cool_override=False → cool_sensible=False.
        """
        d = _demand(cool_sur_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="heating")
        assert g.cool_sensible is False, (
            f"cool_sensible deve essere False con PDC in heating e cool_sur_global=0.0, "
            f"got cool_override={g.cool_override}"
        )
        assert g.any_cool is False

    def test_A2_pdc_in_heating_cool_sur_above_threshold_cool_sensible(self):
        """PDC in heating, cool_sur_global=0.6 > soglia start (0.5) → cool_sensible=True.

        Il ramo start deve funzionare anche quando il modo è heating:
        il cooling si attiva se c'è davvero un surplus significativo.
        """
        d = _demand(cool_sur_global_c=0.6, cool_sur_max_c=0.6)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="heating")
        assert g.cool_sensible is True, (
            f"cool_sensible deve essere True con cool_sur_global=0.6 >= soglia {_COOL_ON_THR}"
        )

    def test_A3_pdc_in_cooling_cool_sur_zero_keep_running(self):
        """PDC on in cooling, cool_sur_global=0.0 → keep-running: cool_sensible=True.

        La logica di mantenimento (0.0 > -0.3) deve funzionare correttamente
        quando la PDC è effettivamente in cooling.
        """
        d = _demand(cool_sur_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="cooling")
        assert g.cool_sensible is True, (
            f"cool_sensible deve essere True con PDC in cooling (keep-running): "
            f"(0.0 > -{_COOL_OFF_MARGIN}) = True"
        )

    def test_A4_pdc_in_cooling_cool_sur_below_off_margin_stops(self):
        """PDC on in cooling, cool_sur_global=-0.4 < -off_margin → keep-running: False.

        Il surplus è rientrato abbondantemente: (-0.4 > -0.3) = False.
        L'isteresi permette lo spegnimento.
        """
        d = _demand(cool_sur_global_c=-0.4)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="cooling")
        assert g.cool_sensible is False, (
            f"cool_sensible deve essere False: (-0.4 > -{_COOL_OFF_MARGIN}) = False"
        )


# ===========================================================================
# CLUSTER B — Simmetrico: heating forzato con PDC in cooling
# ===========================================================================

class TestHeatingIsteresiConPdcInCooling:
    """PDC in cooling: l'isteresi 'keep heating running' NON deve scattare."""

    def test_B1_pdc_in_cooling_heat_def_zero_no_heat_sensible(self):
        """Simmetrico di A1: PDC on in cooling, heat_def_global=0.0 → heat_sensible=False."""
        d = _demand(heat_def_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="cooling")
        assert g.heat_sensible is False, (
            f"heat_sensible deve essere False con PDC in cooling e heat_def_global=0.0"
        )
        assert g.any_heat is False

    def test_B2_pdc_in_cooling_heat_def_above_threshold_heat_sensible(self):
        """PDC in cooling, heat_def_global=0.6 > soglia start (0.5) → heat_sensible=True."""
        d = _demand(heat_def_global_c=0.6, heat_def_max_c=0.6, heat_def_wmean_c=0.6)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="cooling")
        assert g.heat_sensible is True, (
            f"heat_sensible deve essere True con heat_def_global=0.6 >= soglia {_HEAT_ON_THR}"
        )

    def test_B3_pdc_in_heating_heat_def_zero_keep_running(self):
        """PDC on in heating, heat_def_global=0.0 → keep-running: heat_sensible=True.

        (0.0 > -0.3) = True → heat_override=True → heat_sensible=True.
        """
        d = _demand(heat_def_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="heating")
        assert g.heat_sensible is True, (
            f"heat_sensible deve essere True con PDC in heating (keep-running): "
            f"(0.0 > -{_HEAT_OFF_MARGIN}) = True"
        )

    def test_B4_pdc_in_heating_heat_def_below_off_margin_stops(self):
        """PDC on in heating, heat_def_global=-0.4 < -off_margin → keep-running: False.

        Il deficit è rientrato: (-0.4 > -0.3) = False. L'isteresi permette lo spegnimento.
        """
        d = _demand(heat_def_global_c=-0.4)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="heating")
        assert g.heat_sensible is False, (
            f"heat_sensible deve essere False: (-0.4 > -{_HEAT_OFF_MARGIN}) = False"
        )


# ===========================================================================
# CLUSTER C — Fail-safe: pdc_current_mode=None
# ===========================================================================

class TestFailSafeModeSconosciuto:
    """Quando device_mode non è disponibile, entrambe le isteresi devono essere disabilitate."""

    def test_C1_mode_none_cool_sur_zero_no_cool_sensible(self):
        """PDC on, mode=None, cool_sur_global=0.0 → cool_sensible=False.

        Fail-safe: pdc_cool_on=False → usa ramo start: (0.0 >= 0.5) = False.
        """
        d = _demand(cool_sur_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode=None)
        assert g.cool_sensible is False, (
            "Con pdc_current_mode=None il ramo keep-running non deve attivarsi"
        )

    def test_C2_mode_none_heat_def_zero_no_heat_sensible(self):
        """PDC on, mode=None, heat_def_global=0.0 → heat_sensible=False.

        Fail-safe: pdc_heat_on=False → usa ramo start: (0.0 >= 0.5) = False.
        """
        d = _demand(heat_def_global_c=0.0)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode=None)
        assert g.heat_sensible is False, (
            "Con pdc_current_mode=None il ramo keep-running non deve attivarsi"
        )

    def test_C3_mode_none_sur_above_threshold_cool_sensible(self):
        """PDC on, mode=None, cool_sur_global=0.6 → cool_sensible=True (start path funziona).

        Il fail-safe disabilita solo il keep-running, non il ramo start.
        """
        d = _demand(cool_sur_global_c=0.6, cool_sur_max_c=0.6)
        g = _gate(d, pdc_currently_on=True, pdc_current_mode=None)
        assert g.cool_sensible is True


# ===========================================================================
# CLUSTER D — PDC spenta: nessuna regressione sul path start
# ===========================================================================

class TestPdcSpentaNessunaRegressione:
    """Con PDC spenta (pdc_currently_on=False) il comportamento pre-patch non deve cambiare."""

    def test_D1_pdc_off_cool_sur_above_threshold(self):
        """PDC off, cool_sur_global=0.6 → cool_sensible=True (ramo start invariato)."""
        d = _demand(cool_sur_global_c=0.6, cool_sur_max_c=0.6)
        g = _gate(d, pdc_currently_on=False, pdc_current_mode=None)
        assert g.cool_sensible is True

    def test_D2_pdc_off_cool_sur_below_threshold(self):
        """PDC off, cool_sur_global=0.2 < soglia (0.5) → cool_sensible=False."""
        d = _demand(cool_sur_global_c=0.2)
        g = _gate(d, pdc_currently_on=False, pdc_current_mode=None)
        assert g.cool_sensible is False

    def test_D3_pdc_off_heat_def_above_threshold(self):
        """PDC off, heat_def_global=0.6 → heat_sensible=True (ramo start invariato)."""
        d = _demand(heat_def_global_c=0.6, heat_def_max_c=0.6, heat_def_wmean_c=0.6)
        g = _gate(d, pdc_currently_on=False, pdc_current_mode=None)
        assert g.heat_sensible is True

    def test_D4_pdc_off_heat_def_below_threshold(self):
        """PDC off, heat_def_global=0.2 < soglia (0.5) → heat_sensible=False."""
        d = _demand(heat_def_global_c=0.2, heat_def_max_c=0.2)
        g = _gate(d, pdc_currently_on=False, pdc_current_mode=None)
        assert g.heat_sensible is False

    def test_D5_pdc_off_mode_cooling_same_as_none(self):
        """PDC off, mode='cooling': pdc_cool_on=False (off AND cooling = False)."""
        d = _demand(cool_sur_global_c=0.0)
        g = _gate(d, pdc_currently_on=False, pdc_current_mode="cooling")
        assert g.cool_sensible is False


# ===========================================================================
# CLUSTER E — Scenario real-world dal log del 2026-05-17 16:05
# ===========================================================================

class TestScenarioRealWorld20260517:
    """Riproduce il tick 16:05:33 che generava il falso COOLING.

    Condizioni dal log:
    - Profilo ECO, stagione shoulder
    - PDC on, device_mode=1 (heating), compressore on da 14 min
    - cool_sur_global = 0.0°C (nessuna zona calda)
    - heat_def_max = 1.4°C (guest_bedroom fredda a 21.4°C)
    - T_ext = 24.3°C (usata per guard shoulder → disabilitato via cfg)
    """

    def test_E1_cool_sensible_false_tick_16_05(self):
        """Con PDC in heating e cool_sur=0.0: cool_sensible DEVE essere False."""
        d = _demand(
            heat_def_max_c=1.4,
            heat_def_global_c=0.1,
            heat_def_wmean_c=0.3,
            heat_cov=0.64,
            cool_sur_max_c=0.0,
            cool_sur_global_c=0.0,
            cool_cov=0.0,
            cool_sur_wmean_c=0.0,
        )
        g = _gate(
            d,
            pdc_currently_on=True,
            pdc_current_mode="heating",  # device_mode=1
            t_ext=None,                  # guard shoulder disabilitato
        )
        assert g.cool_sensible is False
        assert g.any_cool is False

    def test_E2_any_cool_false_no_cooling_mode(self):
        """any_cool deve essere False: nessun trigger cooling attivo."""
        d = _demand(
            heat_def_max_c=1.4,
            heat_def_global_c=0.1,
            heat_def_wmean_c=0.3,
            heat_cov=0.64,
            cool_sur_global_c=0.0,
        )
        g = _gate(d, pdc_currently_on=True, pdc_current_mode="heating")
        assert g.any_cool is False
        assert g.any_cool_or_dehum is False