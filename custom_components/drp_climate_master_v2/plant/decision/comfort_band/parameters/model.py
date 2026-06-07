"""Dataclass e enum pubblici del modulo CloMetProvider.

Questo file definisce i tipi di input/output del componente.
Non contiene logica di calcolo né valori numerici: è il contratto
del modulo verso il resto del sistema.

Riferimenti normativi
---------------------
- ISO 7730:2005 — dominio di validità PMV: 0.5 ≤ met ≤ 4.0; 0 ≤ clo ≤ 2.
- ISO 8996:2004 — tabelle metabolismo per tipo di attività.
- ISO 9920:2007 — tabelle isolamento termico abbigliamento.
- ASHRAE 55-2020 §5.4 — running mean outdoor temperature (base S3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RoomType(Enum):
    """Tipo di stanza — determina il met base (ISO 8996) e la curva v_air.

    Usato dal MetProvider per selezionare il metabolismo atteso per tipo
    di attività tipica svolta in quella stanza.
    Non influenza direttamente il CLO (che è funzione dell'occupante,
    non dello spazio), salvo attraverso la deduzione dell'attività.
    """

    BEDROOM = "bedroom"
    BATHROOM = "bathroom"
    KITCHEN = "kitchen"
    LIVING = "living"
    HALLWAY = "hallway"
    GLOBAL = "global"
    OTHER = "other"


class TimeOfDay(Enum):
    """Fascia oraria locale — influenza secondaria su CLO e MET.

    Derivata dall'ora locale HA al momento del tick di decisione.
    Fascia NIGHT: 22:00–06:00 (ora locale).
    Fascia MORNING: 06:00–10:00.
    Fascia DAYTIME: 10:00–18:00.
    Fascia EVENING: 18:00–22:00.
    """

    NIGHT = "night"
    MORNING = "morning"
    DAYTIME = "daytime"
    EVENING = "evening"

    @classmethod
    def from_hour(cls, hour: int) -> "TimeOfDay":
        """Restituisce la fascia oraria dato l'ora locale (0-23)."""
        if 6 <= hour < 10:
            return cls.MORNING
        if 10 <= hour < 18:
            return cls.DAYTIME
        if 18 <= hour < 22:
            return cls.EVENING
        return cls.NIGHT


@dataclass(frozen=True, slots=True)
class CloEstimate:
    """Stima del CLO (isolamento termico abbigliamento) per una zona.

    ``value`` è il valore effettivo da passare al calcolo PMV di Fanger.
    I campi di dettaglio sono per commissioning e debug: consentono di
    tracciare esattamente quale contributo ha prodotto il valore finale.

    Nota sul modello ibrido
    -----------------------
    Il componente usa il modello PMV di Fanger (ISO 7730, per edifici a
    condizionamento meccanico) con CLO inferito da temperatura outdoor
    via EWMA — approccio adattivo (ASHRAE 55 §5.4 / EN 16798-1 Annex B).
    CLO non è misurato ma stimato come proxy della termoregolazione
    comportamentale degli occupanti. Documentato come approssimazione
    ingegneristica, non conformità letterale a un singolo standard.
    """

    value: float
    """CLO effettivo da usare nel calcolo PMV (adimensionale, 1 clo = 0.155 m²K/W)."""

    season_base: float
    """CLO dal bucket stagionale interpolato su season_progress."""

    s3_delta: float
    """Correzione adattiva EWMA S3: deviazione T_rm dalla neutrale stagionale."""

    s4_delta: float
    """Correzione ramp S4: interpolazione lineare verso estate/inverno su progress."""

    profile_delta: float
    """Delta da profilo operativo (SLEEP → coperte; AWAY → riduzione invernale)."""

    cold_snap_delta: float
    """Delta cold snap: interpolazione shoulder → winter se anomalia fredda."""

    tod_delta: float
    """Delta fascia oraria: aggiustamento secondario per ora del giorno."""

    profile_override: Optional[float]
    """Se non None, ha sostituito l'intero calcolo (es. SLEEP con coperte fisse)."""

    source: str
    """Stringa descrittiva della path di calcolo (per log commissioning)."""


@dataclass(frozen=True, slots=True)
class MetEstimate:
    """Stima del MET (tasso metabolico) per una zona.

    ``value`` è il valore effettivo da passare al calcolo PMV di Fanger.
    MET è funzione dell'attività (room_type + profile + time_of_day),
    non della stagione né della temperatura.
    """

    value: float
    """MET effettivo (adimensionale; 1 met = 58.15 W/m²)."""

    room_base: float
    """MET base per tipo di stanza (ISO 8996 Tab. A.1)."""

    profile_override: Optional[float]
    """Se non None, ha sostituito il room_base (es. SLEEP → 0.70)."""

    tod_delta: float
    """Delta fascia oraria (secondario)."""

    source: str
    """Stringa descrittiva della path di calcolo."""


@dataclass(frozen=True, slots=True)
class ComfortParameters:
    """Parametri CLO+MET per una zona o per il global.

    Output principale del ComfortParameterProvider.
    Passato al comfort band builder come sostituto dei parametri
    attualmente cablati in ConfortPolicyConfig.
    """

    zone_id: str
    room_type: RoomType
    clo: CloEstimate
    met: MetEstimate
