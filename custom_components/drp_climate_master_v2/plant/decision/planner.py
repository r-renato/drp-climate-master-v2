from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
import logging

from homeassistant.util import dt as dt_util

from ...helpers.logger import log_debug, log_exception
from ...helpers.utils import as_float, as_int
from ...plant.monitor.plant import PlantSnapshot
from ...domain.enums import HVACOperatingProfile

from .zone.model import ZonesDecision
from .confort_band.builder import build_confort_zones
from .confort_band.policy_layer import ComfortPolicyLayer, ConfortPolicyConfig
from .confort_band.mpc.provider import ZonesMpcProvider
from .confort_band.trm import ZoneTrmTracker
from .confort_band.config import (
    T_RM_TAU_HOURS,
    T_RM_WARMUP_TICKS,
    T_RM_CLAMP_MIN,
    T_RM_CLAMP_MAX,
)

from .context import DecisionDerivedInputs

from .config import PlantPlannerConfig
from .contracts import PlantDecision, PlantDemandSignals, PlantMode
from .signals.builder import DemandSignalsBuilder
from .vmc.policy import VmcPolicy, VmcState

from .commands.pdc import PdcCommandBuilder
from .commands.supply import SupplyCommandBuilder
from .commands.valves import ZoneValvesCommandBuilder
from .commands.vmc import VmcCommandBuilder
from .mode.resolver import ModeResolver
from .validation import validate_decision
from .safety.dew_guard import DewGuardPolicy, DewGuardResult

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
    cpcfg: ConfortPolicyConfig = field(default_factory=ConfortPolicyConfig)
    cpl: ComfortPolicyLayer = field(init=False)

    _signals: DemandSignalsBuilder = field(init=False, repr=False)
    _vmc_policy: VmcPolicy = field(init=False, repr=False)
    _vmc_state: VmcState = field(default_factory=VmcState, init=False, repr=False)

    _mode_resolver: ModeResolver = field(init=False, repr=False)
    _pdc_cmd: PdcCommandBuilder = field(init=False, repr=False)
    _valves_cmd: ZoneValvesCommandBuilder = field(init=False, repr=False)
    _supply_cmd: SupplyCommandBuilder = field(init=False, repr=False)
    _vmc_cmd: VmcCommandBuilder = field(init=False, repr=False)

    _dew_guard: DewGuardPolicy = field(init=False, repr=False)

    # Zones MPC-lite integration (kept isolated in `zone/provider.py`)
    _zones_mpc: ZonesMpcProvider = field(init=False, repr=False)

    # S3 — Adaptive CLO: running mean T_op per zona (ZoneTrmTracker)
    # Stato in memoria; reset al riavvio HA (warm-up ~2h a 30s/tick).
    _zone_trm: ZoneTrmTracker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Legge la configurazione comfort dal PlantPlannerConfig (unica fonte di verità).
        # Non usare build_comfort_engine() che ha valori hardcoded.
        self.cpcfg = self.cfg.comfort_policy
        self.cpl = ComfortPolicyLayer(self.cpcfg)
        self.cpcfg.validate()

        # Observation builder (zone comfort + dew point)
        self._signals = DemandSignalsBuilder(self.cfg, zone_weight_fn=_zone_weight)

        # VMC domain policy (requests + DP hysteresis)
        self._vmc_policy = VmcPolicy(self.cfg.vmc, state=self._vmc_state)

        # Mode resolver (regime selection)
        self._mode_resolver = ModeResolver(self.cfg)

        # Device command builders
        self._pdc_cmd = PdcCommandBuilder(self.cfg)
        self._valves_cmd = ZoneValvesCommandBuilder(self.cfg)
        self._supply_cmd = SupplyCommandBuilder(self.cfg)
        self._vmc_cmd = VmcCommandBuilder(self.cfg, self._vmc_policy)

        # Dew-point safety (single source of truth)
        self._dew_guard = DewGuardPolicy(self.cfg)

        # Zones MPC provider (optional)
        self._zones_mpc = ZonesMpcProvider(cfg=self.cfg.zones_mpc)

        # S3 — Adaptive CLO: tracker running mean T_op per zona.
        # Parametri letti da config.py (tau, warmup, clamp) per coerenza
        # con la policy layer che usa le stesse costanti.
        self._zone_trm = ZoneTrmTracker(
            tau_hours=T_RM_TAU_HOURS,
            warmup_ticks=T_RM_WARMUP_TICKS,
            t_rm_clamp_min=T_RM_CLAMP_MIN,
            t_rm_clamp_max=T_RM_CLAMP_MAX,
        )

    def plan(
        self,
        *,
        snapshot: PlantSnapshot,
        reason: str,
    ) -> PlantDecision:
        ts = snapshot.timestamp if isinstance(snapshot.timestamp, datetime) else dt_util.utcnow()
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)

        dec = PlantDecision(ts=ts, mode=PlantMode.OFF, reason=reason)

        # ── FASE 1 ─ Sanity check temperatura esterna ──────────────────────────
        # t_out è necessaria per le curve climatiche ma non è bloccante.
        # Se mancante o non finita, il planner continua con t_out=None.
        t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
        if t_out is None or (isinstance(t_out, float) and (math.isnan(t_out) or math.isinf(t_out))):
            dec.warnings.append("missing_outdoor_temperature")
            t_out = None

        # ── FASE 2 ─ Comfort bands e running mean T_op per zona ────────────────
        # Calcola le bande di comfort dinamiche per zona in base a stagione,
        # profilo operativo e running mean T_operativa (ZoneTrmTracker, ~2h warm-up).
        # Fail-safe: eccezione interna non rompe il tick (derived → None).
        derived = self._conf_bands_derived(snapshot=snapshot)
        if derived is not None:
            dec.derived_input = derived

        comfort_bands_by_zone = derived.comfort_bands_by_zone if derived is not None else None

        # ── FASE 3 ─ Segnali di domanda aggregati ─────────────────────────────
        # Produce PlantDemandSignals: deficit/surplus termico per zona,
        # metriche quorum/coverage pesate, dp_max_c, dp_dehum_c.
        # DemandSignalsBuilder è osservazione-only: nessuna logica di controllo.
        demand = self._signals.build(snapshot=snapshot, comfort_bands_by_zone=comfort_bands_by_zone)

        # ── FASE 4 ─ Arricchimento segnali VMC ────────────────────────────────
        # VmcPolicy calcola setpoint dew point, soglie deumidifica e flag
        # richieste (heating/cooling/dehum/water) con isteresi.
        # I risultati sono scritti flat su demand per compatibilità logging.
        # DemandSignalsBuilder rimane osservazione-only; la logica VMC è separata.
        self._enrich_vmc_signals(snapshot, demand)

        # ── FASE 5 ─ Piano zone MPC-lite e risoluzione modo impianto ──────────
        # ZonesMpcProvider produce il piano zone (opzionale, può restituire None).
        # ModeResolver sceglie PlantMode combinando snapshot + demand + zones_decision.
        zones_decision = self._zones_mpc.maybe_plan(snapshot=snapshot, reason=f"{reason}/zones", comfort_bands_by_zone=comfort_bands_by_zone)

        # Expose zones plan in the decision object (single output artifact)
        dec.zones = zones_decision

        # Surface zones MPC warnings without losing plant-level robustness.
        if (
            zones_decision
            and getattr(zones_decision, "warnings", None)
            and bool(getattr(self.cfg.zones_mpc, "propagate_warnings_to_plant", True))
        ):
            dec.warnings.extend([f"zones_mpc_{w}" for w in zones_decision.warnings])

        # --- Determine regime
        mode = self._mode_resolver.decide(snapshot=snapshot, demand=demand, zones_decision=zones_decision)
        dec.mode = mode

        # Arricchisce reason con il motivo specifico del VENT_ONLY se free vent attivo.
        # Distingue il VENT_ONLY intenzionale (free cooling/heating) dal fallback di sicurezza.
        if mode == PlantMode.VENT_ONLY:
            if demand.vmc_req_free_cooling:
                dec.reason = f"{reason}|vent_only_free_cooling"
            elif demand.vmc_req_free_heating:
                dec.reason = f"{reason}|vent_only_free_heating"

        # Diagnostics / warnings derived from enriched demand
        if getattr(demand, "vmc_dehum_feasible", None) is False:
            dec.warnings.append("vmc_dehum_unfeasible_outdoor_dp")
        if demand.zones_any_heat_demand and getattr(demand, "zones_mpc_heat_preheat_ok", None) is False:
            dec.warnings.append("zones_mpc_heat_ignored_headroom")

        log_debug(_LOGGER, "Computed plant demands: %s", demand)
        log_debug(_LOGGER, "Computed plant regime: %s", mode)
        dec.signals = demand

        # ── FASE 6 ─ Dew-point guard ──────────────────────────────────────────
        # Unica fonte di verità per la sicurezza anti-condensa.
        # Applicata DOPO la risoluzione del modo: se COOLING è unsafe,
        # il modo viene degradato qui e solo qui.
        # NOTA TERMOTECNICA (P0): opera su dp_max_c aggregato (stima),
        # non su T_mandata_radiante misurata. Secondo layer di verifica mancante.
        dew_guard: DewGuardResult = self._dew_guard.evaluate(
            mode=dec.mode,
            dp_max_c=getattr(demand, "dp_max_c", None),
            allow_dehum_assist=bool(getattr(demand, "vmc_req_dehumidif", False)),
        )

        # If cooling was selected but radiant cooling is unsafe/unknown, degrade the plant mode.
        if dec.mode == PlantMode.COOLING and dew_guard.suggested_mode is not None:
            # Keep existing warning name for backward compatibility.
            if dew_guard.reason == "unachievable":
                dec.warnings.append("dew_guard_unachievable_switch_mode")
            elif dew_guard.reason == "missing_dp_max":
                dec.warnings.append("dew_guard_missing_dp_max_switch_mode")
            else:
                dec.warnings.append(f"dew_guard_{dew_guard.reason}_switch_mode")
            dec.mode = dew_guard.suggested_mode

        # ── FASE 7 ─ Compilazione comandi dispositivo ─────────────────────────
        # I quattro builder traducono il PlantMode scelto in comandi concreti.
        # Ordine obbligatorio: PDC → valvole → supply → VMC.
        # dew_guard è passato esplicitamente a valves e supply per modulare
        # setpoint T_mandata e apertura valvole in funzione del rischio condensa.
        # NOTA TERMOTECNICA (P2): arbitraggio setpoint PDC tra circuito VMC
        # e radiante non è esplicito — rischio PDC fuori punto di lavoro.
        self._pdc_cmd.fill(dec, snapshot, t_out, demand)
        self._valves_cmd.fill(dec, snapshot, demand, zones_decision, dew_guard=dew_guard)
        self._supply_cmd.fill(dec, snapshot, demand, zones_decision, dew_guard=dew_guard)
        self._vmc_cmd.fill(dec, snapshot, demand)

        # ── FASE 7b ─ Validazione coerenza ────────────────────────────────────
        # Verifica invarianti PlantMode (es. valvole aperte senza PDC attivo).
        # Warning aggiuntivi appesi a dec.warnings senza bloccare l'output.
        dec.warnings.extend(validate_decision(dec))

        return dec

    # ---- Confoert Band integration -------------------------------------------------

    def _conf_bands_derived(self, snapshot: PlantSnapshot) -> DecisionDerivedInputs | None:
        derived: DecisionDerivedInputs | None = None

        try:
            if snapshot.indoor_zones and snapshot.season is not None:
                # Copia difensiva: evita di mutare snapshot.indoor_zones
                # aggiungendo global_indoor_zone (che non ha area nel config).
                all_indoor_zones = dict(snapshot.indoor_zones)
                if snapshot.global_indoor_zone is not None:
                    all_indoor_zones["global"] = snapshot.global_indoor_zone
                vmc_speed = as_int(getattr(getattr(snapshot, "vmc", None), "spare_setpoint", None), default=0, min_value=0, max_value=5) or 0
                t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
                preset = getattr(snapshot, "climate_preset_mode", None) or HVACOperatingProfile.ECO

                # S3 — aggiorna la running mean T_op per ogni zona e raccoglie
                # i valori pronti (None per le zone ancora in warm-up).
                ts = snapshot.timestamp
                t_op_rm: dict[str, float] = {}
                for zone_id, z in snapshot.indoor_zones.items():
                    t_op_val = as_float(getattr(getattr(z, "t_op", None), "value", None))
                    self._zone_trm.update(zone_id, t_op_val, ts)
                    rm = self._zone_trm.get_t_rm(zone_id)
                    if rm is not None:
                        t_op_rm[zone_id] = rm

                bands = build_confort_zones(
                    now=snapshot.timestamp,
                    season_state=snapshot.season,
                    # indoor_zones=snapshot.indoor_zones,
                    indoor_zones=all_indoor_zones,
                    vmc_speed=int(vmc_speed),
                    outdoor_temp=t_out,
                    preset_mode=preset,
                    policy_cfg=self.cpcfg,
                    policy_layer=self.cpl,
                    # None se nessuna zona ha superato il warm-up (fail-safe)
                    t_op_rm_by_zone=t_op_rm if t_op_rm else None,
                )
                derived = DecisionDerivedInputs(comfort_bands_by_zone=bands)
        except Exception as e:
            # Comfort-band failures must not break the plant tick.
            log_exception(_LOGGER, "Comfort-band computation failed: %s", e)

        return derived
    
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
            free_cool_feasible=bool(getattr(demand, "free_cool_feasible", False)),
            free_heat_feasible=bool(getattr(demand, "free_heat_feasible", False)),
        )

        # PlantDemandSignals stores VMC policy outputs flat for logging/backward-compat.
        demand.vmc_dp_sp_c = vmc_dem.dp_sp_c
        demand.vmc_ddp_cmd_c = getattr(vmc_dem, "ddp_cmd_c", None)
        demand.vmc_dp_sp_raw_c = getattr(vmc_dem, "dp_sp_raw_c", None)
        demand.vmc_dehum_on_thr_c = vmc_dem.dehum_on_thr_c
        demand.vmc_dehum_off_thr_c = vmc_dem.dehum_off_thr_c
        demand.vmc_dehum_feasible = vmc_dem.dehum_feasible
        demand.vmc_req_heating = vmc_dem.req_heating
        demand.vmc_req_cooling = vmc_dem.req_cooling
        demand.vmc_req_dehumidif = vmc_dem.req_dehumidif
        demand.vmc_req_water = vmc_dem.req_water
        demand.vmc_req_free_cooling = vmc_dem.req_free_cooling
        demand.vmc_req_free_heating = vmc_dem.req_free_heating
