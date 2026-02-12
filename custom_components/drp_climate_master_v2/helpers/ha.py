#
from __future__ import annotations
from enum import StrEnum
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.const import (
    STATE_ON, STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE
)

from .utils import as_float

from .logger import log_warning

from ..const import TURN_OFF, TURN_ON

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

def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if entity_id else ""

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
        # return False
    else:
        # Se è già nello stato desiderato, non fare nulla
        if cur.state == desired_state:
            _LOGGER.debug("No-op: %s already %s", entity_id, desired_state)
            return True

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
    tol: float = 1e-3,   # tolleranza per evitare chiamate inutili per micro-differenze
) -> bool:
    dom = _domain(entity_id)
    if not EntityType.supports_number(dom):
        log_warning(_LOGGER, f"Unsupported bool entity domain: {dom} ({entity_id})")
        return False

    cur = hass.states.get(entity_id)
    if cur is not None and cur.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        cur_val = as_float(cur.state)
        if cur_val is not None and abs(cur_val - float(value)) <= tol:
            # già impostato (entro tolleranza)
            return True

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id, "value": value},
        blocking=False,
    )
    return True

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
            return True

    # Nota: il servizio è sempre "select.select_option" anche per input_select
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=False,
    )
    return True



