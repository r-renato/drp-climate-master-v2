from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...helpers.formatter import fbool, fnum, fstr

from ...devices.caleffi_pumps import CaleffiSupplyPumps
from ...devices.eurotherm import EurothermElectrovalve
from ...devices.eneren_rer020i import EnerenRER020I
from ...devices.aermec_hmi080 import AermecHMI080

from ...domain.models.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ...helpers.logger import log_debug, log_info
from ...helpers.utils import as_float, slugify

from ..decision.contracts import PdcCommand, PlantDecision, VmcCommand

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BoilerReadinessDebug:
    """Dati diagnostici della logica di *boiler readiness*.

    Questa struttura sostituisce il precedente `dict[str, Optional[float]]` per:
    - evitare chiavi stringa fragili
    - rendere espliciti i significati termotecnici (soglie ON/OFF)

    Attributi
    ---------
    t_boiler_supply_c:
        Temperatura attuale di mandata (lato impianto / buffer) usata come proxy di prontezza.
    t_target_c:
        Target di temperatura acqua (WOT o target mandata) ricavato dalla decisione.
    on_thr_c / off_thr_c:
        Soglie di isteresi per l'aggiornamento di `boiler_ready`.
    """

    t_boiler_supply_c: Optional[float]
    t_target_c: Optional[float]
    on_thr_c: Optional[float]
    off_thr_c: Optional[float]


@dataclass(slots=True)
class BoilerReadinessUpdate:
    """Risultato dell'aggiornamento dello stato di prontezza dell'acqua (boiler/buffer)."""

    ready: bool
    debug: BoilerReadinessDebug


@dataclass(slots=True)
class ZoneValvesDesired:
    """Stato desiderato delle valvole di zona (zona -> ON/OFF).

    Incapsula la mappa per evitare di passare `dict` grezzi tra funzioni e per aggiungere
    utilità (conteggi, proprietà) in modo tipizzato.
    """

    by_zone: dict[str, bool] = field(default_factory=dict)

    @property
    def on_count(self) -> int:
        """Numero di zone richieste ON."""
        return sum(1 for v in self.by_zone.values() if v)

    @property
    def any_open(self) -> bool:
        """True se almeno una zona è richiesta ON."""
        return self.on_count > 0


@dataclass(slots=True)
class ZoneValvesStats:
    """Statistiche/diagnostica per lo staging delle elettrovalvole.

    Attributi
    ---------
    zones_total:
        Numero di zone effettivamente gestite (valvola configurata).
    zones_on:
        Numero di zone richieste ON.
    requested_at:
        Timestamp del primo comando di apertura (o dell'ultimo reset per transizione OFF->ON).
    elapsed_s:
        Secondi trascorsi dall'inizio apertura; None se non applicabile.
    opening_transition:
        True se almeno una valvola è passata OFF->ON in questo tick (usato per reset timer).
    """

    zones_total: int
    zones_on: int
    requested_at: Optional[datetime]
    elapsed_s: Optional[float]
    opening_transition: bool


@dataclass(slots=True)
class ZoneValvesActuationResult:
    """Risultato dello step (2): comando valvole + valutazione 'ready'."""

    ready: bool
    desired: ZoneValvesDesired
    stats: ZoneValvesStats


@dataclass(slots=True)
class SupplyActuationResult:
    """Risultato dello step (3): comando pompe/miscelatrice.

    Attributi
    ---------
    direct_on:
        Stato applicato alla pompa diretta (eventuale anello diretto).
    adj_on:
        Stato applicato alla pompa regolabile (tipicamente circuito radiante + miscelatrice).
    mix_valve_pct_applied:
        Setpoint miscelatrice applicato (0..100) se presente e se `adj_on` è True.
    """

    direct_on: bool
    adj_on: bool
    mix_valve_pct_applied: Optional[float]


@dataclass(slots=True)
class _StagingState:
    """Stato interno di staging per attuazione idronica sicura (no sleep, no blocchi).

    Nota
    ----
    Lo staging evita di:
    - aprire circuiti radianti quando la PDC non sta realmente erogando
    - far partire pompe su collettori con elettrovalvole ancora in fase di apertura
    - oscillare rapidamente su readiness grazie a isteresi.
    """

    # Boiler readiness hysteresis
    boiler_ready: bool = False

    # Valve opening tracking
    valves_open_request_ts: Optional[datetime] = None
    last_valves_desired: dict[str, bool] = field(default_factory=dict)


class PlantActuator:
    """Translate a ControlPlan into Home Assistant service calls.

    Staging rationale (impianto con accumulo + valvole elettrotermiche):
      1) Comanda PDC + VMC subito (warm-up accumulo/boiler).
      2) Comanda le elettrovalvole di zona quando la PDC è effettivamente "attiva"
         (compressore ON) oppure quando l'acqua risulta già pronta (boiler_ready).
         Le valvole richiedono ~95s per aprirsi completamente.
      3) Comanda pompe/miscelatrice solo se:
           - PDC richiesta ON
           - acqua boiler pronta (con isteresi)
           - (per radiante) valvole ormai aperte

    """
    def __init__(self, hass: HomeAssistant, runtime_cfg: RuntimeConfig):
        """Crea l'attuatore di impianto.

        Parametri
        ---------
        hass:
            Istanza di Home Assistant.
        runtime_cfg:
            Configurazione runtime dell'integrazione (device, aree, sensori).
        """

        self._snapshot: Optional[PlantSnapshot] = None
        # Serializza l'applicazione dei comandi: evita race tra tick ravvicinati
        # e side-effect concorrenti su staging state / dispositivi.
        self._apply_lock = asyncio.Lock()
        self._runtime = runtime_cfg
        self._heatpump = AermecHMI080(hass=hass, runtime_cfg=runtime_cfg)
        self._vmc = EnerenRER020I(hass=hass, runtime_cfg=runtime_cfg)
        
        self._electrovalve = EurothermElectrovalve(hass=hass, runtime_cfg=runtime_cfg)
        self._supply_pumps = CaleffiSupplyPumps(hass=hass, runtime_cfg=runtime_cfg)

        self._radiant_cfg = getattr(runtime_cfg.climate.devices, "radiant", None)
        self._supply_cfg = getattr(runtime_cfg.climate.devices, "supply_units", None)

        # Sensors
        self._pdc_compressor_state_ent: Optional[str] = None
        if self._radiant_cfg and getattr(self._radiant_cfg, "sensors", None):
            self._pdc_compressor_state_ent = getattr(self._radiant_cfg.sensors, "pdc_compressor_state", None)

        self._boiler_supply_ent: Optional[str] = None
        if self._supply_cfg and getattr(self._supply_cfg, "sensors", None):
            self._boiler_supply_ent = getattr(self._supply_cfg.sensors, "boiler_temp_system_supply", None)

        # Zone valves mapping: slugified zone_key -> HA switch entity_id
        # self._zone_valves: dict[str, str] = {}
        # for area in (runtime_cfg.climate.areas or []):
        #     ent = getattr(area, "thermal_collector_valve_switch", None)
        #     if ent:
        #         self._zone_valves[slugify(area.name)] = str(ent)

        self._stage = _StagingState(boiler_ready=False, valves_open_request_ts=None, last_valves_desired={})

        # Tuning knobs (can be promoted to config later)
        self._valve_open_delay_s: float = 95.0

        # Boiler readiness hysteresis margins (°C)
        # - ON when within on_margin from target
        # - OFF only when drifting beyond off_margin (off_margin > on_margin)
        self._boiler_ready_on_margin_c: float = 0.5
        self._boiler_ready_off_margin_c: float = 1.5

    # ------------------------- low-level helpers -------------------------

    @staticmethod
    def _now() -> datetime:
        """Timestamp corrente (UTC) coerente con Home Assistant."""
        return dt_util.utcnow()

    @staticmethod
    def _mode_value(decision: PlantDecision) -> str:
        """Ritorna il valore stringa della modalità (heating/cooling/...) anche se è un Enum."""
        m = getattr(decision, "mode", None)
        return getattr(m, "value", str(m))

    @staticmethod
    def _pdc_requested_on(pdc: PdcCommand) -> bool:
        """True se la decisione richiede PDC ON (power o fm_power)."""
        return bool(getattr(pdc, "power", False) or getattr(pdc, "fm_power", False))

    def _compute_target_c(self, decision: PlantDecision) -> Optional[float]:
        """Calcola il miglior target di temperatura acqua disponibile per la readiness.

        Strategia (ordine di priorità):
        1) `decision.supply.rad_supply_target_c` se presente (target specifico impianto/radiante)
        2) `decision.pdc.heat_wot_c` / `decision.pdc.cool_wot_c` in base alla modalità

        Ritorna
        -------
        float | None:
            Target in °C, oppure None se non determinabile.
        """
        mode = self._mode_value(decision)
        supply = getattr(decision, "supply", None)
        if supply is not None:
            t = as_float(getattr(supply, "rad_supply_target_c", None))
            if t is not None:
                return float(t)

        pdc = getattr(decision, "pdc", None)
        if pdc is None:
            return None

        if mode == "heating":
            return as_float(getattr(pdc, "heat_wot_c", None))
        if mode in ("cooling", "dehum_assist"):
            return as_float(getattr(pdc, "cool_wot_c", None))
        return None

    def _update_boiler_ready(
        self,
        *,
        mode: str,
        t_boiler_supply: Optional[float],
        t_target: Optional[float],
        compressor_on: Optional[bool],
    ) -> BoilerReadinessUpdate:
        """Aggiorna `boiler_ready` con isteresi.

        Parametri
        ---------
        mode:
            Modalità di impianto (heating/cooling/dehum_assist/...).
        t_boiler_supply:
            Temperatura di mandata misurata usata come proxy di prontezza (°C).
        t_target:
            Target di temperatura acqua (°C).
        compressor_on:
            Stato compressore (se disponibile). Non viene usato per calcolare soglie,
            ma è utile per diagnosi/fallback (mancanza sensori).

        Ritorna
        -------
        BoilerReadinessUpdate:
            Oggetto tipizzato con `ready` e diagnostica (soglie, valori).
        """

        dbg = BoilerReadinessDebug(
            t_boiler_supply_c=t_boiler_supply,
            t_target_c=t_target,
            on_thr_c=None,
            off_thr_c=None,
        )

        if t_boiler_supply is None or t_target is None:
            # Fallback: if no temperature sensor but compressor signal exists, we can only log.
            return BoilerReadinessUpdate(ready=self._stage.boiler_ready, debug=dbg)

        on_margin = float(self._boiler_ready_on_margin_c)
        off_margin = float(self._boiler_ready_off_margin_c)

        if mode == "heating":
            on_thr = float(t_target) - on_margin
            off_thr = float(t_target) - off_margin
            dbg.on_thr_c = on_thr
            dbg.off_thr_c = off_thr

            if not self._stage.boiler_ready:
                if float(t_boiler_supply) >= on_thr:
                    self._stage.boiler_ready = True
            else:
                if float(t_boiler_supply) < off_thr:
                    self._stage.boiler_ready = False

        elif mode in ("cooling", "dehum_assist"):
            on_thr = float(t_target) + on_margin
            off_thr = float(t_target) + off_margin
            dbg.on_thr_c = on_thr
            dbg.off_thr_c = off_thr

            if not self._stage.boiler_ready:
                if float(t_boiler_supply) <= on_thr:
                    self._stage.boiler_ready = True
            else:
                if float(t_boiler_supply) > off_thr:
                    self._stage.boiler_ready = False

        else:
            self._stage.boiler_ready = False

        return BoilerReadinessUpdate(ready=self._stage.boiler_ready, debug=dbg)

    def _desired_zone_valves(self, decision: PlantDecision) -> ZoneValvesDesired:
        """Calcola lo stato desiderato delle valvole di zona (zona -> bool).

        Fonti (priorità):
        1) `decision.valves.by_zone` (comando esplicito)
        2) `decision.zones.zones[*].valve_on` (piano MPC/zone planner)
        3) fallback su metriche per-zona in `decision.signals` (soglie on_thr)
        """
        desired: dict[str, bool] = {}

        # Preferred source: explicit plant-level valves command.
        valves_cmd = getattr(decision, "valves", None)
        by_zone_cmd = getattr(valves_cmd, "by_zone", None) if valves_cmd is not None else None
        if by_zone_cmd:
            for zone_key, valve_on in by_zone_cmd.items():
                desired[str(zone_key)] = bool(valve_on)
            return ZoneValvesDesired(by_zone=desired)

        zones_plan = getattr(decision, "zones", None)
        zones_map = getattr(zones_plan, "zones", None) if zones_plan is not None else None
        if zones_map:
            for zone_key, zcmd in zones_map.items():
                desired[str(zone_key)] = bool(getattr(zcmd, "valve_on", False))
            return ZoneValvesDesired(by_zone=desired)

        sig = getattr(decision, "signals", None)
        if sig is None:
            return ZoneValvesDesired(by_zone=desired)

        mode = self._mode_value(decision)
        if mode == "heating":
            by_zone = getattr(sig, "heat_def_by_zone_c", None) or {}
            thr = as_float(getattr(sig, "heat_on_thr_c", None)) or 0.3
            for k, v in by_zone.items():
                fv = as_float(v) or 0.0
                desired[str(k)] = bool(fv >= float(thr))
        elif mode in ("cooling", "dehum_assist"):
            by_zone = getattr(sig, "cool_sur_by_zone_c", None) or {}
            thr = as_float(getattr(sig, "cool_on_thr_c", None)) or 0.3
            for k, v in by_zone.items():
                fv = as_float(v) or 0.0
                desired[str(k)] = bool(fv >= float(thr))

        return ZoneValvesDesired(by_zone=desired)

    # --------------------------- actuators ---------------------------

    async def _async_pdc_actuator(self, pdc_command: PdcCommand):
        """Applica un comando PDC (pompa di calore) alle entità Home Assistant."""

        # await self._heatpump.async_set_power(fm_power=pdc_command.fm_power, power=pdc_command.power) # NON MODIFICARE
        await self._heatpump.async_set_processing_mode(mode=pdc_command.mode)
        await self._heatpump.async_set_heat_setpoints(t=pdc_command.heat_wot_c, dt=pdc_command.heat_dt_c)
        await self._heatpump.async_set_cool_setpoints(t=pdc_command.cool_wot_c, dt=pdc_command.cool_dt_c)

    async def _async_vmc_actuator(self, vmc_command: VmcCommand):
        """Applica un comando VMC (ventilazione meccanica controllata) alle entità HA."""

        await self._vmc.async_set_power(power=vmc_command.power)
        await self._vmc.async_set_processing_mode(mode=vmc_command.mode)
        await self._vmc.async_set_spare(spare=vmc_command.air_speed)
        await self._vmc.async_set_temperature(target=vmc_command.setpoint_t_c)
        await self._vmc.async_set_humidity(target=vmc_command.setpoint_rh_pct)
        await self._vmc.async_set_dew_point(target=vmc_command.setpoint_dp_c)
        await self._vmc.async_set_delta_dew_point(target=vmc_command.setpoint_ddp_c)

    async def _async_zone_valves_actuator(
        self,
        decision: PlantDecision,
        *,
        allow_valves: bool,
    ) -> ZoneValvesActuationResult:
        """Comanda le elettrovalvole di zona e valuta la 'prontezza' (ready).

        Nota: invia comandi solo se cambia lo stato rispetto al tick precedente (idempotenza).
        """
        areas = [
            a for a in (self._runtime.climate.areas or [])
            if getattr(a, "thermal_collector_valve_switch", None)
        ]

        if not areas:
            self._stage.valves_open_request_ts = None
            self._stage.last_valves_desired = {}
            desired = ZoneValvesDesired(by_zone={})
            stats = ZoneValvesStats(
                zones_total=0,
                zones_on=0,
                requested_at=None,
                elapsed_s=None,
                opening_transition=False,
            )
            return ZoneValvesActuationResult(ready=True, desired=desired, stats=stats)

        if not allow_valves:
            # Fail-safe: chiudi tutto. Evita comandi ripetuti se già chiuse.
            last = self._stage.last_valves_desired or {}
            closed_map: dict[str, bool] = {}
            for area in areas:
                zkey = slugify(area.name)
                closed_map[zkey] = False
                if last.get(zkey) is not False:
                    await self._electrovalve.async_set_circuit_open(
                        area_name=area.name,
                        state=False,
                    )
            self._stage.valves_open_request_ts = None
            # Mantieni uno stato consistente (tutte OFF) per:
            # - idempotenza
            # - transizioni OFF->ON corrette al tick successivo
            self._stage.last_valves_desired = closed_map
            desired = ZoneValvesDesired(by_zone={})
            stats = ZoneValvesStats(
                zones_total=len(areas),
                zones_on=0,
                requested_at=None,
                elapsed_s=None,
                opening_transition=False,
            )
            return ZoneValvesActuationResult(ready=False, desired=desired, stats=stats)

        desired_raw = self._desired_zone_valves(decision)
        by_zone: dict[str, bool] = desired_raw.by_zone

        desired_map: dict[str, bool] = {}
        last = self._stage.last_valves_desired or {}
        for area in areas:
            zkey = slugify(area.name)
            desired_map[zkey] = bool(by_zone.get(zkey, False))
            # Idempotenza: invia comando solo se cambia stato
            if last.get(zkey) != desired_map[zkey]:
                await self._electrovalve.async_set_circuit_open(
                    area_name=area.name,
                    state=desired_map[zkey],
                )

        on_cnt = sum(1 for v in desired_map.values() if v)
        any_open = on_cnt > 0

        opening_transition = any(desired_map.get(z, False) and not last.get(z, False) for z in desired_map)

        now = self._now()
        if any_open:
            if self._stage.valves_open_request_ts is None or opening_transition:
                self._stage.valves_open_request_ts = now
        else:
            self._stage.valves_open_request_ts = None

        self._stage.last_valves_desired = desired_map

        ts = self._stage.valves_open_request_ts
        elapsed: Optional[float] = (now - ts).total_seconds() if ts is not None else None
        valves_ready = bool(elapsed is not None and elapsed >= float(self._valve_open_delay_s))

        desired = ZoneValvesDesired(by_zone=desired_map)
        stats = ZoneValvesStats(
            zones_total=len(desired_map),
            zones_on=on_cnt,
            requested_at=ts,
            elapsed_s=elapsed,
            opening_transition=opening_transition,
        )
        return ZoneValvesActuationResult(ready=valves_ready, desired=desired, stats=stats)


    async def _async_supply_actuator(
        self,
        decision: PlantDecision,
        *,
        pdc_on: bool,
        boiler_ready: bool,
        valves_ready: bool,
    ) -> SupplyActuationResult:
        """Step (3): comanda pompe e miscelatrice rispettando i gate di staging.

        Gate tipici (impianto con buffer + radiante):
        - pompe attive solo se PDC è ON (richiesta o compressore)
        - avvio distribuzione solo se acqua "pronta" (isteresi)
        - per circuito radiante: attendo anche elettrovalvole aperte (proxy temporale)
        """
        su = self._supply_cfg
        if su is None:
            return SupplyActuationResult(False, False, None)

        supply_cmd = getattr(decision, "supply", None)
        if supply_cmd is None:
            return SupplyActuationResult(False, False, None)

        direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False))
        adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False))

        direct_on = bool(pdc_on and boiler_ready and direct_desired)
        adj_on = bool(pdc_on and boiler_ready and valves_ready and adj_desired)

        await self._supply_pumps.async_set_direct_power(power=direct_on)
        await self._supply_pumps.async_set_adj_power(power=adj_on)

        # await set_entity_bool(self._hass, entity_id=str(su.direct_supply_unit), value=direct_on)
        # await set_entity_bool(self._hass, entity_id=str(su.adjustable_supply_unit), value=adj_on)

        mv_applied: Optional[float] = None
        mv = as_float(getattr(supply_cmd, "mix_valve_pct", None))
        if mv is not None and adj_on:
            mv_applied = float(mv)

            await self._supply_pumps.async_set_mix_adj_setpoints(value=mv_applied)

            # await set_entity_number(
            #     self._hass,
            #     entity_id=str(su.three_point_mixing_valve),
            #     value=mv_applied,
            #     min_value=0.0,
            #     max_value=100.0,
            #     blocking=False,
            # )

        return SupplyActuationResult(direct_on=direct_on, adj_on=adj_on, mix_valve_pct_applied=mv_applied)

    async def async_apply(self, snapshot: PlantSnapshot, decision: PlantDecision) -> None:
        """Applica una `PlantDecision` allo stato reale dell'impianto.

        Il metodo implementa lo staging in 3 step:
        1) PDC + VMC: comandi immediati
        2) Valvole di zona: solo quando consentito (PDC realmente in erogazione o acqua pronta)
        3) Pompe/miscelatrice: solo quando i gate termici/idraulici sono soddisfatti
        """
        async with self._apply_lock:
            self._snapshot = snapshot
            pdc_command = decision.pdc
            vmc_command = decision.vmc

            # (1) PDC + VMC first
            await self._async_pdc_actuator(pdc_command)
            await self._async_vmc_actuator(vmc_command)

            pdc_req_on = self._pdc_requested_on(pdc_command)
            compressor_on: Optional[bool] = (
                self._snapshot.pdc.sensor_compressor_state
                if (self._snapshot and self._snapshot.pdc)
                else None
            )

            t_boiler_supply: Optional[float] = (
                as_float(self._snapshot.supply_unit.sensor_boiler_temp_system_supply)
                if (self._snapshot and self._snapshot.supply_unit)
                else None
            )
            mode = self._mode_value(decision)
            t_target = self._compute_target_c(decision)

            # Boiler readiness hysteresis (updated each tick)
            prev_ready = self._stage.boiler_ready
            boiler_update = self._update_boiler_ready(
                mode=mode,
                t_boiler_supply=t_boiler_supply,
                t_target=t_target,
                compressor_on=compressor_on,
            )
            boiler_ready = boiler_update.ready
            boiler_dbg = boiler_update.debug

            if prev_ready != boiler_ready:
                log_info(
                    _LOGGER,
                    "Boiler_ready changed: %s -> %s (mode=%s t=%.2f target=%s)",
                    prev_ready,
                    boiler_ready,
                    mode,
                    t_boiler_supply if t_boiler_supply is not None else float("nan"),
                    f"{t_target:.2f}" if t_target is not None else "-",
                )

            # Determine when we are allowed to actuate valves.
            # We consider PDC 'on' if either requested ON or compressor is ON.
            pdc_on = bool(pdc_req_on or compressor_on is True)

            # Step (2) gating:
            # - If compressor state is available: wait for compressor ON (PDC actually producing)
            #   OR boiler_ready already True (compressor may cycle OFF when target reached).
            # - If compressor state is NOT available: fall back to requested PDC ON.
            if compressor_on is None:
                allow_valves = bool(pdc_on)
            else:
                allow_valves = bool(pdc_on and (compressor_on is True or boiler_ready))

            valves_result = await self._async_zone_valves_actuator(decision, allow_valves=allow_valves)
            valves_ready = valves_result.ready
            valves_stats = valves_result.stats

            # Step (3)
            supply_result = await self._async_supply_actuator(
                decision,
                pdc_on=pdc_on,
                boiler_ready=boiler_ready,
                valves_ready=valves_ready,
            )
            direct_on = supply_result.direct_on
            adj_on = supply_result.adj_on
            mv_applied = supply_result.mix_valve_pct_applied

            # ---- Debug staging log (single line, reasoned)
            reasons: list[str] = []
            if not pdc_on:
                reasons.append("pdc_off")
            if pdc_on and not boiler_ready:
                reasons.append("boiler_not_ready")
                if compressor_on is False:
                    reasons.append("waiting_compressor")
            if pdc_on and boiler_ready and not valves_ready and bool(getattr(getattr(decision, "supply", None), "adj_pump_on", False)):
                reasons.append("valves_not_ready")

            if compressor_on is None and self._pdc_compressor_state_ent:
                reasons.append("compressor_state_unavailable")

            msg = (
                f"[staging] mode={mode} pdc_req={bool(pdc_req_on)} comp={fbool(compressor_on)} "
                f"pdc_on={fbool(pdc_on)} boiler_t={fnum(t_boiler_supply, 2)} "
                f"target={fnum(t_target, 2)} ready={fbool(boiler_ready)} "
                f"(on_thr={fnum(boiler_dbg.on_thr_c, 2)} off_thr={fnum(boiler_dbg.off_thr_c, 2)}) "
                f"valves={int(valves_stats.zones_total)}(on={int(valves_stats.zones_on)}) "
                f"valves_ready={fbool(valves_ready)} "
                f"pumps(direct={fbool(direct_on)} adj={fbool(adj_on)}) "
                f"mix={fnum(mv_applied, 1)} reasons={fstr(reasons)}"
            )
            log_debug(_LOGGER, msg)

