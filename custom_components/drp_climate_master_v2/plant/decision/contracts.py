from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, Dict, List, Optional

class MetricBasis(StrEnum):
    WEIGHTED = "weighted"
    COUNT = "count"
    NONE = "none"
    
class PlantMode(str, Enum):
    """High-level plant operating mode (impianto).

    Nota: volutamente disaccoppiato dagli enum dei singoli device.
    """

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DEHUM_ASSIST = "dehum_assist"
    VENT_ONLY = "vent_only"

@dataclass(slots=True)
class PlantDemandSignals:
    """
    Segnali aggregati di domanda (multi-zona) e richieste device, prodotti dal planner impianto.

    I nomi dei campi sono allineati alle chiavi attuali di PlantDecision.signals per:
      - logging/telemetria consistenti
      - retro-compatibilità con la struttura dict già usata nel planner

    --- DOMANDA SENSIBILE (comfort band su T_op) ---
    Definizioni per una singola zona z:
      - heat_def_z = max(0, T_op_min(z) - T_meas(z))   [°C]
      - cool_sur_z = max(0, T_meas(z) - T_op_max(z))   [°C]
    dove T_meas è tipicamente T_op di zona (fallback: temperatura aria).

    *heat_def_*: misura “quanto manca” per rientrare nella banda di comfort lato heating.
    *cool_sur_*: misura “quanto eccede” per rientrare nella banda di comfort lato cooling.

    - *_max: worst-case (massimo su tutte le zone). Utile per COMFORT/BOOST e come override safety/comfort.
    - *_by_zone: mappa dettagliata per diagnosi/telemetria.
    - *_wmean: media (preferibilmente pesata per area/importanza zona) dei deficit/surplus.
      Se i pesi non sono disponibili o tutti zero, degrada a media “count-based”.
    - *_cov: coverage (0..1), cioè frazione di “casa” fuori banda:
        - in weighted: somma pesi delle zone con deficit/surplus > 0 diviso somma pesi zone valide
        - in count: numero zone con deficit/surplus > 0 diviso numero zone valide
      Serve a evitare avvii PDC per una sola zona appena fuori banda in profili ECO/SLEEP/AWAY/VACATION.

    - *_metric_basis: indica come sono state calcolate coverage e wmean:
        "weighted" = usati pesi di zona
        "count"    = fallback a conteggio zone (pesi non disponibili/utili)
        "none"     = nessuna zona valida per calcolo

    --- SAFETY IGROMETRICA (anticondensa) ---
    - dp_max_c: massimo dew point tra zone [°C]. È un segnale di safety: “zona più a rischio condensa”.
      In cooling è tipicamente usato per fissare una mandata radiante minima sicura.

    --- RICHIESTE VMC (vincoli macchina) ---
    - vmc_req_heating/cooling/dehumidif/water: richieste della VMC verso il circuito idraulico/produzione.
      Possono forzare o influenzare il regime impianto (es. DEHUM_ASSIST).
    """

    # --- Worst-case (max across zones) ---
    heat_def_max_c: float = field(
        default=0.0,
        metadata={"doc": "Massimo deficit heating tra zone: max(0, T_min - T_meas). [°C]"},
    )
    cool_sur_max_c: float = field(
        default=0.0,
        metadata={"doc": "Massimo surplus cooling tra zone: max(0, T_meas - T_max). [°C]"},
    )

    # --- Per-zone maps ---
    heat_def_by_zone_c: Dict[str, float] = field(
        default_factory=dict,
        metadata={"doc": "Mappa {zona: deficit heating} in °C."},
    )
    cool_sur_by_zone_c: Dict[str, float] = field(
        default_factory=dict,
        metadata={"doc": "Mappa {zona: surplus cooling} in °C."},
    )

    # --- Profile-aware multi-zone demand metrics ---
    heat_def_wmean_c: float = field(
        default=0.0,
        metadata={"doc": "Media (pesata o count-based) dei deficit heating tra zone. [°C]"},
    )
    cool_sur_wmean_c: float = field(
        default=0.0,
        metadata={"doc": "Media (pesata o count-based) dei surplus cooling tra zone. [°C]"},
    )
    heat_cov: float = field(
        default=0.0,
        metadata={"doc": "Coverage heating (0..1): quota di zone/area con deficit>0."},
    )
    cool_cov: float = field(
        default=0.0,
        metadata={"doc": "Coverage cooling (0..1): quota di zone/area con surplus>0."},
    )
    heat_metric_basis: MetricBasis = field(
        default=MetricBasis.NONE,
        metadata={"doc": 'Metodo usato per heat_cov/heat_def_wmean: "weighted"|"count"|"none".'},
    )
    cool_metric_basis: MetricBasis = field(
        default=MetricBasis.NONE,
        metadata={"doc": 'Metodo usato per cool_cov/cool_sur_wmean: "weighted"|"count"|"none".'},
    )

    # --- Dew point safety ---
    dp_max_c: Optional[float] = field(
        default=None,
        metadata={"doc": "Dew point massimo tra zone (worst-case anticondensa). [°C]"},
    )

    # --- VMC requests ---
    vmc_req_heating: bool = field(
        default=False,
        metadata={"doc": "VMC richiede heating (acqua calda / batteria)."},
    )
    vmc_req_cooling: bool = field(
        default=False,
        metadata={"doc": "VMC richiede cooling (acqua fredda / batteria)."},
    )
    vmc_req_dehumidif: bool = field(
        default=False,
        metadata={"doc": "VMC richiede deumidificazione (latente)."},
    )
    vmc_req_water: bool = field(
        default=False,
        metadata={"doc": "VMC richiede acqua/circolazione (pompa circuito diretto)."},
    )

    # --- Other ---
    user_hvac_mode: str = field(
        default="off",
        metadata={"doc": "tbd"},
    )
    user_profile: str = field(
        default="off",
        metadata={"doc": "tbd"},
    )
    user_forced_off: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    ctrl_aggr: float = field(
        default=0.0,
        metadata={"doc": "tbd"},
    )
    heat_on_thr_c: float = field(
        default=0.0,
        metadata={"doc": "tbd"},
    )
    cool_on_thr_c: float = field(
        default=0.0,
        metadata={"doc": "tbd"},
    )
    quorum_cov_req: float = field(
        default=0.0,
        metadata={"doc": "tbd"},
    )
    heat_override: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    heat_quorum_ok: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    heat_mean_ok: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    cool_override: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    cool_quorum_ok: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    cool_mean_ok: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    any_heat: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    any_cool: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    any_dehum: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    heat_sensible: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    cool_sensible: bool = field(
        default=False,
        metadata={"doc": "tbd"},
    )
    runtime_season: str = field(
        default="--",
        metadata={"doc": "tbd"},
    )
    operative_season: str = field(
        default="--",
        metadata={"doc": "tbd"},
    )

    # def as_signals(self) -> Dict[str, Any]:
    #     """
    #     Restituisce un dict con le stesse chiavi già usate dal planner (compatibilità logging/telemetria).
    #     """
    #     return {
    #         "heat_def_max_c": float(self.heat_def_max_c),
    #         "cool_sur_max_c": float(self.cool_sur_max_c),
    #         "heat_def_by_zone_c": dict(self.heat_def_by_zone_c),
    #         "cool_sur_by_zone_c": dict(self.cool_sur_by_zone_c),
    #         "heat_def_wmean_c": float(self.heat_def_wmean_c),
    #         "cool_sur_wmean_c": float(self.cool_sur_wmean_c),
    #         "heat_cov": float(self.heat_cov),
    #         "cool_cov": float(self.cool_cov),
    #         "heat_metric_basis": self.heat_metric_basis,
    #         "cool_metric_basis": self.cool_metric_basis,
    #         "dp_max_c": self.dp_max_c,
    #         "vmc_req_heating": bool(self.vmc_req_heating),
    #         "vmc_req_cooling": bool(self.vmc_req_cooling),
    #         "vmc_req_dehumidif": bool(self.vmc_req_dehumidif),
    #         "vmc_req_water": bool(self.vmc_req_water),
    #     }

@dataclass(slots=True)
class PdcCommand:
    """Comandi desiderati per la PDC (produzione primaria).

    I campi sono volutamente 'Optional' perché questo layer NON esegue direttamente attuazioni
    (in questa fase il codice non è integrato nel Supervisor).
    """

    power: Optional[bool] = None
    mode: Optional[str] = None  # "heating" | "cooling"
    heat_wot_c: Optional[float] = None
    heat_dt_c: Optional[float] = None
    cool_wot_c: Optional[float] = None
    cool_dt_c: Optional[float] = None

    # Alcuni impianti distinguono "Workload FM power" vs "Device Power"
    fm_power: Optional[bool] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SupplyCommand:
    """Comandi desiderati per i circuiti secondari (pompe + miscelazione)."""

    # Pompe
    direct_pump_on: Optional[bool] = None   # circuito VMC (diretto)
    adj_pump_on: Optional[bool] = None      # circuito radiante (mix)

    # Miscelatrice 3-vie (0-100%)
    mix_valve_pct: Optional[float] = None

    # Target "fisici" (utile per logging/telemetria anche se non mappati 1:1 su attuatori)
    rad_supply_target_c: Optional[float] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VmcCommand:
    """Comandi desiderati per la VMC."""

    power: Optional[bool] = None
    mode: Optional[str] = None  # "winter" | "summer" | "off"
    air_speed: Optional[int] = None

    setpoint_t_c: Optional[float] = None
    setpoint_rh_pct: Optional[float] = None
    setpoint_dp_c: Optional[float] = None
    setpoint_ddp_c: Optional[float] = None

    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlantDecision:
    """Risultato della pianificazione a livello impianto."""

    ts: datetime
    mode: PlantMode
    reason: str

    pdc: PdcCommand = field(default_factory=PdcCommand)
    supply: SupplyCommand = field(default_factory=SupplyCommand)
    vmc: VmcCommand = field(default_factory=VmcCommand)

    signals: PlantDemandSignals = field(default_factory=PlantDemandSignals)
    warnings: List[str] = field(default_factory=list)

    def to_log_dict(self) -> Dict[str, Any]:
        """Versione 'log friendly' (serializzabile) della decisione."""
        # NOTE: dataclass(slots=True) non garantisce __dict__ sulle istanze.
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["mode"] = self.mode.value
        return d

    def __str__(self) -> str:
        # --- helper di formattazione compatti e robusti ---
        def fnum(x, nd=1):
            return f"{x:.{nd}f}" if x is not None else "-"

        def fpct01(x, nd=0):
            # x in [0..1] -> percentuale
            return f"{(x * 100):.{nd}f}%" if x is not None else "-"

        def fbool(b, on="on", off="off"):
            return on if b is True else (off if b is False else "-")

        def fstr(s):
            return s if s else "-"

        def flist(xs):
            return " | ".join(xs) if xs else "-"

        def fmb(x: MetricBasis | None):
            return x.value if x is not None else "-"

        def fdict_compact(d: Dict[str, float] | None, nd=1, max_items=8):
            """
            dict compatto tipo: zona1=0.5, zona2=1.2, ...
            taglia dopo max_items per non esplodere i log.
            """
            if not d:
                return "-"
            items = sorted(d.items(), key=lambda kv: kv[0])
            more = ""
            if len(items) > max_items:
                items = items[:max_items]
                more = f" (+{len(d) - max_items})"
            s = ", ".join(f"{k}={fnum(v, nd)}" for k, v in items)
            return s + more

        # --- top-level ---
        ts = self.ts.isoformat()
        lines = [
            f"",
            f"Plant decision",
            f"  Timestamp          :: {ts}",
            f"  Mode               :: {self.mode.value if self.mode else '-'}",
            f"  Reason             :: {fstr(self.reason)}",
        ]

        # --- signals (formattati "a mano", non dump generico) ---
        s = self.signals
        if s is not None:
            lines += [
                f"------------------------------------------------------------------",
                f"Signals",
                # user
                f"  User HVAC mode     :: {fstr(s.user_hvac_mode)}",
                f"  User profile       :: {fstr(s.user_profile)}",
                f"  User forced off    :: {fbool(s.user_forced_off, 'True', 'False')}",
                # season / runtime
                f"  Runtime season     :: {fstr(s.runtime_season)}",
                f"  Operative season   :: {fstr(s.operative_season)}",
                # demand worst-case
                f"  Heat def max       :: {fnum(s.heat_def_max_c)} °C",
                f"  Cool sur max       :: {fnum(s.cool_sur_max_c)} °C",
                # demand means + quorum
                f"  Heat def mean      :: {fnum(s.heat_def_wmean_c)} °C",
                f"  Heat coverage      :: {fpct01(s.heat_cov, 0)} ({fmb(s.heat_metric_basis)})",
                f"  Heat on thr        :: {fnum(s.heat_on_thr_c)} °C",
                f"  Heat quorum req    :: {fpct01(s.quorum_cov_req, 0)}",
                f"  Heat override      :: {fbool(s.heat_override, 'True', 'False')}",
                f"  Heat quorum ok     :: {fbool(s.heat_quorum_ok, 'True', 'False')}",
                f"  Heat mean ok       :: {fbool(s.heat_mean_ok, 'True', 'False')}",
                f"  Cool sur mean      :: {fnum(s.cool_sur_wmean_c)} °C",
                f"  Cool coverage      :: {fpct01(s.cool_cov, 0)} ({fmb(s.cool_metric_basis)})",
                f"  Cool on thr        :: {fnum(s.cool_on_thr_c)} °C",
                f"  Cool override      :: {fbool(s.cool_override, 'True', 'False')}",
                f"  Cool quorum ok     :: {fbool(s.cool_quorum_ok, 'True', 'False')}",
                f"  Cool mean ok       :: {fbool(s.cool_mean_ok, 'True', 'False')}",
                # any / sensible
                f"  Any heat           :: {fbool(s.any_heat, 'True', 'False')}",
                f"  Any cool           :: {fbool(s.any_cool, 'True', 'False')}",
                f"  Any dehum          :: {fbool(s.any_dehum, 'True', 'False')}",
                f"  Heat sensible      :: {fbool(s.heat_sensible, 'True', 'False')}",
                f"  Cool sensible      :: {fbool(s.cool_sensible, 'True', 'False')}",
                # dew point safety
                f"  DP max             :: {fnum(s.dp_max_c)} °C",
                # VMC requests
                f"  VMC req heating    :: {fbool(s.vmc_req_heating, 'True', 'False')}",
                f"  VMC req cooling    :: {fbool(s.vmc_req_cooling, 'True', 'False')}",
                f"  VMC req dehumidif  :: {fbool(s.vmc_req_dehumidif, 'True', 'False')}",
                f"  VMC req water      :: {fbool(s.vmc_req_water, 'True', 'False')}",
                # per-zone maps (compatte)
                f"  Heat def by zone   :: {fdict_compact(s.heat_def_by_zone_c, nd=1)}",
                f"  Cool sur by zone   :: {fdict_compact(s.cool_sur_by_zone_c, nd=1)}",
            ]

        # --- warnings ---
        if self.warnings:
            lines += [
                f"------------------------------------------------------------------",
                f"Warnings            :: {flist(self.warnings)}",
            ]

        lines += [f"------------------------------------------------------------------"]

        # --- PDC ---
        lines += [f"PDC"]
        if self.pdc:
            lines += [
                f"  Power              :: {fbool(self.pdc.power, 'On', 'Off')}",
                f"  FM power           :: {fbool(self.pdc.fm_power, 'On', 'Off')}",
                f"  Mode               :: {self.pdc.mode or '-'}",
                f"  Heat-WOT           :: {fnum(self.pdc.heat_wot_c)} °C",
                f"  Heat-ΔT            :: {fnum(self.pdc.heat_dt_c)} °C",
                f"  Cool-WOT           :: {fnum(self.pdc.cool_wot_c)} °C",
                f"  Cool-ΔT            :: {fnum(self.pdc.cool_dt_c)} °C",
            ]
            if getattr(self.pdc, "debug", None):
                lines += [f"  Debug              :: {self.pdc.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        # --- Supply ---
        lines += [f"Supply"]
        if self.supply:
            lines += [
                f"  Direct pump        :: {fbool(self.supply.direct_pump_on, 'On', 'Off')}",
                f"  Adj pump           :: {fbool(self.supply.adj_pump_on, 'On', 'Off')}",
                f"  Mix valve          :: {fnum(self.supply.mix_valve_pct, 0)} %",
                f"  Rad target         :: {fnum(self.supply.rad_supply_target_c)} °C",
            ]
            if getattr(self.supply, "debug", None):
                lines += [f"  Debug              :: {self.supply.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        # --- VMC ---
        lines += [f"VMC"]
        if self.vmc:
            lines += [
                f"  Power              :: {fbool(self.vmc.power, 'On', 'Off')}",
                f"  Mode               :: {self.vmc.mode or '-'}",
                f"  Air speed          :: {self.vmc.air_speed if self.vmc.air_speed is not None else '-'}",
                f"  Setpoint T         :: {fnum(self.vmc.setpoint_t_c)} °C",
                f"  Setpoint RH        :: {fnum(self.vmc.setpoint_rh_pct, 0)} %",
                f"  Setpoint DP        :: {fnum(self.vmc.setpoint_dp_c)} °C",
                f"  Setpoint ΔDP       :: {fnum(self.vmc.setpoint_ddp_c)} °C",
            ]
            if getattr(self.vmc, "debug", None):
                lines += [f"  Debug              :: {self.vmc.debug}"]
        else:
            lines += [f"  -"]
        lines += [f"------------------------------------------------------------------"]

        return "\n".join(lines)


