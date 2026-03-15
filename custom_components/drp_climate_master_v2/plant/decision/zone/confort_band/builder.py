#
from __future__ import annotations

import logging
from datetime import datetime, timezone, time
from typing import Dict

from ....monitor.plant import ZoneSnapshot

from .....domain.enums import HVACOperatingProfile
from .....helpers.utils import slugify
from .....helpers.logger import log_debug

from .calculator import ComfortBandCalculator
from .policy_layer import ClimateZoneIT, ComfortPolicyLayer, ComplianceMode, ConfortPolicyConfig
from .model import ComfortBandResult

_LOGGER = logging.getLogger(__name__)

def build_comfort_engine() -> tuple[ConfortPolicyConfig, ComfortPolicyLayer]:
    # 1) CONFIG policy (tarabile da YAML/config entry)
    policy_cfg = ConfortPolicyConfig(
        climate_zone=ClimateZoneIT.D,
        # met/clo base (poi il policy layer può modificarli per profilo/stagione)
        base_met=1.10,
        base_clo_summer=0.50,
        base_clo_shoulder=0.70,
        default_clo_winter=1.00,
        zone_clo_delta_enabled=True,

        # calibrazione draft/aria (scaling su v_hi)
        non_living_high_speed_hi_scale=0.90,
        living_high_speed_hi_scale=1.05,

        # opzionale compliance oraria
        compliance_mode=ComplianceMode.OFF,     # oppure WARN / ENFORCE
        heating_allowed_from=time(5, 30),
        heating_allowed_to=time(23, 30),
        cooling_allowed_from=time(8, 0),
        cooling_allowed_to=time(22, 30),
    )
    policy_cfg.validate()

    # 2) POLICY layer
    policy_layer = ComfortPolicyLayer(policy_cfg)

    return policy_cfg, policy_layer

def build_confort_zones(
    *,
    now: datetime | None = None,
    season_state,
    indoor_zones: dict[str, ZoneSnapshot],        # dict[str, ZoneSnapshot]
    vmc_speed: int,
    outdoor_temp: float | None,
    preset_mode: HVACOperatingProfile,
    policy_cfg: ConfortPolicyConfig | None = None,
    policy_layer: ComfortPolicyLayer | None = None,

) -> Dict[str, ComfortBandResult]:
    if policy_cfg is None or policy_layer is None:
        policy_cfg, policy_layer = build_comfort_engine()

    # CALCOLATORE: i default qui sono poco critici perché compute_single usa policy/met/clo override
    calculator = ComfortBandCalculator(
        met=policy_cfg.base_met,
        clo_winter=policy_cfg.default_clo_winter,
        clo_summer=policy_cfg.base_clo_summer,
        clo_shoulder=policy_cfg.base_clo_shoulder,
    )

    # Le room_id usate dal tuo mapping devono combaciare con indoor_zones keys
    room_names = [slugify(a) for a in indoor_zones.keys()]

    bands = calculator.compute_many(
        now=(now or datetime.now(timezone.utc)),
        season=season_state.season,          # accetta Seasons o OperativeSeason
        vmc_air_speed=int(vmc_speed),
        indoor_zones=indoor_zones,
        outdoor_temp=outdoor_temp,
        mode=preset_mode,                   # HVACOperatingProfile.COMFORT/ECO/SLEEP...
        policy_layer=policy_layer,
        room_names=room_names,
        include_global=True,                # se indoor_zones contiene "global" -> "global_indoor"
        # humidity_solve_mode=None,          # None = usa la policy (estate/inverno PA_CONST; shoulder AUTO)
        # humidity_solve_mode="rh_const",    # override forzato per commissioning
    )

    # bands: dict[str, ComfortBandResult]
    for room, r in bands.items():
        log_debug(_LOGGER, f"{r}")

    return bands
