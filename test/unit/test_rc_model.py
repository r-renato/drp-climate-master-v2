"""Test unitari per RcZoneParams e RcZoneModel.

Copertura
---------
A. RcZoneParams - costruzione, invarianti fisici, campi
B. RcZoneModel.simulate (heating) - comportamento originale invariato
C. RcZoneModel.simulate (cooling) - direzione opposta, stessa struttura
D. Proprietà fisiche comuni - stabilità, punto fisso, output length
E. Retrocompatibilità - chiamate senza parametro cooling
F. Casi limite - valvola sempre off, t0 = T_out, u vuota

Nessuna dipendenza da homeassistant.
"""

from __future__ import annotations

import math
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from custom_components.drp_climate_master_v2.plant.decision.comfort_band.mpc.rc_model import (
    RcZoneModel,
)
from custom_components.drp_climate_master_v2.plant.decision.zone.config import (
    RcZoneParams,
)

# ---------------------------------------------------------------------------
# Costanti di riferimento (fisica Eurotherm Leonardo 3.5 via Renato)
# ---------------------------------------------------------------------------
Q_HEAT_W_M2 = 69.0  # W/m² riscaldamento
Q_COOL_W_M2 = 52.0  # W/m² raffrescamento
K_HEAT_DEFAULT = 0.8
K_COOL_DEFAULT = -0.603  # = -0.8 x (52/69), arrotondato a 3 cifre
TAU_DEFAULT = 6.0
DT_MINUTES = 10
HORIZON = 12


# ---------------------------------------------------------------------------
# A. RcZoneParams - costruzione e invarianti
# ---------------------------------------------------------------------------


class TestRcZoneParamsDefaults:
    """Verifica campi default di RcZoneParams."""

    def test_tau_h_default(self):
        p = RcZoneParams()
        assert p.tau_h == pytest.approx(6.0)

    def test_k_heat_default(self):
        p = RcZoneParams()
        assert p.k_c_per_h == pytest.approx(K_HEAT_DEFAULT)

    def test_k_cool_default_negative(self):
        p = RcZoneParams()
        assert p.k_cool_c_per_h < 0.0

    def test_k_cool_default_physical_ratio(self):
        """k_cool / k_heat ~= -(52/69) entro 1%."""
        p = RcZoneParams()
        expected_ratio = -(Q_COOL_W_M2 / Q_HEAT_W_M2)
        actual_ratio = p.k_cool_c_per_h / p.k_c_per_h
        assert actual_ratio == pytest.approx(expected_ratio, rel=0.01)

    def test_custom_params_accepted(self):
        p = RcZoneParams(tau_h=4.0, k_c_per_h=1.2, k_cool_c_per_h=-0.9)
        assert p.tau_h == pytest.approx(4.0)
        assert p.k_c_per_h == pytest.approx(1.2)
        assert p.k_cool_c_per_h == pytest.approx(-0.9)


class TestRcZoneParamsInvariants:
    """Verifica che __post_init__ rifiuti parametri fisicamente errati."""

    def test_tau_zero_raises(self):
        with pytest.raises(ValueError, match="tau_h"):
            RcZoneParams(tau_h=0.0)

    def test_tau_negative_raises(self):
        with pytest.raises(ValueError, match="tau_h"):
            RcZoneParams(tau_h=-1.0)

    def test_k_heat_zero_raises(self):
        with pytest.raises(ValueError, match="k_c_per_h"):
            RcZoneParams(k_c_per_h=0.0)

    def test_k_heat_negative_raises(self):
        """Un k_heat negativo significherebbe che aprire la valvola raffredda."""
        with pytest.raises(ValueError, match="k_c_per_h"):
            RcZoneParams(k_c_per_h=-0.5)

    def test_k_cool_zero_raises(self):
        with pytest.raises(ValueError, match="k_cool_c_per_h"):
            RcZoneParams(k_cool_c_per_h=0.0)

    def test_k_cool_positive_raises(self):
        """Un k_cool positivo significherebbe che aprire la valvola scalda."""
        with pytest.raises(ValueError, match="k_cool_c_per_h"):
            RcZoneParams(k_cool_c_per_h=+0.5)


# ---------------------------------------------------------------------------
# B. simulate - heating (comportamento originale, retrocompatibilità)
# ---------------------------------------------------------------------------


class TestSimulateHeating:
    """Verifica il comportamento heating di simulate().

    Questi test devono passare identici alla versione pre-patch:
    il parametro cooling=False (default) non cambia il comportamento.
    """

    def test_heating_raises_temperature(self):
        """Con valvola aperta in riscaldamento, T deve salire rispetto a T0."""
        model = RcZoneModel(params=RcZoneParams())
        t_out = [5.0] * HORIZON  # esterno freddo
        u = [1] * HORIZON
        temps = model.simulate(t0_c=22.0, t_out_c=t_out, u=u, dt_minutes=DT_MINUTES)
        temps_off = model.simulate(
            t0_c=22.0, t_out_c=t_out, u=[0] * HORIZON, dt_minutes=DT_MINUTES
        )
        for t_on, t_off in zip(temps, temps_off):
            assert t_on >= t_off, "Con heating ON la T deve essere >= OFF"

    def test_heating_convergence_to_fixed_point(self):
        """Con u=1 costante, T converge a T_eq = T_out + k_heat * tau_h."""
        p = RcZoneParams(tau_h=1.0, k_c_per_h=2.0)
        model = RcZoneModel(params=p)
        t_out_val = 10.0
        t_eq_expected = t_out_val + p.k_c_per_h * p.tau_h
        t_out = [t_out_val] * 60
        u = [1] * 60
        temps = model.simulate(t0_c=0.0, t_out_c=t_out, u=u, dt_minutes=60)
        assert temps[-1] == pytest.approx(t_eq_expected, abs=0.1)

    def test_heating_explicit_backward_compat(self):
        """cooling=False esplicito produce lo stesso risultato del default."""
        model = RcZoneModel(params=RcZoneParams())
        t_out = [5.0] * HORIZON
        u = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
        temps_default = model.simulate(
            t0_c=20.0, t_out_c=t_out, u=u, dt_minutes=DT_MINUTES
        )
        temps_explicit = model.simulate(
            t0_c=20.0,
            t_out_c=t_out,
            u=u,
            dt_minutes=DT_MINUTES,
            cooling=False,
        )
        assert temps_default == pytest.approx(temps_explicit)

    def test_heating_output_length(self):
        model = RcZoneModel(params=RcZoneParams())
        temps = model.simulate(
            t0_c=20.0,
            t_out_c=[5.0] * HORIZON,
            u=[1] * HORIZON,
            dt_minutes=DT_MINUTES,
        )
        assert len(temps) == HORIZON

    def test_heating_all_finite(self):
        model = RcZoneModel(params=RcZoneParams())
        temps = model.simulate(
            t0_c=20.0,
            t_out_c=[5.0] * HORIZON,
            u=[1] * HORIZON,
            dt_minutes=DT_MINUTES,
        )
        assert all(math.isfinite(t) for t in temps)


# ---------------------------------------------------------------------------
# C. simulate - cooling (nuovo comportamento)
# ---------------------------------------------------------------------------


class TestSimulateCooling:
    """Verifica il comportamento cooling di simulate(cooling=True)."""

    def test_cooling_lowers_temperature(self):
        """Con valvola aperta in raffrescamento, T deve essere inferiore a u=0."""
        model = RcZoneModel(params=RcZoneParams())
        t_out = [21.0] * HORIZON
        u_on = [1] * HORIZON
        u_off = [0] * HORIZON
        temps_on = model.simulate(
            t0_c=25.7,
            t_out_c=t_out,
            u=u_on,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        temps_off = model.simulate(
            t0_c=25.7,
            t_out_c=t_out,
            u=u_off,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        for t_on, t_off in zip(temps_on, temps_off):
            assert t_on <= t_off, (
                f"Con cooling ON T={t_on:.3f} deve essere <= OFF T={t_off:.3f}"
            )

    def test_cooling_decreases_from_t0(self):
        """Con zona calda e valvola cooling sempre aperta, T deve scendere da t0."""
        model = RcZoneModel(params=RcZoneParams())
        t0 = 25.7
        t_out = [21.0] * HORIZON
        temps = model.simulate(
            t0_c=t0,
            t_out_c=t_out,
            u=[1] * HORIZON,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        assert all(t < t0 for t in temps), "T deve scendere sotto t0 con cooling ON"

    def test_cooling_step_magnitude_vs_heating(self):
        """Il delta cooling deve essere ~= (52/69) x delta heating."""
        p = RcZoneParams()
        model = RcZoneModel(params=p)
        t0 = 24.0
        t_out = [t0] * 1
        dt_min = 10

        t_heat = model.simulate(
            t0_c=t0, t_out_c=t_out, u=[1], dt_minutes=dt_min, cooling=False
        )[0]
        t_cool = model.simulate(
            t0_c=t0, t_out_c=t_out, u=[1], dt_minutes=dt_min, cooling=True
        )[0]

        delta_heat = t_heat - t0
        delta_cool = t_cool - t0

        expected_ratio = -(Q_COOL_W_M2 / Q_HEAT_W_M2)
        actual_ratio = delta_cool / delta_heat
        assert actual_ratio == pytest.approx(expected_ratio, rel=0.01)

    def test_cooling_convergence_to_fixed_point(self):
        """Con u=1 costante in cooling, T converge a T_eq = T_out + k_cool * tau_h."""
        p = RcZoneParams(tau_h=1.0, k_cool_c_per_h=-2.0)
        model = RcZoneModel(params=p)
        t_out_val = 21.0
        t_eq_expected = t_out_val + p.k_cool_c_per_h * p.tau_h
        t_out = [t_out_val] * 60
        u = [1] * 60
        temps = model.simulate(
            t0_c=30.0,
            t_out_c=t_out,
            u=u,
            dt_minutes=60,
            cooling=True,
        )
        assert temps[-1] == pytest.approx(t_eq_expected, abs=0.1)

    def test_cooling_output_length(self):
        model = RcZoneModel(params=RcZoneParams())
        temps = model.simulate(
            t0_c=25.7,
            t_out_c=[21.0] * HORIZON,
            u=[1] * HORIZON,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        assert len(temps) == HORIZON

    def test_cooling_all_finite(self):
        model = RcZoneModel(params=RcZoneParams())
        temps = model.simulate(
            t0_c=25.7,
            t_out_c=[21.0] * HORIZON,
            u=[1] * HORIZON,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        assert all(math.isfinite(t) for t in temps)

    def test_cooling_living_room_scenario(self):
        """Scenario reale: living 25.7°C, T_out=21°C, banda [22.15, 24.68]."""
        model = RcZoneModel(params=RcZoneParams())
        t_out = [21.0] * HORIZON
        u = [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        t_max = 24.68
        temps = model.simulate(
            t0_c=25.7,
            t_out_c=t_out,
            u=u,
            dt_minutes=DT_MINUTES,
            cooling=True,
        )
        assert all(t <= t_max for t in temps[6:]), (
            f"La zona deve rientrare in banda dal passo 6: {temps[6:]}"
        )


# ---------------------------------------------------------------------------
# D. Proprietà fisiche comuni
# ---------------------------------------------------------------------------


class TestPhysicalProperties:
    """Verifica proprietà indipendenti dalla direzione."""

    def test_valve_off_converges_to_t_out(self):
        """Con u=0 la zona converge a T_out per entrambe le modalità."""
        model = RcZoneModel(params=RcZoneParams(tau_h=1.0))
        for cooling in (False, True):
            temps = model.simulate(
                t0_c=25.0,
                t_out_c=[21.0] * 60,
                u=[0] * 60,
                dt_minutes=60,
                cooling=cooling,
            )
            assert temps[-1] == pytest.approx(21.0, abs=0.2), (
                f"cooling={cooling}: T_finale={temps[-1]:.3f} deve -> T_out=21.0"
            )

    def test_t0_equals_t_out_no_valve_stable(self):
        """Se T0 = T_out e u=0, T rimane costante."""
        model = RcZoneModel(params=RcZoneParams())
        for cooling in (False, True):
            temps = model.simulate(
                t0_c=24.0,
                t_out_c=[24.0] * HORIZON,
                u=[0] * HORIZON,
                dt_minutes=DT_MINUTES,
                cooling=cooling,
            )
            assert all(t == pytest.approx(24.0) for t in temps)

    def test_euler_stability_default_params(self):
        """dt_h / tau_h deve essere << 1."""
        p = RcZoneParams()
        dt_h = DT_MINUTES / 60.0
        ratio = dt_h / p.tau_h
        assert ratio < 0.1, f"dt_h/tau_h={ratio:.4f} deve essere < 0.1"

    def test_empty_sequence_returns_empty(self):
        model = RcZoneModel(params=RcZoneParams())
        for cooling in (False, True):
            result = model.simulate(
                t0_c=24.0,
                t_out_c=[],
                u=[],
                dt_minutes=DT_MINUTES,
                cooling=cooling,
            )
            assert result == []


# ---------------------------------------------------------------------------
# E. Retrocompatibilità - chiamata senza cooling (vecchio codice)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Verifica che il codice esistente (senza cooling) sia invariato."""

    def test_no_cooling_param_equals_heating(self):
        """Il chiamante in planner.py non passa cooling: deve comportarsi come cooling=False."""
        model = RcZoneModel(params=RcZoneParams())
        t_out = [5.0] * HORIZON
        u = [1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 1]

        temps_old = model.simulate(
            t0_c=21.0, t_out_c=t_out, u=u, dt_minutes=DT_MINUTES
        )
        temps_heat = model.simulate(
            t0_c=21.0,
            t_out_c=t_out,
            u=u,
            dt_minutes=DT_MINUTES,
            cooling=False,
        )

        assert temps_old == pytest.approx(temps_heat)

    def test_result_unchanged_vs_pre_patch_values(self):
        """Valori numerici attesi pre-patch: heating step 1."""
        model = RcZoneModel(params=RcZoneParams())
        temps = model.simulate(
            t0_c=22.0, t_out_c=[5.0], u=[1], dt_minutes=DT_MINUTES
        )
        assert temps[0] == pytest.approx(21.661, abs=0.001)
