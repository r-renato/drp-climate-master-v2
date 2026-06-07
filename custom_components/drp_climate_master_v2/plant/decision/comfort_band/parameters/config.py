"""Configurazione numerica del CloMetProvider.

Tutti i magic number del componente sono qui, con nome HVAC sensato
e docstring normativo. I default rispecchiano i valori già validati
in ``comfort_band/config.py`` (sezioni C, D, G) per garantire
continuità comportamentale dopo l'integrazione.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class CloSeasonSeeds:
    """CLO base per bucket stagionale (ISO 9920 / ASHRAE 55 Tab. 5.2.2)."""

    summer: float = 0.50
    """Estate — abbigliamento leggero (T-shirt + pantaloni corti)."""

    shoulder: float = 0.82
    """Mezza stagione — mix primavera/autunno."""

    winter_default: float = 1.00
    """Inverno — valore base ISO 7730 Tab. B.1 (zona C / riferimento)."""

    winter_by_zone: dict[str, float] = field(
        default_factory=lambda: {
            "A": 0.90,
            "B": 0.95,
            "C": 1.00,
            "D": 1.05,
            "E": 1.15,
            "F": 1.25,
        }
    )

    def winter(self, climate_zone: Optional[str] = None) -> float:
        """CLO invernale per zona climatica; fallback a winter_default."""
        if climate_zone:
            return self.winter_by_zone.get(climate_zone.upper(), self.winter_default)
        return self.winter_default


@dataclass(frozen=True, slots=True)
class CloS3Config:
    """Parametri per la correzione adattiva S3 (ASHRAE 55 §5.4 / EN 16798-1)."""

    neutral_by_season: dict[str, float] = field(
        default_factory=lambda: {
            "winter": 21.5,
            "shoulder": 22.5,
            "summer": 25.0,
        }
    )
    """T_op neutra per stagione (°C) — calibrata per zona D (Roma)."""

    sensitivity_clo_per_degc: float = 0.05
    """Sensitività CLO alla deviazione T_rm (clo/°C)."""

    cap_delta_clo: float = 0.25
    """Cap simmetrico della correzione S3 (±0.25 clo)."""

    max_deviation_suppress_c: float = 4.0
    """Deviazione massima |T_op_current − T_rm| oltre cui S3 è soppressa (°C)."""


@dataclass(frozen=True, slots=True)
class CloS4Config:
    """Parametri per la ramp S4: interpolazione CLO verso estate/inverno."""

    enabled: bool = True
    """Abilita S4. Se False il CLO shoulder rimane fisso a CloSeasonSeeds.shoulder."""

    ramp_start_pct: float = 50.0
    """Progresso stagionale (%) oltre il quale inizia la ramp."""


@dataclass(frozen=True, slots=True)
class CloColdSnapConfig:
    """Correzione CLO per giornata anomala fredda in stagione shoulder."""

    fraction: float = 0.40
    """Frazione di interpolazione shoulder → winter in caso di cold snap."""


@dataclass(frozen=True, slots=True)
class CloProfileDeltas:
    """Delta CLO per profilo operativo (sezione C di comfort_band/config.py)."""

    sleep_winter_delta: float = 0.95
    """SLEEP invernale: pigiama + piumino pesante (+0.95 → clo≈2.0)."""

    sleep_shoulder_delta: float = 0.70
    """SLEEP shoulder: pigiama + coperta primaverile (+0.70 → clo≈1.52)."""

    away_vacation_winter_delta: float = -0.10
    """AWAY/VACATION invernale: casa vuota → abbigliamento leggero (-0.10)."""

    away_vacation_winter_floor: float = 0.70
    """Floor CLO per AWAY/VACATION in inverno (min. mezza stagione)."""

    cap_sleep: float = 2.40
    """Cap CLO massimo in modalità SLEEP (ISO 7730 validato < 2.0 clo)."""

    cap_default: float = 1.60
    """Cap CLO massimo per tutti gli altri profili."""


@dataclass(frozen=True, slots=True)
class CloTimeOfDayDeltas:
    """Delta CLO per fascia oraria (effetto secondario, ±0.05–0.10)."""

    morning: float = +0.05
    """Mattino: appena alzati, possibile vestaglia/pigiama residuo."""

    daytime: float = 0.00
    """Giorno: riferimento neutro (base di calibrazione)."""

    evening: float = -0.05
    """Sera: abbigliamento più leggero o casalingo."""

    night: float = -0.10
    """Notte (stanze non-bedroom): pigiama o simile."""


@dataclass(frozen=True, slots=True)
class MetRoomSeeds:
    """MET base per tipo di stanza (ISO 8996:2004 Tab. A.1)."""

    bedroom_awake: float = 1.00
    """Camera da letto sveglio: seduto/in piedi rilassato (ISO 8996: 1.0)."""

    bedroom_sleep: float = 0.70
    """Camera da letto dormiente: sonno (ISO 8996 Tab. A.1: 0.70 met)."""

    bathroom: float = 1.90
    """Bagno: igiene personale in piedi/doccia."""

    kitchen: float = 1.70
    """Cucina: preparazione pasti leggera."""

    living: float = 1.10
    """Soggiorno: attività sedentaria con occasionali spostamenti."""

    hallway: float = 1.20
    """Corridoio/ingresso: transito in piedi (ISO 8996: 1.2)."""

    other: float = 1.10
    """Fallback generico: valore base residenziale."""

    global_zone: float = 1.10
    """Global: valore base (sostituito dalla media pesata in ComfortParameterProvider)."""


@dataclass(frozen=True, slots=True)
class MetTimeOfDayDeltas:
    """Delta MET per fascia oraria (effetto secondario)."""

    bedroom_night_delta: float = -0.10
    """Camera da letto di notte (non SLEEP): occupante a riposo, met ridotto."""

    kitchen_daytime_delta: float = +0.10
    """Cucina in orario diurno: attività culinaria più probabile."""

    bathroom_night_delta: float = -0.40
    """Bagno di notte: visita rapida, attività minima vs routine mattutina.

    La doccia mattutina (MET~2.0) è il caso di progetto ISO 8996; di notte il
    bagno è usato per igiene veloce o necessità fisiologica (MET~1.2-1.5).
    Delta -0.40 porta il MET notturno da 1.90 a 1.50, ancora sopra "in piedi
    rilassato" (1.2) ma sotto la routine completa.
    """


@dataclass(frozen=True, slots=True)
class CloMetConfig:
    """Configurazione completa del CloMetProvider."""

    season_seeds: CloSeasonSeeds = field(default_factory=CloSeasonSeeds)
    s3: CloS3Config = field(default_factory=CloS3Config)
    s4: CloS4Config = field(default_factory=CloS4Config)
    cold_snap: CloColdSnapConfig = field(default_factory=CloColdSnapConfig)
    profile_deltas: CloProfileDeltas = field(default_factory=CloProfileDeltas)
    tod_deltas: CloTimeOfDayDeltas = field(default_factory=CloTimeOfDayDeltas)
    met_seeds: MetRoomSeeds = field(default_factory=MetRoomSeeds)
    met_tod: MetTimeOfDayDeltas = field(default_factory=MetTimeOfDayDeltas)

    climate_zone: Optional[str] = "D"
    """Zona climatica italiana (A..F). Default "D" = Roma."""

    clo_min: float = 0.20
    """Bound inferiore fisico del CLO (adimensionale)."""

    clo_min_shoulder: float = 0.30
    """Floor CLO specifico per stagione shoulder (> clo_min generico).

    Impedisce che S3 molto positiva (T_rm >> neutrale in tarda primavera)
    abbassi il CLO sotto 0.30 quando la stagione è ancora SHOULDER.
    Fisicamente: anche in una tarda primavera calda, abbigliamento indoor
    minimo in un appartamento condizionato non scende sotto una maglietta
    leggera + pantaloni (clo~0.30 per ISO 9920 ensemble leggero).
    Questo floor non si applica in SUMMER: una volta transitata la stagione
    il seed 0.50 e il clo_min generico (0.20) gestiscono il range estivo.
    """

    clo_max_default: float = 1.60
    """Bound superiore CLO per profili non-SLEEP."""

    clo_max_sleep: float = 2.40
    """Bound superiore CLO per profilo SLEEP."""
