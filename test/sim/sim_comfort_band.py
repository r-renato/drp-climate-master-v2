"""Simulazione della comfort band attraverso diversi input.

Scopo
-----
Strumento di simulazione/esplorazione: mostra come la comfort band varia al
variare degli input.  Non contiene assert — nessun test fallisce mai.
Gli input base sono in ``simulate_comfort_band_input.json`` nella stessa
directory; possono essere sovrascritti a riga di comando senza modificare
file su disco.

Utilizzo
--------
    # Esecuzione base (legge il JSON di default)
    python3 -m pytest test/sim/sim_comfort_band.py -s -v

    # File JSON alternativo
    python3 -m pytest test/sim/sim_comfort_band.py -s \\
        --sim-input test/sim/simulate_comfort_band_input.json

    # Override puntuali (ripetibili, senza toccare il file)
    python3 -m pytest test/sim/sim_comfort_band.py -s \\
        --sim-override global.climate_zone=E \\
        --sim-override global.default_t_op=19.5 \\
        --sim-override global.default_rh_pct=65 \\
        --sim-override "sim_06_adaptive_clo_s3.t_rm_values_by_season.winter=[10,14,17,null,24,28]"

    # Solo alcune simulazioni (-k filtra per nome funzione)
    python3 -m pytest test/sim/sim_comfort_band.py -s -k "sim_01 or sim_10"
    python3 -m pytest test/sim/sim_comfort_band.py -s -k "scenario"

Opzioni a riga di comando
--------------------------
  --sim-input PATH
      Sostituisce il file JSON di default.

  --sim-override SEZIONE.CAMPO=VALORE  (ripetibile)
      Applica un override puntuale alla chiave dotted del JSON.
      Il valore è interpretato come JSON (int, float, bool, null, lista);
      se non è JSON valido, viene trattato come stringa nuda.

File di input
-------------
``simulate_comfort_band_input.json`` contiene:
- sezione ``global`` con i default condivisi
- sezioni ``sim_01_...`` … ``sim_10_...`` per le simulazioni base
- sezioni ``scenario_*`` per scenari tematici (rientro vacanza, sleep floor, ecc.)

Campi speciali supportati nelle sezioni
----------------------------------------
  t_op_by_season: { "winter": 21.0, "shoulder": 19.0, "summer": 25.5 }
      Se presente, sovrascrive t_op con il valore per la stagione corrente.
      Usato nelle sim multi-stagione per avere T_op realistica per stagione.

  seasons_config: [ { "season": ..., "profile": ..., "t_op_values": [...] } ]
      Usato in sim_07 per configurare range T_op diversi per stagione.

  t_rm_values_by_season: { "winter": [...], "shoulder": [...], "summer": [...] }
      Usato in sim_06 per range T_rm diversi per stagione.

Struttura simulazioni
---------------------
  sim_01  Stagioni × profilo operativo
  sim_02  Sweep umidità relativa
  sim_03  Sweep step VMC (draft effect)
  sim_04  Sweep zona climatica A→F
  sim_05  Cold snap in mezza stagione
  sim_06  Adaptive CLO S3: sweep running mean T_op
  sim_07  Sweep T_op corrente (dentro/fuori banda)
  sim_08  Confronto living vs non-living
  sim_09  Sequenza temporale warm-up ZoneTrmTracker
  sim_10  Matrice riassuntiva di commissioning

  scenario_rientro_vacanza_invernale   Verifica soppressione S3 post-rientro invernale
  scenario_rientro_vacanza_estiva      Verifica soppressione S3 post-rientro estivo
  scenario_sleep_floor_invernale       Documenta floor SLEEP (criticità P0-B)
  scenario_estate_roma_umida           PMV floor cooling + RH alta estiva (criticità P1-B)
  scenario_shoulder_roma_transitorio   Bande shoulder con T_op transitoria (criticità P2)
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_HERE  = pathlib.Path(__file__).parent
_STUBS = _HERE.parent / "stubs"
if str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

# ---------------------------------------------------------------------------
# Import moduli
# ---------------------------------------------------------------------------

from custom_components.drp_climate_master_v2.plant.decision.comfort_band.calculator import (
    ComfortBandCalculator,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.policy_layer import (
    ComfortPolicyLayer,
    ConfortPolicyConfig,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.model import (
    ClimateZoneIT,
    PolicyContext,
    HumiditySolveMode,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.trm import (
    ZoneTrmTracker,
)
from custom_components.drp_climate_master_v2.plant.decision.comfort_band.config import (
    T_RM_NEUTRAL_BY_SEASON,
    CLO_WINTER_BY_ZONE,
    T_OP_MIN_SLEEP_CAP_SHOULDER_C,
    T_OP_MIN_SLEEP_HEATING_CAP_WINTER_C,
    T_OP_MIN_SLEEP_CAP_T_OUT_C,
)
from custom_components.drp_climate_master_v2.domain.enums import HVACOperatingProfile
from custom_components.drp_climate_master_v2.domain.models.season import OperativeSeason

# ---------------------------------------------------------------------------
# Caricamento e override del JSON di input
# ---------------------------------------------------------------------------

_DEFAULT_INPUT = _HERE / "simulate_comfort_band_input.json"


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    """Carica il file JSON degli input."""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _apply_overrides(cfg: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """Applica override dotted-key=value al dizionario cfg."""
    cfg = copy.deepcopy(cfg)
    for raw in overrides:
        if "=" not in raw:
            raise ValueError(
                f"Override malformato — manca '=': '{raw}'\n"
                f"Formato atteso: sezione.campo=valore  "
                f"(es. global.default_t_op=19.5)"
            )
        dotted_key, _, raw_value = raw.partition("=")
        keys = [k.strip() for k in dotted_key.strip().split(".")]
        if not keys or any(k == "" for k in keys):
            raise ValueError(f"Chiave dotted non valida: '{dotted_key}'")
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


def _build_config(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Costruisce la configurazione finale: file JSON + override CLI."""
    input_opt: str | None = request.config.getoption("--sim-input", default=None)
    json_path = pathlib.Path(input_opt) if input_opt else _DEFAULT_INPUT
    if not json_path.exists():
        raise FileNotFoundError(
            f"File JSON di input non trovato: {json_path}\n"
            f"Specificare --sim-input oppure creare {_DEFAULT_INPUT.name}"
        )
    cfg = _load_json(json_path)
    overrides: list[str] = request.config.getoption("--sim-override", default=[])
    if overrides:
        cfg = _apply_overrides(cfg, overrides)
        print(f"\n  [CONFIG] JSON: {json_path.name}")
        print(f"  [CONFIG] Override applicati ({len(overrides)}):")
        for ov in overrides:
            print(f"    • {ov}")
    return cfg


@pytest.fixture
def sim_cfg(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Fixture che espone la configurazione finale (JSON + override CLI)."""
    return _build_config(request)


# ---------------------------------------------------------------------------
# Conversione da stringa JSON → tipi di dominio
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

_ZONE_MAP: dict[str, ClimateZoneIT] = {z.value: z for z in ClimateZoneIT}


def _season(s: str) -> OperativeSeason:
    v = _SEASON_MAP.get(s.lower())
    if v is None:
        raise ValueError(f"Stagione sconosciuta: '{s}'. Valori ammessi: {list(_SEASON_MAP)}")
    return v


def _profile(s: str) -> HVACOperatingProfile:
    v = _PROFILE_MAP.get(s)
    if v is None:
        raise ValueError(f"Profilo sconosciuto: '{s}'. Valori ammessi: {[p.value for p in HVACOperatingProfile]}")
    return v


def _zone(s: str) -> ClimateZoneIT:
    v = _ZONE_MAP.get(s.upper())
    if v is None:
        raise ValueError(f"Zona climatica sconosciuta: '{s}'. Valori ammessi: {list(_ZONE_MAP)}")
    return v


def _cfg(sim_config: dict[str, Any], sim_key: str) -> dict[str, Any]:
    """Ritorna la sezione del JSON per la simulazione richiesta."""
    return sim_config.get(sim_key, {})


def _g(sim_config: dict[str, Any], key: str, fallback: Any = None) -> Any:
    """Legge un valore dalla sezione 'global' del JSON."""
    return sim_config.get("global", {}).get(key, fallback)


def _t_op_for_season(
    s: dict[str, Any],
    season: OperativeSeason,
    sim_config: dict[str, Any],
    fallback: float = 21.0,
) -> float:
    """Risolve T_op per la stagione corrente.

    Precedenza (crescente):
      1. fallback hardcoded
      2. global.default_t_op
      3. sezione.t_op (scalare, compatibilità v1)
      4. global.t_op_<season> (es. global.t_op_winter)
      5. sezione.t_op_by_season.<season> (massima priorità)
    """
    season_key = season.value.lower()
    t = float(_g(sim_config, "default_t_op", fallback))
    global_key = f"t_op_{season_key}"
    if _g(sim_config, global_key) is not None:
        t = float(_g(sim_config, global_key))
    if "t_op" in s:
        t = float(s["t_op"])
    t_op_by_season: dict = s.get("t_op_by_season", {})
    if season_key in t_op_by_season:
        t = float(t_op_by_season[season_key])
    return t


# ---------------------------------------------------------------------------
# Costante di sessione
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helper: build layer / calc / ctx / run
# ---------------------------------------------------------------------------

def _layer(
    zone: Optional[ClimateZoneIT] = None,
    sim_cfg: Optional[dict[str, Any]] = None,
) -> ComfortPolicyLayer:
    if zone is not None:
        z = zone
    elif sim_cfg is not None:
        z = _zone(_g(sim_cfg, "climate_zone", "D"))
    else:
        z = ClimateZoneIT.D
    cfg = ConfortPolicyConfig(climate_zone=z, zone_clo_delta_enabled=True)
    return ComfortPolicyLayer(cfg)


def _calc() -> ComfortBandCalculator:
    return ComfortBandCalculator()


def _ctx(
    *,
    season: OperativeSeason,
    mode: HVACOperatingProfile,
    room: str,
    rh_pct: float,
    t_op_current: float,
    cold_snap: bool = False,
    t_op_running_mean: Optional[float] = None,
    vmc_speed: int,
) -> PolicyContext:
    return PolicyContext(
        now=_NOW,
        room=room,
        season=season,
        vmc_speed=vmc_speed,
        rh_pct=rh_pct,
        t_op_current=t_op_current,
        mode=mode,
        cold_snap=cold_snap,
        t_op_running_mean=t_op_running_mean,
    )


def _run(
    ctx: PolicyContext,
    *,
    sim_cfg: Optional[dict[str, Any]] = None,
    layer: Optional[ComfortPolicyLayer] = None,
    calc: Optional[ComfortBandCalculator] = None,
    humidity_solve_mode: Optional[HumiditySolveMode] = None,
):
    if layer is None:
        layer = _layer(sim_cfg=sim_cfg)
    if calc is None:
        calc = _calc()
    dec = layer.decide(ctx)
    kwargs: dict[str, Any] = dict(
        vmc_air_speed=ctx.vmc_speed,
        room=ctx.room,
        season=ctx.season,
        rh_pct=ctx.rh_pct,
        t_op_current=ctx.t_op_current,
        policy=dec,
    )
    if humidity_solve_mode is not None:
        kwargs["humidity_solve_mode"] = humidity_solve_mode
    res = calc.compute_single(**kwargs)
    return dec, res


# ---------------------------------------------------------------------------
# Formattatori di output
# ---------------------------------------------------------------------------

_SEP = "─" * 110

_SEASON_LABELS = {
    OperativeSeason.WINTER:   "INVERNO",
    OperativeSeason.SHOULDER: "MEZZA S.",
    OperativeSeason.SUMMER:   "ESTATE",
}


def _section(title: str) -> None:
    print(f"\n{'═'*110}")
    print(f"  {title}")
    print("═" * 110)


def _subsection(title: str) -> None:
    print(f"\n  {title}")
    print("  " + _SEP[:80])


def _row(
    dec: Any,
    res: Any,
    *,
    label: str = "",
    show_reasons: bool = True,
) -> str:
    ok_sym  = "✓" if res.ok is True else ("✗" if res.ok is False else "—")
    pmv_str = f"{res.pmv:+.3f}" if res.pmv is not None else "  —  "
    ppd_str = f"{res.ppd:.1f}%" if res.ppd is not None else "  — "
    band    = f"[{res.t_op_min:5.1f}, {res.t_op_max:5.1f}]"
    width   = res.t_op_max - res.t_op_min
    t_op_s  = f"{res.t_op:.1f}" if res.t_op is not None else "  —"
    line = (
        f"  {label:<28}"
        f"  CLO={dec.clo:.3f}  MET={dec.met:.2f}"
        f"  {band}  Δ={width:.1f}°C"
        f"  T_op={t_op_s}°C"
        f"  PMV={pmv_str}  PPD={ppd_str}  {ok_sym}"
    )
    if show_reasons:
        reasons_short = [r for r in dec.reasons if not r.startswith("humidity")]
        line += f"\n    ↳ {' | '.join(reasons_short)}"
    return line


def _run_trm_sequenza(
    s: dict[str, Any],
    sim_config: dict[str, Any],
    section_title: str,
) -> None:
    """Esegue una simulazione temporale ZoneTrmTracker.

    Estratto in funzione condivisa da sim_09 e dagli scenari scenario_rientro_*.
    """
    season        = _season( s.get("season",  "winter"))
    prof          = _profile(s.get("profile", _g(sim_config, "default_profile", "Comfort")))
    rh_pct        = float(s.get("rh_pct",          _g(sim_config, "default_rh_pct", 50.0)))
    warmup        = int(  s.get("warmup_ticks",     10))
    tau           = float(s.get("tau_hours",        168.0))
    tick_count    = int(  s.get("tick_count",       61))
    tick_interval = int(  s.get("tick_interval_s",  30))
    t_start       = float(s.get("t_op_start",       17.0))
    t_end         = float(s.get("t_op_end",         21.0))
    print_every   = int(  s.get("print_every_n_ticks", 5))
    room          = _g(sim_config, "room_non_living", "camera_1")

    _section(
        f"{section_title}: {tick_count} tick × {tick_interval}s  "
        f"({season.value}, {prof.value})"
    )

    trk   = ZoneTrmTracker(tau_hours=tau, warmup_ticks=warmup)
    layer = _layer(zone=_zone(_g(sim_config, "climate_zone", "D")))
    calc  = _calc()

    print(
        f"\n  warmup_ticks={warmup} | tau={tau:.0f}h | "
        f"T_op: {t_start:.0f}°C → {t_end:.0f}°C in {tick_count - 1} tick"
    )
    print(
        f"\n  {'Tick':>5}  {'T_op':>6}  {'n_upd':>6}  {'T_rm':>8}  "
        f"{'CLO':>6}  {'[min, max]':^15}  {'Δ':>4}  PMV   Stato"
    )
    print("  " + "─" * 95)

    ts = _NOW
    for i in range(tick_count):
        t_op = t_start + (t_end - t_start) * i / max(tick_count - 1, 1)
        trk.update(room, t_op, ts + timedelta(seconds=tick_interval * i))
        t_rm  = trk.get_t_rm(room)
        state = trk.state_for(room)
        n_upd = state.n_updates if state else 0

        if i % print_every != 0 and i != tick_count - 1:
            continue

        ctx = _ctx(season=season, mode=prof, room=room,
                   rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2,
                   t_op_running_mean=t_rm)
        dec, res = _run(ctx, layer=layer, calc=calc)
        pmv_s  = f"{res.pmv:+.3f}" if res.pmv else "  —  "
        t_rm_s = f"{t_rm:.2f}°C" if t_rm is not None else "  warmup"
        stato  = "ATTIVO" if t_rm is not None else f"warm-up ({n_upd}/{warmup})"
        print(
            f"  {i:>5}  {t_op:>6.2f}  {n_upd:>6}  {t_rm_s:>8}  "
            f"{dec.clo:>6.3f}  [{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  "
            f"{res.t_op_max - res.t_op_min:>4.1f}  PMV={pmv_s}  {stato}"
        )


# ===========================================================================
# SIM 01 — Stagioni × profilo operativo
# ===========================================================================

def test_sim_01_stagioni_per_profilo(sim_cfg: dict):
    """Matrice stagione × profilo con T_op realistica per stagione."""
    s        = _cfg(sim_cfg, "sim_01_stagioni_per_profilo")
    seasons  = [_season(x)  for x in s.get("seasons",  ["winter", "shoulder", "summer"])]
    profiles = [_profile(x) for x in s.get("profiles", ["Comfort", "Eco", "Boost", "Sleep", "Away", "Vacation"])]
    room      = _g(sim_cfg, "room_non_living", "camera_1")
    rh_pct    = float(s.get("rh_pct",   _g(sim_cfg, "default_rh_pct", 50.0)))
    vmc_speed = int(  s.get("vmc_speed", _g(sim_cfg, "default_vmc_speed", 2)))

    _section(f"SIM 01 — Stagioni × Profilo operativo  (zona {_g(sim_cfg, 'climate_zone', 'D')}, RH={rh_pct}%, VMC={vmc_speed})")

    for season in seasons:
        t_op = _t_op_for_season(s, season, sim_cfg)
        _subsection(f"Stagione: {_SEASON_LABELS.get(season, season.value.upper())}  T_op={t_op}°C")
        for prof in profiles:
            ctx = _ctx(season=season, mode=prof, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            print(_row(dec, res, label=f"{prof.value.upper():<10}"))


# ===========================================================================
# SIM 02 — Effetto umidità relativa
# ===========================================================================

def test_sim_02_umidita_relativa(sim_cfg: dict):
    """Sweep RH per tutte le stagioni e profili configurati."""
    s         = _cfg(sim_cfg, "sim_02_umidita_relativa")
    seasons   = [_season(x) for x in s.get("seasons", [_g(sim_cfg, "default_season", "winter")])]
    profiles  = [_profile(x) for x in s.get("profiles", [_g(sim_cfg, "default_profile", "Comfort")])]
    vmc_speed = int(s.get("vmc_speed", _g(sim_cfg, "default_vmc_speed", 2)))
    rh_values = s.get("rh_values", [30, 40, 50, 60, 70, 80])
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SIM 02 — Umidità relativa  (VMC={vmc_speed})")

    for season in seasons:
        t_op = _t_op_for_season(s, season, sim_cfg)
        for prof in profiles:
            _subsection(f"Stagione: {_SEASON_LABELS.get(season)}  Profilo: {prof.value}  T_op={t_op}°C")
            for rh in rh_values:
                ctx = _ctx(season=season, mode=prof, room=room,
                           rh_pct=float(rh), t_op_current=t_op, vmc_speed=vmc_speed)
                dec, res = _run(ctx, sim_cfg=sim_cfg, humidity_solve_mode=HumiditySolveMode.RH_CONST)
                print(_row(dec, res, label=f"RH={rh}%", show_reasons=False))


# ===========================================================================
# SIM 03 — Step VMC
# ===========================================================================

def test_sim_03_step_vmc(sim_cfg: dict):
    """Sweep step VMC 0→5 per tutte le stagioni e zone configurate."""
    s       = _cfg(sim_cfg, "sim_03_step_vmc")
    seasons = [_season(x) for x in s.get("seasons", [_g(sim_cfg, "default_season", "winter")])]
    prof    = _profile(s.get("profile", _g(sim_cfg, "default_profile", "Comfort")))
    rh_pct  = float(s.get("rh_pct", _g(sim_cfg, "default_rh_pct", 50.0)))
    zones   = s.get("zones", [
        {"room": _g(sim_cfg, "room_non_living", "camera_1"), "label": "non-living"},
        {"room": _g(sim_cfg, "room_living", "soggiorno"),    "label": "living"},
    ])

    _section(f"SIM 03 — Step VMC 0→5  ({prof.value}, RH={rh_pct}%)")

    for season in seasons:
        t_op = _t_op_for_season(s, season, sim_cfg)
        _subsection(f"Stagione: {_SEASON_LABELS.get(season)}  T_op={t_op}°C")
        for zone_cfg in zones:
            room  = zone_cfg["room"]
            label = zone_cfg.get("label", room)
            print(f"\n    Zona: {label}")
            for speed in range(6):
                ctx = _ctx(season=season, mode=prof, room=room,
                           rh_pct=rh_pct, t_op_current=t_op, vmc_speed=speed)
                dec, res = _run(ctx, sim_cfg=sim_cfg)
                v_info = f"  v_best={res.v_air_best:.3f} m/s  v_hi={res.v_air_hi:.3f} m/s"
                print(_row(dec, res, label=f"VMC step={speed}", show_reasons=False) + v_info)


# ===========================================================================
# SIM 04 — Zona climatica A → F
# ===========================================================================

def test_sim_04_zona_climatica(sim_cfg: dict):
    """Sweep zona climatica A→F: CLO invernale e posizione banda."""
    s      = _cfg(sim_cfg, "sim_04_zona_climatica")
    season = _season( s.get("season",  "winter"))
    prof   = _profile(s.get("profile", _g(sim_cfg, "default_profile", "Comfort")))
    t_op   = float(s.get("t_op", _g(sim_cfg, "default_t_op", 21.0)))
    rh_pct = float(s.get("rh_pct", _g(sim_cfg, "default_rh_pct", 50.0)))
    zones  = s.get("zones", [{"zone": z.value, "gradi_giorno": ""} for z in ClimateZoneIT])
    room   = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SIM 04 — Zona climatica A→F  ({season.value}, {prof.value}, T_op={t_op}°C, RH={rh_pct}%)")

    for z in zones:
        zone_enum  = _zone(z["zone"])
        gdd        = z.get("gradi_giorno", "")
        ctx        = _ctx(season=season, mode=prof, room=room,
                          rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2)
        dec, res   = _run(ctx, layer=_layer(zone_enum), sim_cfg=sim_cfg)
        clo_table  = CLO_WINTER_BY_ZONE.get(zone_enum)
        label      = f"Zona {z['zone']} ({gdd} GG)" if gdd else f"Zona {z['zone']}"
        suffix     = f"  (CLO tabella={clo_table:.2f})" if clo_table else ""
        print(_row(dec, res, label=label, show_reasons=False) + suffix)


# ===========================================================================
# SIM 05 — Cold snap
# ===========================================================================

def test_sim_05_cold_snap(sim_cfg: dict):
    """Cold snap in mezza stagione con T_op transitoria realistica."""
    s              = _cfg(sim_cfg, "sim_05_cold_snap")
    primary_season = _season( s.get("primary_season", "shoulder"))
    prof           = _profile(s.get("profile", _g(sim_cfg, "default_profile", "Comfort")))
    t_op           = float(s.get("t_op", _g(sim_cfg, "default_t_op", 21.0)))
    rh_pct         = float(s.get("rh_pct", _g(sim_cfg, "default_rh_pct", 50.0)))
    no_effect      = [_season(x) for x in s.get("verify_no_effect_seasons", ["summer", "winter"])]
    room           = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SIM 05 — Cold snap  ({primary_season.value}, {prof.value}, T_op={t_op}°C, RH={rh_pct}%)")

    for cold_snap in [False, True]:
        ctx = _ctx(season=primary_season, mode=prof, room=room,
                   rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2,
                   cold_snap=cold_snap)
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        print(_row(dec, res, label=f"cold_snap={'SÌ' if cold_snap else 'NO':<3}"))

    if no_effect:
        print()
        print("  Verifica: cold_snap non ha effetto in queste stagioni:")
        for season in no_effect:
            for cold_snap in [False, True]:
                ctx = _ctx(season=season, mode=prof, room=room,
                           rh_pct=rh_pct, t_op_current=t_op,
                           vmc_speed=2, cold_snap=cold_snap)
                dec, res = _run(ctx, sim_cfg=sim_cfg)
                label = f"{season.value:<8} cold_snap={'SÌ' if cold_snap else 'NO'}"
                print(_row(dec, res, label=label, show_reasons=False))


# ===========================================================================
# SIM 06 — Adaptive CLO S3
# ===========================================================================

def test_sim_06_adaptive_clo_s3(sim_cfg: dict):
    """Sweep T_rm per tutte le stagioni con T_op e range T_rm stagionali."""
    s        = _cfg(sim_cfg, "sim_06_adaptive_clo_s3")
    seasons  = [_season(x) for x in s.get("seasons", ["winter"])]
    rh_pct   = float(s.get("rh_pct", _g(sim_cfg, "default_rh_pct", 50.0)))
    profiles = [_profile(x) for x in s.get("profiles_to_compare", ["Comfort", "Eco", "Sleep", "Away"])]
    t_rm_cold = float(s.get("t_rm_cold_for_profile_comparison", 17.0))
    room      = _g(sim_cfg, "room_non_living", "camera_1")
    prof_comfort = _profile(_g(sim_cfg, "default_profile", "Comfort"))

    _section(f"SIM 06 — Adaptive CLO S3  (zona {_g(sim_cfg, 'climate_zone', 'D')}, RH={rh_pct}%)")

    t_rm_values_by_season: dict = s.get("t_rm_values_by_season", {})
    # Compatibilità v1: se presente t_rm_values flat, usalo per la prima stagione
    t_rm_values_flat: list = s.get("t_rm_values", [])

    for season in seasons:
        t_op    = _t_op_for_season(s, season, sim_cfg)
        neutral = T_RM_NEUTRAL_BY_SEASON.get(season.value.lower(), 21.5)

        # Risolvi t_rm_values per stagione
        season_key = season.value.lower()
        raw_vals = t_rm_values_by_season.get(season_key, t_rm_values_flat) or \
                   [14.0, 16.0, 17.5, 19.0, None, 22.0, 24.0, 26.0]
        t_rm_values = [neutral if v is None else float(v) for v in raw_vals]

        _subsection(f"Stagione: {_SEASON_LABELS.get(season)}  T_op={t_op}°C  neutro={neutral:.1f}°C")
        print(f"  {'T_rm':>7}  {'vs neutro':>10}  {'CLO':>6}  {'banda':^15}  {'Δmin vs base':>13}  PMV")

        ctx_base = _ctx(season=season, mode=prof_comfort, room=room,
                        rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2,
                        t_op_running_mean=None)
        dec_base, res_base = _run(ctx_base, sim_cfg=sim_cfg)
        min_base = res_base.t_op_min

        for t_rm in t_rm_values:
            ctx = _ctx(season=season, mode=prof_comfort, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2,
                       t_op_running_mean=t_rm)
            dec, res  = _run(ctx, sim_cfg=sim_cfg)
            delta_t   = t_rm - neutral
            delta_min = res.t_op_min - min_base
            pmv_s     = f"{res.pmv:+.3f}" if res.pmv is not None else "  —  "
            marker    = " ← neutro" if abs(delta_t) < 0.01 else ""
            print(
                f"  {t_rm:>6.1f}°C  {delta_t:>+9.1f}°C  {dec.clo:>6.3f}  "
                f"[{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  "
                f"Δmin={delta_min:>+6.2f}°C        PMV={pmv_s}{marker}"
            )

        print(f"\n  baseline (T_rm=None): CLO={dec_base.clo:.3f}  banda=[{min_base:.1f},{res_base.t_op_max:.1f}]")

        _subsection(f"Confronto profili con T_rm={t_rm_cold:.1f}°C — SLEEP/AWAY devono ignorare S3")
        for prof in profiles:
            ctx = _ctx(season=season, mode=prof, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=2,
                       t_op_running_mean=t_rm_cold)
            dec, res  = _run(ctx, sim_cfg=sim_cfg)
            s3_active = any("trm" in r for r in dec.reasons)
            print(
                _row(dec, res, label=f"{prof.value:<10}", show_reasons=False)
                + f"  S3={'attiva' if s3_active else 'SKIP  '}"
            )


# ===========================================================================
# SIM 07 — Sweep T_op corrente
# ===========================================================================

def test_sim_07_t_op_sweep(sim_cfg: dict):
    """Sweep T_op per tutte le stagioni con range centrati sulla T_op realistica."""
    s    = _cfg(sim_cfg, "sim_07_t_op_sweep")
    room = _g(sim_cfg, "room_non_living", "camera_1")

    # Supporta sia seasons_config (v2) sia singola stagione (v1)
    seasons_config: list[dict] = s.get("seasons_config", [])
    if not seasons_config:
        # Compatibilità v1: singola stagione
        seasons_config = [{
            "season":      s.get("season", _g(sim_cfg, "default_season", "winter")),
            "profile":     s.get("profile", _g(sim_cfg, "default_profile", "Comfort")),
            "t_op_values": s.get("t_op_values", [16.0, 18.0, 19.5, 20.5, 21.0, 21.5, 22.5, 23.5, 25.0, 27.0]),
        }]

    rh_pct    = float(s.get("rh_pct",    _g(sim_cfg, "default_rh_pct", 50.0)))
    vmc_speed = int(  s.get("vmc_speed", _g(sim_cfg, "default_vmc_speed", 2)))

    _section(f"SIM 07 — Sweep T_op corrente  (RH={rh_pct}%, VMC={vmc_speed})")

    for sc in seasons_config:
        season    = _season(sc["season"])
        prof      = _profile(sc.get("profile", _g(sim_cfg, "default_profile", "Comfort")))
        t_op_vals = [float(x) for x in sc["t_op_values"]]

        _subsection(f"Stagione: {_SEASON_LABELS.get(season)}  Profilo: {prof.value}")

        pivot = t_op_vals[len(t_op_vals) // 2]
        ctx_band = _ctx(season=season, mode=prof, room=room,
                        rh_pct=rh_pct, t_op_current=pivot, vmc_speed=vmc_speed)
        dec_band, res_band = _run(ctx_band, sim_cfg=sim_cfg)
        t_min, t_max = res_band.t_op_min, res_band.t_op_max
        print(f"\n  Banda a T_op={pivot}°C: [{t_min:.1f}, {t_max:.1f}]°C  CLO={dec_band.clo:.3f}  MET={dec_band.met:.2f}")
        print(f"  {'T_op':>7}  {'PMV':>8}  {'PPD%':>6}  {'OK':>4}  Posizione relativa")
        print("  " + "─" * 65)

        for t_op in t_op_vals:
            ctx = _ctx(season=season, mode=prof, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            ok_sym = "✓" if res.ok else "✗"
            pmv_s  = f"{res.pmv:+.3f}" if res.pmv else "  —  "
            ppd_s  = f"{res.ppd:.1f}"  if res.ppd else "  — "
            if t_op < t_min:
                pos = f"freddo  ({t_op - t_min:.1f}°C sotto min)"
            elif t_op > t_max:
                pos = f"caldo   (+{t_op - t_max:.1f}°C sopra max)"
            else:
                pos = f"DENTRO  (↑{t_op - t_min:.1f}°C  ↓{t_max - t_op:.1f}°C dai bordi)"
            print(f"  {t_op:>6.1f}°C  {pmv_s:>8}  {ppd_s:>6}  {ok_sym:>4}  {pos}")


# ===========================================================================
# SIM 08 — Living vs non-living
# ===========================================================================

def test_sim_08_living_vs_non_living(sim_cfg: dict):
    """Confronto v_air per tutte le stagioni su zone living e non-living."""
    s       = _cfg(sim_cfg, "sim_08_living_vs_non_living")
    seasons = [_season(x) for x in s.get("seasons", [_g(sim_cfg, "default_season", "winter")])]
    prof    = _profile(s.get("profile", _g(sim_cfg, "default_profile", "Comfort")))
    rh_pct  = float(s.get("rh_pct", _g(sim_cfg, "default_rh_pct", 50.0)))
    zones   = s.get("zones", [
        {"room": _g(sim_cfg, "room_non_living", "camera_1"), "label": "camera_1 (non-liv)"},
        {"room": _g(sim_cfg, "room_living", "soggiorno"),    "label": "soggiorno (living)"},
    ])

    _section(f"SIM 08 — Living vs non-living  ({prof.value}, RH={rh_pct}%)")

    for season in seasons:
        t_op = _t_op_for_season(s, season, sim_cfg)
        _subsection(f"Stagione: {_SEASON_LABELS.get(season)}  T_op={t_op}°C")
        print(
            f"\n  {'VMC':>4}  {'Zona':<20}  {'CLO':>6}  {'[min, max]':^15}  {'Δ':>4}  "
            f"{'v_best':>7}  {'v_hi':>7}  {'v_draft':>8}  PMV"
        )
        print("  " + "─" * 100)
        for speed in range(6):
            for zone_cfg in zones:
                room  = zone_cfg["room"]
                label = zone_cfg.get("label", room)
                ctx   = _ctx(season=season, mode=prof, room=room,
                             rh_pct=rh_pct, t_op_current=t_op, vmc_speed=speed)
                dec, res = _run(ctx, sim_cfg=sim_cfg)
                pmv_s = f"{res.pmv:+.3f}" if res.pmv else "  —  "
                print(
                    f"  {speed:>4}  {label:<20}  {dec.clo:>6.3f}  "
                    f"[{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  "
                    f"{res.t_op_max - res.t_op_min:>4.1f}  "
                    f"{res.v_air_best:>7.3f}  {res.v_air_hi:>7.3f}  "
                    f"{res.v_air_draft:>8.4f}  PMV={pmv_s}"
                )
            print()


# ===========================================================================
# SIM 09 — Warm-up ZoneTrmTracker
# ===========================================================================

def test_sim_09_trm_warmup_sequenza(sim_cfg: dict):
    """Warm-up tracker S3 a tick reali (30s). Scenario: prima accensione stagionale."""
    s = _cfg(sim_cfg, "sim_09_trm_warmup_sequenza")
    _run_trm_sequenza(s, sim_cfg, "SIM 09 — Warm-up ZoneTrmTracker")


# ===========================================================================
# SIM 10 — Matrice riassuntiva di commissioning
# ===========================================================================

def test_sim_10_matrice_profili_stagioni(sim_cfg: dict):
    """Matrice compatta di commissioning con T_op realistica per stagione."""
    s         = _cfg(sim_cfg, "sim_10_matrice_profili_stagioni")
    seasons   = [_season(x)  for x in s.get("seasons",  ["winter", "shoulder", "summer"])]
    profiles  = [_profile(x) for x in s.get("profiles", ["Comfort", "Eco", "Boost", "Sleep", "Away", "Vacation"])]
    rh_pct    = float(s.get("rh_pct",    _g(sim_cfg, "default_rh_pct", 50.0)))
    vmc_speed = int(  s.get("vmc_speed", _g(sim_cfg, "default_vmc_speed", 2)))
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    # Intestazione con T_op per stagione
    t_ops = {season: _t_op_for_season(s, season, sim_cfg) for season in seasons}
    header_note = "  ".join(f"{_SEASON_LABELS[season]}={t_ops[season]:.1f}°C" for season in seasons)
    _section(f"SIM 10 — Matrice commissioning  (zona {_g(sim_cfg, 'climate_zone', 'D')}, RH={rh_pct}%, VMC={vmc_speed})")
    print(f"\n  T_op per stagione: {header_note}")

    COL = 38
    print(f"\n  {'':18}", end="")
    for season in seasons:
        print(f"  {_SEASON_LABELS.get(season, season.value[:8]):^{COL}}", end="")
    print()

    sub = f"{'CLO':>5}  {'[min,max]':^13}  {'Δ':>3}  PMV"
    print(f"  {'PROFILO':<18}", end="")
    for _ in seasons:
        print(f"  {sub:^{COL}}", end="")
    print()
    print("  " + "─" * (18 + (COL + 2) * len(seasons)))

    for prof in profiles:
        print(f"  {prof.value:<18}", end="")
        for season in seasons:
            t_op = t_ops[season]
            ctx  = _ctx(season=season, mode=prof, room=room,
                        rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            pmv_s = f"{res.pmv:+.2f}" if res.pmv is not None else "  — "
            cell  = (
                f"{dec.clo:>5.2f}  "
                f"[{res.t_op_min:4.1f},{res.t_op_max:4.1f}]  "
                f"{res.t_op_max - res.t_op_min:>3.1f}  {pmv_s}"
            )
            print(f"  {cell:^{COL}}", end="")
        print()

    print(f"\n  Legenda: CLO=clo | banda=[T_min,T_max]°C | Δ=larghezza (°C) | PMV=voto comfort")
    print(f"  PMV calcolato con T_op stagionale — non confrontabile tra stagioni diverse.")


# ===========================================================================
# SCENARI TEMATICI
# ===========================================================================

def test_scenario_rientro_vacanza_invernale(sim_cfg: dict):
    """Rientro da vacanza invernale: verifica soppressione S3 per deviazione eccessiva."""
    s = _cfg(sim_cfg, "scenario_rientro_vacanza_invernale")
    if not s:
        pytest.skip("scenario_rientro_vacanza_invernale non configurato nel JSON")
    _run_trm_sequenza(s, sim_cfg, "SCENARIO — Rientro vacanza invernale")


def test_scenario_rientro_vacanza_estiva(sim_cfg: dict):
    """Rientro da vacanza estiva: verifica soppressione S3 lato caldo."""
    s = _cfg(sim_cfg, "scenario_rientro_vacanza_estiva")
    if not s:
        pytest.skip("scenario_rientro_vacanza_estiva non configurato nel JSON")
    _run_trm_sequenza(s, sim_cfg, "SCENARIO — Rientro vacanza estiva")


def test_scenario_sleep_floor_invernale(sim_cfg: dict):
    """Floor SLEEP invernale: documenta banda [15.6, 20.4]°C pre/post fix T_OP_MIN_SLEEP_FLOOR_C."""
    s = _cfg(sim_cfg, "scenario_sleep_floor_invernale")
    if not s:
        pytest.skip("scenario_sleep_floor_invernale non configurato nel JSON")

    season    = _season( s.get("season",  "winter"))
    prof      = _profile(s.get("profile", "Sleep"))
    rh_pct    = float(s.get("rh_pct",    _g(sim_cfg, "default_rh_pct", 50.0)))
    vmc_speed = int(  s.get("vmc_speed", 1))
    t_op_vals = [float(x) for x in s.get("t_op_values", [14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0])]
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SCENARIO — Sleep floor invernale  ({season.value}, {prof.value}, RH={rh_pct}%, VMC={vmc_speed})")

    pivot = t_op_vals[len(t_op_vals) // 2]
    ctx_band = _ctx(season=season, mode=prof, room=room,
                    rh_pct=rh_pct, t_op_current=pivot, vmc_speed=vmc_speed)
    dec_band, res_band = _run(ctx_band, sim_cfg=sim_cfg)
    print(f"\n  Banda a T_op={pivot}°C: [{res_band.t_op_min:.1f}, {res_band.t_op_max:.1f}]°C  CLO={dec_band.clo:.3f}")
    print(f"  {'T_op':>7}  {'PMV':>8}  {'PPD%':>6}  {'OK':>4}  Note")
    print("  " + "─" * 65)

    for t_op in t_op_vals:
        ctx = _ctx(season=season, mode=prof, room=room,
                   rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        ok_sym = "✓" if res.ok else "✗"
        pmv_s  = f"{res.pmv:+.3f}" if res.pmv else "  —  "
        ppd_s  = f"{res.ppd:.1f}"  if res.ppd else "  — "
        note   = "⚠ SOTTO FLOOR 17°C" if t_op < 17.0 else ""
        print(f"  {t_op:>6.1f}°C  {pmv_s:>8}  {ppd_s:>6}  {ok_sym:>4}  {note}")


def test_scenario_estate_roma_umida(sim_cfg: dict):
    """Luglio-agosto Roma: PMV floor cooling + sensitività banda a RH alta estiva."""
    s = _cfg(sim_cfg, "scenario_estate_roma_umida")
    if not s:
        pytest.skip("scenario_estate_roma_umida non configurato nel JSON")

    season    = _season(s.get("season", "summer"))
    profiles  = [_profile(x) for x in s.get("profiles", ["Comfort", "Sleep"])]
    rh_pct    = float(s.get("rh_pct", 65.0))
    vmc_speed = int(  s.get("vmc_speed", 2))
    t_op_vals = [float(x) for x in s.get("t_op_values", [21.0, 23.0, 25.5, 27.0, 29.0])]
    rh_values = s.get("rh_values", [50, 60, 65, 70, 75, 80])
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SCENARIO — Estate Roma umida  ({season.value}, RH base={rh_pct}%, VMC={vmc_speed})")

    # Parte 1: sweep T_op per ogni profilo a RH fissa
    _subsection("Sweep T_op per profilo (RH fissa)")
    for prof in profiles:
        print(f"\n    Profilo: {prof.value}")
        for t_op in t_op_vals:
            ctx = _ctx(season=season, mode=prof, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            pmv_s  = f"{res.pmv:+.3f}" if res.pmv else "  —  "
            ok_sym = "✓" if res.ok else "✗"
            note   = "⚠ PMV<-1.5" if res.pmv is not None and res.pmv < -1.5 else ""
            print(f"    T_op={t_op:.1f}°C  PMV={pmv_s}  {ok_sym}  {note}")

    # Parte 2: sweep RH a T_op fissa per Comfort
    _subsection(f"Sweep RH (T_op={t_op_vals[len(t_op_vals)//2]:.1f}°C, Comfort)")
    pivot_t_op = t_op_vals[len(t_op_vals) // 2]
    prof_comfort = _profile("Comfort")
    for rh in rh_values:
        ctx = _ctx(season=season, mode=prof_comfort, room=room,
                   rh_pct=float(rh), t_op_current=pivot_t_op, vmc_speed=vmc_speed)
        dec, res = _run(ctx, sim_cfg=sim_cfg, humidity_solve_mode=HumiditySolveMode.RH_CONST)
        print(_row(dec, res, label=f"RH={rh}%", show_reasons=False))


def test_scenario_shoulder_roma_transitorio(sim_cfg: dict):
    """Mezza stagione Roma: bande con T_op transitoria, cold snap, S3."""
    s = _cfg(sim_cfg, "scenario_shoulder_roma_transitorio")
    if not s:
        pytest.skip("scenario_shoulder_roma_transitorio non configurato nel JSON")

    season    = _season(s.get("season", "shoulder"))
    profiles  = [_profile(x) for x in s.get("profiles", ["Comfort", "Eco", "Boost", "Sleep", "Away", "Vacation"])]
    rh_pct    = float(s.get("rh_pct", 55.0))
    vmc_speed = int(  s.get("vmc_speed", 2))
    t_op_vals = [float(x) for x in s.get("t_op_values", [17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0])]
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    _section(f"SCENARIO — Shoulder Roma transitorio  ({season.value}, RH={rh_pct}%, VMC={vmc_speed})")

    # Parte 1: matrice profili × T_op
    _subsection("Sweep T_op per profilo")
    for prof in profiles:
        print(f"\n    Profilo: {prof.value}")
        for t_op in t_op_vals:
            ctx = _ctx(season=season, mode=prof, room=room,
                       rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed)
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            ok_sym = "✓" if res.ok else "✗"
            pmv_s  = f"{res.pmv:+.3f}" if res.pmv else "  —  "
            print(f"    T_op={t_op:.1f}°C  CLO={dec.clo:.3f}  PMV={pmv_s}  {ok_sym}")

    # Parte 2: cold snap a T_op transitoria
    cold_snap_t_op   = float(s.get("cold_snap_t_op", 18.0))
    cold_snap_rh_pct = float(s.get("cold_snap_rh_pct", 60.0))
    prof_comfort     = _profile("Comfort")
    _subsection(f"Cold snap a T_op={cold_snap_t_op}°C, RH={cold_snap_rh_pct}%")
    for cold_snap in [False, True]:
        ctx = _ctx(season=season, mode=prof_comfort, room=room,
                   rh_pct=cold_snap_rh_pct, t_op_current=cold_snap_t_op,
                   vmc_speed=vmc_speed, cold_snap=cold_snap)
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        print(_row(dec, res, label=f"cold_snap={'SÌ' if cold_snap else 'NO':<3}"))

    # Parte 3: sweep T_rm S3 in shoulder
    neutral   = T_RM_NEUTRAL_BY_SEASON.get("shoulder", 22.5)
    raw_vals  = s.get("t_rm_values", [14.0, 16.0, 18.0, None, 23.0, 25.0, 27.0])
    t_rm_vals = [neutral if v is None else float(v) for v in raw_vals]
    pivot_t_op_s3 = _t_op_for_season(s, season, sim_cfg)
    _subsection(f"Adaptive CLO S3 in shoulder  T_op={pivot_t_op_s3}°C  neutro={neutral:.1f}°C")
    print(f"  {'T_rm':>7}  {'vs neutro':>10}  {'CLO':>6}  {'banda':^15}  PMV")
    for t_rm in t_rm_vals:
        ctx = _ctx(season=season, mode=prof_comfort, room=room,
                   rh_pct=rh_pct, t_op_current=pivot_t_op_s3, vmc_speed=vmc_speed,
                   t_op_running_mean=t_rm)
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        delta_t = t_rm - neutral
        pmv_s   = f"{res.pmv:+.3f}" if res.pmv is not None else "  —  "
        print(
            f"  {t_rm:>6.1f}°C  {delta_t:>+9.1f}°C  {dec.clo:>6.3f}  "
            f"[{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  PMV={pmv_s}"
        )

def test_scenario_cold_snap_s3_combinati(sim_cfg: dict):
    """Cold snap + S3 combinati in shoulder: interazione tra le due correzioni CLO."""
    s = _cfg(sim_cfg, "scenario_cold_snap_s3_combinati")
    if not s:
        pytest.skip("scenario_cold_snap_s3_combinati non configurato nel JSON")

    season    = _season( s.get("season",  "shoulder"))
    prof_base = _profile(s.get("profile", "Comfort"))
    t_op      = float(s.get("t_op",    19.0))
    rh_pct    = float(s.get("rh_pct",  55.0))
    vmc_speed = int(  s.get("vmc_speed", 2))
    profiles  = [_profile(x) for x in s.get("profiles_to_compare",
                 ["Comfort", "Eco", "Sleep", "Away"])]
    room      = _g(sim_cfg, "room_non_living", "camera_1")

    neutral   = T_RM_NEUTRAL_BY_SEASON.get(season.value.lower(), 22.5)
    raw_vals  = s.get("t_rm_values", [None, 14.0, 16.0, 17.5, 19.0, 22.5])
    t_rm_vals = [neutral if v is None else float(v) for v in raw_vals]
    cold_snap_values: list[bool] = s.get("cold_snap_values", [False, True])

    _section(
        f"SCENARIO — Cold snap + S3 combinati  "
        f"({season.value}, T_op={t_op}°C, RH={rh_pct}%)"
    )

    # ------------------------------------------------------------------
    # Parte 1: sweep T_rm × cold_snap per profilo Comfort
    # Mostra la CLO risultante per ogni combinazione
    # ------------------------------------------------------------------
    _subsection(
        f"Parte 1 — Sweep T_rm × cold_snap  "
        f"(profilo {prof_base.value}, neutro={neutral:.1f}°C)"
    )
    print(
        f"\n  {'T_rm':>8}  {'cold_snap':>10}  {'CLO':>6}  "
        f"{'banda':^15}  {'PMV':>8}  {'OK':>3}  Reasons (CLO)"
    )
    print("  " + "─" * 100)

    for t_rm in t_rm_vals:
        for cold_snap in cold_snap_values:
            ctx = _ctx(
                season=season, mode=prof_base, room=room,
                rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed,
                cold_snap=cold_snap, t_op_running_mean=t_rm,
            )
            dec, res = _run(ctx, sim_cfg=sim_cfg)
            ok_sym  = "✓" if res.ok else "✗"
            pmv_s   = f"{res.pmv:+.3f}" if res.pmv is not None else "  —  "
            t_rm_s  = f"{t_rm:.1f}°C" if t_rm is not None else f"{neutral:.1f}°C(N)"
            cs_s    = "SÌ " if cold_snap else "NO "
            # Estrai solo i reasons relativi al CLO
            clo_reasons = [r for r in dec.reasons if r.startswith("clo:")]
            reasons_str = " | ".join(clo_reasons)
            print(
                f"  {t_rm_s:>8}  {cs_s:>10}  {dec.clo:>6.3f}  "
                f"[{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  "
                f"{pmv_s:>8}  {ok_sym:>3}  {reasons_str}"
            )
        print()  # riga vuota tra T_rm diversi

    # ------------------------------------------------------------------
    # Parte 2: caso peggiore — cold_snap=True + T_rm più bassa S3-attiva
    # Confronto tutti i profili
    # ------------------------------------------------------------------
    # T_rm più bassa con S3 attiva: prima sotto la soglia di soppressione
    # con cold_snap=True attivo
    threshold = float(t_op) - 4.0   # T_RM_MAX_DEVIATION_SUPPRESS_C=4.0
    t_rm_s3_active = next(
        (t for t in sorted(t_rm_vals) if t is not None and t >= threshold),
        t_rm_vals[-1],
    )

    _subsection(
        f"Parte 2 — Tutti i profili con cold_snap=SÌ + T_rm={t_rm_s3_active:.1f}°C "
        f"(S3 attiva, deviazione={abs(t_op - t_rm_s3_active):.1f}°C < 4°C)"
    )
    for prof in profiles:
        ctx = _ctx(
            season=season, mode=prof, room=room,
            rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed,
            cold_snap=True, t_op_running_mean=t_rm_s3_active,
        )
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        s3_active   = any("trm" in r for r in dec.reasons)
        cs_active   = any("cold_snap" in r for r in dec.reasons)
        flags = []
        if cs_active: flags.append("cold_snap")
        if s3_active: flags.append("S3")
        flag_str = "+".join(flags) if flags else "nessuna"
        print(
            _row(dec, res, label=f"{prof.value:<10}", show_reasons=False)
            + f"  correzioni={flag_str}"
        )

    # ------------------------------------------------------------------
    # Parte 3: confronto baseline vs cold_snap vs S3 vs entrambi
    # Solo profilo Comfort, T_rm=16°C (S3 attiva)
    # ------------------------------------------------------------------
    t_rm_ref = next(
        (t for t in t_rm_vals if t is not None and abs(t - (threshold + 1.0)) < 1.5),
        t_rm_vals[1] if len(t_rm_vals) > 1 else t_rm_vals[0],
    )
    _subsection(
        f"Parte 3 — Isolamento effetti  "
        f"(profilo Comfort, T_rm={t_rm_ref:.1f}°C)"
    )
    cases = [
        ("baseline",            False, None),
        ("solo cold_snap",      True,  None),
        ("solo S3",             False, t_rm_ref),
        ("cold_snap + S3",      True,  t_rm_ref),
    ]
    print(f"\n  {'Caso':<22}  {'CLO':>6}  {'banda':^15}  {'PMV':>8}  {'Δ CLO vs base':>14}")
    print("  " + "─" * 80)

    clo_base = None
    for label, cold_snap, t_rm in cases:
        ctx = _ctx(
            season=season, mode=prof_base, room=room,
            rh_pct=rh_pct, t_op_current=t_op, vmc_speed=vmc_speed,
            cold_snap=cold_snap, t_op_running_mean=t_rm,
        )
        dec, res = _run(ctx, sim_cfg=sim_cfg)
        if clo_base is None:
            clo_base = dec.clo
        delta = dec.clo - clo_base
        pmv_s = f"{res.pmv:+.3f}" if res.pmv is not None else "  —  "
        delta_s = f"{delta:+.3f}" if label != "baseline" else "    —  "
        print(
            f"  {label:<22}  {dec.clo:>6.3f}  "
            f"[{res.t_op_min:5.1f},{res.t_op_max:5.1f}]  "
            f"{pmv_s:>8}  {delta_s:>14}"
        )


def test_scenario_sleep_cap_stagionale(sim_cfg: dict):
    """Documenta l'effetto del cap t_op_min per RISCALDAMENTO Sleep (Patch 0016).

    Scopo
    -----
    Verifica che _apply_sleep_shoulder_cap e _apply_sleep_winter_cap
    (attivi in compute_many) riducano t_op_min rispetto al valore grezzo del
    bisettore PMV, eliminando la domanda di riscaldamento artificiosa prodotta
    da met=0.70 fuori dominio ISO 7730.

    Chiama compute_many con ZoneSnapshot mock (SimpleNamespace con attributo
    .value), che è il pattern minimo sufficiente per _extract_zone_inputs.

    Scenari
    -------
    1. Shoulder + outdoor_temp >= 8°C  → cap a 20°C attivo
       Replica la situazione del 17/05 mattina: stanze 22-23°C, notte 11.6°C.
    2. Shoulder + outdoor_temp < 8°C   → cap inattivo (notte autunnale fredda)
    3. Winter                           → cap a 21°C attivo
    4. Summer                           → nessun cap (not applicable)
    5. Riepilogo stagionale: deficit PMV grezzo vs effettivo post-cap.
    """
    from types import SimpleNamespace

    s = _cfg(sim_cfg, "scenario_sleep_cap_stagionale")
    rh_pct               = float(s.get("rh_pct",    51.0))
    vmc_speed            = int(  s.get("vmc_speed",  2))
    t_op_shoulder        = float(s.get("t_op_shoulder", 22.7))
    t_op_winter          = float(s.get("t_op_winter",   21.0))
    t_out_shoulder_night = float(s.get("t_out_shoulder_night", 11.6))
    t_out_autumn_cold    = float(s.get("t_out_autumn_cold",     7.0))
    winter_t_ops         = [float(x) for x in s.get(
        "winter_t_op_values", [18.0, 19.0, 20.0, 21.0, 22.0, 23.0]
    )]
    room = _g(sim_cfg, "room_non_living", "camera_1")

    _section(
        f"SCENARIO — Sleep cap stagionale RISCALDAMENTO (Patch 0016)  "
        f"(RH={rh_pct}%, VMC={vmc_speed})"
    )

    # ------------------------------------------------------------------
    # Helper: ZoneSnapshot mock minimale (solo campi letti da compute_many)
    # ------------------------------------------------------------------
    def _mock_zone(t_op_val: float, rh_val: float) -> SimpleNamespace:
        class _V:
            def __init__(self, v):
                self.value = v
        return SimpleNamespace(
            humidity=_V(rh_val),
            t_op=_V(t_op_val),
            temperature=_V(t_op_val),
            mrt=None,
        )

    # ------------------------------------------------------------------
    # Helper: compute_many con cap Sleep attivo
    # ------------------------------------------------------------------
    def _run_many(t_op_val: float, season: OperativeSeason, outdoor_temp: Optional[float]):
        layer_obj = _layer(sim_cfg=sim_cfg)
        calc_obj  = _calc()
        results = calc_obj.compute_many(
            now=datetime(2025, 5, 17, 6, 0, 0, tzinfo=timezone.utc),
            season=season,
            vmc_air_speed=vmc_speed,
            indoor_zones={room: _mock_zone(t_op_val, rh_pct)},
            outdoor_temp=outdoor_temp,
            mode=HVACOperatingProfile.SLEEP,
            policy_layer=layer_obj,
            room_names=[room],
            include_global=False,
        )
        return results.get(room)

    # ------------------------------------------------------------------
    # Parte 1 — Shoulder: cap attivo (t_out >= 8°C) vs inattivo (t_out < 8°C)
    # ------------------------------------------------------------------
    _subsection(
        f"Parte 1 — Shoulder: effetto t_out sul cap  "
        f"(T_op={t_op_shoulder}°C, cap={T_OP_MIN_SLEEP_CAP_SHOULDER_C}°C, "
        f"soglia t_out={T_OP_MIN_SLEEP_CAP_T_OUT_C}°C)"
    )
    print(
        f"\n  {'Caso':<38}  {'t_op_min PMV':>12}  "
        f"{'t_op_min eff':>12}  {'deficit eff':>11}  {'cap':>8}"
    )
    print("  " + "─" * 90)

    shoulder_cases = [
        (f"notte maggio  t_out={t_out_shoulder_night}°C",
         t_out_shoulder_night, OperativeSeason.SHOULDER),
        (f"notte novembre t_out={t_out_autumn_cold}°C",
         t_out_autumn_cold,    OperativeSeason.SHOULDER),
        ("t_out=None (sensore KO)",
         None,                 OperativeSeason.SHOULDER),
    ]
    for label, t_out, season in shoulder_cases:
        # t_op_min raw dal bisettore PMV (compute_single senza cap)
        ctx_raw = _ctx(
            season=season, mode=HVACOperatingProfile.SLEEP,
            room=room, rh_pct=rh_pct, t_op_current=t_op_shoulder,
            vmc_speed=vmc_speed,
        )
        _, res_raw = _run(ctx_raw, sim_cfg=sim_cfg)
        t_min_pmv = res_raw.t_op_min

        # t_op_min effettiva dopo cap (compute_many)
        res_cap = _run_many(t_op_shoulder, season, t_out)
        if res_cap is None:
            print(f"  {label:<38}  [zona non calcolata]")
            continue
        t_min_eff   = res_cap.t_op_min
        cap_active  = t_min_eff < t_min_pmv - 0.05
        deficit_eff = max(0.0, t_min_eff - t_op_shoulder)
        print(
            f"  {label:<38}  {t_min_pmv:>11.2f}°C  "
            f"{t_min_eff:>11.2f}°C  "
            f"{deficit_eff:>+10.2f}°C  "
            f"{'✓ SÌ' if cap_active else '✗ NO':>8}"
        )
        deficit_pmv = max(0.0, t_min_pmv - t_op_shoulder)
        if deficit_pmv > 0 and deficit_eff == 0.0:
            print(f"    → deficit PMV +{deficit_pmv:.2f}°C eliminato (riscaldamento notturno soppresso)")
        elif deficit_pmv > 0 and 0.0 < deficit_eff < deficit_pmv:
            print(f"    → deficit ridotto: +{deficit_pmv:.2f}°C → +{deficit_eff:.2f}°C")

    # ------------------------------------------------------------------
    # Parte 2 — Winter: cap a 21°C, stanze fredde vs calde
    # ------------------------------------------------------------------
    _subsection(
        f"Parte 2 — Winter: cap a {T_OP_MIN_SLEEP_HEATING_CAP_WINTER_C}°C  "
        f"(stanze fredde → riscaldamento legittimo; stanze calde → soppresso)"
    )
    print(
        f"\n  {'T_op zona':<10}  {'t_op_min PMV':>13}  "
        f"{'t_op_min eff':>13}  {'deficit PMV':>12}  {'deficit eff':>12}  {'cap':>4}"
    )
    print("  " + "─" * 80)

    for t_op_w in winter_t_ops:
        ctx_raw = _ctx(
            season=OperativeSeason.WINTER, mode=HVACOperatingProfile.SLEEP,
            room=room, rh_pct=rh_pct, t_op_current=t_op_w, vmc_speed=vmc_speed,
        )
        _, res_raw = _run(ctx_raw, sim_cfg=sim_cfg)
        t_min_pmv = res_raw.t_op_min

        res_cap = _run_many(t_op_w, OperativeSeason.WINTER, outdoor_temp=None)
        if res_cap is None:
            continue
        t_min_eff   = res_cap.t_op_min
        cap_active  = t_min_eff < t_min_pmv - 0.05
        def_pmv     = max(0.0, t_min_pmv - t_op_w)
        def_eff     = max(0.0, t_min_eff - t_op_w)
        note = ""
        if def_pmv > 0 and def_eff == 0.0:
            note = "← soppresso"
        elif def_eff > 0:
            note = "← legittimo"
        print(
            f"  T_op={t_op_w:>5.1f}°C  "
            f"{t_min_pmv:>12.2f}°C  "
            f"{t_min_eff:>12.2f}°C  "
            f"{def_pmv:>+11.2f}°C  "
            f"{def_eff:>+11.2f}°C  "
            f"{'✓' if cap_active else '✗':>4}  {note}"
        )

    # ------------------------------------------------------------------
    # Parte 3 — Summer: nessun cap (atteso)
    # ------------------------------------------------------------------
    _subsection("Parte 3 — Summer: cap non applicato (expected)")
    ctx_raw = _ctx(
        season=OperativeSeason.SUMMER, mode=HVACOperatingProfile.SLEEP,
        room=room, rh_pct=rh_pct, t_op_current=25.5, vmc_speed=vmc_speed,
    )
    _, res_raw_s = _run(ctx_raw, sim_cfg=sim_cfg)
    res_cap_s    = _run_many(25.5, OperativeSeason.SUMMER, outdoor_temp=28.0)
    if res_cap_s is not None:
        delta = res_raw_s.t_op_min - res_cap_s.t_op_min
        ok_s  = "cap non applicato ✓" if abs(delta) < 0.05 else "CAP APPLICATO ✗ (inatteso)"
        print(
            f"\n  t_op_min PMV={res_raw_s.t_op_min:.2f}°C  "
            f"t_op_min eff={res_cap_s.t_op_min:.2f}°C  "
            f"Δ={delta:.3f}°C  {ok_s}"
        )

    # ------------------------------------------------------------------
    # Parte 4 — Riepilogo stagionale
    # ------------------------------------------------------------------
    _subsection("Parte 4 — Riepilogo stagionale: t_op_min PMV vs effettivo")
    print(
        f"\n  {'Stagione':<10}  {'T_op':>6}  {'t_op_min PMV':>13}  "
        f"{'t_op_min eff':>13}  {'deficit soppresso':>19}"
    )
    print("  " + "─" * 72)
    summary = [
        (OperativeSeason.SHOULDER, t_op_shoulder, t_out_shoulder_night, "notte maggio"),
        (OperativeSeason.WINTER,   t_op_winter,   None,                 "notte invernale"),
        (OperativeSeason.SUMMER,   25.5,          28.0,                 "notte estiva"),
    ]
    for season, t_op_val, t_out, note in summary:
        ctx_raw = _ctx(
            season=season, mode=HVACOperatingProfile.SLEEP,
            room=room, rh_pct=rh_pct, t_op_current=t_op_val, vmc_speed=vmc_speed,
        )
        _, res_r = _run(ctx_raw, sim_cfg=sim_cfg)
        res_c    = _run_many(t_op_val, season, t_out)
        if res_c is None:
            continue
        def_pmv    = max(0.0, res_r.t_op_min - t_op_val)
        def_eff    = max(0.0, res_c.t_op_min - t_op_val)
        suppressed = max(0.0, def_pmv - def_eff)
        print(
            f"  {season.value:<10}  {t_op_val:>5.1f}°C  "
            f"{res_r.t_op_min:>12.2f}°C  "
            f"{res_c.t_op_min:>12.2f}°C  "
            f"{suppressed:>+18.2f}°C  ({note})"
        )

