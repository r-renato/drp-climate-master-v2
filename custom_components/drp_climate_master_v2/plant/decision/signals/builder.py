from __future__ import annotations

import logging
from dataclasses import fields as dc_fields, is_dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from ....helpers.logger import log_debug

from ....helpers.num import quantile_linear

from ..contracts import MetricBasis, PlantDemandSignals
from ....plant.monitor.plant import PlantSnapshot
from ....helpers.psychrometric import dew_point_celsius
from ....helpers.utils import as_float

from ..config import PlantPlannerConfig
from .model import DewPointCluster, FreeVentCluster, ZoneComfortCluster, ZoneDemandMetrics

# Soglie default per il cluster free-vent (override tramite config se necessario)
_FREE_COOL_DELTA_MIN_C: float = 3.0   # delta T minimo per attivare il free cooling
_FREE_HEAT_DELTA_MIN_C: float = 3.0   # delta T minimo per attivare il free heating
_FREE_COOL_DP_MARGIN_C: float = 5.0   # margine DP esterno vs DP indoor max

_LOGGER = logging.getLogger(__name__)

class DemandSignalsBuilder:
    """
    Costruttore dei segnali di domanda impianto (`PlantDemandSignals`) a partire
    dallo snapshot corrente (`PlantSnapshot`).

    Scopo
    -----
    Questo builder aggrega e “compatta” le informazioni di domanda termica/igrometrica
    provenienti dalle zone interne, producendo un payload unico usato dal planner.

    In particolare calcola:
    - **comfort cluster di zona**: deficit di riscaldamento e surplus di raffrescamento
      rispetto alla comfort band (T_op_min / T_op_max), inclusi headroom minimi.
    - **metriche aggregate di domanda**: media pesata (o per conteggio) e coverage
      (quanta parte delle zone è fuori banda).
    - **cluster dew point**: max dew point indoor, dew point robusto per controllo
      deumidifica (percentile), e dew point outdoor stimato (best effort).

    NOTE
    ----
    Questo builder è intenzionalmente **osservativo**:
    - aggrega delta di comfort e segnali di dew point;
    - NON calcola policy VMC (richieste, setpoint DP, isteresi), che risiede nel
      dominio dedicato alla VMC.
    """

    def __init__(
        self,
        cfg: PlantPlannerConfig,
        *,
        zone_weight_fn: Callable[[Any], float],
        # percentile_sorted_fn: Callable[[List[float], float], float] = quantile_linear,
    ) -> None:
        """
        Inizializza il builder.

        Args:
            cfg: configurazione del planner (include anche sotto-config VMC per
                percentile dew point, ecc.).
            zone_weight_fn: funzione che ritorna il peso di una zona (>=0) per
                calcoli pesati. Tipicamente dipende da priorità stanza, area,
                occupancy, ecc.
            # percentile_sorted_fn: (opzionale) funzione percentile; lasciata
            # commentata perché si usa direttamente `quantile_linear`.
        """
        self.cfg = cfg
        self._zone_weight_fn = zone_weight_fn
        # self._percentile_sorted_fn = percentile_sorted_fn

    def build(self, *, snapshot: PlantSnapshot, comfort_bands_by_zone: Optional[Mapping[str, Any]] = None) -> PlantDemandSignals:
        """
        Costruisce l'oggetto `PlantDemandSignals` a partire dallo snapshot impianto.

        Pipeline:
        1) Cluster comfort di zona (deficit heating / surplus cooling + headroom + dp values)
        2) Metriche aggregate (media pesata / coverage e basis)
        3) Cluster dew point (indoor max, dehum percentile, outdoor dp best-effort)
        4) Assemblaggio in `PlantDemandSignals` con filtraggio safe dei campi.

        Args:
            snapshot: fotografia coerente dello stato sensori/zone/impianto.

        Returns:
            Istanza di `PlantDemandSignals` pronta per l'uso nel planner.
        """
        zc = self._compute_zone_comfort_cluster(snapshot, comfort_bands_by_zone=comfort_bands_by_zone)
        zm = self._compute_zone_demand_metrics(zc)
        dp = self._compute_dew_point_cluster(snapshot, zc)
        fv = self._compute_free_vent_cluster(snapshot, zc, dp)

        payload: Dict[str, Any] = dict(
            # zone comfort
            heat_def_max_c=zc.heat_def_max_c,
            cool_sur_max_c=zc.cool_sur_max_c,
            heat_def_by_zone_c=zc.heat_def_by_zone_c,
            cool_sur_by_zone_c=zc.cool_sur_by_zone_c,
            heat_headroom_min_c=zc.heat_headroom_min_c,
            cool_headroom_min_c=zc.cool_headroom_min_c,
            # zone metrics
            heat_def_wmean_c=zm.heat_def_wmean_c,
            cool_sur_wmean_c=zm.cool_sur_wmean_c,
            heat_cov=zm.heat_cov,
            cool_cov=zm.cool_cov,
            heat_metric_basis=zm.heat_metric_basis,
            cool_metric_basis=zm.cool_metric_basis,
            # dew point
            dp_max_c=dp.dp_max_c,
            dp_dehum_c=dp.dp_dehum_c,
            outdoor_dp_c=dp.outdoor_dp_c,
            # free vent (Cluster F)
            free_cool_delta_c=fv.free_cool_delta_c,
            free_heat_delta_c=fv.free_heat_delta_c,
            free_cool_dp_ok=fv.free_cool_dp_ok,
            free_cool_feasible=fv.free_cool_feasible,
            free_heat_feasible=fv.free_heat_feasible,
        )

        log_debug(_LOGGER, "Computed ZoneComfortCluster: %s", zc)
        
        return self._safe_signals_init(payload)

    # -----------------
    # Cluster builders
    # -----------------

    def _compute_zone_comfort_cluster(self, snapshot: PlantSnapshot, *, comfort_bands_by_zone: Optional[Mapping[str, Any]] = None) -> ZoneComfortCluster:
        """
        Calcola il cluster “comfort” sulle zone indoor.

        Per ogni zona:
        - legge la temperatura misurata (preferendo T_op se disponibile, altrimenti T aria)
        - legge i limiti di comfort band (t_op_min, t_op_max)
        - calcola:
          - heating deficit: max(0, t_min - t_meas)
          - cooling surplus: max(0, t_meas - t_max)
          - headroom heating: t_meas - t_min (quanto margine prima di andare “freddo”)
          - headroom cooling: t_max - t_meas (quanto margine prima di andare “caldo”)
        - accumula anche statistiche per metriche aggregate:
          - denominatori pesati e per conteggio
          - “out-of-band” pesato e per conteggio
          - somme pesate e non pesate dei delta

        Inoltre raccoglie dew point indoor per stimare:
        - dp_max_c: massimo dew point osservato nelle zone
        - dp_values: lista di dew point validi (per percentile robusto)

        Args:
            snapshot: stato corrente che contiene `indoor_zones`.

        Returns:
            `ZoneComfortCluster` con massimi, mappe per zona, headroom minimi e
            accumulatori (pesati e per conteggio) per metriche successive.
        """
        heat_def_max = 0.0
        cool_sur_max = 0.0
        heat_def_by_zone: Dict[str, float] = {}
        cool_sur_by_zone: Dict[str, float] = {}
        heat_headroom_min_c: Optional[float] = None
        cool_headroom_min_c: Optional[float] = None

        # Accumulatori heating (pesati e per conteggio)
        heat_den_w = 0.0
        heat_out_w = 0.0
        heat_sum_wdef = 0.0
        heat_den_n = 0
        heat_out_n = 0
        heat_sum_ndef = 0.0

        # Accumulatori cooling (pesati e per conteggio)
        cool_den_w = 0.0
        cool_out_w = 0.0
        cool_sum_wsur = 0.0
        cool_den_n = 0
        cool_out_n = 0
        cool_sum_nsur = 0.0

        # Dew point indoor
        dp_max: Optional[float] = None
        dp_values: List[float] = []

        for zone_key, z in (getattr(snapshot, "indoor_zones", None) or {}).items():
            # Temperatura di riferimento: T_op se presente, altrimenti T aria.
            t_meas = as_float(getattr(getattr(z, "t_op", None), "value", None))
            if t_meas is None:
                t_meas = as_float(getattr(getattr(z, "temperature", None), "value", None))

            band = (comfort_bands_by_zone or {}).get(zone_key) if comfort_bands_by_zone is not None else getattr(z, "confort_band", None)
            t_min = as_float(getattr(band, "t_op_min", None))
            t_max = as_float(getattr(band, "t_op_max", None))

            # headroom (margine rispetto ai limiti banda)
            if t_meas is not None and t_min is not None:
                hh = float(t_meas) - float(t_min)
                heat_headroom_min_c = hh if heat_headroom_min_c is None else min(heat_headroom_min_c, hh)
            if t_meas is not None and t_max is not None:
                ch = float(t_max) - float(t_meas)
                cool_headroom_min_c = ch if cool_headroom_min_c is None else min(cool_headroom_min_c, ch)

            # heating deficit (quanto manca al minimo comfort)
            if t_meas is not None and t_min is not None:
                d = max(0.0, float(t_min) - float(t_meas))
                heat_def_by_zone[zone_key] = d
                heat_def_max = max(heat_def_max, d)

                w = float(self._zone_weight_fn(z) or 0.0)

                heat_den_n += 1
                heat_sum_ndef += d
                if d > 0.0:
                    heat_out_n += 1

                if w > 0.0:
                    heat_den_w += w
                    heat_sum_wdef += w * d
                    if d > 0.0:
                        heat_out_w += w

            # cooling surplus (quanto eccede oltre massimo comfort)
            if t_meas is not None and t_max is not None:
                d = max(0.0, float(t_meas) - float(t_max))
                cool_sur_by_zone[zone_key] = d
                cool_sur_max = max(cool_sur_max, d)

                w = float(self._zone_weight_fn(z) or 0.0)

                cool_den_n += 1
                cool_sum_nsur += d
                if d > 0.0:
                    cool_out_n += 1

                if w > 0.0:
                    cool_den_w += w
                    cool_sum_wsur += w * d
                    if d > 0.0:
                        cool_out_w += w

            # dew point indoor (se disponibile)
            dp = as_float(getattr(getattr(z, "dew_point", None), "value", None))
            if dp is not None:
                dp_f = float(dp)
                dp_values.append(dp_f)
                dp_max = dp_f if dp_max is None else max(dp_max, dp_f)

        return ZoneComfortCluster(
            heat_def_max_c=heat_def_max,
            cool_sur_max_c=cool_sur_max,
            heat_def_by_zone_c=heat_def_by_zone,
            cool_sur_by_zone_c=cool_sur_by_zone,
            heat_headroom_min_c=heat_headroom_min_c,
            cool_headroom_min_c=cool_headroom_min_c,
            heat_den_w=heat_den_w,
            heat_out_w=heat_out_w,
            heat_sum_wdef=heat_sum_wdef,
            heat_den_n=heat_den_n,
            heat_out_n=heat_out_n,
            heat_sum_ndef=heat_sum_ndef,
            cool_den_w=cool_den_w,
            cool_out_w=cool_out_w,
            cool_sum_wsur=cool_sum_wsur,
            cool_den_n=cool_den_n,
            cool_out_n=cool_out_n,
            cool_sum_nsur=cool_sum_nsur,
            dp_max_c=dp_max,
            dp_values=dp_values,
        )

    def _compute_zone_demand_metrics(self, zc: ZoneComfortCluster) -> ZoneDemandMetrics:
        """
        Converte gli accumulatori del cluster comfort in metriche sintetiche di domanda.

        Heating:
        - heat_def_wmean_c: deficit medio (pesato se disponibile, altrimenti medio semplice)
        - heat_cov: coverage (quota di zone/peso fuori banda per heating)
        - heat_metric_basis: base del calcolo (WEIGHTED / COUNT / NONE)

        Cooling:
        - cool_sur_wmean_c: surplus medio (pesato se disponibile, altrimenti medio semplice)
        - cool_cov: coverage (quota di zone/peso fuori banda per cooling)
        - cool_metric_basis: base del calcolo (WEIGHTED / COUNT / NONE)

        Args:
            zc: cluster comfort con accumulatori già popolati.

        Returns:
            `ZoneDemandMetrics` con medie e coverage coerenti e un flag esplicito
            che indica se il calcolo è pesato o per conteggio.
        """
        # heating
        if zc.heat_den_w > 0.0:
            heat_def_wmean = zc.heat_sum_wdef / zc.heat_den_w
            heat_cov = zc.heat_out_w / zc.heat_den_w
            heat_metric_basis = MetricBasis.WEIGHTED
        elif zc.heat_den_n > 0:
            heat_def_wmean = zc.heat_sum_ndef / float(zc.heat_den_n)
            heat_cov = float(zc.heat_out_n) / float(zc.heat_den_n)
            heat_metric_basis = MetricBasis.COUNT
        else:
            heat_def_wmean = 0.0
            heat_cov = 0.0
            heat_metric_basis = MetricBasis.NONE

        # cooling
        if zc.cool_den_w > 0.0:
            cool_sur_wmean = zc.cool_sum_wsur / zc.cool_den_w
            cool_cov = zc.cool_out_w / zc.cool_den_w
            cool_metric_basis = MetricBasis.WEIGHTED
        elif zc.cool_den_n > 0:
            cool_sur_wmean = zc.cool_sum_nsur / float(zc.cool_den_n)
            cool_cov = float(zc.cool_out_n) / float(zc.cool_den_n)
            cool_metric_basis = MetricBasis.COUNT
        else:
            cool_sur_wmean = 0.0
            cool_cov = 0.0
            cool_metric_basis = MetricBasis.NONE

        return ZoneDemandMetrics(
            heat_def_wmean_c=float(heat_def_wmean),
            heat_cov=float(heat_cov),
            heat_metric_basis=heat_metric_basis,
            cool_sur_wmean_c=float(cool_sur_wmean),
            cool_cov=float(cool_cov),
            cool_metric_basis=cool_metric_basis,
        )

    def _compute_dew_point_cluster(self, snapshot: PlantSnapshot, zc: ZoneComfortCluster) -> DewPointCluster:
        """
        Calcola il cluster dew point per supportare logiche di deumidifica/condensa.

        Include:
        - outdoor_dp_c: dew point esterno (best effort):
          - se presente `snapshot.global_outdoor_dew_point` lo usa direttamente
          - altrimenti lo stima da T_out e RH_out con formula psicrometrica
        - dp_dehum_c: dew point indoor robusto per controllo deumidifica:
          - percentile configurabile (es. 1.0 = max, 0.9 = 90° percentile, ecc.)
          - fallback al massimo se percentile fallisce
          - fallback finale a `zc.dp_max_c`

        Args:
            snapshot: snapshot impianto per recuperare grandezze outdoor.
            zc: zone cluster che contiene `dp_values` e `dp_max_c`.

        Returns:
            `DewPointCluster` con dp max indoor, dp “robusto” per deumidifica e dp outdoor.
        """
        # Outdoor dew point (best effort)
        outdoor_dp_c = as_float(getattr(getattr(snapshot, "global_outdoor_dew_point", None), "value", None))
        if outdoor_dp_c is None:
            t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))
            rh_out = as_float(getattr(getattr(snapshot, "global_outdoor_humidity", None), "value", None))
            if t_out is not None and rh_out is not None:
                try:
                    outdoor_dp_c = float(dew_point_celsius(float(t_out), float(rh_out)))
                except Exception:
                    outdoor_dp_c = None

        # Robust DP for dehumidification control
        dp_dehum_c: Optional[float] = None
        if zc.dp_values:
            xs = sorted(zc.dp_values)
            p = float(getattr(self.cfg.vmc.dehum, "dp_control_percentile", 1.0))
            try:
                dp_dehum_c = quantile_linear(xs, p, assume_sorted=True, clamp_p=True, nan_policy="raise")
            except Exception:
                dp_dehum_c = float(xs[-1])

        if dp_dehum_c is None:
            dp_dehum_c = zc.dp_max_c

        return DewPointCluster(
            dp_max_c=zc.dp_max_c,
            dp_dehum_c=dp_dehum_c,
            outdoor_dp_c=float(outdoor_dp_c) if outdoor_dp_c is not None else None,
        )

    def _compute_free_vent_cluster(
        self,
        snapshot: PlantSnapshot,
        zc: ZoneComfortCluster,
        dp: DewPointCluster,
    ) -> FreeVentCluster:
        """Calcola i segnali di fattibilità per free cooling/heating ventilativo.

        Tre condizioni indipendenti per il free cooling:
        1. Delta T sufficiente: T_indoor_mean − T_outdoor > soglia
        2. Dew point esterno sicuro: DP_outdoor < DP_indoor_max − margine
        3. Finestre chiuse: evita dispersione inutile

        Per il free heating: solo delta T e stagione (no vincolo DP).

        Questo metodo è osservativo: calcola segnali, non decide nulla.
        """
        del zc

        # --- temperatura esterna ---
        t_out = as_float(getattr(getattr(snapshot, "global_outdoor_temperature", None), "value", None))

        # --- temperatura indoor: usa global_indoor_zone (già mediata a monte) ---
        t_indoor_mean: Optional[float] = None
        g_indoor = getattr(snapshot, "global_indoor_zone", None)
        if g_indoor is not None:
            t_indoor_mean = as_float(getattr(getattr(g_indoor, "t_op", None), "value", None))
            if t_indoor_mean is None:
                t_indoor_mean = as_float(getattr(getattr(g_indoor, "temperature", None), "value", None))

        # --- delta T ---
        free_cool_delta_c: Optional[float] = None
        free_heat_delta_c: Optional[float] = None
        if t_indoor_mean is not None and t_out is not None:
            free_cool_delta_c = float(t_indoor_mean) - float(t_out)
            free_heat_delta_c = float(t_out) - float(t_indoor_mean)

        # --- DP esterno sicuro ---
        free_cool_dp_ok: Optional[bool] = None
        if dp.outdoor_dp_c is not None and dp.dp_max_c is not None:
            free_cool_dp_ok = float(dp.outdoor_dp_c) < (
                float(dp.dp_max_c) - _FREE_COOL_DP_MARGIN_C
            )

        # --- finestre chiuse ---
        windows_closed = bool(getattr(snapshot, "windows_close_state", True))

        # --- stagione (summer blocca free heating) ---
        season_val = getattr(getattr(getattr(snapshot, "season", None), "season", None), "value", None)
        is_summer = season_val == "summer"

        # --- fattibilità ---
        free_cool_feasible = bool(
            free_cool_delta_c is not None
            and free_cool_delta_c >= _FREE_COOL_DELTA_MIN_C
            and free_cool_dp_ok is True
            and windows_closed
        )
        free_heat_feasible = bool(
            free_heat_delta_c is not None
            and free_heat_delta_c >= _FREE_HEAT_DELTA_MIN_C
            and not is_summer
            and windows_closed
        )

        return FreeVentCluster(
            free_cool_delta_c=free_cool_delta_c,
            free_heat_delta_c=free_heat_delta_c,
            free_cool_dp_ok=free_cool_dp_ok,
            free_cool_feasible=free_cool_feasible,
            free_heat_feasible=free_heat_feasible,
        )

    # -----------------
    # Assembly helper
    # -----------------

    def _safe_signals_init(self, payload: Dict[str, Any]) -> PlantDemandSignals:
        """
        Inizializza `PlantDemandSignals` in modo robusto rispetto a differenze di schema.

        Questa funzione:
        - parte da `payload` e costruisce kwargs
        - se `PlantDemandSignals` è una dataclass, filtra i campi non previsti
          (utile in refactor/versioni diverse)
        - infine istanzia `PlantDemandSignals(**kwargs)`.

        Args:
            payload: dizionario con i segnali calcolati (può contenere extra keys).

        Returns:
            Istanza di `PlantDemandSignals`.

        Note:
            - In caso di eccezioni nel riflesso dataclass, non fallisce:
              tenta comunque l'inizializzazione.
        """
        kwargs = dict(payload)
        try:
            if is_dataclass(PlantDemandSignals):
                allowed = {f.name for f in dc_fields(PlantDemandSignals)}
                kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        except Exception:
            pass

        return PlantDemandSignals(**kwargs)  # type: ignore[arg-type]


# Backward-compat alias
PlantDemandSignalsBuilder = DemandSignalsBuilder
