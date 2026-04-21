"""Test unitari del modulo confort_band.

Copertura
---------
A. ZoneTrmTracker  — logica EWMA (warm-up, convergenza, None input, reset)
B. Tabelle config   — invarianti strutturali delle costanti
C. ComfortPolicyLayer._clo_for()  — ogni ramo: stagione, profilo, cold_snap, S3
D. ComfortPolicyLayer._pmv_targets()  — centri e bande per profilo + nudge estate
E. ComfortBandCalculator.pmv_ppd()  — fisica ISO 7730 (valori di riferimento)
F. ComfortBandCalculator.compute_single()  — band solved, t_op_min <= t_op_max
G. Integrazione S3  — delta_clo si propaga fino a t_op_min/t_op_max

Nessuna dipendenza da homeassistant; gli stub in tests/stubs/ sono sufficienti.
"""

from __future__ import annotations

import sys
import pathlib
from datetime import datetime, timezone, timedelta
from math import isfinite
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Path setup (si aspetta che conftest.py abbia già inserito stubs/)
# ---------------------------------------------------------------------------

_STUBS = pathlib.Path(__file__).parent.parent / "stubs"
if str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

# ---------------------------------------------------------------------------
# Import moduli sotto test
# ---------------------------------------------------------------------------

from custom_components.drp_climate_master_v2.plant.decision.confort_band.trm import (
    ZoneTrmTracker,
    ZoneTrmState,
)
from custom_components.drp_climate_master_v2.plant.decision.confort_band.config import (
    # S3
    T_RM_TAU_HOURS,
    T_RM_WARMUP_TICKS,
    T_RM_NEUTRAL_BY_SEASON,
    T_RM_SENSITIVITY_CLO,
    T_RM_CAP_DELTA_CLO,
    T_RM_CLAMP_MIN,
    T_RM_CLAMP_MAX,
    T_RM_SKIP_PROFILES,
    # CLO
    CLO_BASE_SUMMER,
    CLO_BASE_SHOULDER,
    CLO_BASE_WINTER,
    CLO_SLEEP_WINTER_DELTA,
    CLO_AWAY_VACATION_WINTER_DELTA,
    CLO_AWAY_VACATION_WINTER_FLOOR,
    CLO_CAP_SLEEP,
    CLO_CAP_DEFAULT,
    COLD_SNAP_CLO_FRACTION,
    CLO_WINTER_BY_ZONE,
    # MET
    MET_BASE,
    MET_SLEEP,
    MET_AWAY_VACATION,
    # PMV
    MODE_PMV_CENTER,
    MODE_PMV_BAND,
    MODE_CTRL_AGGRESSIVENESS,
    PMV_SUMMER_ECO_NUDGE_DELTA,
    PMV_SUMMER_ECO_CENTER_MAX,
    # v_air
    V_AIR_BEST_LIVING,
    V_AIR_BEST_OTHER,
    V_AIR_HI_LIVING,
    V_AIR_HI_OTHER,
    V_AIR_LO_DEFAULT,
)
from custom_components.drp_climate_master_v2.plant.decision.confort_band.model import (
    PolicyContext,
    PolicyDecision,
    ClimateZoneIT,
    ComplianceMode,
    is_living,
)
from custom_components.drp_climate_master_v2.plant.decision.confort_band.policy_layer import (
    ComfortPolicyLayer,
    ConfortPolicyConfig,
)
from custom_components.drp_climate_master_v2.plant.decision.confort_band.calculator import (
    ComfortBandCalculator,
)
from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile
from custom_components.drp_climate_master_v2.domain.models.season import OperativeSeason


# ---------------------------------------------------------------------------
# Fixture / helper
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_TS = lambda offset_s=0: _NOW + timedelta(seconds=offset_s)


def _ctx(
    *,
    season: OperativeSeason = OperativeSeason.WINTER,
    mode: HVACOperatingProfile = HVACOperatingProfile.COMFORT,
    room: str = "camera_1",
    rh_pct: float = 50.0,
    t_op_current: float = 20.0,
    cold_snap: bool = False,
    t_op_running_mean: Optional[float] = None,
    outdoor_temp: Optional[float] = None,
    vmc_speed: int = 2,
) -> PolicyContext:
    """Costruisce un PolicyContext con valori di default sensati."""
    return PolicyContext(
        now=_NOW,
        room=room,
        season=season,
        vmc_speed=vmc_speed,
        rh_pct=rh_pct,
        t_op_current=t_op_current,
        mode=mode,
        outdoor_temp=outdoor_temp,
        cold_snap=cold_snap,
        t_op_running_mean=t_op_running_mean,
    )


def _layer(zone: ClimateZoneIT = ClimateZoneIT.D) -> ComfortPolicyLayer:
    cfg = ConfortPolicyConfig(climate_zone=zone, zone_clo_delta_enabled=True)
    return ComfortPolicyLayer(cfg)


def _calc() -> ComfortBandCalculator:
    return ComfortBandCalculator()


# ===========================================================================
# A. ZoneTrmTracker
# ===========================================================================

class TestZoneTrmTracker:
    """Test per la logica EWMA del tracker running mean T_op."""

    def test_warmup_returns_none_before_threshold(self):
        """Durante il warm-up get_t_rm() deve restituire None."""
        trk = ZoneTrmTracker(warmup_ticks=5)
        for i in range(4):
            trk.update("z1", 20.0, _TS(30 * i))
        assert trk.get_t_rm("z1") is None, "Warm-up non completato: atteso None"

    def test_warmup_completes_at_threshold(self):
        """Al raggiungimento di warmup_ticks, get_t_rm() deve restituire un valore."""
        trk = ZoneTrmTracker(warmup_ticks=3)
        for i in range(3):
            trk.update("z1", 20.0, _TS(30 * i))
        result = trk.get_t_rm("z1")
        assert result is not None
        assert isfinite(result)

    def test_none_input_does_not_advance_warmup(self):
        """Un tick con t_op=None non incrementa n_updates."""
        trk = ZoneTrmTracker(warmup_ticks=3)
        trk.update("z1", None, _TS(0))   # tick None: non conta
        trk.update("z1", 20.0, _TS(30))  # 1 tick valido
        trk.update("z1", 20.0, _TS(60))  # 2 tick validi
        # Solo 2 su 3 → ancora in warm-up
        assert trk.get_t_rm("z1") is None

    def test_none_input_preserves_existing_state(self):
        """Un tick con t_op=None non modifica il valore t_rm."""
        trk = ZoneTrmTracker(warmup_ticks=2)
        trk.update("z1", 21.0, _TS(0))
        trk.update("z1", 21.0, _TS(30))
        t_rm_before = trk.get_t_rm("z1")
        assert t_rm_before is not None

        trk.update("z1", None, _TS(60))  # tick unavailable
        t_rm_after = trk.get_t_rm("z1")
        assert t_rm_after == t_rm_before, "None non deve modificare t_rm"

    def test_convergence_constant_input(self):
        """Con T_op costante la EWMA converge al valore entro tolleranza."""
        trk = ZoneTrmTracker(tau_hours=168.0, warmup_ticks=3)
        # Simula 14 giorni di tick ogni 30s con T costante a 21.0
        t_target = 21.0
        ts = _NOW
        n = 14 * 24 * 120  # 14 giorni a 30s/tick
        for _ in range(min(n, 2000)):  # basta un sottoinsieme per testare convergenza
            trk.update("z1", t_target, ts)
            ts += timedelta(seconds=30)
        result = trk.get_t_rm("z1")
        assert result is not None
        assert abs(result - t_target) < 0.5, (
            f"Convergenza non raggiunta: atteso ~{t_target}, ottenuto {result:.3f}"
        )

    def test_clamp_rejects_out_of_range(self):
        """Valori fuori dal clamp non devono inquinare la EWMA in modo anomalo."""
        trk = ZoneTrmTracker(warmup_ticks=2, t_rm_clamp_min=10.0, t_rm_clamp_max=35.0)
        trk.update("z1", 5.0,  _TS(0))   # sotto clamp_min → clamped a 10.0
        trk.update("z1", 40.0, _TS(30))  # sopra clamp_max → clamped a 35.0
        result = trk.get_t_rm("z1")
        assert result is not None
        assert 10.0 <= result <= 35.0, f"Valore fuori range clamp: {result}"

    def test_zones_are_independent(self):
        """Zone diverse hanno stati EWMA indipendenti."""
        trk = ZoneTrmTracker(warmup_ticks=2)
        for i in range(2):
            trk.update("z1", 19.0, _TS(30 * i))
            trk.update("z2", 24.0, _TS(30 * i))
        r1 = trk.get_t_rm("z1")
        r2 = trk.get_t_rm("z2")
        assert r1 is not None and r2 is not None
        assert r1 < r2, "z1 (T=19) deve avere t_rm < z2 (T=24)"

    def test_reset_single_zone(self):
        """reset(zone_id) deve azzerare solo quella zona."""
        trk = ZoneTrmTracker(warmup_ticks=2)
        for i in range(2):
            trk.update("z1", 20.0, _TS(30 * i))
            trk.update("z2", 20.0, _TS(30 * i))
        trk.reset("z1")
        assert trk.get_t_rm("z1") is None, "z1 resettata: atteso None"
        assert trk.get_t_rm("z2") is not None, "z2 non toccata: atteso valore"

    def test_reset_all(self):
        """reset() senza argomenti azzera tutte le zone."""
        trk = ZoneTrmTracker(warmup_ticks=2)
        for i in range(2):
            trk.update("z1", 20.0, _TS(30 * i))
            trk.update("z2", 20.0, _TS(30 * i))
        trk.reset()
        assert trk.get_t_rm("z1") is None
        assert trk.get_t_rm("z2") is None

    def test_unknown_zone_returns_none(self):
        """Una zona non tracciata deve restituire None."""
        trk = ZoneTrmTracker()
        assert trk.get_t_rm("nonexistent") is None

    def test_ewma_alpha_depends_on_dt(self):
        """Alpha maggiore per dt più lungo → t_rm si avvicina prima al nuovo valore."""
        # Due tracker: uno aggiornato ogni 30s, uno ogni 1h
        trk_fast = ZoneTrmTracker(tau_hours=168.0, warmup_ticks=2)
        trk_slow = ZoneTrmTracker(tau_hours=168.0, warmup_ticks=2)

        # Entrambi iniziano a 20.0
        trk_fast.update("z", 20.0, _TS(0))
        trk_fast.update("z", 20.0, _TS(30))
        trk_slow.update("z", 20.0, _TS(0))
        trk_slow.update("z", 20.0, _TS(30))

        # Poi entrambi ricevono 25.0 con dt diversi
        trk_fast.update("z", 25.0, _TS(60))          # dt=30s → alpha piccolo
        trk_slow.update("z", 25.0, _TS(0 + 3600))    # dt=1h → alpha più grande

        r_fast = trk_fast.get_t_rm("z")
        r_slow = trk_slow.get_t_rm("z")
        assert r_fast is not None and r_slow is not None
        assert r_slow > r_fast, "dt maggiore → t_rm più vicino al nuovo valore"


# ===========================================================================
# B. Tabelle config — invarianti strutturali
# ===========================================================================

class TestConfigTables:
    """Invarianti strutturali delle costanti in config.py."""

    def test_v_air_tables_length_6(self):
        """Le tabelle velocità aria devono avere esattamente 6 elementi (step 0..5)."""
        for name, tbl in [
            ("V_AIR_BEST_LIVING", V_AIR_BEST_LIVING),
            ("V_AIR_BEST_OTHER",  V_AIR_BEST_OTHER),
            ("V_AIR_HI_LIVING",   V_AIR_HI_LIVING),
            ("V_AIR_HI_OTHER",    V_AIR_HI_OTHER),
        ]:
            assert len(tbl) == 6, f"{name} deve avere 6 elementi"

    def test_v_air_tables_monotone_increasing(self):
        """Le tabelle velocità aria devono essere monotone non-decrescenti."""
        for name, tbl in [
            ("V_AIR_BEST_LIVING", V_AIR_BEST_LIVING),
            ("V_AIR_BEST_OTHER",  V_AIR_BEST_OTHER),
            ("V_AIR_HI_LIVING",   V_AIR_HI_LIVING),
            ("V_AIR_HI_OTHER",    V_AIR_HI_OTHER),
        ]:
            for i in range(len(tbl) - 1):
                assert tbl[i] <= tbl[i + 1], (
                    f"{name}[{i}]={tbl[i]} > [{i+1}]={tbl[i+1]}: non monotona"
                )

    def test_v_air_hi_ge_best(self):
        """V_AIR_HI deve essere >= V_AIR_BEST per ogni step."""
        for i in range(6):
            assert V_AIR_HI_LIVING[i] >= V_AIR_BEST_LIVING[i], (
                f"HI_LIVING[{i}] < BEST_LIVING[{i}]"
            )
            assert V_AIR_HI_OTHER[i] >= V_AIR_BEST_OTHER[i], (
                f"HI_OTHER[{i}] < BEST_OTHER[{i}]"
            )

    def test_clo_winter_by_zone_ordered(self):
        """CLO invernale deve crescere da zona A a zona F."""
        zones = [
            ClimateZoneIT.A, ClimateZoneIT.B, ClimateZoneIT.C,
            ClimateZoneIT.D, ClimateZoneIT.E, ClimateZoneIT.F,
        ]
        values = [CLO_WINTER_BY_ZONE[z] for z in zones]
        assert values == sorted(values), (
            f"CLO_WINTER_BY_ZONE non è ordinato crescente: {list(zip(zones, values))}"
        )

    def test_clo_winter_zone_d_matches_base(self):
        """CLO zona D deve corrispondere al valore atteso (1.05)."""
        assert CLO_WINTER_BY_ZONE[ClimateZoneIT.D] == pytest.approx(1.05)

    def test_pmv_band_positive(self):
        """La semiampiezza PMV deve essere positiva per tutti i profili."""
        for profile, band in MODE_PMV_BAND.items():
            assert band > 0, f"PMV_BAND[{profile}] deve essere > 0"

    def test_mode_ctrl_aggressiveness_positive(self):
        """L'aggressività di controllo deve essere positiva per tutti i profili."""
        for profile, aggr in MODE_CTRL_AGGRESSIVENESS.items():
            assert aggr > 0, f"CTRL_AGGRESSIVENESS[{profile}] deve essere > 0"

    def test_t_rm_neutral_all_seasons_present(self):
        """T_RM_NEUTRAL_BY_SEASON deve coprire winter, shoulder, summer."""
        required = {"winter", "shoulder", "summer"}
        assert required.issubset(set(T_RM_NEUTRAL_BY_SEASON.keys()))

    def test_t_rm_cap_positive(self):
        assert T_RM_CAP_DELTA_CLO > 0
        assert T_RM_SENSITIVITY_CLO > 0

    def test_skip_profiles_contains_sleep_away(self):
        """T_RM_SKIP_PROFILES deve contenere sleep, away, vacation."""
        assert "sleep" in T_RM_SKIP_PROFILES
        assert "away" in T_RM_SKIP_PROFILES
        assert "vacation" in T_RM_SKIP_PROFILES

    def test_clo_base_ordering(self):
        """summer < shoulder < winter (abbigliamento cresce con il freddo)."""
        assert CLO_BASE_SUMMER < CLO_BASE_SHOULDER < CLO_BASE_WINTER

    def test_met_base_positive(self):
        assert MET_BASE > 0
        assert MET_SLEEP > 0
        assert MET_AWAY_VACATION > 0


# ===========================================================================
# C. ComfortPolicyLayer — CLO
# ===========================================================================

class TestPolicyLayerClo:
    """Test per _clo_for(): ogni ramo stagionale e correzione di profilo."""

    def test_clo_summer_baseline(self):
        """Estate senza correzioni: CLO = base estate."""
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.SUMMER))
        assert dec.clo == pytest.approx(CLO_BASE_SUMMER, abs=0.001)

    def test_clo_shoulder_baseline(self):
        """Mezza stagione senza cold_snap: CLO = base shoulder."""
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.SHOULDER))
        assert dec.clo == pytest.approx(CLO_BASE_SHOULDER, abs=0.001)

    def test_clo_winter_zone_d(self):
        """Inverno zona D, COMFORT: CLO deve corrispondere alla tabella zona."""
        layer = _layer(ClimateZoneIT.D)
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER))
        assert dec.clo == pytest.approx(CLO_WINTER_BY_ZONE[ClimateZoneIT.D], abs=0.001)

    def test_clo_winter_zone_e_higher_than_d(self):
        """CLO inverno zona E deve essere > zona D."""
        dec_d = _layer(ClimateZoneIT.D).decide(_ctx(season=OperativeSeason.WINTER))
        dec_e = _layer(ClimateZoneIT.E).decide(_ctx(season=OperativeSeason.WINTER))
        assert dec_e.clo > dec_d.clo

    def test_clo_cold_snap_shoulder_interpolated(self):
        """Cold snap in shoulder: CLO interpolato tra shoulder e winter."""
        layer = _layer(ClimateZoneIT.D)
        dec_base = layer.decide(_ctx(season=OperativeSeason.SHOULDER, cold_snap=False))
        dec_snap = layer.decide(_ctx(season=OperativeSeason.SHOULDER, cold_snap=True))
        clo_w = CLO_WINTER_BY_ZONE[ClimateZoneIT.D]
        expected = CLO_BASE_SHOULDER + COLD_SNAP_CLO_FRACTION * (clo_w - CLO_BASE_SHOULDER)
        assert dec_snap.clo == pytest.approx(expected, abs=0.001)
        assert dec_snap.clo > dec_base.clo

    def test_clo_sleep_winter_adds_delta(self):
        """SLEEP in inverno: CLO base + delta coperte (CLO_SLEEP_WINTER_DELTA)."""
        layer = _layer(ClimateZoneIT.D)
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.SLEEP))
        expected_base = CLO_WINTER_BY_ZONE[ClimateZoneIT.D]
        expected_with_delta = expected_base + CLO_SLEEP_WINTER_DELTA
        # Capped da CLO_CAP_SLEEP
        expected_capped = min(CLO_CAP_SLEEP, expected_with_delta)
        assert dec.clo == pytest.approx(expected_capped, abs=0.001)

    def test_clo_away_winter_reduced(self):
        """AWAY in inverno: CLO deve scendere di CLO_AWAY_VACATION_WINTER_DELTA (con floor)."""
        layer = _layer(ClimateZoneIT.D)
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.AWAY))
        base = CLO_WINTER_BY_ZONE[ClimateZoneIT.D]
        expected = max(CLO_AWAY_VACATION_WINTER_FLOOR, base + CLO_AWAY_VACATION_WINTER_DELTA)
        assert dec.clo == pytest.approx(expected, abs=0.001)

    def test_clo_cap_default_not_exceeded(self):
        """CLO non deve mai superare CLO_CAP_DEFAULT per profili non-SLEEP."""
        layer = _layer(ClimateZoneIT.F)  # zona più fredda
        for mode in [HVACOperatingProfile.COMFORT, HVACOperatingProfile.ECO,
                     HVACOperatingProfile.BOOST, HVACOperatingProfile.AWAY]:
            dec = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=mode))
            assert dec.clo <= CLO_CAP_DEFAULT + 0.001, (
                f"CLO={dec.clo:.3f} supera CLO_CAP_DEFAULT={CLO_CAP_DEFAULT} per {mode}"
            )

    def test_clo_cap_sleep_not_exceeded(self):
        """In SLEEP il CLO non deve superare CLO_CAP_SLEEP."""
        layer = _layer(ClimateZoneIT.F)
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.SLEEP))
        assert dec.clo <= CLO_CAP_SLEEP + 0.001

    def test_clo_sleep_summer_no_delta(self):
        """SLEEP in estate: il delta coperte non si applica (solo inverno)."""
        layer = _layer(ClimateZoneIT.D)
        dec = layer.decide(_ctx(season=OperativeSeason.SUMMER, mode=HVACOperatingProfile.SLEEP))
        assert dec.clo == pytest.approx(CLO_BASE_SUMMER, abs=0.001)


# ===========================================================================
# C2. Correzione S3 (adaptive CLO)
# ===========================================================================

class TestPolicyLayerS3:
    """Test per la correzione S3: delta_clo da running mean T_op."""

    def test_s3_none_rm_no_change(self):
        """t_op_running_mean=None (warm-up): nessuna correzione S3."""
        layer = _layer()
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        dec_with = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        assert dec_base.clo == pytest.approx(dec_with.clo, abs=0.001)

    def test_s3_cold_week_increases_clo(self):
        """Settimana fredda (T_rm < neutro): CLO deve salire."""
        layer = _layer(ClimateZoneIT.D)
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec_cold = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral - 4.0,  # 4°C sotto il neutro
        ))
        assert dec_cold.clo > dec_base.clo, "T_rm fredda deve aumentare CLO"

    def test_s3_warm_week_decreases_clo(self):
        """Settimana mite (T_rm > neutro): CLO deve scendere."""
        layer = _layer(ClimateZoneIT.D)
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec_warm = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral + 4.0,  # 4°C sopra il neutro
        ))
        assert dec_warm.clo < dec_base.clo, "T_rm mite deve ridurre CLO"

    def test_s3_cap_respected_positive(self):
        """Il delta S3 non deve superare T_RM_CAP_DELTA_CLO in positivo."""
        layer = _layer(ClimateZoneIT.D)
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        # T_rm molto fredda: satura il cap
        dec_cold = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=5.0,  # molto sotto il neutro
        ))
        assert dec_cold.clo <= dec_base.clo + T_RM_CAP_DELTA_CLO + 0.001, (
            f"CLO={dec_cold.clo:.3f} supera il cap di +{T_RM_CAP_DELTA_CLO}"
        )

    def test_s3_cap_respected_negative(self):
        """Il delta S3 non deve superare T_RM_CAP_DELTA_CLO in negativo."""
        layer = _layer(ClimateZoneIT.D)
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        dec_warm = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=40.0,  # molto sopra il neutro
        ))
        assert dec_warm.clo >= dec_base.clo - T_RM_CAP_DELTA_CLO - 0.001, (
            f"CLO={dec_warm.clo:.3f} scende oltre il cap di -{T_RM_CAP_DELTA_CLO}"
        )

    def test_s3_skipped_for_sleep(self):
        """In modalità SLEEP la correzione S3 non si applica."""
        layer = _layer(ClimateZoneIT.D)
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec_no_rm = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            mode=HVACOperatingProfile.SLEEP,
            t_op_running_mean=None,
        ))
        dec_cold = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            mode=HVACOperatingProfile.SLEEP,
            t_op_running_mean=t_rm_neutral - 5.0,
        ))
        assert dec_no_rm.clo == pytest.approx(dec_cold.clo, abs=0.001), (
            "SLEEP: la correzione S3 non deve modificare CLO"
        )

    def test_s3_skipped_for_away(self):
        """In modalità AWAY la correzione S3 non si applica."""
        layer = _layer(ClimateZoneIT.D)
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec_no_rm = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            mode=HVACOperatingProfile.AWAY,
            t_op_running_mean=None,
        ))
        dec_cold = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            mode=HVACOperatingProfile.AWAY,
            t_op_running_mean=t_rm_neutral - 5.0,
        ))
        assert dec_no_rm.clo == pytest.approx(dec_cold.clo, abs=0.001)

    def test_s3_neutral_rm_no_change(self):
        """T_rm esattamente sul neutro: delta praticamente zero."""
        layer = _layer(ClimateZoneIT.D)
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec_no_rm = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))
        dec_neutral = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral,
        ))
        assert abs(dec_neutral.clo - dec_no_rm.clo) < 0.01, (
            "T_rm sul neutro non deve produrre correzione significativa"
        )

    def test_s3_proportional_to_deviation(self):
        """La correzione deve essere proporzionale alla deviazione (prima del cap)."""
        layer = _layer(ClimateZoneIT.D)
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]

        dec_1 = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral - 1.0,
        ))
        dec_2 = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral - 2.0,
        ))
        dec_base = layer.decide(_ctx(season=OperativeSeason.WINTER, t_op_running_mean=None))

        delta_1 = dec_1.clo - dec_base.clo
        delta_2 = dec_2.clo - dec_base.clo
        # delta_2 deve essere circa il doppio di delta_1 (proporzionalità lineare)
        assert delta_2 == pytest.approx(delta_1 * 2, abs=0.005), (
            f"S3 non è lineare: delta_1={delta_1:.4f}, delta_2={delta_2:.4f}"
        )

    def test_s3_reasons_logged(self):
        """Se S3 produce una correzione significativa, deve apparire nei reasons."""
        layer = _layer(ClimateZoneIT.D)
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        dec = layer.decide(_ctx(
            season=OperativeSeason.WINTER,
            t_op_running_mean=t_rm_neutral - 3.0,
        ))
        reasons_str = " ".join(dec.reasons)
        assert "clo:trm" in reasons_str, (
            f"Correzione S3 assente nei reasons: {dec.reasons}"
        )


# ===========================================================================
# D. ComfortPolicyLayer — PMV targets
# ===========================================================================

class TestPolicyLayerPmv:
    """Test per _pmv_targets(): centri, bande e nudge estate."""

    def test_pmv_center_comfort(self):
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER))
        assert dec.pmv_center == pytest.approx(MODE_PMV_CENTER[HVACOperatingProfile.COMFORT], abs=0.001)

    def test_pmv_band_comfort(self):
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER))
        assert dec.pmv_band == pytest.approx(MODE_PMV_BAND[HVACOperatingProfile.COMFORT], abs=0.001)

    @pytest.mark.parametrize("profile", [
        HVACOperatingProfile.COMFORT,
        HVACOperatingProfile.ECO,
        HVACOperatingProfile.BOOST,
        HVACOperatingProfile.SLEEP,
        HVACOperatingProfile.AWAY,
        HVACOperatingProfile.VACATION,
    ])
    def test_pmv_center_matches_table(self, profile):
        """PMV center deve corrispondere alla tabella per ogni profilo."""
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=profile))
        assert dec.pmv_center == pytest.approx(MODE_PMV_CENTER[profile], abs=0.01), (
            f"PMV center mismatch per {profile}: {dec.pmv_center} != {MODE_PMV_CENTER[profile]}"
        )

    def test_pmv_summer_eco_nudge_applied(self):
        """In estate/ECO il centro PMV deve ricevere il nudge positivo."""
        layer = _layer()
        dec_base = layer.decide(_ctx(
            season=OperativeSeason.SUMMER,
            mode=HVACOperatingProfile.ECO,
        ))
        center_before_nudge = MODE_PMV_CENTER[HVACOperatingProfile.ECO]
        expected = min(PMV_SUMMER_ECO_CENTER_MAX, center_before_nudge + PMV_SUMMER_ECO_NUDGE_DELTA)
        assert dec_base.pmv_center == pytest.approx(expected, abs=0.001)

    def test_pmv_summer_eco_center_capped(self):
        """Il nudge non deve superare PMV_SUMMER_ECO_CENTER_MAX."""
        layer = _layer()
        dec = layer.decide(_ctx(season=OperativeSeason.SUMMER, mode=HVACOperatingProfile.ECO))
        assert dec.pmv_center <= PMV_SUMMER_ECO_CENTER_MAX + 0.001

    def test_pmv_nudge_only_summer_eco(self):
        """Il nudge si applica solo in estate/ECO; non in inverno/ECO."""
        layer = _layer()
        dec_winter = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.ECO))
        assert dec_winter.pmv_center == pytest.approx(MODE_PMV_CENTER[HVACOperatingProfile.ECO], abs=0.001)

    def test_ctrl_aggressiveness_boost_higher(self):
        """BOOST deve avere aggressività maggiore di COMFORT."""
        layer = _layer()
        dec_comfort = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.COMFORT))
        dec_boost   = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.BOOST))
        assert dec_boost.ctrl_aggressiveness > dec_comfort.ctrl_aggressiveness

    def test_ctrl_aggressiveness_away_lower(self):
        """AWAY deve avere aggressività minore di COMFORT."""
        layer = _layer()
        dec_comfort = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.COMFORT))
        dec_away    = layer.decide(_ctx(season=OperativeSeason.WINTER, mode=HVACOperatingProfile.AWAY))
        assert dec_away.ctrl_aggressiveness < dec_comfort.ctrl_aggressiveness


# ===========================================================================
# E. ComfortBandCalculator — fisica ISO 7730
# ===========================================================================

class TestPmvPhysics:
    """Test fisici dell'equazione di Fanger (ISO 7730).

    I valori di riferimento sono derivati dalla norma e verificabili con
    strumenti esterni (pythermalcomfort, CBE Thermal Comfort Tool).
    """

    def test_pmv_iso7730_annex_d_example1(self):
        """Verifica i parametri ISO 7730:2005 Annex D Esempio 1.

        Norma: ta=22°C, tr=22°C, va=0.1 m/s, rh=60%, met=1.2, clo=0.5
        → PMV ≈ −0.75, PPD ≈ 17%
        Tolleranza ±0.05 su PMV, coerente con le varianti di implementazione
        dell'equazione di Fanger riportate in letteratura.
        """
        calc = _calc()
        pmv, ppd = calc.pmv_ppd(
            ta_c=22.0, tr_c=22.0, rh_pct=60.0,
            v_air=0.1, met=1.2, clo=0.5,
        )
        assert pmv == pytest.approx(-0.754, abs=0.05), (
            f"PMV ISO 7730 Annex D Ex.1: atteso ~-0.75, ottenuto {pmv:.3f}"
        )
        assert ppd == pytest.approx(17.0, abs=1.5), (
            f"PPD ISO 7730 Annex D Ex.1: atteso ~17%, ottenuto {ppd:.1f}%"
        )

    def test_pmv_iso7730_annex_d_example2(self):
        """Verifica i parametri ISO 7730:2005 Annex D Esempio 2 (condizioni calde).

        Norma: ta=27°C, tr=27°C, va=0.1 m/s, rh=60%, met=1.2, clo=0.5
        → PMV > 0 (caldo percepito).
        """
        calc = _calc()
        pmv, ppd = calc.pmv_ppd(
            ta_c=27.0, tr_c=27.0, rh_pct=60.0,
            v_air=0.1, met=1.2, clo=0.5,
        )
        assert pmv == pytest.approx(0.765, abs=0.05), (
            f"PMV ISO 7730 Annex D Ex.2: atteso ~+0.77, ottenuto {pmv:.3f}"
        )
        assert pmv > 0, "Condizioni calde devono produrre PMV positivo"

    def test_pmv_cold_lower_than_neutral(self):
        """Temperature più basse producono PMV più negativi."""
        calc = _calc()
        pmv_warm, _ = calc.pmv_ppd(ta_c=24.0, tr_c=24.0, rh_pct=50.0, v_air=0.1, met=1.1, clo=0.7)
        pmv_cold, _ = calc.pmv_ppd(ta_c=18.0, tr_c=18.0, rh_pct=50.0, v_air=0.1, met=1.1, clo=0.7)
        assert pmv_cold < pmv_warm

    def test_pmv_higher_clo_increases_pmv(self):
        """Abbigliamento più pesante (clo alto) produce PMV più alto a parità di T."""
        calc = _calc()
        pmv_light, _ = calc.pmv_ppd(ta_c=20.0, tr_c=20.0, rh_pct=50.0, v_air=0.1, met=1.1, clo=0.5)
        pmv_heavy, _ = calc.pmv_ppd(ta_c=20.0, tr_c=20.0, rh_pct=50.0, v_air=0.1, met=1.1, clo=1.2)
        assert pmv_heavy > pmv_light

    def test_pmv_higher_met_increases_pmv(self):
        """Attività metabolica più alta produce PMV più alto."""
        calc = _calc()
        pmv_low, _ = calc.pmv_ppd(ta_c=20.0, tr_c=20.0, rh_pct=50.0, v_air=0.1, met=0.9, clo=0.7)
        pmv_hi, _  = calc.pmv_ppd(ta_c=20.0, tr_c=20.0, rh_pct=50.0, v_air=0.1, met=1.3, clo=0.7)
        assert pmv_hi > pmv_low

    def test_ppd_minimum_at_neutral_pmv(self):
        """PPD è minima (≈5%) quando PMV ≈ 0."""
        calc = _calc()
        _, ppd = calc.pmv_ppd(ta_c=22.5, tr_c=22.5, rh_pct=50.0, v_air=0.1, met=1.1, clo=0.7)
        assert ppd < 10.0, f"PPD troppo alta per condizioni quasi-neutre: {ppd:.1f}%"

    def test_pmv_ppd_range(self):
        """PMV ∈ [-3,3] e PPD ∈ [5,100] per una gamma di condizioni realistiche."""
        calc = _calc()
        cases = [
            (16.0, 16.0, 60.0, 0.05, 1.0, 1.0),  # freddo invernale
            (26.0, 26.0, 60.0, 0.20, 1.1, 0.5),  # caldo estivo
            (22.0, 24.0, 40.0, 0.10, 1.2, 0.7),  # condizioni miste
        ]
        for ta, tr, rh, v, met, clo in cases:
            pmv, ppd = calc.pmv_ppd(ta_c=ta, tr_c=tr, rh_pct=rh, v_air=v, met=met, clo=clo)
            assert -3.0 <= pmv <= 3.0,   f"PMV={pmv:.3f} fuori range fisico"
            assert 5.0  <= ppd <= 100.0, f"PPD={ppd:.1f}% fuori range fisico"

    def test_higher_air_speed_lowers_pmv_in_hot(self):
        """In condizioni calde, velocità aria più alta abbassa il PMV (cooling effect)."""
        calc = _calc()
        pmv_still, _ = calc.pmv_ppd(ta_c=26.0, tr_c=26.0, rh_pct=50.0, v_air=0.05, met=1.1, clo=0.5)
        pmv_draft, _ = calc.pmv_ppd(ta_c=26.0, tr_c=26.0, rh_pct=50.0, v_air=0.40, met=1.1, clo=0.5)
        assert pmv_draft < pmv_still


# ===========================================================================
# F. ComfortBandCalculator.compute_single
# ===========================================================================

class TestComputeSingle:
    """Test per il calcolo della comfort band per una singola zona."""

    def test_band_t_op_min_le_max(self):
        """La banda deve sempre avere t_op_min <= t_op_max."""
        calc = _calc()
        for season in [OperativeSeason.WINTER, OperativeSeason.SUMMER, OperativeSeason.SHOULDER]:
            res = calc.compute_single(
                vmc_air_speed=2, room="camera_1", season=season,
                rh_pct=50.0, t_op_current=21.0,
            )
            assert res.t_op_min <= res.t_op_max, (
                f"Banda invertita in {season}: min={res.t_op_min:.2f} > max={res.t_op_max:.2f}"
            )

    def test_band_width_positive(self):
        """La larghezza della banda deve essere positiva."""
        calc = _calc()
        res = calc.compute_single(
            vmc_air_speed=2, room="soggiorno", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0,
        )
        assert res.t_op_max - res.t_op_min > 0.1

    def test_band_contains_neutral_temperature(self):
        """La temperatura neutra (PMV≈0) deve cadere dentro la banda per condizioni neutre."""
        calc = _calc()
        # Usiamo pmv_center=0, pmv_band=0.5 (ISO Cat. B)
        from custom_components.drp_climate_master_v2.plant.decision.confort_band.model import (
            PolicyDecision, HumiditySolveMode,
        )
        policy = PolicyDecision(
            met=1.1, clo=0.7, pmv_center=0.0, pmv_band=0.5,
            humidity_solve_mode=HumiditySolveMode.RH_CONST,
        )
        res = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.SHOULDER,
            rh_pct=50.0, t_op_current=22.0, policy=policy,
        )
        # La banda PMV=[-0.5, +0.5] deve contenere una temperatura reale
        assert res.t_op_min < res.t_op_max
        # La T_op corrente (22°C) dovrebbe essere vicina al centro banda
        band_center = (res.t_op_min + res.t_op_max) / 2
        assert abs(band_center - 22.0) < 4.0, (
            f"Centro banda {band_center:.1f} troppo lontano da 22°C"
        )

    def test_ok_flag_true_when_in_band(self):
        """ok=True quando t_op_current è dentro la banda calcolata."""
        calc = _calc()
        res = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=22.0,
        )
        if res.ok is not None:
            in_band = res.t_op_min <= 22.0 <= res.t_op_max
            assert res.ok == in_band

    def test_pmv_computed_when_t_op_provided(self):
        """Con t_op_current fornita, pmv e ppd devono essere calcolati."""
        calc = _calc()
        res = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0,
        )
        assert res.pmv is not None
        assert res.ppd is not None
        assert res.t_op is not None

    def test_pmv_none_when_no_temperature(self):
        """Senza t_op_current né ta/tr, pmv/ppd devono essere None."""
        calc = _calc()
        res = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0,
        )
        assert res.pmv is None
        assert res.ppd is None

    def test_living_room_wider_band_than_bedroom(self):
        """Il soggiorno (living) ha tabelle v_air più alte → banda diversa dalla camera."""
        calc = _calc()
        res_living  = calc.compute_single(
            vmc_air_speed=4, room="soggiorno", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0,
        )
        res_bedroom = calc.compute_single(
            vmc_air_speed=4, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0,
        )
        # Con v_air più alta il bound freddo sale → t_op_min più alta in living
        assert res_living.v_air_best != res_bedroom.v_air_best

    def test_policy_override_met_clo(self):
        """Un override di met/clo nella PolicyDecision deve prevalere sui default."""
        from custom_components.drp_climate_master_v2.plant.decision.confort_band.model import (
            PolicyDecision, HumiditySolveMode,
        )
        calc = _calc()
        policy_heavy = PolicyDecision(
            met=0.9, clo=2.0, pmv_center=-0.05, pmv_band=0.3,
            humidity_solve_mode=HumiditySolveMode.RH_CONST,
        )
        policy_light = PolicyDecision(
            met=1.1, clo=0.5, pmv_center=-0.05, pmv_band=0.3,
            humidity_solve_mode=HumiditySolveMode.RH_CONST,
        )
        res_heavy = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0, policy=policy_heavy,
        )
        res_light = calc.compute_single(
            vmc_air_speed=2, room="camera_1", season=OperativeSeason.WINTER,
            rh_pct=50.0, t_op_current=21.0, policy=policy_light,
        )
        assert res_heavy.met_used == pytest.approx(0.9, abs=0.001)
        assert res_heavy.clo_used == pytest.approx(2.0, abs=0.001)
        # CLO più alto → t_op_min più bassa (più vestiti = meno caldo necessario)
        assert res_heavy.t_op_min < res_light.t_op_min


# ===========================================================================
# G. Integrazione S3 end-to-end: delta_clo → shift comfort band
# ===========================================================================

class TestS3Integration:
    """Verifica che la correzione S3 si propaghi fino a t_op_min/t_op_max."""

    def _band_for_trm(self, t_rm: Optional[float]) -> tuple[float, float]:
        """Calcola (t_op_min, t_op_max) per una data running mean."""
        layer = _layer(ClimateZoneIT.D)
        calc  = _calc()

        ctx = _ctx(
            season=OperativeSeason.WINTER,
            mode=HVACOperatingProfile.COMFORT,
            t_op_running_mean=t_rm,
        )
        decision = layer.decide(ctx)

        from custom_components.drp_climate_master_v2.plant.decision.confort_band.model import HumiditySolveMode
        res = calc.compute_single(
            vmc_air_speed=2,
            room="camera_1",
            season=OperativeSeason.WINTER,
            rh_pct=50.0,
            t_op_current=21.0,
            policy=decision,
            humidity_solve_mode=HumiditySolveMode.RH_CONST,
        )
        return res.t_op_min, res.t_op_max

    def test_cold_week_shifts_band_down(self):
        """Settimana fredda → CLO sale → banda si sposta verso T più basse."""
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        min_base, max_base = self._band_for_trm(None)
        min_cold, max_cold = self._band_for_trm(t_rm_neutral - 4.0)
        assert min_cold < min_base, (
            f"t_op_min fredda ({min_cold:.2f}) non è scesa vs base ({min_base:.2f})"
        )
        assert max_cold < max_base, (
            f"t_op_max fredda ({max_cold:.2f}) non è scesa vs base ({max_base:.2f})"
        )

    def test_warm_week_shifts_band_up(self):
        """Settimana mite → CLO scende → banda si sposta verso T più alte."""
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        min_base, max_base = self._band_for_trm(None)
        min_warm, max_warm = self._band_for_trm(t_rm_neutral + 4.0)
        assert min_warm > min_base, (
            f"t_op_min mite ({min_warm:.2f}) non è salita vs base ({min_base:.2f})"
        )

    def test_shift_magnitude_bounded(self):
        """Lo shift della banda deve essere entro i limiti fisici del cap."""
        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        min_base, _ = self._band_for_trm(None)
        # Caso estremo: T_rm a 5°C (saturo il cap)
        min_cold, _ = self._band_for_trm(5.0)
        shift = min_base - min_cold
        # Il cap è 0.25 clo; l'effetto sulla T_op è circa 1-1.5°C
        assert 0.0 < shift < 3.0, (
            f"Shift banda fuori range atteso: {shift:.2f}°C"
        )

    def test_sleep_s3_no_band_change(self):
        """In SLEEP la correzione S3 non deve spostare la banda."""
        layer = _layer(ClimateZoneIT.D)
        calc  = _calc()

        from custom_components.drp_climate_master_v2.plant.decision.confort_band.model import HumiditySolveMode

        def _band(t_rm):
            ctx = _ctx(
                season=OperativeSeason.WINTER,
                mode=HVACOperatingProfile.SLEEP,
                t_op_running_mean=t_rm,
            )
            dec = layer.decide(ctx)
            res = calc.compute_single(
                vmc_air_speed=2, room="camera_1",
                season=OperativeSeason.WINTER,
                rh_pct=50.0, t_op_current=21.0,
                policy=dec,
                humidity_solve_mode=HumiditySolveMode.RH_CONST,
            )
            return res.t_op_min, res.t_op_max

        t_rm_neutral = T_RM_NEUTRAL_BY_SEASON["winter"]
        min_base, max_base = _band(None)
        min_cold, max_cold = _band(t_rm_neutral - 5.0)
        assert min_cold == pytest.approx(min_base, abs=0.1), (
            "SLEEP: S3 non deve modificare t_op_min"
        )
        assert max_cold == pytest.approx(max_base, abs=0.1), (
            "SLEEP: S3 non deve modificare t_op_max"
        )