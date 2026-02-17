#
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Hashable, Mapping
import math
from typing import Any, Callable, Final, Optional, Awaitable, cast
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from enum import StrEnum

import inspect

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.util import dt as dt_util

from homeassistant.const import (
    STATE_ON, STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE
)

from .utils import as_float, clamp
from .logger import log_exception, log_warning
from ..const import TURN_OFF, TURN_ON

_LOCKS_KEY: Final = "set_entity_number_locks"

# Service mapping: entity domain -> (service domain, service name)
_SERVICE_BY_DOMAIN: Final[dict[str, tuple[str, str]]] = {
    "number": ("number", "set_value"),
    "input_number": ("input_number", "set_value"),
}

@dataclass(frozen=True)
class EntityTimeInStateStats:
    """Statistiche di permanenza “time-in-state” per una singola entità Home Assistant.

    Questa struttura rappresenta, su una finestra temporale definita (`window_start` → `as_of`),
    per quanto tempo (in secondi) l’entità è rimasta in ciascuna “chiave” (stato/bucket)
    calcolata tramite una funzione di mapping (es. `State.state`, un attributo, un bucket numerico).

    È pensata per essere ottenuta interrogando lo storico (Recorder) e poi aggregando
    la timeline in segmenti consecutivi (stato_i valido da t_i a t_{i+1}).

    Attributi:
        entity_id:
            Entity ID Home Assistant (es. ``"number.vmc_set_ricambio"``).

        window_start:
            Inizio finestra temporale di aggregazione.
            Tipicamente:
            - “today”: mezzanotte locale convertita in UTC
            - “last_24h”: `as_of - 24h`
            Deve essere timezone-aware.

        as_of:
            Istante di riferimento/fine finestra temporale usata per il calcolo.
            Deve essere timezone-aware.

        durations_s:
            Mappa ``chiave -> secondi`` trascorsi in quella chiave entro la finestra.
            La chiave è ``Hashable`` per consentire:
            - stati stringa (``"on"``, ``"off"``, ``"idle"``…)
            - interi (bucket 0..5)
            - tuple (es. (hvac_mode, hvac_action))
            Il valore è espresso in secondi (float), già aggregato.

        current_key:
            Chiave calcolata dallo *stato corrente* dell’entità al momento `as_of`
            (cioè `hass.states.get(entity_id)` mappato con `key_fn`).
            Può essere ``None`` se:
            - l’entità non esiste in `hass.states`
            - lo stato è non mappabile (e non stai includendo un bucket “unknown”)

        current_key_age_min:
            Età in minuti della chiave corrente, cioè da quanto tempo l’entità è
            nell’attuale chiave (basato su `last_changed`/`last_updated` dello stato corrente).
            Può essere ``None`` se non determinabile (manca lo stato corrente o timestamp).
    """

    entity_id: str
    window_start: datetime
    as_of: datetime
    durations_s: Mapping[Hashable, float]
    current_key: Optional[Hashable]
    current_key_age_min: Optional[float]

def get_entity_value(entities_state: dict, entity_id: str | None) -> Any | None:
    """Restituisce lo stato di un'entità HA dallo store delle entità, o None se non disponibile."""
    if not entity_id:
        return None
    
    if entity_id not in entities_state:
        return None

    return entities_state[entity_id].state

_LOGGER = logging.getLogger(__name__)

class EntityType(StrEnum):
    SWITCH = "switch"
    INPUT_BOOLEAN = "input_boolean"
    NUMBER = "number"
    SELECT = "select"
    INPUT_SELECT = "input_select"

    @classmethod
    def _coerce(cls, value: "EntityType | str") -> "EntityType | None":
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def supports_bool(cls, value: "EntityType | str") -> bool:
        return cls._coerce(value) in (cls.SWITCH, cls.INPUT_BOOLEAN)

    @classmethod
    def supports_number(cls, value: "EntityType | str") -> bool:
        return cls._coerce(value) is cls.NUMBER

    @classmethod
    def supports_select(cls, value: "EntityType | str") -> bool:
        return cls._coerce(value) in (cls.SELECT, cls.INPUT_SELECT)

def _ts_of(st: State) -> Optional[datetime]:
    # robusto su versioni/forme diverse
    return getattr(st, "last_changed", None) or getattr(st, "last_updated", None)

def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if entity_id else ""

def _quantize_to_step(value: float, step: float, base: float) -> float:
    """
    Rende `value` allineato a `step` a partire da `base` usando Decimal (meno artefatti float).
    Esempio: base=min, step=0.5 -> 20.3 diventa 20.5
    """
    try:
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        d_base = Decimal(str(base))
        if d_step <= 0:
            return value

        n_steps = (d_value - d_base) / d_step
        n_steps_rounded = n_steps.to_integral_value(rounding=ROUND_HALF_UP)
        out = d_base + (n_steps_rounded * d_step)
        return float(out)
    except (InvalidOperation, ValueError):
        return value

def _get_entity_lock(hass: HomeAssistant, entity_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_LOCKS_KEY, {})
    lock = locks.get(entity_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[entity_id] = lock
    return lock

async def set_entity_bool(hass: HomeAssistant, *, entity_id: str, value: bool) -> bool:
    dom = _domain(entity_id)
    if not EntityType.supports_bool(dom):
        log_warning(_LOGGER, f"Unsupported bool entity domain: {dom} ({entity_id})")
        return False

    desired_state = STATE_ON if value else STATE_OFF
    cur = hass.states.get(entity_id)

    # Se l'entità non esiste nello state machine, decidi tu:
    # - o logghi e ritorni False
    # - o chiami comunque il servizio
    if cur is None:
        _LOGGER.warning("Entity %s not found in state machine", entity_id)
        return False
    else:
        # Se è già nello stato desiderato, non fare nulla
        if cur.state == desired_state:
            _LOGGER.debug("No-op: %s already %s", entity_id, desired_state)
            return False

        # Se è unknown/unavailable, spesso ha senso provare comunque a comandarla
        if cur.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            _LOGGER.debug("Entity %s is %s; calling service anyway", entity_id, cur.state)

    await hass.services.async_call(
        dom,
        TURN_ON if value else TURN_OFF,
        {"entity_id": entity_id},
        blocking=False,  # metti True se vuoi evitare chiamate ravvicinate “duplicate”
    )
    return True

async def set_entity_number(
    hass: HomeAssistant,
    *,
    entity_id: str,
    value: float,
    tol: float = 1e-3,                 # tolleranza assoluta per evitare micro-chiamate
    min_value: float | None = None,    # override/fallback se attrs min mancante
    max_value: float | None = None,    # override/fallback se attrs max mancante
    step: float | None = None,         # override/fallback se attrs step mancante
    blocking: bool = False,            # True = attende l'esecuzione del service
    allow_missing: bool = False,       # True = prova anche se entity non è nello state machine
) -> bool:
    """
    Imposta un entity_id numerico (number.* o input_number.*).

    Ritorna True se invia una service call, False se:
    - dominio non supportato / entity mancante (se allow_missing=False)
    - valore già “uguale” entro tol
    - service non disponibile
    - value non valido (NaN/inf)
    """
    if tol < 0:
        log_warning(_LOGGER, "tol negativo (%s) non valido; uso tol=0", tol)
        tol = 0.0

    if not isinstance(entity_id, str) or "." not in entity_id:
        log_warning(_LOGGER, "entity_id non valido: %r", entity_id)
        return False

    dom = entity_id.split(".", 1)[0]
    svc = _SERVICE_BY_DOMAIN.get(dom)
    if svc is None:
        log_warning(_LOGGER, "Dominio non supportato per set_entity_number: %s (%s)", dom, entity_id)
        return False

    service_domain, service_name = svc

    target = as_float(value)
    if target is None or not math.isfinite(target):
        log_warning(_LOGGER, "Valore non valido per %s: %r", entity_id, value)
        return False

    # normalizza eventuali override bounds/step
    o_min = as_float(min_value)
    o_max = as_float(max_value)
    o_step = as_float(step)

    if o_min is not None and not math.isfinite(o_min):
        o_min = None
    if o_max is not None and not math.isfinite(o_max):
        o_max = None
    if o_step is not None and (not math.isfinite(o_step) or o_step <= 0):
        o_step = None

    if o_min is not None and o_max is not None and o_min > o_max:
        log_warning(_LOGGER, "Bounds invalidi per %s: min_value (%s) > max_value (%s)", entity_id, o_min, o_max)
        return False

    lock = _get_entity_lock(hass, entity_id)
    async with lock:
        state = hass.states.get(entity_id)

        attrs: dict[str, Any] = {}
        cur_val: Optional[float] = None

        if state is None:
            if not allow_missing:
                log_warning(_LOGGER, "Entità non trovata nello state machine: %s", entity_id)
                return False
        else:
            attrs = dict(state.attributes or {})
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                cur_val = as_float(state.state)

        # Leggi vincoli dagli attributi...
        a_min = as_float(attrs.get("min"))
        a_max = as_float(attrs.get("max"))
        a_step = as_float(attrs.get("step"))

        # ...ma se l'utente passa min/max/step, questi hanno precedenza.
        min_v = o_min if o_min is not None else a_min
        max_v = o_max if o_max is not None else a_max
        step_v = o_step if o_step is not None else a_step

        if min_v is not None and max_v is not None and min_v > max_v:
            log_warning(_LOGGER, "Bounds invalidi (da attrs/override) per %s: min (%s) > max (%s)", entity_id, min_v, max_v)
            return False

        # 1) clamp
        target = clamp(target, min_v, max_v)

        # 2) quantizzazione allo step
        if step_v is not None and step_v > 0 and math.isfinite(step_v):
            base = min_v if (min_v is not None and math.isfinite(min_v)) else 0.0
            target = _quantize_to_step(target, step_v, base)
            target = clamp(target, min_v, max_v)

        # 3) evita chiamate inutili
        if cur_val is not None and math.isfinite(cur_val):
            if math.isclose(cur_val, target, rel_tol=0.0, abs_tol=tol):
                return False

        # 4) verifica service
        if not hass.services.has_service(service_domain, service_name):
            log_warning(_LOGGER, "Service non disponibile: %s.%s (per %s)", service_domain, service_name, entity_id)
            return False

        # 5) call
        try:
            await hass.services.async_call(
                service_domain,
                service_name,
                {"entity_id": entity_id, "value": target},
                blocking=blocking,
            )
            return True
        except Exception:
            log_exception(_LOGGER,
                "Errore chiamando %s.%s per %s (value=%s, target=%s)",
                service_domain,
                service_name,
                entity_id,
                value,
                target,
            )
            return False
        
async def set_entity_select(
    hass: HomeAssistant,
    *,
    entity_id: str,
    option: str,
) -> bool:
    dom = _domain(entity_id)
    if not EntityType.supports_select(dom):
        log_warning(_LOGGER, f"Unsupported bool entity domain: {dom} ({entity_id})")
        return False

    cur = hass.states.get(entity_id)
    if cur is not None and cur.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        if cur.state == option:
            # già selezionato
            return False

    # Nota: il servizio è sempre "select.select_option" anche per input_select
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=False,
    )
    return True

async def async_time_in_states(
    hass: HomeAssistant,
    entity_id: str,
    ts_utc: datetime,
    *,
    window: str = "today",  # "today" | "last_24h"
    key_fn: Optional[Callable[[State], Optional[Hashable]]] = None,
    include_unknown: bool = False,
    unknown_key: Hashable = "__unknown__",
) -> EntityTimeInStateStats:
    """Calcola durata cumulata per 'chiave' su una finestra, interrogando Recorder.

    - key_fn: mappa uno State -> chiave (es. state.state, oppure un attributo, oppure bucket numerico).
    - include_unknown: se True, conteggia stati non mappabili in unknown_key.
    """

    end = ts_utc
    if window == "today":
        start_local = dt_util.start_of_local_day(dt_util.as_local(ts_utc))
        start = dt_util.as_utc(start_local)
    elif window == "last_24h":
        start = ts_utc - timedelta(hours=24)
    else:
        raise ValueError(f"Unsupported window={window}")

    if key_fn is None:
        key_fn = lambda s: s.state  # default: distribuzione per stato raw

    def _fetch():
        return state_changes_during_period(
            hass,
            start_time=start,
            end_time=end,
            entity_id=entity_id,
            include_start_time_state=True,
            no_attributes=False,
        )

    rec = recorder_helper.get_instance(hass)

    # opzionale: aspetta che recorder sia pronto (se il metodo esiste)
    _wait_ready = getattr(rec, "async_wait_ready", None)
    if callable(_wait_ready):
        maybe = _wait_ready()
        if inspect.isawaitable(maybe):
            await cast(Awaitable[Any], maybe)

    hist = await rec.async_add_executor_job(_fetch)  # database executor del recorder
    states = (hist or {}).get(entity_id) or []

    durations = defaultdict(float)

    # se non c'è storico, comunque ritorniamo un risultato coerente
    if not states:
        cur = hass.states.get(entity_id)
        cur_key = None
        if cur is not None:
            cur_key = key_fn(cur)
            if cur_key is None and include_unknown:
                cur_key = unknown_key
        return EntityTimeInStateStats(
            entity_id=entity_id,
            window_start=start,
            as_of=end,
            durations_s={},
            current_key=cur_key,
            current_key_age_min=None,
        )

    # timeline: ogni segmento va da t_i a t_{i+1} (oppure end)
    for i, st in enumerate(states):
        t0 = _ts_of(st) or start
        t1 = end if i == len(states) - 1 else (_ts_of(states[i + 1]) or end)

        dt_s = (t1 - t0).total_seconds()
        if dt_s < 0:
            dt_s = 0.0

        k = key_fn(st)
        if k is None:
            if include_unknown:
                k = unknown_key
            else:
                continue

        durations[k] += dt_s

    # current key + age: prendiamo lo stato corrente da hass.states
    cur = hass.states.get(entity_id)
    cur_key = None
    age_min = None
    if cur is not None:
        cur_key = key_fn(cur)
        if cur_key is None and include_unknown:
            cur_key = unknown_key

        t_changed = getattr(cur, "last_changed", None) or getattr(cur, "last_updated", None)
        if t_changed is not None:
            age_s = (end - t_changed).total_seconds()
            if age_s < 0:
                age_s = 0.0
            age_min = age_s / 60.0

    return EntityTimeInStateStats(
        entity_id=entity_id,
        window_start=start,
        as_of=end,
        durations_s=dict(durations),
        current_key=cur_key,
        current_key_age_min=age_min,
    )




















