from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...devices.caleffi_pumps import CaleffiSupplyPumps
from ...devices.eurotherm import EurothermElectrovalve
from ...devices.eneren_rer020i import EnerenRER020I
from ...devices.aermec_hmi080 import AermecHMI080

from ...domain.models.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ...helpers.ha import set_entity_bool, set_entity_number
from ...helpers.logger import log_debug, log_info
from ...helpers.utils import as_float, slugify

from ..decision.contracts import PdcCommand, PlantDecision, VmcCommand

_LOGGER = logging.getLogger(__name__)

@dataclass(slots=True)
class _StagingState:
    """Staging state for safe hydronic actuation (no sleeps / no blocking delays)."""

    # Boiler readiness hysteresis
    boiler_ready: bool = False

    # Valve opening tracking
    valves_open_request_ts: Optional[datetime] = None
    last_valves_desired: dict[str, bool] = None  # type: ignore[assignment]


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
        # self._hass = hass
        self._snapshot = None
        self._runtime = runtime_cfg
        self._heatpump = AermecHMI080(hass=hass, runtime_cfg=runtime_cfg)
        self._vmc = EnerenRER020I(hass=hass, runtime_cfg=runtime_cfg)
        
        self._electrovalve = EurothermElectrovalve(hass=hass, runtime_cfg=runtime_cfg)
        self._supplypums = CaleffiSupplyPumps(hass=hass, runtime_cfg=runtime_cfg)

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
        return dt_util.utcnow()

    # def _read_float_state(self, entity_id: Optional[str]) -> Optional[float]:
    #     if not entity_id:
    #         return None
    #     st = self._hass.states.get(entity_id)
    #     if st is None:
    #         return None
    #     return as_float(st.state)

    # def _read_bool_state(self, entity_id: Optional[str]) -> Optional[bool]:
    #     if not entity_id:
    #         return None
    #     st = self._hass.states.get(entity_id)
    #     if st is None:
    #         return None
    #     v = (st.state or "").strip()

    #     if v == STATE_ON:
    #         return True
    #     if v == STATE_OFF:
    #         return False

    #     vlow = v.lower()
    #     if vlow in ("on", "true", "yes", "open", "running"):
    #         return True
    #     if vlow in ("off", "false", "no", "closed", "idle", "stopped"):
    #         return False

    #     # Numeric fallbacks
    #     try:
    #         fv = float(v)
    #         if fv == 1.0:
    #             return True
    #         if fv == 0.0:
    #             return False
    #     except Exception:
    #         pass

    #     return None

    @staticmethod
    def _mode_value(decision: PlantDecision) -> str:
        m = getattr(decision, "mode", None)
        return getattr(m, "value", str(m))

    @staticmethod
    def _pdc_requested_on(pdc: PdcCommand) -> bool:
        return bool(getattr(pdc, "power", False) or getattr(pdc, "fm_power", False))

    def _compute_target_c(self, decision: PlantDecision) -> Optional[float]:
        """Compute the best available 'water target' for boiler readiness."""
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
    ) -> tuple[bool, dict[str, Optional[float]]]:
        """Update boiler readiness with hysteresis.

        Returns (boiler_ready, debug_info).
        """
        dbg: dict[str, Optional[float]] = {
            "t_boiler_supply": t_boiler_supply,
            "t_target": t_target,
            "on_thr": None,
            "off_thr": None,
        }

        if t_boiler_supply is None or t_target is None:
            # Fallback: if no temperature sensor but compressor signal exists, we can only log.
            return self._stage.boiler_ready, dbg

        on_margin = float(self._boiler_ready_on_margin_c)
        off_margin = float(self._boiler_ready_off_margin_c)

        if mode == "heating":
            on_thr = float(t_target) - on_margin
            off_thr = float(t_target) - off_margin
            dbg["on_thr"] = on_thr
            dbg["off_thr"] = off_thr

            if not self._stage.boiler_ready:
                if float(t_boiler_supply) >= on_thr:
                    self._stage.boiler_ready = True
            else:
                if float(t_boiler_supply) < off_thr:
                    self._stage.boiler_ready = False

        elif mode in ("cooling", "dehum_assist"):
            on_thr = float(t_target) + on_margin
            off_thr = float(t_target) + off_margin
            dbg["on_thr"] = on_thr
            dbg["off_thr"] = off_thr

            if not self._stage.boiler_ready:
                if float(t_boiler_supply) <= on_thr:
                    self._stage.boiler_ready = True
            else:
                if float(t_boiler_supply) > off_thr:
                    self._stage.boiler_ready = False

        else:
            self._stage.boiler_ready = False

        return self._stage.boiler_ready, dbg

    def _desired_zone_valves(self, decision: PlantDecision) -> dict[str, bool]:
        """Compute desired zone valves ON/OFF (zone_key -> bool) from decision.

        Uses MPC plan if present; otherwise falls back to per-zone metrics in signals.
        """
        desired: dict[str, bool] = {}

        # Preferred source: explicit plant-level valves command.
        valves_cmd = getattr(decision, "valves", None)
        by_zone_cmd = getattr(valves_cmd, "by_zone", None) if valves_cmd is not None else None
        if by_zone_cmd:
            for zone_key, valve_on in by_zone_cmd.items():
                desired[str(zone_key)] = bool(valve_on)
            return desired

        zones_plan = getattr(decision, "zones", None)
        zones_map = getattr(zones_plan, "zones", None) if zones_plan is not None else None
        if zones_map:
            for zone_key, zcmd in zones_map.items():
                desired[str(zone_key)] = bool(getattr(zcmd, "valve_on", False))
            return desired

        sig = getattr(decision, "signals", None)
        if sig is None:
            return desired

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

        return desired

    # --------------------------- actuators ---------------------------

    async def _async_pdc_actuator(self, pdc_command: PdcCommand):
        """Apply a PDC command to HA entities."""

        # await self._heatpump.async_set_power(fm_power=pdc_command.fm_power, power=pdc_command.power) # NON MODIFICARE
        await self._heatpump.async_set_processing_mode(mode=pdc_command.mode)
        await self._heatpump.async_set_heat_setpoints(t=pdc_command.heat_wot_c, dt=pdc_command.heat_dt_c)
        await self._heatpump.async_set_cool_setpoints(t=pdc_command.cool_wot_c, dt=pdc_command.cool_dt_c)

    async def _async_vmc_actuator(self, vmc_command: VmcCommand):
        """Apply a VMC command to HA entities."""

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
    ) -> tuple[bool, dict[str, int]]:
        """Step (2): command zone valves when allowed.

        Returns (valves_ready, stats).
        """

        # Caso base: nessuna elettrovalvola configurata nel runtime.
        # In questo caso non c'è nulla da attendere, quindi consideriamo "ready" subito.
        # Puliamo anche lo stato di staging per coerenza.
        if not self._runtime.climate.areas:
            self._stage.valves_open_request_ts = None
            self._stage.last_valves_desired = {}
            return True, {"zones": 0, "on": 0}

        # Fail-safe: se in questo tick NON è consentito comandare le valvole
        # (es. PDC non attiva / non siamo nella finestra corretta dello staging),
        # chiudiamo tutte le valvole per evitare circolazioni indesiderate e resettiamo il timer.
        if not allow_valves:
            count = 0
            for area in self._runtime.climate.areas:
                if getattr(area, "thermal_collector_valve_switch", None):
                    count += 1
                    await self._electrovalve.async_set_circuit_open(area_name=area.name, state=False)
            # for _, entity_id in self._zone_valves.items():
            #     await set_entity_bool(self._hass, entity_id=entity_id, value=False)
            self._stage.valves_open_request_ts = None
            self._stage.last_valves_desired = {}
            return False, {"zones": count, "on": 0}

        # Calcola lo stato desiderato delle valvole (zona -> bool) dalla decisione:
        # - Preferibilmente dal piano MPC (decision.zones)
        # - In fallback da metriche per-zona (decision.signals)
        desired_raw = self._desired_zone_valves(decision)

        # Applica comandi alle entità HA reali.
        # Nota: iteriamo sul mapping "zona logica -> entity_id" e:
        # - default OFF per zone non presenti in desired_raw
        # - inviamo un set_entity_bool per ogni zona gestita.
        desired: dict[str, bool] = {}
        for area in self._runtime.climate.areas:
            if getattr(area, "thermal_collector_valve_switch", None):
                desired[slugify(area.name)] = bool(desired_raw.get(slugify(area.name), False))
                await self._electrovalve.async_set_circuit_open(area_name=area.name, state=desired[slugify(area.name)])

        # for zone_key, entity_id in self._zone_valves.items():
        #     desired[zone_key] = bool(desired_raw.get(zone_key, False))
        #     await set_entity_bool(self._hass, entity_id=entity_id, value=desired[zone_key])

        # Statistiche utili (logging/diagnostica)
        on_cnt = sum(1 for x in desired.values() if x)
        any_open = on_cnt > 0

        # Rileva una transizione OFF->ON rispetto al tick precedente.
        # Serve per (ri)avviare il timer di apertura quando comincia ad aprire
        # almeno una valvola che prima era chiusa.
        last = self._stage.last_valves_desired or {}
        opening_transition = any(
            desired.get(z, False) and not last.get(z, False)
            for z in desired.keys()
        )

        # Gestione del timestamp di "richiesta apertura":
        # - se almeno una valvola è ON:
        #     - avvia il timer se non esiste
        #     - oppure lo resetta se si è appena verificata una transizione OFF->ON
        # - se tutte sono OFF: azzera il timer (non ha senso attendere "apertura completa")
        now = self._now()
        if any_open:
            if self._stage.valves_open_request_ts is None or opening_transition:
                self._stage.valves_open_request_ts = now
        else:
            self._stage.valves_open_request_ts = None

        # Salva lo snapshot dei comandi desiderati per confronti al tick successivo.
        self._stage.last_valves_desired = desired

        # Se non esiste un timestamp di apertura, le valvole NON possono essere "ready":
        # - oppure perché sono tutte OFF
        # - oppure perché il timer è stato resettato/azzerato
        if self._stage.valves_open_request_ts is None:
            return False, {"zones": len(desired), "on": on_cnt}

        # Calcola da quanto tempo è iniziata (o ri-iniziata) l'apertura di almeno una valvola
        # e dichiara "ready" quando supera la soglia configurata (es. 95s).
        # Nota termotecnica: per attuatori elettrotermici lenti è un proxy pratico per "aperta completamente".
        elapsed = (now - self._stage.valves_open_request_ts).total_seconds()
        valves_ready = elapsed >= float(self._valve_open_delay_s)

        return valves_ready, {"zones": len(desired), "on": on_cnt}

    async def _async_supply_actuator(
        self,
        decision: PlantDecision,
        *,
        pdc_on: bool,
        boiler_ready: bool,
        valves_ready: bool,
    ) -> tuple[bool, bool, Optional[float]]:
        """Step (3): command pumps/mixing with staging gates.

        Returns (direct_on, adj_on, mix_valve_pct_applied).
        """
        su = self._supply_cfg
        if su is None:
            return False, False, None

        supply_cmd = getattr(decision, "supply", None)
        if supply_cmd is None:
            return False, False, None

        direct_desired = bool(getattr(supply_cmd, "direct_pump_on", False))
        adj_desired = bool(getattr(supply_cmd, "adj_pump_on", False))

        direct_on = bool(pdc_on and boiler_ready and direct_desired)
        adj_on = bool(pdc_on and boiler_ready and valves_ready and adj_desired)

        await self._supplypums.async_set_direct_power(power=direct_on)
        await self._supplypums.async_set_adj_power(power=adj_on)

        # await set_entity_bool(self._hass, entity_id=str(su.direct_supply_unit), value=direct_on)
        # await set_entity_bool(self._hass, entity_id=str(su.adjustable_supply_unit), value=adj_on)

        mv_applied: Optional[float] = None
        mv = as_float(getattr(supply_cmd, "mix_valve_pct", None))
        if mv is not None and adj_on:
            mv_applied = float(mv)

            await self._supplypums.async_set_mix_adj_setpoints(value=mv_applied)

            # await set_entity_number(
            #     self._hass,
            #     entity_id=str(su.three_point_mixing_valve),
            #     value=mv_applied,
            #     min_value=0.0,
            #     max_value=100.0,
            #     blocking=False,
            # )

        return direct_on, adj_on, mv_applied

    async def async_apply(self, snapshot: PlantSnapshot, decision: PlantDecision) -> None:
        """..."""
        self._snapshot = snapshot
        pdc_command = decision.pdc
        vmc_command = decision.vmc

        # (1) PDC + VMC first
        await self._async_pdc_actuator(pdc_command)
        await self._async_vmc_actuator(vmc_command)

        pdc_req_on = self._pdc_requested_on(pdc_command)
        compressor_on = (self._snapshot.pdc.sensor_compressor_state or False) if self._snapshot.pdc else False
        # compressor_on = self._read_bool_state(self._pdc_compressor_state_ent)

        t_boiler_supply = as_float(self._snapshot.supply_unit.sensor_boiler_temp_system_supply) if self._snapshot.supply_unit else 0
        # t_boiler_supply = self._read_float_state(self._boiler_supply_ent)
        mode = self._mode_value(decision)
        t_target = self._compute_target_c(decision)

        # Boiler readiness hysteresis (updated each tick)
        prev_ready = self._stage.boiler_ready
        boiler_ready, boiler_dbg = self._update_boiler_ready(
            mode=mode,
            t_boiler_supply=t_boiler_supply,
            t_target=t_target,
            compressor_on=compressor_on,
        )

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

        valves_ready, valves_stats = await self._async_zone_valves_actuator(decision, allow_valves=allow_valves)

        # Step (3)
        direct_on, adj_on, mv_applied = await self._async_supply_actuator(
            decision,
            pdc_on=pdc_on,
            boiler_ready=boiler_ready,
            valves_ready=valves_ready,
        )

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

        log_debug(
            _LOGGER,
            "[staging] mode=%s pdc_req=%s comp=%s pdc_on=%s boiler_t=%s target=%s ready=%s (on_thr=%s off_thr=%s) "
            "valves=%d(on=%d) valves_ready=%s pumps(direct=%s adj=%s) mix=%s reasons=%s",
            mode,
            bool(pdc_req_on),
            compressor_on,
            pdc_on,
            f"{t_boiler_supply:.2f}" if t_boiler_supply is not None else "-",
            f"{t_target:.2f}" if t_target is not None else "-",
            boiler_ready,
            f"{boiler_dbg.get('on_thr'):.2f}" if boiler_dbg.get("on_thr") is not None else "-",
            f"{boiler_dbg.get('off_thr'):.2f}" if boiler_dbg.get("off_thr") is not None else "-",
            int(valves_stats.get("zones", 0)),
            int(valves_stats.get("on", 0)),
            valves_ready,
            direct_on,
            adj_on,
            f"{mv_applied:.1f}" if mv_applied is not None else "-",
            ",".join(reasons) if reasons else "-",
        )
