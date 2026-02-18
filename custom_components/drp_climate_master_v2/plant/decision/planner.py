from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
import logging

from homeassistant.util import dt as dt_util

from ...helpers.logger import log_debug
from ...helpers.utils import as_float
from ...domain.models.plant import PlantSnapshot
from ...domain.enums import HVACOperatingProfile

from .zone.contracts import ZonesDecision

from .config import PlantPlannerConfig
from .contracts import PlantDecision, PlantDemandSignals, PlantMode
from .signals.builder import DemandSignalsBuilder
from .vmc.policy import VmcPolicy, VmcState

from .commands.pdc import PdcCommandBuilder
from .commands.supply import SupplyCommandBuilder
from .commands.vmc import VmcCommandBuilder
from .mode.resolver import ModeResolver
from .validation import validate_decision

_LOGGER = logging.getLogger(__name__)


def _zone_weight(z: Any) -> float:
    """Best-effort zone weight extraction. Defaults to 1.0.

    Weights are used only for quorum/coverage and weighted means.
    If weights are missing or all zero, we fallback to count-based metrics.
    """

    w = as_float(getattr(z, "weight", None))
    if w is None:
        w = as_float(getattr(z, "area_weight", None))
    if w is None:
        w = 1.0
    try:
        wf = float(w)
    except Exception:
        wf = 1.0
    # Negative weights make no sense; allow 0 (zone ignored in weighted metrics)
    return max(0.0, wf)


@dataclass(slots=True, eq=False)
class PlantDecisionPlanner:
    """Planner impianto (PDC + pompe + VMC) - versione disaccoppiata.

    Obiettivo
      - produrre un PlantDecision (regime + target principali) usando PlantSnapshot
        e il piano di valvole di zona.
      - NON esegue attuazioni dirette (lascia a layer superiori / Supervisor).

    Architettura
      - signals.builder: osservazione/aggregazione (zone comfort, dew point)
      - mode.resolver: scelta regime (PlantMode)
      - commands.*: traduzione regime -> comandi dispositivo (PDC / supply / VMC)
      - validation: invarianti/coerenza post-compilazione

    Nota
      Il planner mantiene una superficie API piccola: `plan()` e poco altro.
      La logica è distribuita in moduli testabili e riusabili.
    """

    cfg: PlantPlannerConfig = field(default_factory=PlantPlannerConfig)

    _signals: DemandSignalsBuilder = field(init=False, repr=False)
    _vmc_policy: VmcPolicy = field(init=False, repr=False)
    _vmc_state: VmcState = field(default_factory=VmcState, init=False, repr=False)

    _mode_resolver: ModeResolver = field(init=False, repr=False)
    _pdc_cmd: PdcCommandBuilder = field(init=False, repr=False)
    _supply_cmd: SupplyCommandBuilder = field(init=False, repr=False)
    _vmc_cmd: VmcCommandBuilder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Observation builder (zone comfort + dew point)
        self._signals = DemandSignalsBuilder(self.cfg, zone_weight_fn=_zone_weight)

        # VMC domain policy (requests + DP hysteresis)
        self._vmc_policy = VmcPolicy(self.cfg.vmc, state=self._vmc_state)

        # Mode resolver (regime selection)
        self._mode_resolver = ModeResolver(self.cfg)

        # Device command builders
        self._pdc_cmd = PdcCommandBuilder(self.cfg)
        self._supply_cmd = SupplyCommandBuilder(self.cfg)
        self._vmc_cmd = VmcCommandBuilder(self.cfg, self._vmc_policy)

    def plan(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
        zones_decision: Optional[ZonesDecision] = None,
    ) -> PlantDecision:
        ts = snapshot.timestamp if isinstance(snapshot.timestamp, datetime) else dt_util.utcnow()
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)

        dec = PlantDecision(ts=ts, mode=PlantMode.OFF, reason=reason)

        # --- Outdoor temperature (required to compute curves, but we can fallback)
        t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
        if t_out is None or (isinstance(t_out, float) and (math.isnan(t_out) or math.isinf(t_out))):
            dec.warnings.append("missing_outdoor_temperature")
            t_out = None

        # --- Extract indoor demand signals
        demand = self._signals.build(snapshot=snapshot)

        # --- VMC domain: compute requests & DP setpoints (hysteresis-aware)
        self._enrich_vmc_signals(snapshot, demand)

        # --- Determine regime
        mode = self._mode_resolver.decide(snapshot=snapshot, demand=demand, zones_decision=zones_decision)
        dec.mode = mode

        # Diagnostics / warnings derived from enriched demand
        if getattr(demand, "vmc_dehum_feasible", None) is False:
            dec.warnings.append("vmc_dehum_unfeasible_outdoor_dp")
        if demand.zones_any_heat_demand and getattr(demand, "zones_mpc_heat_preheat_ok", None) is False:
            dec.warnings.append("zones_mpc_heat_ignored_headroom")

        log_debug(_LOGGER, "Computed plant demands: %s", demand)
        log_debug(_LOGGER, "Computed plant regime: %s", mode)
        dec.signals = demand

        # --- HARD dew-guard: degrade cooling if safe radiant supply is unachievable.
        # Kept here for now (safety pre-mode); can be extracted into a dedicated policy.
        if dec.mode == PlantMode.COOLING and demand.dp_max_c is not None:
            safe_required = float(demand.dp_max_c) + float(self.cfg.dp_guard.dp_margin_c) + float(self.cfg.dp_guard.delta_surface_water_c)
            if safe_required > float(self.cfg.radiant.cool_supply_max_c) + 1e-6:
                dec.warnings.append("dew_guard_unachievable_switch_mode")
                dec.mode = PlantMode.DEHUM_ASSIST if bool(demand.vmc_req_dehumidif) else PlantMode.VENT_ONLY

        # --- Build device commands for the chosen mode
        self._pdc_cmd.fill(dec, snapshot, t_out, demand)
        self._supply_cmd.fill(dec, snapshot, demand, zones_decision)
        self._vmc_cmd.fill(dec, snapshot, demand)

        # --- Coherence validation (PlantMode invariants)
        dec.warnings.extend(validate_decision(dec))

        return dec

    # ---- VMC integration -------------------------------------------------

    def _enrich_vmc_signals(self, snapshot: PlantSnapshot, demand: PlantDemandSignals) -> None:
        """Fill VMC-related signals using the VMC domain policy.

        This keeps `signals/builder.py` observation-only.
        """

        profile = HVACOperatingProfile.from_value(
            getattr(snapshot, "climate_preset_mode", None),
            default=HVACOperatingProfile.COMFORT,
        ) or HVACOperatingProfile.COMFORT

        vmc_dem = self._vmc_policy.compute(
            snapshot=snapshot,
            profile=profile,
            heat_def_max_c=float(demand.heat_def_max_c),
            heat_def_wmean_c=float(demand.heat_def_wmean_c),
            heat_cov=float(demand.heat_cov),
            cool_sur_max_c=float(demand.cool_sur_max_c),
            cool_sur_wmean_c=float(demand.cool_sur_wmean_c),
            cool_cov=float(demand.cool_cov),
            dp_dehum_c=getattr(demand, "dp_dehum_c", None),
            dp_max_c=getattr(demand, "dp_max_c", None),
            outdoor_dp_c=getattr(demand, "outdoor_dp_c", None),
        )

        # PlantDemandSignals stores VMC policy outputs flat for logging/backward-compat.
        demand.vmc_dp_sp_c = vmc_dem.dp_sp_c
        demand.vmc_dehum_on_thr_c = vmc_dem.dehum_on_thr_c
        demand.vmc_dehum_off_thr_c = vmc_dem.dehum_off_thr_c
        demand.vmc_dehum_feasible = vmc_dem.dehum_feasible
        demand.vmc_req_heating = vmc_dem.req_heating
        demand.vmc_req_cooling = vmc_dem.req_cooling
        demand.vmc_req_dehumidif = vmc_dem.req_dehumidif
        demand.vmc_req_water = vmc_dem.req_water
