"""Policy layer for DRP Climate Master v2 comfort calculations.

Purpose
-------
This module provides a *policy* abstraction that selects the *inputs* for the
physical comfort model (PMV/PPD band + v_air robustness) based on:

- Italian climate zone (A..F)
- operative season (winter/summer/shoulder)
- runtime mode (normal/eco/boost/sleep/away)
- room-specific draft risk calibration
- optional compliance gate (off / warn / enforce)

The policy layer does NOT replace physics.
It produces a PolicyDecision that is then passed to ComfortBandCalculator.

Integration pattern
-------------------
In your coordinator/strategy per zone:

    ctx = PolicyContext(
        now=dt_util.utcnow(),
        room="kitchen",
        season=OperativeSeason.WINTER,
        vmc_speed=vmc_speed,
        rh_pct=rh,
        t_op_current=t_op,
        mode="normal",
        outdoor_temp=t_out,
    )

    decision = policy.decide(ctx)

    res = comfort_calc.compute_comfort_band(
        speed=ctx.vmc_speed,
        room=ctx.room,
        season=ctx.season,
        rh_pct=ctx.rh_pct,
        t_op_current=ctx.t_op_current,
        pmv_center=decision.pmv_center,
        pmv_band=decision.pmv_band,
        met_override=decision.met,
        clo_override=decision.clo,
        v_air_scale_best=decision.v_air_scale_best,
        v_air_scale_lo=decision.v_air_scale_lo,
        v_air_scale_hi=decision.v_air_scale_hi,
    )

Then you can surface decision.reasons as HA attributes for transparency.

Notes
-----
- Italian climate zones A..F are typically defined by degree-days (DPR 412/1993).
  This file intentionally does not implement municipality->zone lookup.
  Provide climate_zone in config (or compute upstream).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Dict, Literal, Optional, Tuple

from ...domain.enums import HVACOperatingProfile
from ...domain.models.season import OperativeSeason

try:
    # Preferito in Home Assistant per gestione timezone corretta
    from homeassistant.util import dt as dt_util  # type: ignore
except Exception:  # pragma: no cover
    dt_util = None

# -----------------------------
# Public types
# -----------------------------

# ClimateZoneIT = Literal["A", "B", "C", "D", "E", "F"]
# ComfortMode = Literal["normal", "eco", "boost", "sleep", "away"]
# ComfortMode = Literal[HVACOperatingProfile.values()]
# ComplianceMode = Literal["off", "warn", "enforce"]

class ClimateZoneIT(StrEnum):
    """Zona climatica italiana (A-F)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"

    @classmethod
    def is_member(cls, value: Any) -> bool:
        """
        Ritorna True se `value` appartiene all'enumeration.

        Accetta sia:
        - una stringa (es. "A", "b", " C "), normalizzata con strip() e upper()
        - un'istanza di ClimateZoneIT (es. ClimateZoneIT.A)

        Args:
            value: stringa o ClimateZoneIT (o altro tipo)

        Returns:
            True se il valore rappresenta una zona valida (A..F), altrimenti False.
        """
        if isinstance(value, cls):
            return True
        if isinstance(value, str):
            v = value.strip().upper()
            return v in cls._value2member_map_
        return False

    @classmethod
    def from_str(cls, value: Optional[str]) -> Optional["ClimateZoneIT"]:
        """
        Converte una stringa in `ClimateZoneIT`.

        - Normalizza con `strip()` e `upper()`
        - Se `value` è None, vuota o non valida, ritorna None

        Args:
            value: stringa come "A", "b", "  C " oppure None

        Returns:
            Un membro `ClimateZoneIT` oppure None.
        """
        if value is None:
            return None
        v = value.strip().upper()
        if not v:
            return None
        try:
            return cls(v)
        except ValueError:
            return None

class ComplianceMode(StrEnum):
    """
    Modalità di *compliance gating* per la policy HVAC.

    Questa enum definisce **come** il sistema deve comportarsi quando una decisione
    (es. richiesta di heating/cooling) cade **fuori** da vincoli di conformità
    configurati (tipicamente finestre orarie, regole contrattuali/condominiali,
    limiti imposti da policy energetiche, ecc.).

    L'enum viene usata come “interruttore di severità”:
    - `OFF`: nessun controllo/nessun blocco;
    - `WARN`: controlla e segnala, ma lascia procedere;
    - `ENFORCE`: controlla e blocca se non conforme.

    Essendo uno `StrEnum`, i membri:
    - sono confrontabili come stringhe (es. `mode == "warn"`),
    - serializzano naturalmente in JSON/YAML (salvando il valore stringa).
    """

    OFF = "off"
    """
    Compliance disabilitata.

    Il sistema non applica vincoli di conformità e non produce warning
    (equivale a ignorare eventuali finestre/limitazioni configurate).
    """

    WARN = "warn"
    """
    Compliance in modalità avviso.

    Se una richiesta è fuori dai vincoli configurati, la policy:
    - registra un warning (log/telemetria/evento),
    - **non** blocca l'azione: la decisione può comunque essere eseguita.

    Utile in fase di tuning o per monitorare l'impatto dei vincoli prima di
    renderli vincolanti.
    """

    ENFORCE = "enforce"
    """
    Compliance in modalità vincolante.

    Se una richiesta è fuori dai vincoli configurati, la policy deve:
    - segnalare la non conformità (come in WARN),
    - **impedire** l'azione (blocco/hard stop), ad esempio:
      - non emettendo comandi verso PDC/VMC/attuatori,
      - forzando un profilo neutro/sicuro,
      - ritornando una decisione "denied" al controller.
    """


# -----------------------------
# Configuration / data model
# -----------------------------

@dataclass(slots=True)
class PolicyConfig:
    """
    Configurazione statica del *policy layer* (comfort + guardrail operativi).

    Questa classe raccoglie i parametri “di intenzione” che la policy usa per:
    - definire valori base di comfort (MET/CLO) in funzione della stagione;
    - calibrare la gestione del *draft risk* (correnti d’aria) tramite fattori di scala;
    - opzionalmente applicare vincoli di compliance temporale (finestre orarie
      in cui è consentito chiedere heating/cooling).

    Note concettuali:
    - **met** (Metabolic Equivalent): misura dell’attività metabolica (adimensionale).
    - **clo**: isolamento dell’abbigliamento (adimensionale).
    - I campi “*_hi_scale” sono **moltiplicatori** (es. 1.05 = +5%) applicabili
      a soglie/limiti “conservativi” della velocità aria (v_air_hi) quando cambia
      il profilo stanza o la velocità VMC.

    La classe è pensata come “contenitore” + piccoli metodi helper, in modo che
    la logica di policy resti leggibile e centralizzata.
    """

    # -------------------------------------------------------------------------
    # Contesto climatico
    # -------------------------------------------------------------------------

    climate_zone: Optional[ClimateZoneIT] = None
    """
    Zona climatica italiana (opzionale).

    Se valorizzata, la policy può:
    - applicare correzioni/offset a parametri stagionali;
    - utilizzare (se abilitato) un delta clo invernale specifico di zona
      (il delta vero e proprio può essere fornito dal layer superiore).
    """

    # -------------------------------------------------------------------------
    # Comfort profile (metabolismo / vestiario)
    # -------------------------------------------------------------------------

    base_met: float = 1.10
    """
    MET base usato per il calcolo del comfort (tipicamente persona seduta / attività leggera).
    Valori tipici:
      - 1.0-1.2: seduto / attività molto leggera
      - 1.2-1.6: attività leggera in casa
    """

    base_clo_summer: float = 0.50
    """CLO base estivo (abbigliamento leggero, casa in estate)."""

    base_clo_shoulder: float = 0.70
    """CLO base mezze stagioni (primavera/autunno)."""

    default_clo_winter: float = 1.00
    """CLO base invernale (valore di default, prima di eventuali correzioni per zona)."""

    zone_clo_delta_enabled: bool = True
    """
    Se True e `climate_zone` è valorizzata, la policy *può* applicare un delta CLO invernale
    specifico di zona climatica.

    Importante: questo flag abilita la *possibilità* di applicare il delta.
    Il valore del delta può essere gestito altrove (mapping, database, costante),
    e passato alla method `winter_clo(zone_delta=...)`.
    """

    # -------------------------------------------------------------------------
    # Draft calibration (correnti d'aria / rischio discomfort)
    # -------------------------------------------------------------------------

    non_living_high_speed_hi_scale: float = 0.90
    """
    Fattore di scala per stanze NON living (bagni, corridoi, camere poco occupate)
    a velocità VMC medio/alta.

    Interpretazione: riduce una soglia/limite conservativo (es. v_air_hi) rendendo
    la policy meno restrittiva in ambienti dove il rischio “sofa/diffusore” è minore.
    Esempio: 0.90 => -10%.
    """

    living_high_speed_hi_scale: float = 1.05
    """
    Fattore di scala per stanze living (soggiorno, aree divano) a velocità VMC medio/alta.

    Interpretazione: aumenta una soglia/limite (es. v_air_hi) rendendo la policy più prudente
    dove il rischio di corrente percepita è più alto (posizione persone, getti, diffusori).
    Esempio: 1.05 => +5%.
    """

    # -------------------------------------------------------------------------
    # Compliance gating (finestre orarie ammesse)
    # -------------------------------------------------------------------------

    compliance_mode: ComplianceMode = ComplianceMode.OFF
    """
    Modalità compliance:
      - "off": nessun vincolo temporale, nessun warning.
      - "warn": se fuori finestra, la policy segnala (log/telemetria) ma non blocca.
      - "enforce": se fuori finestra, la policy deve impedire la richiesta (blocco/hard stop).

    Nota: i metodi helper qui sotto calcolano solo “dentro/fuori finestra”.
    Il comportamento warn/enforce tipicamente viene applicato dal controller/policy engine.
    """

    heating_allowed_from: Optional[time] = None
    heating_allowed_to: Optional[time] = None
    """
    Finestra oraria locale (HH:MM) in cui è consentito chiedere *heating*.
    Se uno dei due è None => vincolo disabilitato (sempre consentito).
    """

    cooling_allowed_from: Optional[time] = None
    cooling_allowed_to: Optional[time] = None
    """
    Finestra oraria locale (HH:MM) in cui è consentito chiedere *cooling*.
    Se uno dei due è None => vincolo disabilitato (sempre consentito).
    """

    # -------------------------------------------------------------------------
    # Metodi helper (comfort + compliance)
    # -------------------------------------------------------------------------

    def clo_for_season(self, season: Literal["summer", "shoulder", "winter"]) -> float:
        """
        Restituisce il CLO base per la stagione richiesta.

        Args:
            season: "summer" | "shoulder" | "winter"

        Returns:
            CLO base per stagione (per l'inverno: `default_clo_winter`).
            Se vuoi applicare un delta zona in inverno, usa `winter_clo(zone_delta=...)`.
        """
        if season == "summer":
            return self.base_clo_summer
        if season == "shoulder":
            return self.base_clo_shoulder
        return self.default_clo_winter

    def winter_clo(self, *, zone_delta: Optional[float] = None) -> float:
        """
        CLO invernale effettivo, con possibilità di applicare un delta (tipicamente da climate zone).

        Args:
            zone_delta: delta CLO da sommare al valore base (es. +0.05 o -0.10).
                Se None, non viene applicata alcuna correzione.

        Returns:
            CLO invernale risultante.

        Note:
            - Il delta viene applicato solo se `zone_clo_delta_enabled` è True.
            - La scelta del delta (mapping per zona climatica) è responsabilità del layer superiore.
        """
        clo = self.default_clo_winter
        if self.zone_clo_delta_enabled and zone_delta is not None:
            clo += zone_delta
        return clo

    def draft_hi_scale(self, *, is_living: bool) -> float:
        """
        Restituisce il fattore di scala da usare per la calibrazione del draft ad alta velocità VMC.

        Args:
            is_living: True se stanza "living" (soggiorno/zone con persone sedute e rischio corrente),
                       False per stanze non-living.

        Returns:
            Moltiplicatore (float) da applicare a una soglia/limite “hi” (es. v_air_hi).
        """
        return self.living_high_speed_hi_scale if is_living else self.non_living_high_speed_hi_scale

    def allowed_window(self, mode: Literal["heating", "cooling"]) -> tuple[Optional[time], Optional[time]]:
        """
        Restituisce (from, to) della finestra oraria configurata per heating/cooling.

        Args:
            mode: "heating" oppure "cooling"

        Returns:
            Coppia (allowed_from, allowed_to). Se uno dei due è None => vincolo non attivo.
        """
        if mode == "heating":
            return self.heating_allowed_from, self.heating_allowed_to
        return self.cooling_allowed_from, self.cooling_allowed_to

    def is_within_allowed_window(self, *, mode: Literal["heating", "cooling"], now_local: time) -> bool:
        """
        Verifica se l'orario locale corrente è dentro la finestra consentita per la modalità.

        Supporta anche finestre che attraversano la mezzanotte.
        Esempio: 22:00 -> 06:00 (consentito dalle 22:00 fino alle 06:00 del giorno dopo).

        Args:
            mode: "heating" oppure "cooling"
            now_local: orario locale corrente (datetime.time)

        Returns:
            True se:
              - finestra non configurata (from/to None), oppure
              - now_local rientra nella finestra (anche wrap midnight).
        """
        start, end = self.allowed_window(mode)
        if start is None or end is None:
            return True  # vincolo disabilitato

        if start <= end:
            # finestra standard: es. 06:00-23:00  (end escluso)
            return start <= now_local < end

        # finestra wrap midnight: es. 22:00-06:00
        return now_local >= start or now_local < end

    def compliance_enabled(self) -> bool:
        """
        True se la compliance è attiva (warn/enforce), False se "off".
        """
        return self.compliance_mode in (ComplianceMode.WARN, ComplianceMode.ENFORCE)

    def validate(self) -> None:
        """
        Validazione minimale dei parametri.

        Solleva ValueError se individua valori non plausibili (negativi o nulli dove non ha senso).
        Utile da chiamare in fase di bootstrap/config load.
        """
        if self.base_met <= 0:
            raise ValueError("base_met deve essere > 0")

        for name, v in (
            ("base_clo_summer", self.base_clo_summer),
            ("base_clo_shoulder", self.base_clo_shoulder),
            ("default_clo_winter", self.default_clo_winter),
            ("non_living_high_speed_hi_scale", self.non_living_high_speed_hi_scale),
            ("living_high_speed_hi_scale", self.living_high_speed_hi_scale),
        ):
            if v <= 0:
                raise ValueError(f"{name} deve essere > 0")

        if self.compliance_mode not in (ComplianceMode.OFF, ComplianceMode.WARN, ComplianceMode.ENFORCE):
            raise ValueError("compliance_mode deve essere: 'off', 'warn' o 'enforce'")



@dataclass(frozen=True, slots=True)
class PolicyContext:
    """
    Contesto *runtime* usato per calcolare una decisione di policy HVAC.

    Questa struttura dati rappresenta lo “snapshot” minimo delle informazioni
    necessarie alla policy per:
    - valutare comfort e rischio (es. UR, temperatura operativa, stagione);
    - applicare profili operativi (Comfort/Eco/Boost/...);
    - modulare regole legate a VMC (velocità/portata) e caratteristiche della stanza;
    - (opzionale) includere condizioni esterne per strategie meteo-aware.

    Tipicamente viene costruita dal controller/coordinator a ogni ciclo di decisione
    e passata al motore di policy (che produce un *policy decision* o un setpoint).
    """

    # ---------------------------------------------------------------------
    # Identità e tempo di valutazione
    # ---------------------------------------------------------------------

    now: datetime
    """
    Timestamp (timezone-aware, preferibilmente) della valutazione.

    Deve rappresentare “adesso” nel momento in cui si prende la decisione,
    e viene usato per:
    - gating temporale (finestre di compliance),
    - logging/telemetria,
    - eventuali logiche di hysteresis/anti-rimbalzo.
    """

    room: str
    """
    Identificativo logico della stanza/zona.

    Esempi:
    - "living"
    - "kitchen"
    - "master_bedroom"

    Serve per:
    - distinguere regole “living vs non-living” (draft calibration, priorità),
    - mappare verso attuatori/sensori della zona,
    - logging e tracciabilità.
    """

    # ---------------------------------------------------------------------
    # Condizioni operative / regime impianto
    # ---------------------------------------------------------------------

    season: OperativeSeason
    """
    Stagione operativa corrente (es. WINTER / SUMMER / SHOULDER).

    È una stagione “di controllo” (decisionale), non necessariamente
    la stagione meteorologica/solare.
    Influenza comfort target (clo), strategie e vincoli.
    """

    vmc_speed: int
    """
    Velocità/step corrente della VMC.

    Convenzione tipica: 0..N (o 1..N) a seconda del tuo device.
    La policy può usarla per:
    - stimare impatto su ventilazione e correnti d’aria,
    - scalare soglie di draft (v_air_hi),
    - decidere se limitare/consentire azioni in certi regimi.
    """

    # ---------------------------------------------------------------------
    # Stato termoigrometrico interno (zona)
    # ---------------------------------------------------------------------

    rh_pct: float
    """
    Umidità relativa interna della zona in percentuale (0..100).

    Usata per:
    - comfort (PMV/PPD, dew point, sensazione di “secco/umido”),
    - rischio condensa (se combinata con temperature superficiali),
    - decisioni deumidifica/ventilazione.
    """

    t_op_current: Optional[float]
    """
    Temperatura operativa corrente della zona (°C), se disponibile.

    La temperatura operativa (T_op) è una combinazione di:
    - temperatura aria (T_air)
    - temperatura media radiante (MRT)

    Se non disponibile (None), la policy può:
    - degradare su T_air o su un fallback,
    - dichiarare decisione “insufficient data”,
    - applicare HOLD-LAST-GOOD (se previsto dal tuo sistema).
    """

    # ---------------------------------------------------------------------
    # Profilo di controllo (intent)
    # ---------------------------------------------------------------------

    mode: HVACOperatingProfile
    """
    Profilo operativo richiesto (preset): Comfort / Eco / Boost / Sleep / Away / Vacation.

    Influenza:
    - setpoint e tolleranze,
    - aggressività del controllo (ramp rate, hysteresis),
    - priorità tra comfort e consumi,
    - eventuali limitazioni (es. Away riduce heating/cooling).
    """

    # ---------------------------------------------------------------------
    # Condizioni esterne (opzionali)
    # ---------------------------------------------------------------------

    outdoor_temp: Optional[float] = None
    """
    Temperatura esterna (°C), se disponibile.

    Può essere usata per:
    - strategie meteo-aware (anticipare carichi, regole di regime),
    - controlli di fattibilità (es. free-cooling / limiti PDC),
    - guardrail (evitare cooling con condizioni esterne sfavorevoli).

    Se None, la policy non deve assumere valori esterni noti.
    """



@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Alias/compat: decisione policy usata dal ComfortBandCalculator."""

    met: float
    clo: float
    pmv_center: float
    pmv_band: float

    # Patch A: controller-side knob (the physics does not change)
    ctrl_aggressiveness: float = 1.0

    # Naming allineato al ComfortBandCalculator
    v_air_best_scale: float = 1.0
    v_air_hi_scale: float = 1.0
    v_air_lo_override: Optional[float] = None
    draft_robustness: Optional[float] = None

    # Optional compliance flags (None = not evaluated)
    heating_allowed: Optional[bool] = None
    cooling_allowed: Optional[bool] = None

    # Human-readable reasons for debugging/telemetry
    reasons: Tuple[str, ...] = ()


# -----------------------------
# Defaults / tables
# -----------------------------


# Heuristic mapping of winter clothing insulation by climate zone.
# This is a *policy* knob: it influences comfort by changing clothing insulation,
# not by hardcoding any temperature setpoint.
ZONE_CLO_WINTER: Dict[ClimateZoneIT, float] = {
    ClimateZoneIT.A: 0.90,
    ClimateZoneIT.B: 0.95,
    ClimateZoneIT.C: 1.00,
    ClimateZoneIT.D: 1.05,
    ClimateZoneIT.E: 1.15,
    ClimateZoneIT.F: 1.25,
}


# Per-mode PMV targets.
#
# IMPORTANT SEMANTICS (Patch A)
# ----------------------------
# - BOOST does NOT mean a different comfort category: it means **same PMV band as COMFORT**,
#   but the *controller* is allowed to react more aggressively (see MODE_CTRL_DEFAULTS).
# - ECO and SLEEP are *stricter* (narrower PMV band). SLEEP also biases slightly cooler.
# - AWAY/VACATION are not comfort modes: they allow large discomfort (wide band).
#
# Interpretation:
# - pmv_center: desired mean vote bias
# - pmv_band: half-width of acceptable PMV band
MODE_PMV_DEFAULTS: Dict[HVACOperatingProfile, Dict[str, float]] = {
    # Baseline comfort
    HVACOperatingProfile.COMFORT: {"pmv_center": 0.00, "pmv_band": 0.50},

    # BOOST: same comfort category as COMFORT; controller may be more aggressive
    HVACOperatingProfile.BOOST: {"pmv_center": 0.00, "pmv_band": 0.50},

    # ECO/SLEEP: stricter band (narrower) -> less oscillation around comfort.
    # SLEEP: slightly cooler bias.
    HVACOperatingProfile.ECO: {"pmv_center": -0.10, "pmv_band": 0.35},
    # Sleep: vero "setback" (piu fresco) e tolleranza piu ampia.
    # Nota: PMV per il sonno e una proxy; la usiamo per non scaldare troppo presto.
    HVACOperatingProfile.SLEEP: {"pmv_center": -0.40, "pmv_band": 0.45},

    # AWAY/VACATION: not comfort; allow wide discomfort.
    HVACOperatingProfile.AWAY: {"pmv_center": -0.60, "pmv_band": 1.20},
    HVACOperatingProfile.VACATION: {"pmv_center": -0.60, "pmv_band": 1.20},
}


# Controller aggressiveness (Patch A)
#
# This is intentionally decoupled from PMV targets.
# The comfort model remains purely physical; the controller can tune its “reaction”.
#
#  - 1.00 = baseline
#  - >1.0  = more aggressive (faster / less conservative)
#  - <1.0  = more conservative (slower / savings-oriented)
MODE_CTRL_DEFAULTS: Dict[HVACOperatingProfile, float] = {
    HVACOperatingProfile.COMFORT: 1.00,
    HVACOperatingProfile.BOOST: 1.35,
    HVACOperatingProfile.ECO: 0.85,
    HVACOperatingProfile.SLEEP: 0.75,
    HVACOperatingProfile.AWAY: 0.50,
    HVACOperatingProfile.VACATION: 0.50,
}


# -----------------------------
# Implementation
# -----------------------------


class ComfortPolicyLayer:
    """Policy layer that selects comfort model inputs.

    Design principles
    -----------------
    - Keep the physics in ComfortBandCalculator.
    - Make policy decisions transparent (reasons).
    - Allow gradual tuning via config.

    What it controls
    ----------------
    - met, clo: occupant archetype
    - pmv_center, pmv_band: comfort category / bias
    - v_air scaling: adjust conservatism of the v_air table
    - optional compliance windows for heating/cooling
    """

    def __init__(
            self, 
            cfg: PolicyConfig
    ) -> None:
        self._cfg = cfg

    # -------------------------
    # Public API
    # -------------------------

    def decide(self, ctx: PolicyContext) -> PolicyDecision:
        reasons: list[str] = []

        # 0) Validate inputs lightly
        vmc_speed = ctx.vmc_speed
        if not isinstance(vmc_speed, int):
            reasons.append(f"vmc_speed:invalid_type={type(vmc_speed).__name__}")
            try:
                vmc_speed = int(vmc_speed)  # best effort
            except Exception:
                vmc_speed = 0
        if vmc_speed < 0 or vmc_speed > 5:
            reasons.append(f"vmc_speed:clamped_from={vmc_speed}")
            vmc_speed = max(0, min(5, vmc_speed))

        # 1) met (mode-aware)
        met = float(self._cfg.base_met)
        if ctx.mode == HVACOperatingProfile.SLEEP:
            met = 0.90
            reasons.append("met:sleep=0.90")
        elif ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            met = 1.00
            reasons.append("met:away/vacation=1.00")

        # 2) clo (season + climate zone + mode)
        clo = self._clo_for(ctx, reasons)

        # 3) PMV targets from mode
        pmv_center, pmv_band = self._pmv_targets(ctx, reasons)

        # 3b) Controller aggressiveness (decoupled from PMV)
        ctrl_aggr = float(MODE_CTRL_DEFAULTS.get(ctx.mode, 1.0))
        reasons.append(f"ctrl:aggr={ctrl_aggr:.2f}")

        # 4) v_air scaling (draft calibration)
        v_best_s, v_hi_s, v_lo_override = self._v_air_policy(ctx, vmc_speed, reasons)

        # Draft robustness (optional): used by ComfortBandCalculator to blend v_best/v_hi
        draft_alpha: Optional[float] = None
        is_living: Optional[bool] = self._is_living(ctx.room) if ctx.room is not None else None
        if is_living is not None:
            draft_alpha = 0.55 if is_living else 0.35
            if ctx.mode == HVACOperatingProfile.SLEEP:
                # Di notte evitiamo di essere "draft conservative":
                # altrimenti alza t_op_min e anticipa heating.
                draft_alpha = max(0.05, draft_alpha - 0.10)
                reasons.append("SLEEP_delta_draft=-0.10")
            if (vmc_speed >= 4) and (not is_living):
                draft_alpha = max(0.0, draft_alpha - 0.05)

        # 5) optional compliance gating
        heating_allowed, cooling_allowed = self._compliance(ctx, reasons)

        return PolicyDecision(
            met=float(met),
            clo=float(clo),
            pmv_center=float(pmv_center),
            pmv_band=float(pmv_band),
            ctrl_aggressiveness=float(ctrl_aggr),
            v_air_best_scale=float(v_best_s),
            v_air_hi_scale=float(v_hi_s),
            v_air_lo_override=v_lo_override,
            draft_robustness=draft_alpha,
            heating_allowed=heating_allowed,
            cooling_allowed=cooling_allowed,
            reasons=tuple(reasons),
        )

    # -------------------------
    # Internals
    # -------------------------

    @staticmethod
    def _norm_room(room: str) -> str:
        return (room or "").strip().lower()

    def _is_living(self, room: str) -> bool:
        r = self._norm_room(room)
        return (r == "living") or ("soggiorno" in r) or ("salotto" in r)

    def _clo_for(self, ctx: PolicyContext, reasons: list[str]) -> float:
        # Base by season
        if ctx.season == OperativeSeason.SUMMER:
            clo = float(self._cfg.base_clo_summer)
            reasons.append(f"clo:summer={clo:.2f}")
        elif ctx.season == OperativeSeason.SHOULDER:
            clo = float(self._cfg.base_clo_shoulder)
            reasons.append(f"clo:shoulder={clo:.2f}")
        else:
            # Winter: use climate zone mapping if enabled and zone is known
            if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                clo = float(ZONE_CLO_WINTER.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                reasons.append(f"clo:winter:zone={self._cfg.climate_zone}={clo:.2f}")
            else:
                clo = float(self._cfg.default_clo_winter)
                reasons.append(f"clo:winter:default={clo:.2f}")

        # Mode adjustments
        # - sleep: blanket / duvet effect in winter
        if ctx.mode == HVACOperatingProfile.SLEEP and ctx.season == OperativeSeason.WINTER:
            # Effetto "coperta/duvet" (isolamento maggiore rispetto all'awake)
            clo += 0.45
            reasons.append(f"clo:sleep:+0.45 -> {clo:.2f}")

        # - away: assume lighter (nobody cares), but keep bounded
        if ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION) and ctx.season == OperativeSeason.WINTER:
            clo = max(0.70, clo - 0.10)
            reasons.append(f"clo:away:-0.10 -> {clo:.2f}")

        clo_cap = 2.0 if ctx.mode == HVACOperatingProfile.SLEEP else 1.6
        clo = min(clo_cap, clo)
        return float(clo)

    def _pmv_targets(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[float, float]:
        md = MODE_PMV_DEFAULTS.get(ctx.mode, MODE_PMV_DEFAULTS[HVACOperatingProfile.COMFORT])
        pmv_center = float(md["pmv_center"])
        pmv_band = float(md["pmv_band"])
        reasons.append(f"pmv:mode={ctx.mode}:center={pmv_center:+.2f},band={pmv_band:.2f}")

        # Optional tightening/loosening by season (small nudges)
        if ctx.season == OperativeSeason.SUMMER and ctx.mode == HVACOperatingProfile.ECO:
            # In summer eco typically tolerates warmer -> slightly positive center
            pmv_center = min(0.20, pmv_center + 0.10)
            reasons.append(f"pmv:summer_eco:center_adj -> {pmv_center:+.2f}")

        return pmv_center, pmv_band

    def _v_air_policy(self, ctx: PolicyContext, vmc_speed: int, reasons: list[str]) -> Tuple[float, float, Optional[float]]:
        # Defaults: no scaling / no override
        v_best_s = 1.0
        v_hi_s = 1.0
        v_lo_override: Optional[float] = None

        living = self._is_living(ctx.room)

        # High-speed draft conservatism tuning
        if vmc_speed >= 4 and not living:
            v_hi_s = float(self._cfg.non_living_high_speed_hi_scale)
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (speed>=4 non-living)")

        if living and vmc_speed >= 3:
            v_hi_s = max(v_hi_s, float(self._cfg.living_high_speed_hi_scale))
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (living speed>=3)")

        # Sleep mode: people more sensitive to draft (exposed skin) -> slightly higher v_hi
        if ctx.mode == HVACOperatingProfile.SLEEP and ctx.season == OperativeSeason.WINTER:
            # Riduciamo conservativismo sul draft in Sleep
            # (porte chiuse / esposizione ridotta)
            v_hi_s = min(v_hi_s, 1.00)
            reasons.append("v_air_hi:sleep<=1.00")

        return float(v_best_s), float(v_hi_s), v_lo_override

    def _compliance(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[Optional[bool], Optional[bool]]:
        if self._cfg.compliance_mode == ComplianceMode.OFF:
            return None, None

        now_local = ctx.now
        if dt_util is not None:
            now_local = dt_util.as_local(ctx.now)  # Europe/Rome tipicamente
        elif ctx.now.tzinfo is not None:
            now_local = ctx.now.astimezone()

        # If windows are not defined, treat as not-evaluable.
        h_ok = self._time_in_window(
            now_local,
            self._cfg.heating_allowed_from,
            self._cfg.heating_allowed_to,
        )
        c_ok = self._time_in_window(
            now_local,
            self._cfg.cooling_allowed_from,
            self._cfg.cooling_allowed_to,
        )

        # Only evaluate when season suggests a regime
        heating_allowed: Optional[bool] = None
        cooling_allowed: Optional[bool] = None

        if ctx.season == OperativeSeason.WINTER:
            heating_allowed = h_ok
            if heating_allowed is not None:
                reasons.append(f"compliance:heating_allowed={heating_allowed}")
        elif ctx.season == OperativeSeason.SUMMER:
            cooling_allowed = c_ok
            if cooling_allowed is not None:
                reasons.append(f"compliance:cooling_allowed={cooling_allowed}")

        # SHOULDER: do not gate by default (could be both)
        return heating_allowed, cooling_allowed

    @staticmethod
    def _time_in_window(now: datetime, start: Optional[time], end: Optional[time]) -> Optional[bool]:
        """Return whether now.local_time is within [start, end). Supports overnight windows.

        If start or end is None -> returns None (not evaluable).
        Caller decides whether to use the result.

        Important: 'now' should already be in local timezone if you care about local legality.
        """
        if start is None or end is None:
            return None

        t = now.timetz().replace(tzinfo=None)  # compare naive wall-clock

        if start <= end:
            # same-day window
            return start <= t < end

        # overnight window (e.g., 22:00-06:00)
        return (t >= start) or (t < end)


# -----------------------------
# Helpers to build from your config
# -----------------------------


def build_policy_layer(
    *,
    climate_zone: Optional[ClimateZoneIT] = ClimateZoneIT.D,
    compliance_mode: ComplianceMode = ComplianceMode.OFF,
) -> ComfortPolicyLayer:
    """Convenience builder with minimal validation."""

    cz: Optional[ClimateZoneIT]
    if climate_zone is None:
        cz = None
    else:
        v = str(climate_zone).strip().upper()
        cz = ClimateZoneIT.from_str(v) if ClimateZoneIT.is_member(v) else None

    cfg = PolicyConfig(
        climate_zone=cz,
        compliance_mode=compliance_mode,
    )
    return ComfortPolicyLayer(cfg)
