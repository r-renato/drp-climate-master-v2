from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional
import logging

from homeassistant.components.climate.const import HVACMode

from ...helpers.formatter import fbool, fnum
from ...helpers.logger import log_debug, log_exception, log_info, log_warning
from ...helpers.ha import EntityTimeInStateStats

from ...domain.enums import HVACOperatingProfile
from ...helpers.sensor_aggregator import AggregatedValue

from ...helpers.utils import pad

from ...domain.models.season import SeasonState

_LOGGER = logging.getLogger(__name__)

@dataclass(slots=True)
class ZoneSnapshot:
    """
    Istantanea dei **sensori** e dello **stato impianto** per una singola *zona/stanza*
    all’istante `ts`. Funziona da input coerente per stimatore di stato (es. Kalman),
    generatore di target di comfort (adaptive) e controllore (PID/MPC-lite), oltre che
    per i guardiani di sicurezza (dew-point).

    Unità e convenzioni
    -------------------
    • Temperature: **°C** — Umidità relativa: **%** — Attuazioni on/off: **bool**  
    • `ts` deve essere **timezone-aware** (consigliato **UTC**).

    Attributi
    ---------
    timestamp : datetime
        Timestamp dell’istantanea (timezone-aware). Usato per allineare snapshot, forecast
        e scheduling del ciclo di controllo (rilevazione di dati stantii).

    flow_t : Optional[float]
        Temperatura **mandata** del circuito di zona/collettore. Necessaria per Dew-Point Guard e,
        assieme a `return_t`, per stimare la potenza termica (ΔT idronico).

    return_t : Optional[float]
        Temperatura **ritorno** del circuito di zona. Con `flow_t` fornisce ΔT idronico (proxy potenza/efficienza).

    """

    timestamp: datetime
    name: str

    temperature: AggregatedValue
    humidity: AggregatedValue
    heat_index: Optional[AggregatedValue] = None
    dew_point: Optional[AggregatedValue] = None

    t_op: Optional[AggregatedValue] = None
    mrt: Optional[AggregatedValue] = None
    condensation_margin: Optional[AggregatedValue] = None

    radiant_valve: Optional[AggregatedValue] = None

    flow_t: Optional[float] = None
    return_t: Optional[float] = None

    weight: float = 1.0
    """Peso della zona nelle metriche aggregate di domanda (quorum/coverage).
    Corrisponde a AreaConfig.ceiling espresso in m². Default 1.0 per
    retrocompatibilità con zone senza ceiling configurato.
    Una zona con weight=0.0 è visibile al planner (valvola comandata,
    dew-point guard attivo) ma non pesa nel calcolo quorum/coverage globale.
    """


@dataclass(slots=True)
class PDCSnapshot:
    """
    Istantanea degli **stati** e delle **temperature** della Pompa di Calore (PDC)
    e del circuito tecnico associato. È usata da orchestratore (mode/mix), safety
    (min-on/min-off, controlli su ΔT) e controllore (PID/MPC-lite) per vincoli e costi.

    Unità e convenzioni
    -------------------
    • Temperature: **°C** — Durate: **minuti** — Flag/Mode: boolean/int vendor-specific  
    • `timestamp` deve essere **timezone-aware** (consigliato **UTC**).  
    • **WOT** = *Water Outlet Temperature* (setpoint di mandata lato generatore).

    Attributi
    ---------
    timestamp : datetime
        Istante di rilevazione dello snapshot (timezone-aware).

    fm_power_on : bool
        Stato ON/OFF del modulo di movimentazione fluido lato PDC (es. **Flow Manager** /
        circolatore principale / fan module). True ⇒ circolazione attiva.

    power_on : Optional[bool]
        Stato ON/OFF della **PDC** (compressore/centralina in marcia). Può essere `None`
        se il dato non è disponibile.

    device_mode : Optional[int]
        Modalità operativa **grezza** (codice numerico *vendor-specific*: es. 0=standby, 1=heating,
        2=cooling, 3=dhw, 4=defrost). Va normalizzata a valle.

    wot_heat : Optional[float]
        Setpoint **WOT** in **riscaldamento** (spesso da curva climatica + offset).

    delta_t_heat : Optional[float]
        Offset/Delta applicato al setpoint WOT in riscaldamento (boost/eco; >0 ⇒ alza mandata obiettivo).

    wot_cool : Optional[float]
        Setpoint **WOT** in **raffrescamento** (temperatura acqua fredda desiderata).

    delta_t_cool : Optional[float]
        Offset/Delta applicato al setpoint WOT in raffrescamento (più basso ⇒ acqua più fredda).

    t_water_in_pe : Optional[float]
        Temperatura **ingresso** acqua lato scambiatore/plate della PDC (lato impianto).

    t_water_out_pe : Optional[float]
        Temperatura **uscita** acqua lato scambiatore/plate della PDC (lato impianto). Con `t_water_in_pe`
        consente di stimare **ΔT PDC** (= out_pe − in_pe), proxy dello scambio sul generatore.

    boiler_supply_temp : Optional[float]
        **Mandata** circuito tecnico/distribuzione (post PDC/miscelazione). Verifica coerenza con domanda utenze.

    boiler_return_temp : Optional[float]
        **Ritorno** circuito tecnico/distribuzione. Con la mandata fornisce **ΔT idronico** della rete (proxy carico).

    minutes_power_on : Optional[float]
        Minuti consecutivi in **stato acceso** (PDC/gruppo). Usato per policy **min-on time** e per diagnosi cicli brevi.

    minutes_power_off : Optional[float]
        Minuti consecutivi in **stato spento**. Usato per policy **min-off time** e anti-short-cycling.
    """

    timestamp: datetime
    fm_power_on: bool
    power_on: Optional[bool] = None

    device_mode: Optional[int] = None

    wot_heat: Optional[float] = None
    delta_t_heat: Optional[float] = None

    wot_cool: Optional[float] = None
    delta_t_cool: Optional[float] = None

    sensor_t_water_in_pe: Optional[float] = None
    sensor_t_water_out_pe: Optional[float] = None

    sensor_compressor_state: Optional[bool] = None

    minutes_power_on: Optional[float] = None
    minutes_power_off: Optional[float] = None


@dataclass(slots=True)
class VMCSnapshot:
    """
    Istantanea della **Ventilazione Meccanica Controllata (VMC)** / UTA domestica.
    Fornisce stato, richieste termiche e setpoint utili a orchestratore/safety/MPC.

    Unità e convenzioni
    -------------------
    • Temperature: **°C** — Umidità: **%** — Flag: **bool** — Impostazioni vendor: **int/str**  
    • I campi non disponibili possono restare `None`; le logiche degradano in sicurezza.

    Attributi (principali)
    ----------------------
    timestamp : datetime
        Istante di rilevazione (timezone-aware).

    power_on : Optional[bool]
        Stato ON/OFF VMC/UTA.

    t_setpoint : Optional[float]
        Setpoint di temperatura di mandata aria (se supportato).

    rh_setpoint : Optional[float]
        Setpoint di umidità (se supportato).

    t_dew_point_setpoint : Optional[float]
        Setpoint di punto di rugiada per controllo anti-condensa sull’aria trattata.

    delta_t_dew_point_setpoint : Optional[float]
        Offset dinamico al setpoint di dew point (safety margin o correzioni temporanee).

    spare_setpoint : Optional[int]
        Setpoint ausiliario (segnaposto per funzioni vendor-specific).

    act_vent_recirculation / act_force_heating / act_force_cooling / act_force_free_cooling : Optional[bool]
        Forzature operative (ricircolo, heating/cooling, free-cooling). Da usare con cautela nelle politiche.

    processing_mode : Optional[str]
        Modo operativo (stringa controllata: es. "auto", "manual", "eco", "boost", ecc.).

    compressor_management / cooling_management : Optional[int]
        Gestione compressore/raffrescamento (codici vendor-specific).

    request_water / request_dehumidification / request_heating / request_cooling : Optional[bool]
        **Richieste** della VMC verso il circuito idronico o l’impianto (domanda di fluido o di servizio).

    sensor_t_ambient / sensor_h_ambient : Optional[float]
        Sensori aria ambiente lato VMC (T e RH).

    sensor_t_water : Optional[float]
        Temperatura acqua allo scambiatore VMC (se presente).

    sensor_t_outdoor : Optional[float]
        Sensore esterno integrato VMC (se diverso dal meteo provider).

    sensor_power_on_night / sensor_power_on_today : Optional[float]
        Minuti di funzionamento notturno/odierno (telemetria statistica).

    alarm_high_pressure / alarm_dew_point / alarm_low_water_temp / alarm_high_water_temp / alarm_alarm : Optional[bool]
        Segnali d’allarme principali (alta pressione, dew-point, acqua troppo fredda/calda, fault generico).
    """

    timestamp: datetime
    power_on: Optional[bool] = None

    t_setpoint: Optional[float] = None
    rh_setpoint: Optional[float] = None
    t_dew_point_setpoint: Optional[float] = None
    delta_t_dew_point_setpoint: Optional[float] = None

    spare_setpoint: Optional[int] = None

    act_vent_recirculation: Optional[bool] = None
    act_force_heating: Optional[bool] = None
    act_force_cooling: Optional[bool] = None
    act_force_free_cooling: Optional[bool] = None

    processing_mode: Optional[str] = None
    compressor_management: Optional[int] = None
    cooling_management: Optional[int] = None

    request_water: Optional[bool] = None
    request_dehumidification: Optional[bool] = None
    request_heating: Optional[bool] = None
    request_cooling: Optional[bool] = None

    sensor_t_ambient: Optional[float] = None
    sensor_h_ambient: Optional[float] = None
    sensor_t_water: Optional[float] = None
    sensor_t_outdoor: Optional[float] = None

    alarm_high_pressure: Optional[bool] = None
    alarm_dew_point: Optional[bool] = None
    alarm_low_water_temp: Optional[bool] = None
    alarm_high_water_temp: Optional[bool] = None
    alarm_alarm: Optional[bool] = None

    sensor_power_on_night: Optional[float] = None
    sensor_power_on_today: Optional[float] = None

    minutes_power_on: Optional[float] = None
    minutes_power_off: Optional[float] = None
    spare_time_in_state_stats: Optional[EntityTimeInStateStats] = None

@dataclass(slots=True)
class SupplyUnitSnapshot:
    """
    Istantanea dell’**unità di distribuzione** (es. miscelatrice, valvole, pompe,
    UTA di mandata). Descrive potenza idronica disponibile e stato dei rami.

    Attributi
    ---------
    timestamp : datetime
        Istante di rilevazione (timezone-aware).

    direct_su_power_on / adjustable_su_power_on : Optional[bool]
        Stato rami **diretto** e **miscelato/modulabile** (abilitazione pompe/valvole).

    three_point_mixing_valve : Optional[int]
        Stato comando **valvola a 3 punti** (-1 = chiudi/raffredda, 0 = stop, +1 = apri/riscalda).
        Convenzione da confermare con l’hardware.

    sensor_boiler_temp_system_supply / sensor_boiler_temp_system_return : Optional[float]
        Temperature mandata/ritorno sul circuito tecnico.

    sensor_adjustable_temp_system_supply / sensor_adjustable_temp_system_return : Optional[float]
        Temperature mandata/ritorno sul ramo miscelato/modulabile.

    sensor_direct_temp_system_supply / sensor_direct_temp_system_return : Optional[float]
        Temperature mandata/ritorno sul ramo diretto.
    """

    timestamp: datetime

    direct_su_power_on: Optional[bool] = None
    adjustable_su_power_on: Optional[bool] = None
    three_point_mixing_valve: Optional[int] = None

    sensor_boiler_temp_system_supply: Optional[float] = None
    sensor_boiler_temp_system_return: Optional[float] = None
    sensor_adjustable_temp_system_supply: Optional[float] = None
    sensor_adjustable_temp_system_return: Optional[float] = None
    sensor_direct_temp_system_supply: Optional[float] = None
    sensor_direct_temp_system_return: Optional[float] = None

@dataclass(slots=True)
class PlantSnapshot:
    """
    Istantanea dello **stato globale dell’impianto** all’istante `timestamp`.
    Aggrega zone, stagione, generatori e unità di distribuzione per consentire
    decisioni di alto livello (orchestrazione, sicurezza, ottimizzazione).

    Attributi
    ---------
    timestamp : datetime
        Timestamp della fotografia dell’impianto (timezone-aware).

    season : SeasonState
        Stato di stagione/meteo-stagionale (es. WINTER/SUMMER) con regole/bias applicativi.

    zones : dict[str, ZoneSnapshot]
        Mappa *nome-zona → ZoneSnapshot*. Deve contenere almeno le zone “core”.

    out_t / out_rh : Optional[float]
        Sensori esterni **globali** (se non forniti per-zona). Usati come fallback.

    dew_guard_active : Optional[bool]
        Flag globale del **Dew-Point Guard** (true se attivo in questa iterazione).

    free_cooling_possible : Optional[bool]
        True se le condizioni esterne consentono **free-cooling** (policy lato VMC).

    vmc / pdc / supply_unit : Optional[...Snapshot]
        Stati della VMC, PDC/generatore e unità di distribuzione (se disponibili).

    apt_windows_open : Optional[bool]
        Stato finestre dell'appartamento (True = almeno una finestra aperta).
        Utile per politiche di sicurezza/efficienza (es. stop raffrescamento se
        finestre aperte).

    presence_vacation : Optional[bool]
        Flag presenza “vacanza/assenza prolungata” (True = casa non occupata per periodo esteso).

    presence_nobodysin : Optional[bool]
        Flag presenza “nessuno in casa ora” (True = assente nell’immediato). Consente setback/eco.

    faults : tuple[str, ...]
        Elenco di eventuali **faults/allarmi** di impianto aggregati (codici brevi/slug).

    Metodi utility
    --------------
    mean_indoor_temperature() -> Optional[float]
        Media semplice delle temperature delle zone presenti (esclude None).

    mean_indoor_humidity() -> Optional[float]
        Media semplice delle RH delle zone presenti (esclude None).

    iter_zone_names() -> Iterable[str]
        Iteratore sui nomi delle zone (ordine del dizionario).
    """

    timestamp: datetime
    season: Optional[SeasonState] = None
    indoor_zones: Optional[dict[str, ZoneSnapshot]] = None
    outdoor_zones: Optional[dict[str, ZoneSnapshot]] = None

    vmc: Optional[VMCSnapshot] = None
    pdc: Optional[PDCSnapshot] = None
    supply_unit: Optional[SupplyUnitSnapshot] = None

    windows_close_state: Optional[bool] = None
    windows_close_minutes_off: Optional[float] = None

    presence_vacation: Optional[bool] = None
    presence_nobodysin: Optional[bool] = None

    global_indoor_zone: Optional[ZoneSnapshot] = None

    global_outdoor_dew_point: Optional[AggregatedValue] = None
    global_outdoor_humidity: Optional[AggregatedValue] = None
    global_outdoor_temperature: Optional[AggregatedValue] = None

    climate_hvac_mode: Optional[HVACMode] = None
    climate_preset_mode: Optional[HVACOperatingProfile] = None
    # dew_guard_active: Optional[bool] = None
    # free_cooling_possible: Optional[bool] = None
    faults: tuple[str, ...] = ()


    def iter_indoor_zone_names(self) -> Iterable[str]:
        """Itera i nomi delle zone presenti nello snapshot."""
        if not self.indoor_zones:
            return []
        return self.indoor_zones.keys()

    @property
    def indoor_zone_open_count(self) -> int:
        if self.indoor_zones is None:
            return 0
        
        return sum(1 for obj in self.indoor_zones.values() if (obj.radiant_valve.value if obj.radiant_valve else False))

    def __str__(self) -> str:
        try:
            # log_debug(_LOGGER, "PlantSnapshot A")
            # --- helper di formattazione compatti e robusti ---
            # def fnum(x, nd=1):
            #     return f"{x:.{nd}f}" if x is not None else "-"

            def fav(av, nd=1):
                """
                AggregatedValue -> stringa numerica compatta.
                Aggiunge marker:
                ? = is_insufficient
                * = is_stale
                """
                if av is None:
                    return "-"
                v = getattr(av, "value", None)
                s = fnum(v, nd) if v is not None else "-"
                if getattr(av, "is_insufficient", False):
                    s += "?"
                if getattr(av, "is_stale", False):
                    s += "*"
                return s

            # def fbool(b, on="on", off="off"):
            #     return on if b is True else (off if b is False else "-")

            def ffaults(faults):
                return ", ".join(faults) if faults else "-"

            def fmt_minutes_hm(minutes: Optional[float], *, nd_min: int = 0) -> str:
                """Format a duration given in minutes as 'Hh Mm' (or 'Mm').

                Examples:
                - 15      -> '15m'
                - 75      -> '1h 15m'
                - 61.7    -> '1h 2m'       (default rounds to nearest minute)
                - None    -> '-'
                """
                if minutes is None:
                    return "-"

                try:
                    m = float(minutes)
                except (TypeError, ValueError):
                    return "-"

                if m < 0:
                    m = 0.0

                # Round to requested precision in minutes, then convert to integer minutes for H/M split
                if nd_min <= 0:
                    total_min_int = int(round(m))
                    h, mm = divmod(total_min_int, 60)
                    return f"{h}h {mm}m" if h else f"{mm}m"

                # If you want decimals in minutes, keep them only in the minute part
                h = int(m // 60)
                mm = m - (h * 60)
                mm_str = f"{mm:.{nd_min}f}".rstrip("0").rstrip(".")
                return f"{h}h {mm_str}m" if h else f"{mm_str}m"

            # --- parti comuni/top-level ---
            ts = self.timestamp.isoformat()

            indoor: dict[str, ZoneSnapshot] | None = self.indoor_zones
            outdoor: dict[str, ZoneSnapshot] | None = self.outdoor_zones

            # --- composizione finale multi-line ---
            # lines = [
            #     f"Timestamp            :: {ts}",
            #     f"  Season             :: {self.season.season.value if self.season else '-'}",
            #     f"  Total days         :: {self.season.days if self.season else '-'}",
            #     f"  Days passed        :: {self.season.passed if self.season else '-'}",
            #     f"  Days remaining     :: {self.season.remaining if self.season else '-'}",
            #     f"  Weather anomaly    :: {self.season.weather_anomaly if self.season else '-'}",
            #     f"------------------------------------------------------------------",
            # ]
            lines = [
                f"",
                f"Climate entity setting",
                f"  HVAC mode          :: {self.climate_hvac_mode or '-'}",
                f"  Preset mode        :: {self.climate_preset_mode.value if self.climate_preset_mode else '-'}",
                f"------------------------------------------------------------------",
            ]
            
            lines += [line for line in str(self.season).splitlines() if line.strip()]

            # --- conteggio zone ---
            lines += [
                f"Indoor zones         :: {len(indoor or {})}",
            ]

            # --- dettaglio zone indoor ---
            if indoor:
                for key in sorted(indoor.keys()):
                    z = indoor[key]

                    sensors = (
                        f":: ["
                        f"T:{fav(z.temperature)} °C "
                        f"HI:{fav(z.heat_index)} °C "
                        f"RH:{fav(z.humidity, 0)} % "
                        f"DP:{fav(z.dew_point)} °C "
                        f"]"
                    )

                    lines += [
                        f"  {pad(z.name or key, width=16)}   "
                        f"{pad(sensors, width=44)}   -   "
                        f"[Flow:{fnum(z.flow_t)} °C Ret:{fnum(z.return_t)} °C] "
                        f"Circuit:{fbool(z.radiant_valve.value if z.radiant_valve else None, on='Open', off='Closed')} "
                        f"[t_op:{fav(z.t_op)} °C mrt:{fav(z.mrt)} °C cm:{fav(z.condensation_margin)} °C]"
                    ]

                    # if cb:
                    #     conf_band_air = (
                    #         f":: Opr. season: {cb.season} "
                    #         f"Air set: {fnum(cb.speed, 2)} "
                    #         f"Air best: {fnum(cb.v_air_best, 2)} "
                    #         f"Air low: {fnum(cb.v_air_lo, 2)} "
                    #         f"Air High: {fnum(cb.v_air_hi, 2)} "
                    #     )
                    #     conf_band_pov = (
                    #         f":: t_op: {cb.t_op} °C "
                    #         f"t_op_min: {fnum(cb.t_op_min)} °C "
                    #         f"t_op_max: {fnum(cb.t_op_max)} °C "
                    #         f"pmv: {fnum(cb.pmv)} "
                    #         f"ppd: {fnum(cb.ppd)} "
                    #         f"is in band: {fbool(cb.ok)} "
                    #     )
                    #     conf_band_diagnostic = (
                    #         f":: pmv_center: {cb.pmv_center} "
                    #         f"pmv_band: {fnum(cb.pmv_band)} "
                    #         f"met_used: {fnum(cb.met_used)} "
                    #         f"clo_used: {fnum(cb.clo_used)} "
                    #     )
                    #     lines += [
                    #         f"  {pad('confort band Air', width=18, align='right')} "
                    #         f"{conf_band_air}",
                    #         f"  {pad('Pov', width=18, align='right')} "
                    #         f"{conf_band_pov}",
                    #         f"  {pad('diagnostic', width=18, align='right')} "
                    #         f"{conf_band_diagnostic}"
                    #     ]

            # --- dettaglio zone outdoor (se utile) ---
            if outdoor:
                lines += [
                    f"------------------------------------------------------------------",
                    f"Outdoor zones        :: {len(outdoor)}",
                ]
                for key in sorted(outdoor.keys()):
                    z = outdoor[key]
                    sensors = (
                        f":: ["
                        f"T:{fav(z.temperature)} °C "
                        f"RH:{fav(z.humidity, 0)} % "
                        f"DP:{fav(z.dew_point)} °C"
                        f"]"
                    )
                    lines += [
                        f"  {pad(z.name or key, width=16)}   {sensors}"
                    ]

            # --- global aggregates (se presenti) ---
            if self.global_indoor_zone and any([
                self.global_outdoor_temperature, self.global_outdoor_humidity,
                self.global_outdoor_dew_point,
            ]):
                z = self.global_indoor_zone
                # cb = z.confort_band
                lines += [
                    f"------------------------------------------------------------------",
                    f"Global aggregates",
                ]
                sensors = (
                    f":: ["
                    f"T:{fav(z.temperature)} °C "
                    f"HI:{fav(z.heat_index)} °C "
                    f"RH:{fav(z.humidity, 0)} % "
                    f"DP:{fav(z.dew_point)} °C "
                    f"]"
                )

                lines += [
                    f"  {pad('Indoor means', width=16)}   "
                    f"{pad(sensors, width=44)}   -   "
                    f"[Flow:{fnum(z.flow_t)} °C Ret:{fnum(z.return_t)} °C] "
                    f"Valve:{fav(z.radiant_valve, 0)} "
                    f"[t_op:{fav(z.t_op)} °C mrt:{fav(z.mrt)} °C cm:{fav(z.condensation_margin)} °C]"
                ]

                lines += [
                    f"  Outdoor means      :: [T:{fav(self.global_outdoor_temperature)} °C RH:{fav(self.global_outdoor_humidity,0)} % "
                    f"DP:{fav(self.global_outdoor_dew_point)}°C]"
                ]

            # --- presence / safety / faults ---
            lines += [
                f"------------------------------------------------------------------",
                f"Home windows state   :: {fbool(self.windows_close_state, 'All Closed', 'Some Open')}",
                f"Home windows open time :: {fnum(self.windows_close_minutes_off,0)} min",
                f"Vacation state       :: {fbool(self.presence_vacation, 'True', 'False')}",
                f"Nobody's in state    :: {fbool(self.presence_nobodysin, 'True', 'False')}",
                # f"Dew guard active     :: {fbool(self.dew_guard_active, 'Yes', 'No')}",
                # f"Free-cooling possible:: {fbool(self.free_cooling_possible, 'Yes', 'No')}",
                f"Faults               :: {ffaults(self.faults)}",
                f"------------------------------------------------------------------",
            ]

            # --- PDC ---
            lines += [f"PDC"]
            if self.pdc:
                dt_pe = (
                    (self.pdc.sensor_t_water_out_pe - self.pdc.sensor_t_water_in_pe)
                    if (self.pdc.sensor_t_water_in_pe is not None and self.pdc.sensor_t_water_out_pe is not None)
                    else None
                )
                lines += [
                    f"  Workload FM power  :: {fbool(self.pdc.fm_power_on, 'On', 'Off')}",
                    f"  Device Power       :: {fbool(self.pdc.power_on, 'On', 'Off')}",
                    f"  Mode               :: {('heating' if self.pdc.device_mode == 1 else 'cooling') if self.pdc.device_mode is not None else '-'}",
                    f"  Heat-WOT           :: {fnum(self.pdc.wot_heat)} °C",
                    f"  Heat-ΔT            :: {fnum(self.pdc.delta_t_heat)} °C",
                    f"  Cool-WOT           :: {fnum(self.pdc.wot_cool)} °C",
                    f"  Cool-ΔT            :: {fnum(self.pdc.delta_t_cool)} °C",
                    f"  Water-PE-In        :: {fnum(self.pdc.sensor_t_water_in_pe)} °C",
                    f"  Water-PE-Out       :: {fnum(self.pdc.sensor_t_water_out_pe)} °C",
                    f"  Water-PE-ΔT        :: {fnum(dt_pe)} °C",
                    f"  Compressor         :: {fbool(self.pdc.sensor_compressor_state, 'On', 'Off')}",
                    f"  Uptime             :: {fnum(self.pdc.minutes_power_on,0)} min",
                    f"  Downtime           :: {fnum(self.pdc.minutes_power_off,0)} min",
                ]
            else:
                lines += [f"  -"]
            lines += [f"------------------------------------------------------------------"]

            # --- Supply Unit ---
            lines += [f"Supply Unit"]
            if self.supply_unit:
                dt_direct = (
                    (self.supply_unit.sensor_direct_temp_system_supply - self.supply_unit.sensor_direct_temp_system_return)
                    if (self.supply_unit.sensor_direct_temp_system_return is not None and self.supply_unit.sensor_direct_temp_system_supply is not None)
                    else None
                )
                dt_adj = (
                    (self.supply_unit.sensor_adjustable_temp_system_supply - self.supply_unit.sensor_adjustable_temp_system_return)
                    if (self.supply_unit.sensor_adjustable_temp_system_return is not None and self.supply_unit.sensor_adjustable_temp_system_supply is not None)
                    else None
                )
                dt_boiler = (
                    (self.supply_unit.sensor_boiler_temp_system_supply - self.supply_unit.sensor_boiler_temp_system_return)
                    if (self.supply_unit.sensor_boiler_temp_system_return is not None and self.supply_unit.sensor_boiler_temp_system_supply is not None)
                    else None
                )
                lines += [
                    f"  Direct Device Power:: {fbool(self.supply_unit.direct_su_power_on, 'On', 'Off')}",
                    f"  Direct Supply Flow :: {fnum(self.supply_unit.sensor_direct_temp_system_supply)} °C",
                    f"  Direct Return Flow :: {fnum(self.supply_unit.sensor_direct_temp_system_return)} °C",
                    f"  Direct-Water-ΔT    :: {fnum(dt_direct)} °C",
                    f"  Adj Device Power   :: {fbool(self.supply_unit.adjustable_su_power_on, 'On', 'Off')}",
                    f"  Adj Supply Flow    :: {fnum(self.supply_unit.sensor_adjustable_temp_system_supply)} °C",
                    f"  Adj Return Flow    :: {fnum(self.supply_unit.sensor_adjustable_temp_system_return)} °C",
                    f"  Adj-Water-ΔT       :: {fnum(dt_adj)} °C",
                    f"  3-Way Valve        :: {self.supply_unit.three_point_mixing_valve if self.supply_unit.three_point_mixing_valve is not None else '-'} %",
                    f"  Boiler Supply Flow :: {fnum(self.supply_unit.sensor_boiler_temp_system_supply)} °C",
                    f"  Boiler Return Flow :: {fnum(self.supply_unit.sensor_boiler_temp_system_return)} °C",
                    f"  Boiler-Water-ΔT    :: {fnum(dt_boiler)} °C",
                ]
            else:
                lines += [f"  -"]
            lines += [f"------------------------------------------------------------------"]

            # --- VMC ---
            lines += [f"VMC"]
            if self.vmc:
                lines += [
                    f"  Device Power       :: {fbool(self.vmc.power_on, 'On', 'Off')}",
                    f"  Setpoint T         :: {fnum(self.vmc.t_setpoint)} °C",
                    f"  Setpoint RH        :: {fnum(self.vmc.rh_setpoint,0)} %",
                    f"  Setpoint DP        :: {fnum(self.vmc.t_dew_point_setpoint)} °C",
                    f"  Setpoint ΔDP       :: {fnum(self.vmc.delta_t_dew_point_setpoint)} °C",
                    f"  Mode               :: {self.vmc.processing_mode if self.vmc.processing_mode is not None else '-'}",
                    f"  Req Water          :: {fbool(self.vmc.request_water)}",
                    f"  Req Dehumidif      :: {fbool(self.vmc.request_dehumidification)}",
                    f"  Req Heating        :: {fbool(self.vmc.request_heating)}",
                    f"  Req Cooling        :: {fbool(self.vmc.request_cooling)}",
                    f"  Sensor Ambient T   :: {fnum(self.vmc.sensor_t_ambient)} °C",
                    f"  Sensor Ambient RH  :: {fnum(self.vmc.sensor_h_ambient,0)} %",
                    f"  Sensor Water T     :: {fnum(self.vmc.sensor_t_water)} °C",
                    f"  Sensor Outdoor T   :: {fnum(self.vmc.sensor_t_outdoor)} °C",
                    f"  Alarm High Press   :: {fbool(self.vmc.alarm_high_pressure)}",
                    f"  Alarm Dew Point    :: {fbool(self.vmc.alarm_dew_point)}",
                    f"  Alarm Low Water T  :: {fbool(self.vmc.alarm_low_water_temp)}",
                    f"  Alarm High Water T :: {fbool(self.vmc.alarm_high_water_temp)}",
                    f"  Alarm General      :: {fbool(self.vmc.alarm_alarm)}",
                    f"  Uptime             :: {fnum(self.vmc.minutes_power_on,0)} min",
                    f"  Downtime           :: {fnum(self.vmc.minutes_power_off,0)} min",
                ]

                if self.vmc.spare_time_in_state_stats:
                    stiss = [int(self.vmc.spare_time_in_state_stats.durations_s.get(i, 0.0)/60) for i in range(6)]
                    for i, spare in enumerate(stiss):
                        lines += [
                            f"  Uptime Spare set {i} :: {fmt_minutes_hm(spare)}",
                        ]
            else:
                lines += [f"  -"]
            lines += [f"------------------------------------------------------------------"]

            # log_debug(_LOGGER, "PlantSnapshot E")
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            log_exception(_LOGGER, f"Error in PlantSnapshot.__str__: {e}")
            return f"[PlantSnapshot.__str__ error: {e}]"



