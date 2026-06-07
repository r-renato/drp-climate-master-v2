"""Simulazione del CloMetProvider — stima adattiva CLO e MET.

Scopo
-----
Strumento di simulazione/esplorazione del nuovo modulo ``parameters/``
(CloMetProvider). Mostra come CLO e MET variano al variare degli input.
Non contiene assert — nessun test fallisce mai.

Utilizza lo stesso pattern di ``sim_comfort_band.py``:
input da JSON, override CLI via conftest.py.

Utilizzo
--------
    python3 -m pytest test/sim/sim_clo_met_provider.py -s -v

    python3 -m pytest test/sim/sim_clo_met_provider.py -s \\
        --sim-override global.climate_zone=E \\
        --sim-override sim_02_s4_ramp_shoulder.shoulder_direction=autumn

    python3 -m pytest test/sim/sim_clo_met_provider.py -s -k "sim_01 or sim_08"
    python3 -m pytest test/sim/sim_clo_met_provider.py -s -k "scenario"

Struttura simulazioni
---------------------
  sim_01  Matrice stagioni × profilo
  sim_02  Ramp S4: evoluzione CLO durante la stagione shoulder
  sim_03  Adaptive CLO S3: sweep running mean T_op
  sim_04  Cold snap: effetto per progress e profilo
  sim_05  Time-of-day CLO: variazione nelle 4 fasce orarie
  sim_06  MET per tipo di stanza: matrice room_type × profilo × time-of-day
  sim_07  Confronto old (seed fisso) vs new (S4+S3): quantifica il delta
  sim_08  Matrice commissioning: appartamento Roma con zone reali

  scenario_fine_maggio_roma            Situazione attuale (23/05, 09:45)
  scenario_transizione_shoulder_estate Evoluzione CLO da inizio a fine primavera
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Path setup — nessun HA richiesto (parameters/ è puro Python)
# ---------------------------------------------------------------------------

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Import moduli CloMetProvider
# ---------------------------------------------------------------------------

from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile
from custom_components.drp_climate_master_v2.domain.models.season import OperativeSeason
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.config import (
    CloMetConfig,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.model import (
    RoomType,
    TimeOfDay,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.clo_provider import (
    CloProvider,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.met_provider import (
    MetProvider,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.parameters.provider import (
    ComfortParameterProvider,
)

# ---------------------------------------------------------------------------
# Caricamento JSON e override CLI (stesso pattern di sim_comfort_band)
# ---------------------------------------------------------------------------

_DEFAULT_INPUT = _HERE / "simulate_clo_met_input.json"


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _apply_overrides(cfg: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    for raw in overrides:
        if "=" not in raw:
            raise ValueError(f"Override malformato — manca '=': '{raw}'")
        dotted_key, _, raw_value = raw.partition("=")
        keys = [k.strip() for k in dotted_key.strip().split(".")]
        try:
            value = json.loads(raw_value.strip())
        except json.JSONDecodeError:
            value = raw_value.strip()
        d = cfg
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value
    return cfg


@pytest.fixture
def sim_cfg(request: pytest.FixtureRequest) -> dict[str, Any]:
    input_opt: str | None = request.config.getoption("--sim-input", default=None)
    json_path = pathlib.Path(input_opt) if input_opt else _DEFAULT_INPUT
    cfg = _load_json(json_path)
    overrides: list[str] = request.config.getoption("--sim-override", default=[])
    if overrides:
        cfg = _apply_overrides(cfg, overrides)
    return cfg


# ---------------------------------------------------------------------------
# Conversione stringa → tipi di dominio
# ---------------------------------------------------------------------------

_SEASON_MAP: dict[str, OperativeSeason] = {
    "winter":   OperativeSeason.WINTER,
    "shoulder": OperativeSeason.SHOULDER,
    "summer":   OperativeSeason.SUMMER,
}
_PROFILE_MAP: dict[str, HVACOperatingProfile] = {
    p.value.lower(): p for p in HVACOperatingProfile
}
_PROFILE_MAP.update({p.value: p for p in HVACOperatingProfile})
_ROOM_MAP: dict[str, RoomType] = {r.value: r for r in RoomType}
_TOD_MAP: dict[str, TimeOfDay] = {t.value: t for t in TimeOfDay}
_SEASON_LABEL = {
    OperativeSeason.WINTER:   "INVERNO  ",
    OperativeSeason.SHOULDER: "MEZZA S. ",
    OperativeSeason.SUMMER:   "ESTATE   ",
}


def _season(s: str) -> OperativeSeason:
    v = _SEASON_MAP.get(s.lower())
    if v is None:
        raise ValueError(f"Stagione sconosciuta: '{s}'")
    return v


def _profile(s: str) -> HVACOperatingProfile:
    v = _PROFILE_MAP.get(s) or _PROFILE_MAP.get(s.lower())
    if v is None:
        raise ValueError(f"Profilo sconosciuto: '{s}'")
    return v


def _room(s: str) -> RoomType:
    v = _ROOM_MAP.get(s.lower())
    if v is None:
        raise ValueError(f"RoomType sconosciuto: '{s}'")
    return v


def _tod(s: str) -> TimeOfDay:
    v = _TOD_MAP.get(s.lower())
    if v is None:
        raise ValueError(f"TimeOfDay sconosciuto: '{s}'")
    return v


def _g(cfg: dict, key: str, fallback: Any = None) -> Any:
    return cfg.get("global", {}).get(key, fallback)


def _s(cfg: dict, sim_key: str) -> dict[str, Any]:
    return cfg.get(sim_key, {})


# ---------------------------------------------------------------------------
# Helper: build provider dal global config
# ---------------------------------------------------------------------------

def _provider(sim_cfg: dict[str, Any]) -> ComfortParameterProvider:
    climate_zone = _g(sim_cfg, "climate_zone", "D")
    raw_map: dict = _g(sim_cfg, "room_type_map", {})
    room_type_map = {k: _room(v) for k, v in raw_map.items()}
    cfg = CloMetConfig(climate_zone=climate_zone)
    return ComfortParameterProvider(cfg=cfg, room_type_map=room_type_map)


def _clo_prov(sim_cfg: dict[str, Any]) -> CloProvider:
    return CloProvider(CloMetConfig(climate_zone=_g(sim_cfg, "climate_zone", "D")))


def _met_prov(sim_cfg: dict[str, Any]) -> MetProvider:
    return MetProvider(CloMetConfig(climate_zone=_g(sim_cfg, "climate_zone", "D")))


# ---------------------------------------------------------------------------
# Formattatori di output
# ---------------------------------------------------------------------------

_SEP = "─" * 110


def _section(title: str) -> None:
    print(f"\n{'═' * 110}")
    print(f"  {title}")
    print("═" * 110)


def _subsection(title: str) -> None:
    print(f"\n  {title}")
    print("  " + _SEP[:80])


def _clo_row(
    *,
    label: str,
    clo_value: float,
    season_base: float,
    s4_delta: float,
    s3_delta: float,
    cold_snap_delta: float,
    profile_delta: float,
    tod_delta: float,
    source: str,
    show_source: bool = True,
) -> str:
    parts = (
        f"  {label:<30}"
        f"  CLO={clo_value:.3f}"
        f"  base={season_base:.3f}"
        f"  S4={s4_delta:+.3f}"
        f"  S3={s3_delta:+.3f}"
        f"  cs={cold_snap_delta:+.3f}"
        f"  prof={profile_delta:+.3f}"
        f"  tod={tod_delta:+.3f}"
    )
    if show_source:
        parts += f"\n    ↳ {source}"
    return parts


def _met_row(
    *,
    label: str,
    met_value: float,
    room_base: float,
    profile_override: Optional[float],
    tod_delta: float,
    source: str,
) -> str:
    override_s = f"override={profile_override:.2f}" if profile_override is not None else "room_base  "
    return (
        f"  {label:<28}"
        f"  MET={met_value:.3f}"
        f"  room_base={room_base:.3f}"
        f"  {override_s}"
        f"  tod={tod_delta:+.3f}"
        f"  [{source}]"
    )


# ---------------------------------------------------------------------------
# SIM 01 — Matrice stagioni × profilo
# ---------------------------------------------------------------------------

def test_sim_01_stagioni_per_profilo(sim_cfg: dict) -> None:
    """Matrice stagione × profilo: CLO base per ogni combinazione."""
    s        = _s(sim_cfg, "sim_01_stagioni_per_profilo")
    seasons  = [_season(x) for x in s.get("seasons",  ["winter", "shoulder", "summer"])]
    profiles = [_profile(x) for x in s.get("profiles", ["Comfort", "Eco", "Boost", "Sleep", "Away", "Vacation"])]
    progress = float(s.get("shoulder_progress", 50.0))
    direction= s.get("shoulder_direction", "spring")
    cold_snap= bool(s.get("cold_snap", False))
    t_rm     = s.get("t_rm", None)
    t_rm_f   = float(t_rm) if t_rm is not None else None
    prov     = _clo_prov(sim_cfg)
    zone_d   = _g(sim_cfg, "climate_zone", "D")

    _section(
        f"SIM 01 — Stagioni × Profilo  "
        f"(zona {zone_d}, shoulder progress={progress}% {direction}, cold_snap={cold_snap})"
    )

    print(
        f"\n  {'':30}  {'CLO':>6}  {'base':>6}  {'S4':>7}  "
        f"{'S3':>7}  {'cs':>7}  {'prof':>7}  {'tod':>7}"
    )
    print("  " + _SEP[:95])

    for season in seasons:
        _subsection(_SEASON_LABEL.get(season, season.value.upper()))
        dir_s = direction if season == OperativeSeason.SHOULDER else None
        prog_s = progress if season == OperativeSeason.SHOULDER else None
        for prof in profiles:
            r = prov.compute(
                operative_season=season,
                season_progress=prog_s,
                shoulder_direction=dir_s,
                profile=prof,
                time_of_day=TimeOfDay.DAYTIME,
                cold_snap=(cold_snap and season == OperativeSeason.SHOULDER),
                t_op_running_mean=t_rm_f,
                t_op_current=None,
            )
            print(
                f"  {prof.value:<30}  {r.value:>6.3f}  {r.season_base:>6.3f}"
                f"  {r.s4_delta:>+7.3f}  {r.s3_delta:>+7.3f}  "
                f"{r.cold_snap_delta:>+7.3f}  {r.profile_delta:>+7.3f}  {r.tod_delta:>+7.3f}"
            )


# ---------------------------------------------------------------------------
# SIM 02 — Ramp S4: evoluzione CLO durante la stagione shoulder
# ---------------------------------------------------------------------------

def test_sim_02_s4_ramp_shoulder(sim_cfg: dict) -> None:
    """Ramp S4: come CLO scende/sale con il progresso stagionale."""
    s          = _s(sim_cfg, "sim_02_s4_ramp_shoulder")
    direction  = s.get("shoulder_direction", "spring")
    progresses = [float(x) for x in s.get("progress_values", list(range(0, 110, 10)))]
    profiles   = [_profile(x) for x in s.get("profiles", ["Eco", "Sleep"])]
    cold_at    = set(int(x) for x in s.get("cold_snap_at_progress", []))
    prov       = _clo_prov(sim_cfg)
    zone_d     = _g(sim_cfg, "climate_zone", "D")

    _section(f"SIM 02 — S4 Ramp shoulder ({direction}, zona {zone_d})")

    for prof in profiles:
        _subsection(f"Profilo: {prof.value}")
        print(f"\n  {'Progress':>10}  {'CLO':>7}  {'base':>7}  {'S4 delta':>9}  "
              f"{'cold_snap':>10}  Toward")
        print("  " + _SEP[:70])

        target = "estate" if direction == "spring" else "inverno"
        for prog in progresses:
            cold = int(prog) in cold_at
            r = prov.compute(
                operative_season=OperativeSeason.SHOULDER,
                season_progress=prog,
                shoulder_direction=direction,
                profile=prof,
                time_of_day=TimeOfDay.DAYTIME,
                cold_snap=cold,
                t_op_running_mean=None, t_op_current=None,
            )
            cs_marker = " ❄" if cold else "  "
            ramp_pct = max(0.0, (prog - 50.0) / 50.0 * 100) if prog > 50 else 0.0
            print(
                f"  {prog:>9.1f}%  {r.value:>7.3f}  {r.season_base:>7.3f}  "
                f"{r.s4_delta:>+9.3f}  {r.cold_snap_delta:>+9.3f}{cs_marker}"
                f"  → {target} {ramp_pct:.0f}%"
            )


# ---------------------------------------------------------------------------
# SIM 03 — Adaptive CLO S3: sweep running mean T_op
# ---------------------------------------------------------------------------

def test_sim_03_s3_adaptive(sim_cfg: dict) -> None:
    """Sweep T_rm per stagione: impatto di S3 sul CLO."""
    s        = _s(sim_cfg, "sim_03_s3_adaptive")
    seasons  = [_season(x) for x in s.get("seasons", ["winter", "shoulder", "summer"])]
    profiles = [_profile(x) for x in s.get("profiles_to_compare", ["Eco", "Sleep", "Away"])]
    t_rm_cold= float(s.get("t_rm_cold_for_profile_comparison", 17.0))
    t_op_map : dict = s.get("t_op_by_season", {})
    prov     = _clo_prov(sim_cfg)

    _neutrals = {"winter": 21.5, "shoulder": 22.5, "summer": 25.0}

    _section("SIM 03 — Adaptive CLO S3: sweep running mean T_op")

    t_rm_values_by_season: dict = s.get("t_rm_values_by_season", {})

    for season in seasons:
        neutral   = _neutrals.get(season.value.lower(), 22.5)
        t_op_curr = float(t_op_map.get(season.value.lower(), neutral))
        raw_vals  = t_rm_values_by_season.get(season.value.lower(),
                    [neutral - 7, neutral - 4, neutral - 2, None, neutral + 2, neutral + 4, neutral + 7])
        t_rm_vals = [neutral if v is None else float(v) for v in raw_vals]

        _subsection(
            f"{_SEASON_LABEL.get(season)}  T_op_corrente={t_op_curr:.1f}°C  neutrale={neutral:.1f}°C"
        )
        print(f"\n  {'T_rm':>8}  {'Δ vs neutro':>11}  {'CLO':>7}  {'S3 delta':>9}  "
              f"{'baseline CLO':>13}  S3 attiva?")
        print("  " + _SEP[:75])

        r_base = prov.compute(
            operative_season=season, season_progress=None, shoulder_direction=None,
            profile=HVACOperatingProfile.ECO, time_of_day=TimeOfDay.DAYTIME,
            cold_snap=False, t_op_running_mean=None, t_op_current=None,
        )

        for t_rm in t_rm_vals:
            r = prov.compute(
                operative_season=season, season_progress=None, shoulder_direction=None,
                profile=HVACOperatingProfile.ECO, time_of_day=TimeOfDay.DAYTIME,
                cold_snap=False, t_op_running_mean=t_rm, t_op_current=t_op_curr,
            )
            delta = t_rm - neutral
            active = abs(r.s3_delta) >= 0.005
            marker = " ← neutro" if abs(delta) < 0.01 else ""
            print(
                f"  {t_rm:>7.1f}°C  {delta:>+10.1f}°C  {r.value:>7.3f}  "
                f"{r.s3_delta:>+9.3f}  {r_base.value:>12.3f}  "
                f"{'sì' if active else 'NO (suppresso)':>14}{marker}"
            )

        _subsection(f"  Profili con T_rm={t_rm_cold:.1f}°C — SLEEP/AWAY devono ignorare S3")
        for prof in profiles:
            r = prov.compute(
                operative_season=season, season_progress=None, shoulder_direction=None,
                profile=prof, time_of_day=TimeOfDay.DAYTIME,
                cold_snap=False, t_op_running_mean=t_rm_cold, t_op_current=t_op_curr,
            )
            s3_active = abs(r.s3_delta) >= 0.005
            print(
                f"    {prof.value:<12}  CLO={r.value:.3f}  "
                f"S3={'attiva' if s3_active else 'SKIP (profilo)'}"
            )


# ---------------------------------------------------------------------------
# SIM 04 — Cold snap: effetto per progress
# ---------------------------------------------------------------------------

def test_sim_04_cold_snap(sim_cfg: dict) -> None:
    """Cold snap in shoulder: interazione con progress S4."""
    s          = _s(sim_cfg, "sim_04_cold_snap")
    progresses = [float(x) for x in s.get("progress_values", [0, 25, 50, 75, 90, 100])]
    direction  = s.get("shoulder_direction", "spring")
    profiles   = [_profile(x) for x in s.get("profiles", ["Eco", "Comfort"])]
    prov       = _clo_prov(sim_cfg)

    _section(f"SIM 04 — Cold snap in shoulder ({direction})")

    for prof in profiles:
        _subsection(f"Profilo: {prof.value}")
        print(f"\n  {'Progress':>10}  {'CLO no-cs':>10}  {'CLO cs':>8}  "
              f"{'Δ cold_snap':>12}  S4 delta (no-cs)")
        print("  " + _SEP[:65])

        for prog in progresses:
            r_no = prov.compute(
                operative_season=OperativeSeason.SHOULDER,
                season_progress=prog, shoulder_direction=direction,
                profile=prof, time_of_day=TimeOfDay.DAYTIME,
                cold_snap=False, t_op_running_mean=None, t_op_current=None,
            )
            r_cs = prov.compute(
                operative_season=OperativeSeason.SHOULDER,
                season_progress=prog, shoulder_direction=direction,
                profile=prof, time_of_day=TimeOfDay.DAYTIME,
                cold_snap=True, t_op_running_mean=None, t_op_current=None,
            )
            delta_cs = r_cs.value - r_no.value
            print(
                f"  {prog:>9.1f}%  {r_no.value:>10.3f}  {r_cs.value:>8.3f}  "
                f"{delta_cs:>+11.3f}  S4={r_no.s4_delta:+.3f}"
            )


# ---------------------------------------------------------------------------
# SIM 05 — Time-of-day CLO
# ---------------------------------------------------------------------------

def test_sim_05_time_of_day(sim_cfg: dict) -> None:
    """Variazione CLO per fascia oraria: effetto secondario."""
    s        = _s(sim_cfg, "sim_05_time_of_day")
    seasons  = [_season(x) for x in s.get("seasons", ["winter", "shoulder", "summer"])]
    profiles = [_profile(x) for x in s.get("profiles", ["Eco", "Sleep"])]
    tod_vals = [_tod(x) for x in s.get("time_of_day_values",
                ["morning", "daytime", "evening", "night"])]
    prov     = _clo_prov(sim_cfg)

    _section("SIM 05 — Time-of-day CLO")

    for season in seasons:
        _subsection(_SEASON_LABEL.get(season) or season.value.upper())
        print(f"\n  {'Profilo':<12}  " +
              "  ".join(f"{t.value.upper()[:7]:>10}" for t in tod_vals))
        print("  " + _SEP[:70])

        for prof in profiles:
            row = f"  {prof.value:<12}"
            ref = None
            for tod in tod_vals:
                r = prov.compute(
                    operative_season=season, season_progress=None,
                    shoulder_direction=None, profile=prof,
                    time_of_day=tod, cold_snap=False,
                    t_op_running_mean=None, t_op_current=None,
                )
                if ref is None:
                    ref = r.value
                delta_s = f"({r.tod_delta:+.2f})"
                row += f"  {r.value:.3f}{delta_s:>7}"
            print(row)


# ---------------------------------------------------------------------------
# SIM 06 — MET per tipo di stanza
# ---------------------------------------------------------------------------

def test_sim_06_met_room_type(sim_cfg: dict) -> None:
    """Matrice room_type × profilo × time-of-day: valori MET."""
    s         = _s(sim_cfg, "sim_06_met_room_type")
    room_types= [_room(x) for x in s.get("room_types",
                  ["bedroom", "bathroom", "kitchen", "living", "hallway"])]
    profiles  = [_profile(x) for x in s.get("profiles", ["Eco", "Sleep", "Away"])]
    tod_vals  = [_tod(x) for x in s.get("time_of_day_values",
                  ["morning", "daytime", "evening", "night"])]
    prov      = _met_prov(sim_cfg)

    _section("SIM 06 — MET per tipo di stanza")

    # Parte 1: matrice room_type × profilo (tod=DAYTIME)
    _subsection("Parte 1 — room_type × profilo (TimeOfDay=DAYTIME)")
    print(f"\n  {'RoomType':<14}  " +
          "  ".join(f"{p.value[:10]:>10}" for p in profiles))
    print("  " + _SEP[:60])

    for rt in room_types:
        row = f"  {rt.value:<14}"
        for prof in profiles:
            r = prov.compute(room_type=rt, profile=prof, time_of_day=TimeOfDay.DAYTIME)
            ov_s = "*" if r.profile_override is not None else " "
            row += f"  {r.value:.3f}{ov_s:>9}"
        print(row)
    print("\n  (* = override profilo attivo)")

    # Parte 2: time-of-day per ECO
    _subsection("Parte 2 — room_type × time-of-day (Profilo=Eco)")
    print(f"\n  {'RoomType':<14}  " +
          "  ".join(f"{t.value.upper()[:7]:>10}" for t in tod_vals))
    print("  " + _SEP[:60])

    for rt in room_types:
        row = f"  {rt.value:<14}"
        for tod in tod_vals:
            r = prov.compute(room_type=rt, profile=HVACOperatingProfile.ECO, time_of_day=tod)
            delta_s = f"({r.tod_delta:+.2f})"
            row += f"  {r.value:.3f}{delta_s:>7}"
        print(row)

    # Parte 3: fonte dei valori
    _subsection("Parte 3 — MET base ISO 8996 per room_type (ECO, DAYTIME)")
    print()
    for rt in room_types:
        r = prov.compute(room_type=rt, profile=HVACOperatingProfile.ECO, time_of_day=TimeOfDay.DAYTIME)
        print(f"  {rt.value:<14}  MET={r.value:.3f}  room_base={r.room_base:.3f}  [{r.source}]")


# ---------------------------------------------------------------------------
# SIM 07 — Old vs New CLO
# ---------------------------------------------------------------------------

def test_sim_07_old_vs_new_clo(sim_cfg: dict) -> None:
    """Confronto CLO old (seed fisso) vs new (S4 + S3): quantifica il delta."""
    s           = _s(sim_cfg, "sim_07_old_vs_new_clo")
    direction   = s.get("shoulder_direction", "spring")
    progresses  = [float(x) for x in s.get("shoulder_progress_values",
                   [0, 25, 50, 75, 90, 100])]
    t_rm_map    = {str(int(k)): v for k, v in s.get("t_rm_by_progress", {}).items()}
    clo_old_sh  = float(s.get("clo_old_shoulder", 0.82))
    clo_old_sum = float(s.get("clo_old_summer", 0.50))
    clo_old_win = float(s.get("clo_old_winter", 1.05))
    prov        = _clo_prov(sim_cfg)

    _section(f"SIM 07 — Old (seed fisso) vs New (S4+S3) CLO  (shoulder {direction})")

    print(f"\n  OLD: winter={clo_old_win:.2f} shoulder={clo_old_sh:.2f} summer={clo_old_sum:.2f}")
    print(f"  NEW: S4 ramp + S3 adattivo + correzioni profilo/tod")

    _subsection("Shoulder — evoluzione con progress")
    print(f"\n  {'Progress':>10}  {'CLO old':>9}  {'CLO new':>9}  "
          f"{'Δ (new-old)':>12}  {'T_rm':>7}  S3 active?")
    print("  " + _SEP[:75])

    for prog in progresses:
        t_rm_raw = t_rm_map.get(str(int(prog)))
        t_rm = float(t_rm_raw) if t_rm_raw is not None else None
        r = prov.compute(
            operative_season=OperativeSeason.SHOULDER,
            season_progress=prog, shoulder_direction=direction,
            profile=HVACOperatingProfile.ECO,
            time_of_day=TimeOfDay.DAYTIME,
            cold_snap=False,
            t_op_running_mean=t_rm, t_op_current=(t_rm + 0.5 if t_rm else None),
        )
        delta = r.value - clo_old_sh
        s3_active = abs(r.s3_delta) >= 0.005
        t_rm_s = f"{t_rm:.1f}°C" if t_rm is not None else "  None"
        marker = " ←" if abs(delta) > 0.15 else ""
        print(
            f"  {prog:>9.1f}%  {clo_old_sh:>9.3f}  {r.value:>9.3f}  "
            f"{delta:>+11.3f}  {t_rm_s:>7}  {'sì' if s3_active else 'no':>10}{marker}"
        )

    _subsection("Global: old (0.82 fisso) vs new (weighted mean)")
    raw_map: dict = _g(sim_cfg, "room_type_map", {})
    weights: dict = _g(sim_cfg, "zone_weights", {})
    if raw_map and weights:
        cprov = ComfortParameterProvider(
            cfg=CloMetConfig(climate_zone=_g(sim_cfg, "climate_zone", "D")),
            room_type_map={k: _room(v) for k, v in raw_map.items()},
        )
        zone_ids = list(raw_map.keys())
        for prog in [50.0, 90.1]:
            all_p = cprov.compute_all(
                zone_ids=zone_ids,
                operative_season=OperativeSeason.SHOULDER,
                season_progress=prog, shoulder_direction=direction,
                profile=HVACOperatingProfile.ECO,
                time_of_day=TimeOfDay.DAYTIME, cold_snap=False,
                zone_weights={k: float(v) for k, v in weights.items()},
            )
            gclo = all_p["global"].clo.value
            delta_g = gclo - clo_old_sh
            print(
                f"\n  Progress={prog:.1f}%:  "
                f"CLO global old={clo_old_sh:.3f}  new={gclo:.3f}  "
                f"Δ={delta_g:+.3f}"
            )
            zone_clos = {z: all_p[z].clo.value for z in zone_ids}
            print(f"  Zone: " + "  ".join(f"{z}={v:.3f}" for z, v in zone_clos.items()))


# ---------------------------------------------------------------------------
# SIM 08 — Matrice commissioning: appartamento Roma
# ---------------------------------------------------------------------------

def test_sim_08_matrice_commissioning(sim_cfg: dict) -> None:
    """Matrice commissioning: tutte le zone dell'appartamento reale."""
    s         = _s(sim_cfg, "sim_08_matrice_commissioning")
    progress  = float(s.get("shoulder_progress", 90.1))
    direction = s.get("shoulder_direction", "spring")
    profiles  = [_profile(x) for x in s.get("profiles", ["Eco", "Comfort", "Sleep"])]
    t_rm_map  : dict = s.get("t_rm_by_zone", {})
    t_op_map  : dict = s.get("t_op_by_zone", {})
    raw_map   : dict = _g(sim_cfg, "room_type_map", {})
    weights   : dict = _g(sim_cfg, "zone_weights", {})
    zone_d    = _g(sim_cfg, "climate_zone", "D")
    cprov     = ComfortParameterProvider(
        cfg=CloMetConfig(climate_zone=zone_d),
        room_type_map={k: _room(v) for k, v in raw_map.items()},
    )
    zone_ids  = list(raw_map.keys())
    w_float   = {k: float(v) for k, v in weights.items()}
    t_rm_f:dict[str, float | None] | None = {k: float(v) for k, v in t_rm_map.items()}
    t_op_f: dict[str, float | None] | None = {k: float(v) for k, v in t_op_map.items()}

    _section(
        f"SIM 08 — Matrice commissioning Roma  "
        f"(zona {zone_d}, shoulder {progress:.1f}% {direction})"
    )

    for prof in profiles:
        _subsection(f"Profilo: {prof.value}")
        all_p = cprov.compute_all(
            zone_ids=zone_ids,
            operative_season=OperativeSeason.SHOULDER,
            season_progress=progress, shoulder_direction=direction,
            profile=prof,
            time_of_day=TimeOfDay.MORNING if prof == HVACOperatingProfile.SLEEP else TimeOfDay.DAYTIME,
            cold_snap=False,
            zone_weights=w_float,
            t_op_rm_by_zone=t_rm_f,
            t_op_by_zone=t_op_f,
        )

        print(f"\n  {'Zona':<20}  {'RoomType':<12}  {'CLO':>6}  "
              f"{'S4':>7}  {'S3':>7}  {'MET':>6}  {'Peso':>6}")
        print("  " + _SEP[:80])

        for zone_id in zone_ids:
            p = all_p[zone_id]
            w = w_float.get(zone_id, 0.0)
            print(
                f"  {zone_id:<20}  {p.room_type.value:<12}  {p.clo.value:>6.3f}  "
                f"{p.clo.s4_delta:>+7.3f}  {p.clo.s3_delta:>+7.3f}  "
                f"{p.met.value:>6.3f}  {w:>6.1f}"
            )

        gp = all_p["global"]
        print(f"\n  {'GLOBAL (media pesata)':<20}  {'global':<12}  {gp.clo.value:>6.3f}  "
              f"{'---':>7}  {'---':>7}  {gp.met.value:>6.3f}  "
              f"{sum(w_float.values()):>6.1f}")
        print(f"\n  Global CLO: {gp.clo.value:.3f}  (old seed fisso: 0.82  Δ={gp.clo.value-0.82:+.3f})")


# ---------------------------------------------------------------------------
# SCENARIO — Fine maggio Roma (situazione attuale 23/05/2026 09:45)
# ---------------------------------------------------------------------------

def test_scenario_fine_maggio_roma(sim_cfg: dict) -> None:
    """Situazione attuale: 23/05/2026, ore 09:45, profilo ECO, shoulder 90.1%."""
    s         = _s(sim_cfg, "scenario_fine_maggio_roma")
    if not s:
        pytest.skip("scenario_fine_maggio_roma non configurato nel JSON")

    season    = _season(s.get("season", "shoulder"))
    direction = s.get("direction", "spring")
    progress  = float(s.get("progress", 90.1))
    prof      = _profile(s.get("profile", "Eco"))
    hour      = int(s.get("hour", 9))
    cold_snap = bool(s.get("cold_snap", False))
    t_rm_map  : dict = s.get("t_rm_by_zone", {})
    t_op_map  : dict = s.get("t_op_by_zone", {})
    raw_map   : dict = _g(sim_cfg, "room_type_map", {})
    weights   : dict = _g(sim_cfg, "zone_weights", {})
    zone_d    = _g(sim_cfg, "climate_zone", "D")
    tod       = TimeOfDay.from_hour(hour)

    cprov = ComfortParameterProvider(
        cfg=CloMetConfig(climate_zone=zone_d),
        room_type_map={k: _room(v) for k, v in raw_map.items()},
    )
    zone_ids = list(raw_map.keys())
    all_p    = cprov.compute_all(
        zone_ids=zone_ids,
        operative_season=season,
        season_progress=progress, shoulder_direction=direction,
        profile=prof, time_of_day=tod, cold_snap=cold_snap,
        zone_weights={k: float(v) for k, v in weights.items()},
        t_op_rm_by_zone={k: float(v) for k, v in t_rm_map.items()},
        t_op_by_zone={k: float(v) for k, v in t_op_map.items()},
    )

    _section(
        f"SCENARIO — Fine maggio Roma  "
        f"(23/05/2026 ore {hour:02d}:xx, profilo {prof.value}, "
        f"shoulder {progress}% {direction}, tod={tod.value})"
    )

    print(f"\n  Problema rilevato dai log: global CLO=0.82 (seed fisso) → "
          f"cool_sur_global artificialmente alto\n")

    print(f"  {'Zona':<22}  {'T_op':>6}  {'T_rm':>6}  "
          f"{'CLO':>6}  {'MET':>6}  {'source (CLO)':>30}")
    print("  " + _SEP[:90])

    for zone_id in zone_ids:
        p    = all_p[zone_id]
        t_op = float(t_op_map.get(zone_id, 0))
        t_rm = float(t_rm_map.get(zone_id, 0)) if zone_id in t_rm_map else None
        t_rm_s = f"{t_rm:.1f}" if t_rm else "  —"
        print(
            f"  {zone_id:<22}  {t_op:>5.1f}°C  {t_rm_s:>5}°C  "
            f"{p.clo.value:>6.3f}  {p.met.value:>6.3f}  {p.clo.source[:30]}"
        )

    gp = all_p["global"]
    print(
        f"\n  {'GLOBAL (weighted mean)':<22}                 "
        f"{gp.clo.value:>6.3f}  {gp.met.value:>6.3f}"
    )
    print(f"\n  Δ CLO global: {gp.clo.value:.3f} (new) vs 0.82 (old seed fisso)  "
          f"= {gp.clo.value - 0.82:+.3f}")
    print(f"\n  Impatto atteso su cool_sur_global:")
    print(f"    Con CLO=0.82 → t_op_max_global ≈ 24.91°C → surplus = 25.2 - 24.91 = +0.29°C")
    print(f"    Con CLO={gp.clo.value:.3f} → t_op_max_global ≈ 26.0°C (stima) → surplus ≈ 0.0°C")
    print(f"    → Eliminazione del false trigger cooling")


# ---------------------------------------------------------------------------
# SCENARIO — Transizione shoulder → estate
# ---------------------------------------------------------------------------

def test_scenario_transizione_shoulder_estate(sim_cfg: dict) -> None:
    """Evoluzione CLO dalla primavera all'estate: S4 + S3 combinati."""
    s           = _s(sim_cfg, "scenario_transizione_shoulder_estate")
    if not s:
        pytest.skip("scenario_transizione_shoulder_estate non configurato nel JSON")

    direction   = s.get("direction", "spring")
    prog_steps  = [float(x) for x in s.get("progress_steps", list(range(0, 110, 10)))]
    prof        = _profile(s.get("profile", "Eco"))
    tod         = _tod(s.get("tod", "daytime"))
    t_rm_base   = float(s.get("t_rm_base", 22.5))
    t_rm_offset = float(s.get("t_rm_hot_offset", 3.0))
    cold_at     = set(int(x) for x in s.get("cold_snap_at", []))
    raw_map     : dict = _g(sim_cfg, "room_type_map", {})
    weights     : dict = _g(sim_cfg, "zone_weights", {})
    zone_d      = _g(sim_cfg, "climate_zone", "D")

    cprov = ComfortParameterProvider(
        cfg=CloMetConfig(climate_zone=zone_d),
        room_type_map={k: _room(v) for k, v in raw_map.items()},
    )
    zone_ids = list(raw_map.keys())
    w_float  = {k: float(v) for k, v in weights.items()}
    old_seed = 0.82

    _section(
        f"SCENARIO — Transizione shoulder → estate  "
        f"(profilo {prof.value}, {direction}, zona {zone_d})"
    )

    print(f"\n  {'Progress':>10}  {'CLO global new':>15}  {'CLO old seed':>13}  "
          f"{'Δ':>7}  {'T_rm':>7}  cs?")
    print("  " + _SEP[:75])

    for prog in prog_steps:
        t_rm = t_rm_base + t_rm_offset * (prog / 100.0)
        cold = int(prog) in cold_at
        all_p = cprov.compute_all(
            zone_ids=zone_ids,
            operative_season=OperativeSeason.SHOULDER,
            season_progress=prog, shoulder_direction=direction,
            profile=prof, time_of_day=tod, cold_snap=cold,
            zone_weights=w_float,
            t_op_rm_by_zone={z: t_rm for z in zone_ids},
            t_op_by_zone={z: t_rm + 1.0 for z in zone_ids},
        )
        gp    = all_p["global"]
        delta = gp.clo.value - old_seed
        cs_m  = " ❄" if cold else "  "
        print(
            f"  {prog:>9.1f}%  {gp.clo.value:>15.3f}  {old_seed:>13.3f}  "
            f"{delta:>+6.3f}  {t_rm:>6.1f}°C{cs_m}"
        )