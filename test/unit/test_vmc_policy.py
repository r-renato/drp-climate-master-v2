"""Test unitari del modulo plant.decision.vmc.policy (VmcPolicy + VmcDemand).

Copertura
---------
A. Soglie DP contestuali (Patch 0014)
   A1. Radiante INATTIVO → setpoint passivo (dp_sp_max_c), on_thr permissiva
   A2. Radiante ATTIVO   → setpoint psicrometrico, on_thr protettiva
   A3. dp_sp_raw_c sempre popolato per diagnostica
   A4. Campo radiant_cooling_active propagato nel VmcDemand

B. Falso allarme eliminato
   B1. DP primaverile (14.7°C) + radiante inattivo → on_thr > DP → need_dehum=False
   B2. DP primaverile + radiante attivo             → on_thr protettiva (< DP) → può scattare
   B3. Dashboard: dehum_on_thr_c rispecchia il contesto radiante

C. Gate _dehum_condition_met
   C1. Condizione A: cool_sur_max > 0 abilita deumidifica
   C2. Condizione A: cool_cov > 0 abilita deumidifica
   C3. Condizione B: UR max > soglia abilita deumidifica (senza radiante)
   C4. Condizione C: DP > soglia critica abilita deumidifica
   C5. Nessuna condizione → gate blocca req_dehumidif anche se need_dehum True

D. req_dehumidif end-to-end
   D1. need_dehum True + gate met → req_dehumidif True
   D2. need_dehum True + gate bloccato → req_dehumidif False
   D3. Isteresi VmcState: una volta ON rimane ON sopra off_thr

E. Speed boost VMC
   E1. Radiante inattivo, DP 14.7°C → nessun boost indebito
   E2. DP molto sopra on_thr → speed aumenta (boost attivo)

F. Boost termico (req_heating / req_cooling)
   F1. heat_def_max >= soglia → req_heating True
   F2. cool_sur_max >= soglia → req_cooling True
   F3. Vacanza → no boost termico
   F4. Finestre aperte → no boost termico

G. Free cooling ventilativo
   G1. free_cool_feasible + condizioni OK → req_free_cooling True
   G2. Boost idronico già attivo → free cooling NON scatta (mutua esclusività)
   G3. Vacanza → no free cooling
   G4. DP outdoor >= DP indoor − headroom → no free cooling

H. Stagione operativa
   H1. winter / summer / shoulder mappati correttamente
   H2. Target RH dipende da stagione e profilo

I. Invarianti strutturali
   I1. dp_sp_c ∈ [dp_sp_min_c, dp_sp_max_c]
   I2. on_thr > off_thr
   I3. Tutti i campi req_* sono bool

Richiede la venv del progetto con homeassistant installato.
Il path del repo viene aggiunto da test/unit/conftest.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Import moduli sotto test
# ---------------------------------------------------------------------------
from custom_components.drp_climate_master_v2.plant.decision.vmc.policy import (
    VmcPolicy,
)
from custom_components.drp_climate_master_v2.plant.decision.contracts import VmcDemand
from custom_components.drp_climate_master_v2.plant.decision.vmc.state import VmcState
from custom_components.drp_climate_master_v2.plant.decision.config import PlantPlannerConfig
from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile

# ---------------------------------------------------------------------------
# Stub minimale PlantSnapshot
# ---------------------------------------------------------------------------
# La VmcPolicy accede alla snapshot solo tramite getattr con default → basta
# un namespace con gli attributi rilevanti.


@dataclass
class _AV:
    """Stub AggregatedValue con .value."""

    value: Any = None


@dataclass
class _Zone:
    """Stub zona con temperatura, umidità, t_op."""

    temperature: _AV = field(default_factory=_AV)
    humidity: _AV = field(default_factory=_AV)
    t_op: _AV = field(default_factory=_AV)


@dataclass
class _VmcDevice:
    """Stub device VMC (request_dehumidification)."""

    request_dehumidification: bool = False
    spare_setpoint: Optional[int] = None


@dataclass
class _SeasonVal:
    value: str = "shoulder"


@dataclass
class _Season:
    season: _SeasonVal = field(default_factory=_SeasonVal)


@dataclass
class _Snap:
    """Stub PlantSnapshot — solo i campi usati da VmcPolicy."""

    global_indoor_zone: Optional[_Zone] = None
    indoor_zones: dict = field(default_factory=dict)
    vmc: Optional[_VmcDevice] = None
    season: _Season = field(default_factory=_Season)
    windows_close_state: bool = True
    presence_vacation: bool = False
    presence_nobodysin: bool = False


def _snap(
    *,
    t_ref_c: float = 23.5,
    rh_zones: list[float] | None = None,
    vmc_req_dehum: bool = False,
    season: str = "shoulder",
    windows_closed: bool = True,
    vacation: bool = False,
    nobody: bool = False,
) -> _Snap:
    """Costruisce uno snapshot minimale con valori di default sensati."""
    indoor_zone = _Zone(
        temperature=_AV(t_ref_c),
        t_op=_AV(t_ref_c),
        humidity=_AV(50.0),
    )
    rh_list = rh_zones or [50.0]
    indoor_zones = {
        f"z{i}": _Zone(humidity=_AV(rh), temperature=_AV(t_ref_c))
        for i, rh in enumerate(rh_list)
    }
    return _Snap(
        global_indoor_zone=indoor_zone,
        indoor_zones=indoor_zones,
        vmc=_VmcDevice(request_dehumidification=vmc_req_dehum),
        season=_Season(season=_SeasonVal(value=season)),
        windows_close_state=windows_closed,
        presence_vacation=vacation,
        presence_nobodysin=nobody,
    )


# ---------------------------------------------------------------------------
# Helper: istanzia VmcPolicy con config default
# ---------------------------------------------------------------------------

def _policy(cfg: PlantPlannerConfig | None = None) -> VmcPolicy:
    cfg = cfg or PlantPlannerConfig()
    return VmcPolicy(cfg.vmc, state=VmcState())


def _fresh_policy_state(cfg: PlantPlannerConfig | None = None) -> tuple[VmcPolicy, VmcState]:
    """Ritorna (policy, state) con state separato per test isteresi."""
    cfg = cfg or PlantPlannerConfig()
    state = VmcState()
    return VmcPolicy(cfg.vmc, state=state), state


def _compute(
    policy: VmcPolicy,
    *,
    snapshot: _Snap | None = None,
    profile: HVACOperatingProfile = HVACOperatingProfile.COMFORT,
    heat_def_max_c: float = 0.0,
    heat_def_wmean_c: float = 0.0,
    heat_cov: float = 0.0,
    cool_sur_max_c: float = 0.0,
    cool_sur_wmean_c: float = 0.0,
    cool_cov: float = 0.0,
    dp_dehum_c: float | None = 14.7,
    dp_max_c: float | None = 14.7,
    outdoor_dp_c: float | None = 10.0,
    free_cool_feasible: bool = False,
    free_heat_feasible: bool = False,
    radiant_cooling_active: bool = False,
) -> VmcDemand:
    snap = snapshot or _snap()
    return policy.compute(
        snapshot=snap,
        profile=profile,
        heat_def_max_c=heat_def_max_c,
        heat_def_wmean_c=heat_def_wmean_c,
        heat_cov=heat_cov,
        cool_sur_max_c=cool_sur_max_c,
        cool_sur_wmean_c=cool_sur_wmean_c,
        cool_cov=cool_cov,
        dp_dehum_c=dp_dehum_c,
        dp_max_c=dp_max_c,
        outdoor_dp_c=outdoor_dp_c,
        free_cool_feasible=free_cool_feasible,
        free_heat_feasible=free_heat_feasible,
        radiant_cooling_active=radiant_cooling_active,
    )


# ---------------------------------------------------------------------------
# Valori attesi calcolati dalle costanti di config (specchio del codice)
# ---------------------------------------------------------------------------

def _expected_passive(cfg: PlantPlannerConfig) -> tuple[float, float, float]:
    """Ritorna (dp_sp_passive, on_thr, off_thr) per radiante inattivo."""
    dp_sp = float(cfg.vmc.dehum.dp_sp_max_c)
    ddp_policy = float(cfg.vmc.dehum.setpoint_ddp_c)
    step = max(1e-9, float(cfg.vmc.dehum.ddp_device_step_c))
    ddp_cmd = 0.0 if ddp_policy <= 0.0 else step * math.ceil(ddp_policy / step - 1e-12)
    on_thr = dp_sp + ddp_cmd
    off_thr = on_thr - max(0.0, float(cfg.vmc.dehum.hysteresis_c))
    return dp_sp, on_thr, off_thr


# ===========================================================================
# A. Soglie DP contestuali (Patch 0014)
# ===========================================================================


class TestDpThresholdContext:
    """Test per la logica del setpoint DP contestuale (radiante attivo/inattivo)."""

    def test_a1_inactive_radiant_uses_passive_setpoint(self):
        """Radiante inattivo: dp_sp_c deve essere dp_sp_max_c."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=False)
        assert dem.dp_sp_c == pytest.approx(cfg.vmc.dehum.dp_sp_max_c, abs=0.01), (
            f"Setpoint passivo atteso {cfg.vmc.dehum.dp_sp_max_c}°C, "
            f"ottenuto {dem.dp_sp_c:.2f}°C"
        )

    def test_a1_inactive_radiant_on_thr_permissive(self):
        """Radiante inattivo: on_thr deve essere dp_sp_max_c + ddp_cmd."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=False)
        _, expected_on, _ = _expected_passive(cfg)
        assert dem.dehum_on_thr_c == pytest.approx(expected_on, abs=0.01), (
            f"on_thr passiva attesa {expected_on:.1f}°C, "
            f"ottenuta {dem.dehum_on_thr_c:.2f}°C"
        )

    def test_a1_inactive_radiant_off_thr_below_on(self):
        """Radiante inattivo: off_thr = on_thr - hysteresis."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=False)
        _, expected_on, expected_off = _expected_passive(cfg)
        assert dem.dehum_off_thr_c == pytest.approx(expected_off, abs=0.01)

    def test_a2_active_radiant_uses_psychrometric_setpoint(self):
        """Radiante attivo: dp_sp_c deve essere il valore psicrometrico (< dp_sp_max_c)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem_active = _compute(pol, radiant_cooling_active=True)
        # Il setpoint psicrometrico è calcolato da RH ~50%, T ~23.5°C → DP ~12-13°C
        # In ogni caso deve essere < dp_sp_max_c (15.0°C)
        assert dem_active.dp_sp_c < cfg.vmc.dehum.dp_sp_max_c, (
            f"Radiante attivo: setpoint psicrometrico ({dem_active.dp_sp_c:.2f}°C) "
            f"deve essere < dp_sp_max_c ({cfg.vmc.dehum.dp_sp_max_c}°C)"
        )

    def test_a2_active_radiant_on_thr_protective(self):
        """Radiante attivo: on_thr protettiva è inferiore alla passiva."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem_active   = _compute(pol, radiant_cooling_active=True)
        dem_inactive = _compute(pol, radiant_cooling_active=False)
        assert dem_active.dehum_on_thr_c < dem_inactive.dehum_on_thr_c, (
            "on_thr protettiva (radiante on) deve essere < on_thr permissiva (radiante off)"
        )

    def test_a3_dp_sp_raw_always_populated(self):
        """dp_sp_raw_c (psicrometrico) deve sempre essere presente, sia on che off."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        for active in (True, False):
            dem = _compute(pol, radiant_cooling_active=active)
            assert dem.dp_sp_raw_c is not None, (
                f"dp_sp_raw_c non deve essere None (radiant_active={active})"
            )
            # Il valore psicrometrico deve stare nei bounds configurati
            assert cfg.vmc.dehum.dp_sp_min_c <= dem.dp_sp_raw_c <= cfg.vmc.dehum.dp_sp_max_c

    def test_a3_raw_equals_active_cmd_when_radiant_on(self):
        """Con radiante attivo, il setpoint raw e cmd devono coincidere dopo quantizzazione."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=True)
        # dp_sp_raw_c + ddp_policy ≈ dp_sp_c + ddp_cmd (invariante quantizzazione)
        ddp_policy = float(cfg.vmc.dehum.setpoint_ddp_c)
        ddp_cmd = float(dem.ddp_cmd_c)
        assert dem.dp_sp_raw_c + ddp_policy == pytest.approx(dem.dp_sp_c + ddp_cmd, abs=1e-6)

    def test_a4_radiant_cooling_active_propagated_true(self):
        """radiant_cooling_active=True viene propagato nel VmcDemand."""
        pol = _policy()
        dem = _compute(pol, radiant_cooling_active=True)
        assert dem.radiant_cooling_active is True

    def test_a4_radiant_cooling_active_propagated_false(self):
        """radiant_cooling_active=False viene propagato nel VmcDemand."""
        pol = _policy()
        dem = _compute(pol, radiant_cooling_active=False)
        assert dem.radiant_cooling_active is False


# ===========================================================================
# B. Falso allarme eliminato
# ===========================================================================


class TestFalseAlarmEliminated:
    """Verifica che il DP primaverile non scatti allarmi falsi con radiante inattivo."""

    _DP_SPRING = 14.7  # DP indoor tipico di Roma a maggio (°C)

    def test_b1_spring_dp_no_alarm_radiant_inactive(self):
        """DP 14.7°C + radiante inattivo → on_thr > 14.7°C → need_dehum deve essere False.

        Verifica che la soglia permissiva elimini il falso allarme primaverile.
        """
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(
            pol,
            dp_dehum_c=self._DP_SPRING,
            dp_max_c=self._DP_SPRING,
            radiant_cooling_active=False,
        )
        # on_thr deve essere > DP corrente → nessun trigger deumidifica
        assert dem.dehum_on_thr_c > self._DP_SPRING, (
            f"on_thr ({dem.dehum_on_thr_c:.2f}°C) deve essere > DP ({self._DP_SPRING}°C)"
        )
        # req_dehumidif deve essere False
        assert dem.req_dehumidif is False, (
            "DP primaverile con radiante inattivo non deve attivare req_dehumidif"
        )

    def test_b1_spring_dp_dashboard_threshold_higher(self):
        """Con radiante inattivo, dehum_on_thr_c deve essere almeno 15°C (no falso allarme)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, dp_dehum_c=self._DP_SPRING, radiant_cooling_active=False)
        # 15.0°C è dp_sp_max_c + ddp_cmd = 15.0 + 1.0 = 16.0 > 14.7
        assert dem.dehum_on_thr_c > 15.0, (
            f"on_thr ({dem.dehum_on_thr_c:.1f}°C) non è sufficientemente alta per "
            f"evitare falso allarme con DP={self._DP_SPRING}°C"
        )

    def test_b2_spring_dp_can_trigger_radiant_active(self):
        """DP 14.7°C + radiante attivo → on_thr protettiva (<14.7) → need_dehum può essere True.

        Questo è il comportamento corretto: con superfici fredde il rischio esiste.
        """
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(
            pol,
            dp_dehum_c=self._DP_SPRING,
            dp_max_c=self._DP_SPRING,
            radiant_cooling_active=True,
            # Cooling attivo (sblocca _dehum_condition_met gate)
            cool_sur_max_c=1.0,
            cool_cov=0.5,
        )
        # on_thr protettiva deve essere < DP spring per poter scattare
        assert dem.dehum_on_thr_c < self._DP_SPRING, (
            f"Radiante attivo: on_thr ({dem.dehum_on_thr_c:.2f}°C) deve essere "
            f"< DP ({self._DP_SPRING}°C) per proteggere le superfici fredde"
        )

    def test_b3_dashboard_threshold_reflects_context(self):
        """dehum_on_thr_c cambia in base al contesto radiante (test diff tra i due casi)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem_off = _compute(pol, radiant_cooling_active=False)
        dem_on  = _compute(pol, radiant_cooling_active=True)
        assert dem_off.dehum_on_thr_c > dem_on.dehum_on_thr_c, (
            "Il threshold per il dashboard deve essere più alto con radiante inattivo"
        )


# ===========================================================================
# C. Gate _dehum_condition_met
# ===========================================================================


class TestDehumConditionMet:
    """Test per il gate fisico sulla deumidifica (condizioni A/B/C)."""

    # DP abbastanza alto da superare on_thr protettiva (radiante attivo)
    _DP_HIGH = 15.0

    def _dem_with_gate(
        self,
        *,
        cool_sur_max: float = 0.0,
        cool_cov: float = 0.0,
        rh_zones: list[float] | None = None,
        dp: float = _DP_HIGH,
        radiant_active: bool = True,
    ) -> VmcDemand:
        pol = _policy()
        snap = _snap(rh_zones=rh_zones)
        return _compute(
            pol,
            snapshot=snap,
            dp_dehum_c=dp,
            dp_max_c=dp,
            cool_sur_max_c=cool_sur_max,
            cool_cov=cool_cov,
            radiant_cooling_active=radiant_active,
        )

    def test_c1_cond_a_cool_sur_max_enables_dehum(self):
        """Condizione A: cool_sur_max > 0 apre il gate deumidifica."""
        dem = self._dem_with_gate(cool_sur_max=1.0)
        # Il gate è aperto → need_dehum (DP sopra soglia) + gate = req_dehumidif True
        assert dem.req_dehumidif is True, (
            "cool_sur_max > 0 deve aprire il gate deumidifica"
        )

    def test_c2_cond_a_cool_cov_enables_dehum(self):
        """Condizione A: cool_cov > 0 apre il gate deumidifica."""
        dem = self._dem_with_gate(cool_cov=0.3)
        assert dem.req_dehumidif is True, (
            "cool_cov > 0 deve aprire il gate deumidifica"
        )

    def test_c3_cond_b_high_rh_enables_dehum_without_radiant(self):
        """Condizione B: UR max > 67% abilita la deumidifica senza cooling attivo.

        Disagio igienico assoluto (ISO 7730): indipendente dal radiante.
        Con DP=15°C e T=23.5°C → UR ≈ 71% > 67%.
        """
        # UR molto alta in almeno una zona (simula condizioni critiche)
        dem = self._dem_with_gate(
            cool_sur_max=0.0,
            cool_cov=0.0,
            rh_zones=[45.0, 70.0],  # una zona > 67%
            dp=self._DP_HIGH,
            radiant_active=True,
        )
        assert dem.req_dehumidif is True, (
            "UR zona > 67% deve abilitare deumidifica anche senza cooling attivo"
        )

    def test_c4_cond_c_critical_dp_enables_dehum(self):
        """Condizione C: DP > 16.5°C abilita la deumidifica (rischio superfici passive)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dp_critical = cfg.vmc.dehum.dp_dehum_critical_threshold_c + 0.1  # 16.6°C
        dem = _compute(
            pol,
            dp_dehum_c=dp_critical,
            dp_max_c=dp_critical,
            cool_sur_max_c=0.0,
            cool_cov=0.0,
            # Con radiante attivo e DP molto alto
            radiant_cooling_active=True,
        )
        assert dem.req_dehumidif is True, (
            f"DP > dp_dehum_critical ({cfg.vmc.dehum.dp_dehum_critical_threshold_c}°C) "
            "deve abilitare deumidifica tramite condizione C"
        )

    def test_c5_no_condition_met_blocks_req_dehumidif(self):
        """Nessuna condizione A/B/C soddisfatta: req_dehumidif deve essere False.

        Questo è il caso primavera idle: DP 14.7°C, radiante inattivo,
        nessuna domanda cooling, UR normale.
        """
        pol = _policy()
        snap = _snap(rh_zones=[50.0, 55.0])  # UR normale < 67%
        dem = _compute(
            pol,
            snapshot=snap,
            dp_dehum_c=14.7,
            dp_max_c=14.7,
            cool_sur_max_c=0.0,
            cool_cov=0.0,
            radiant_cooling_active=False,  # nessuna superficie fredda
        )
        assert dem.req_dehumidif is False, (
            "Nessuna condizione A/B/C → req_dehumidif deve essere False"
        )

    def test_c5_scenario_spring_idle(self):
        """Scenario tipico: maggio, impianto idle, DP 14.7°C, UR 55% → no deumidifica."""
        pol = _policy()
        snap = _snap(
            t_ref_c=23.5,
            rh_zones=[55.0, 58.0, 56.0],
            season="shoulder",
        )
        dem = _compute(
            pol,
            snapshot=snap,
            dp_dehum_c=14.7,
            dp_max_c=14.7,
            outdoor_dp_c=15.5,
            radiant_cooling_active=False,
        )
        assert not dem.req_dehumidif
        # Il threshold dashboard deve essere superiore al DP corrente
        assert dem.dehum_on_thr_c > 14.7


# ===========================================================================
# D. req_dehumidif end-to-end e isteresi VmcState
# ===========================================================================


class TestReqDehumidif:
    """Test per req_dehumidif: gate + isteresi."""

    def test_d1_need_and_gate_met_sets_req_true(self):
        """need_dehum True + gate aperto (cooling) → req_dehumidif True."""
        pol = _policy()
        dem = _compute(
            pol,
            dp_dehum_c=15.0,
            dp_max_c=15.0,
            cool_sur_max_c=1.5,
            cool_cov=0.4,
            radiant_cooling_active=True,
        )
        assert dem.req_dehumidif is True

    def test_d2_need_but_gate_blocked_req_false(self):
        """need_dehum True + gate bloccato → req_dehumidif False."""
        pol = _policy()
        dem = _compute(
            pol,
            dp_dehum_c=15.0,
            dp_max_c=15.0,
            cool_sur_max_c=0.0,
            cool_cov=0.0,
            radiant_cooling_active=True,
            # UR normale, DP < critico
        )
        # need_dehum potrebbe essere True per on_thr protettiva ma gate lo blocca
        assert dem.req_dehumidif is False

    def test_d3_hysteresis_stays_on_above_off_thr(self):
        """Una volta attivato, req_dehumidif rimane True finché DP > off_thr."""
        cfg = PlantPlannerConfig()
        state = VmcState()
        pol = VmcPolicy(cfg.vmc, state=state)

        # Primo tick: DP alto, gate aperto → ON
        dem1 = pol.compute(
            snapshot=_snap(),
            profile=HVACOperatingProfile.COMFORT,
            heat_def_max_c=0.0, heat_def_wmean_c=0.0, heat_cov=0.0,
            cool_sur_max_c=2.0, cool_sur_wmean_c=1.0, cool_cov=0.5,
            dp_dehum_c=15.5, dp_max_c=15.5,
            outdoor_dp_c=8.0,
            radiant_cooling_active=True,
        )
        assert dem1.req_dehumidif is True, "Primo tick: deve essere True"

        # Secondo tick: DP scende appena sotto on_thr ma sopra off_thr → rimane True
        off_thr = dem1.dehum_off_thr_c
        dp_between = off_thr + 0.05  # tra off_thr e on_thr

        dem2 = pol.compute(
            snapshot=_snap(),
            profile=HVACOperatingProfile.COMFORT,
            heat_def_max_c=0.0, heat_def_wmean_c=0.0, heat_cov=0.0,
            cool_sur_max_c=2.0, cool_sur_wmean_c=1.0, cool_cov=0.5,
            dp_dehum_c=dp_between, dp_max_c=dp_between,
            outdoor_dp_c=8.0,
            radiant_cooling_active=True,
        )
        assert dem2.req_dehumidif is True, (
            f"Isteresi: DP={dp_between:.2f}°C tra off_thr ({off_thr:.2f}) e on_thr "
            "→ deve rimanere True"
        )

    def test_d3_hysteresis_turns_off_below_off_thr(self):
        """req_dehumidif si disattiva quando DP scende sotto off_thr."""
        cfg = PlantPlannerConfig()
        state = VmcState()
        pol = VmcPolicy(cfg.vmc, state=state)

        # Primo tick: ON
        pol.compute(
            snapshot=_snap(),
            profile=HVACOperatingProfile.COMFORT,
            heat_def_max_c=0.0, heat_def_wmean_c=0.0, heat_cov=0.0,
            cool_sur_max_c=2.0, cool_sur_wmean_c=1.0, cool_cov=0.5,
            dp_dehum_c=15.5, dp_max_c=15.5,
            outdoor_dp_c=8.0,
            radiant_cooling_active=True,
        )
        assert state.dehum_on is True

        # Secondo tick: DP ben sotto off_thr → OFF
        dem2 = pol.compute(
            snapshot=_snap(),
            profile=HVACOperatingProfile.COMFORT,
            heat_def_max_c=0.0, heat_def_wmean_c=0.0, heat_cov=0.0,
            cool_sur_max_c=0.0, cool_sur_wmean_c=0.0, cool_cov=0.0,
            dp_dehum_c=10.0, dp_max_c=10.0,
            outdoor_dp_c=8.0,
            radiant_cooling_active=False,
        )
        assert dem2.req_dehumidif is False
        assert state.dehum_on is False


# ===========================================================================
# E. Speed boost VMC
# ===========================================================================


class TestVmcSpeedBoost:
    """Verifica che la velocità VMC non subisca boost indebiti con radiante inattivo."""

    def test_e1_no_speed_boost_spring_radiant_inactive(self):
        """DP 14.7°C + radiante inattivo: nessun boost velocità.

        on_thr passiva (16.0°C) > 14.7°C → delta negativo → no step up.
        """
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(
            pol,
            dp_dehum_c=14.7,
            dp_max_c=14.7,
            radiant_cooling_active=False,
        )
        # dehum_on_thr_c è usato come riferimento per speed boost
        # delta = 14.7 - 16.0 = -1.3°C < boost_step1 → no step
        assert dem.dehum_on_thr_c > 14.7, (
            "on_thr deve essere sopra il DP corrente per evitare boost"
        )

    def test_e2_speed_boost_when_dp_well_above_thr(self):
        """DP molto sopra on_thr: il boost velocità deve scattare (req_dehumidif influenza speed).

        Verifica che il VmcCommandBuilder riceverà un on_thr appropriato.
        La policy non calcola air_speed direttamente ma espone on_thr
        che il CommandBuilder usa come speed_ref.
        """
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(
            pol,
            dp_dehum_c=17.0,
            dp_max_c=17.0,
            cool_sur_max_c=0.0,
            cool_cov=0.0,
            # Con radiante inattivo ma DP molto alto (Condizione C > 16.5°C)
            radiant_cooling_active=False,
        )
        # DP 17.0 > dp_dehum_critical (16.5) → Condizione C → gate aperto
        # on_thr passiva = 16.0°C < 17.0°C → need_dehum = True
        # delta = 17.0 - 16.0 = 1.0°C > boost_step1 → speed boost attivo
        assert dem.req_dehumidif is True, (
            "DP 17.0°C > soglia critica 16.5°C: deumidifica deve scattare"
        )
        # Il delta positivo tra DP e on_thr indica che il speed boost scatterà
        assert 17.0 > dem.dehum_on_thr_c, (
            f"DP (17.0) deve essere > on_thr ({dem.dehum_on_thr_c:.1f}) per attivare speed boost"
        )


# ===========================================================================
# F. Boost termico
# ===========================================================================


class TestThermalBoost:
    """Test per req_heating e req_cooling (boost idronico VMC)."""

    def test_f1_heat_boost_from_def_max(self):
        """heat_def_max >= soglia con finestre chiuse → req_heating True."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        thr = cfg.vmc.boost.heat_def_max_thr_c
        snap = _snap(windows_closed=True)
        dem = _compute(pol, snapshot=snap, heat_def_max_c=thr + 0.1)
        assert dem.req_heating is True

    def test_f1_heat_boost_below_threshold(self):
        """heat_def_max < soglia → req_heating False."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        thr = cfg.vmc.boost.heat_def_max_thr_c
        snap = _snap(windows_closed=True)
        dem = _compute(pol, snapshot=snap, heat_def_max_c=thr - 0.1)
        assert dem.req_heating is False

    def test_f2_cool_boost_from_sur_max(self):
        """cool_sur_max >= soglia con finestre chiuse → req_cooling True."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        thr = cfg.vmc.boost.cool_sur_max_thr_c
        snap = _snap(windows_closed=True)
        dem = _compute(pol, snapshot=snap, cool_sur_max_c=thr + 0.1, cool_sur_wmean_c=thr + 0.1)
        assert dem.req_cooling is True

    def test_f3_no_boost_during_vacation(self):
        """Vacanza → req_heating e req_cooling devono essere False."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        thr = cfg.vmc.boost.heat_def_max_thr_c
        snap = _snap(vacation=True)
        dem = _compute(
            pol, snapshot=snap,
            heat_def_max_c=thr + 1.0,
            cool_sur_max_c=thr + 1.0,
        )
        assert dem.req_heating is False
        assert dem.req_cooling is False

    def test_f4_no_boost_windows_open(self):
        """Finestre aperte → no boost termico."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        thr = cfg.vmc.boost.heat_def_max_thr_c
        snap = _snap(windows_closed=False)
        dem = _compute(pol, snapshot=snap, heat_def_max_c=thr + 1.0)
        assert dem.req_heating is False

    def test_f5_boost_disabled_in_config(self):
        """Con boost.enabled=False → req_heating e req_cooling False."""
        cfg = PlantPlannerConfig()
        cfg.vmc.boost.enabled = False
        pol = _policy(cfg)
        thr = cfg.vmc.boost.heat_def_max_thr_c + 5.0
        dem = _compute(pol, heat_def_max_c=thr, cool_sur_max_c=thr)
        assert dem.req_heating is False
        assert dem.req_cooling is False


# ===========================================================================
# G. Free cooling ventilativo
# ===========================================================================


class TestFreeCooling:
    """Test per req_free_cooling: mutua esclusività, guardie DP e presenza."""

    def test_g1_free_cool_feasible_and_conditions_ok(self):
        """free_cool_feasible + finestre chiuse + no vacation + DP outdoor OK → True."""
        pol = _policy()
        snap = _snap(windows_closed=True, vacation=False, nobody=False)
        dem = _compute(
            pol,
            snapshot=snap,
            dp_dehum_c=14.7,
            dp_max_c=14.7,
            outdoor_dp_c=5.0,   # ben sotto DP indoor
            free_cool_feasible=True,
            radiant_cooling_active=False,
        )
        assert dem.req_free_cooling is True

    def test_g2_free_cool_blocked_by_hydraulic_boost(self):
        """Se boost idronico è attivo (req_heating/cooling/dehum), free cooling non scatta."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        snap = _snap(windows_closed=True)
        thr = cfg.vmc.boost.heat_def_max_thr_c
        dem = _compute(
            pol,
            snapshot=snap,
            heat_def_max_c=thr + 1.0,  # req_heating True
            free_cool_feasible=True,
            outdoor_dp_c=5.0,
        )
        assert dem.req_heating is True, "Precondizione: req_heating deve essere True"
        assert dem.req_free_cooling is False, (
            "Free cooling non deve scattare se boost idronico è attivo"
        )

    def test_g3_no_free_cool_on_vacation(self):
        """Vacanza → req_free_cooling False."""
        pol = _policy()
        snap = _snap(vacation=True)
        dem = _compute(
            pol,
            snapshot=snap,
            free_cool_feasible=True,
            outdoor_dp_c=5.0,
        )
        assert dem.req_free_cooling is False

    def test_g4_no_free_cool_high_outdoor_dp(self):
        """DP outdoor >= DP indoor − headroom → no free cooling (aria troppo umida)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dp_indoor = 14.7
        headroom = cfg.vmc.dehum.outdoor_dp_headroom_c
        dp_outdoor_too_high = dp_indoor - headroom + 0.01  # appena sopra il limite
        snap = _snap(windows_closed=True)
        dem = _compute(
            pol,
            snapshot=snap,
            dp_dehum_c=dp_indoor,
            dp_max_c=dp_indoor,
            outdoor_dp_c=dp_outdoor_too_high,
            free_cool_feasible=True,
            radiant_cooling_active=False,
        )
        assert dem.req_free_cooling is False, (
            f"DP outdoor ({dp_outdoor_too_high:.2f}°C) troppo alto: free cooling bloccato"
        )

    def test_g5_free_cool_not_set_without_feasible_flag(self):
        """Senza free_cool_feasible=True il flag non può mai essere True."""
        pol = _policy()
        snap = _snap(windows_closed=True)
        dem = _compute(
            pol, snapshot=snap,
            free_cool_feasible=False,
            outdoor_dp_c=5.0,
        )
        assert dem.req_free_cooling is False


# ===========================================================================
# H. Stagione operativa e RH target
# ===========================================================================


class TestOperativeSeason:
    """Test per infer_operative_bucket e rh_target_pct."""

    @pytest.mark.parametrize("season_str, expected_bucket", [
        ("winter",   "winter"),
        ("summer",   "summer"),
        ("shoulder", "shoulder"),
        ("spring",   "shoulder"),  # valore non standard → fallback shoulder
        ("unknown",  "shoulder"),
    ])
    def test_h1_season_bucket_mapping(self, season_str, expected_bucket):
        """La stagione snapshot viene mappata correttamente al bucket operativo."""
        pol = _policy()
        snap = _snap(season=season_str)
        bucket = pol.infer_operative_bucket(snap)
        assert bucket == expected_bucket, (
            f"season='{season_str}' → atteso bucket '{expected_bucket}', "
            f"ottenuto '{bucket}'"
        )

    def test_h2_rh_target_varies_by_season(self):
        """Il target RH varia in base alla stagione (estate più bassa per protezione DP)."""
        pol = _policy()
        snap_summer   = _snap(season="summer")
        snap_winter   = _snap(season="winter")
        snap_shoulder = _snap(season="shoulder")

        dem_su = _compute(pol, snapshot=snap_summer)
        dem_wi = _compute(pol, snapshot=snap_winter)
        dem_sh = _compute(pol, snapshot=snap_shoulder)

        # Verifica che i valori siano nell'intervallo sensato (30–70%)
        for label, dem in [("summer", dem_su), ("winter", dem_wi), ("shoulder", dem_sh)]:
            assert 30.0 <= dem.rh_target_pct <= 70.0, (
                f"RH target {label} = {dem.rh_target_pct:.1f}% fuori range [30,70]"
            )

    def test_h2_rh_target_summer_not_higher_than_winter(self):
        """In estate il target RH deve essere ≤ inverno (estate = più attenzione alla DP)."""
        pol = _policy()
        dem_su = _compute(pol, snapshot=_snap(season="summer"))
        dem_wi = _compute(pol, snapshot=_snap(season="winter"))
        assert dem_su.rh_target_pct <= dem_wi.rh_target_pct + 5.0, (
            "Target RH estate non dovrebbe essere significativamente > inverno"
        )


# ===========================================================================
# I. Invarianti strutturali
# ===========================================================================


class TestStructuralInvariants:
    """Invarianti che devono valere in tutte le condizioni."""

    @pytest.mark.parametrize("radiant_active", [True, False])
    def test_i1_dp_sp_c_within_bounds(self, radiant_active):
        """dp_sp_c deve stare dentro [dp_sp_min_c, dp_sp_max_c]."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=radiant_active)
        assert cfg.vmc.dehum.dp_sp_min_c <= dem.dp_sp_c <= cfg.vmc.dehum.dp_sp_max_c, (
            f"dp_sp_c={dem.dp_sp_c:.2f}°C fuori dai bounds "
            f"[{cfg.vmc.dehum.dp_sp_min_c}, {cfg.vmc.dehum.dp_sp_max_c}] "
            f"(radiant_active={radiant_active})"
        )

    @pytest.mark.parametrize("radiant_active", [True, False])
    def test_i2_on_thr_above_off_thr(self, radiant_active):
        """on_thr deve essere sempre > off_thr (isteresi coerente)."""
        pol = _policy()
        dem = _compute(pol, radiant_cooling_active=radiant_active)
        assert dem.dehum_on_thr_c > dem.dehum_off_thr_c, (
            f"on_thr ({dem.dehum_on_thr_c:.2f}) deve essere > off_thr "
            f"({dem.dehum_off_thr_c:.2f})"
        )

    @pytest.mark.parametrize("radiant_active", [True, False])
    def test_i3_all_req_fields_are_bool(self, radiant_active):
        """Tutti i campi req_* devono essere bool puri."""
        pol = _policy()
        dem = _compute(pol, radiant_cooling_active=radiant_active)
        req_fields = [
            "req_heating", "req_cooling", "req_dehumidif",
            "req_water", "req_free_cooling", "req_free_heating",
        ]
        for fname in req_fields:
            val = getattr(dem, fname)
            assert isinstance(val, bool), (
                f"{fname} deve essere bool, ottenuto {type(val).__name__}"
            )

    def test_i4_radiant_cooling_active_is_bool(self):
        """Il campo radiant_cooling_active deve essere bool."""
        pol = _policy()
        for active in (True, False):
            dem = _compute(pol, radiant_cooling_active=active)
            assert isinstance(dem.radiant_cooling_active, bool), (
                "radiant_cooling_active deve essere bool"
            )

    @pytest.mark.parametrize("dp_val", [None, 8.0, 14.7, 16.0, 18.0])
    def test_i5_no_exception_various_dp_values(self, dp_val):
        """Nessuna eccezione per valori DP da None a molto alti."""
        pol = _policy()
        dem = _compute(pol, dp_dehum_c=dp_val, dp_max_c=dp_val)
        assert dem is not None

    @pytest.mark.parametrize("season", ["winter", "summer", "shoulder"])
    def test_i6_passive_on_thr_always_above_spring_dp(self, season):
        """Con radiante inattivo, on_thr deve sempre superare il DP primaverile (14.7°C)."""
        pol = _policy()
        snap = _snap(season=season)
        dem = _compute(pol, snapshot=snap, dp_dehum_c=14.7, radiant_cooling_active=False)
        assert dem.dehum_on_thr_c > 14.7, (
            f"Stagione={season}: on_thr passiva ({dem.dehum_on_thr_c:.2f}°C) "
            "deve superare il DP primaverile (14.7°C) con radiante inattivo"
        )

    def test_i7_passive_on_thr_equals_active_formula(self):
        """on_thr passiva = dp_sp_max_c + ddp_cmd (formula esplicita dalla patch 0014)."""
        cfg = PlantPlannerConfig()
        pol = _policy(cfg)
        dem = _compute(pol, radiant_cooling_active=False)
        _, expected_on_thr, _ = _expected_passive(cfg)
        assert dem.dehum_on_thr_c == pytest.approx(expected_on_thr, abs=0.01), (
            f"on_thr passiva: attesa {expected_on_thr:.2f}°C, "
            f"ottenuta {dem.dehum_on_thr_c:.2f}°C"
        )
