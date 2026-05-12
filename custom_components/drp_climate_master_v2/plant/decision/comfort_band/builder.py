#
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Dict, Optional

from ...monitor.plant import ZoneSnapshot

from ....domain.enums import HVACOperatingProfile
from ....helpers.utils import slugify
from ....helpers.logger import log_debug

from .calculator import ComfortBandCalculator
from .model import ClimateZoneIT, ComplianceMode, ComfortBandResult
from .policy_layer import ComfortPolicyLayer, ConfortPolicyConfig
from .config import (
    MET_BASE,
    CLO_BASE_WINTER,
    CLO_BASE_SUMMER,
    CLO_BASE_SHOULDER,
)

_LOGGER = logging.getLogger(__name__)


def build_comfort_engine(
    cfg: ConfortPolicyConfig | None = None,
) -> tuple[ConfortPolicyConfig, ComfortPolicyLayer]:
    """Costruisce il policy layer del comfort engine.

    Se ``cfg`` è None, viene usata la ``ConfortPolicyConfig`` con i default
    di ``config.py`` (zona D, met=MET_BASE, clo stagionali standard).
    In produzione, passare il ``comfort_policy`` estratto da
    ``PlantPlannerConfig`` per garantire coerenza con la configurazione
    dell'intero impianto.

    Il metodo chiama ``validate()`` sulla config prima di istanziare il layer.
    """
    if cfg is None:
        cfg = ConfortPolicyConfig()
    cfg.validate()
    return cfg, ComfortPolicyLayer(cfg)


def build_confort_zones(
    *,
    now: datetime | None = None,
    season_state,
    indoor_zones: dict[str, ZoneSnapshot],
    vmc_speed: int,
    outdoor_temp: float | None,
    preset_mode: HVACOperatingProfile,
    policy_cfg: ConfortPolicyConfig | None = None,
    policy_layer: ComfortPolicyLayer | None = None,
    t_op_rm_by_zone: Mapping[str, float] | None = None,
) -> Dict[str, ComfortBandResult]:
    """Calcola la comfort band per tutte le zone dell'impianto.

    Parametri
    ---------
    t_op_rm_by_zone : Mapping[str, float] | None
        Running mean EWMA della T_op per zona, prodotta da ``ZoneTrmTracker``
        nel ``PlantDecisionPlanner``.  None → warm-up / fail-safe: la
        correzione adattiva CLO (S3) è soppressa per tutte le zone.

    Se ``policy_cfg`` o ``policy_layer`` sono None, vengono costruiti con i
    default di ``config.py`` tramite ``build_comfort_engine()``.
    In produzione, passare i valori estratti da ``PlantPlannerConfig``.
    """
    if policy_cfg is None or policy_layer is None:
        policy_cfg, policy_layer = build_comfort_engine(policy_cfg)

    calculator = ComfortBandCalculator(
        met=policy_cfg.base_met,
        clo_winter=policy_cfg.default_clo_winter,
        clo_summer=policy_cfg.base_clo_summer,
        clo_shoulder=policy_cfg.base_clo_shoulder,
    )

    room_names = [slugify(a) for a in indoor_zones.keys()]

    cold_snap: bool = bool(getattr(season_state, "weather_cold_snap", False))

    # S4 - Progresso stagionale e direzione shoulder per interpolazione CLO.
    # season_state.progress restituisce [0..100] (scala %, non 0-1).
    # shoulder_direction: "spring" o "autumn" estratto da Seasons.value.
    _season_progress: float | None = None
    _shoulder_direction: str | None = None
    _raw_season = getattr(season_state, "season", None)
    _season_val: str = str(getattr(_raw_season, "value", _raw_season or "")).lower()
    if _season_val in ("spring", "autumn"):
        _raw_progress = getattr(season_state, "progress", None)
        if _raw_progress is not None:
            try:
                _season_progress = float(_raw_progress)
            except (TypeError, ValueError):
                pass
        _shoulder_direction = _season_val

    bands = calculator.compute_many(
        now=(now or datetime.now(timezone.utc)),
        season=season_state.season,
        vmc_air_speed=int(vmc_speed),
        indoor_zones=indoor_zones,
        outdoor_temp=outdoor_temp,
        mode=preset_mode,
        policy_layer=policy_layer,
        room_names=room_names,
        include_global=True,
        cold_snap=cold_snap,
        t_op_rm_by_zone=t_op_rm_by_zone,
        season_progress=_season_progress,
        shoulder_direction=_shoulder_direction,
        # humidity_solve_mode=None,        # None = usa la policy (estate/inverno PA_CONST; shoulder AUTO)
        # humidity_solve_mode="rh_const",  # override forzato per commissioning
    )

    for room, r in bands.items():
        log_debug(_LOGGER, f"{r}")

    return bands
