"""FSM di lockout DP per singola zona (sicurezza anti-condensa, soffitto radiante).

Vedi `safety/dew_guard.py` per la policy di sicurezza whole-plant (mandata
condivisa, unica fonte di verità per il margine °C). Questo modulo applica
la STESSA formula (via `DewGuardPolicy.evaluate()`) al dew point di una
singola zona, per decidere se quella zona può restare nel circuito cooling
oppure va esclusa (valvola forzata chiusa) indipendentemente dal resto
dell'impianto.

Motivazione termotecnica
------------------------
La mandata radiante è condivisa: un'unica valvola miscelatrice (Caleffi 610)
alimenta tutte le zone del circuito B. Un picco di umidità transitorio in
una singola zona (es. doccia in un bagno) non deve forzare l'intero impianto
al derating o allo spegnimento, se le altre zone restano sicure alla mandata
corrente. Il lockout per-zona esclude selettivamente solo la zona a rischio;
il MAX-DP usato per dimensionare la mandata (vedi `planner.py`, FASE 6)
viene poi ricalcolato sulle sole zone ammesse — vedi `PlantDecisionPlanner`.

Riuso della formula esistente
------------------------------
Lo Step 1 ("questa zona è ammissibile al circuito cooling?") è
matematicamente identico a `DewGuardPolicy.evaluate()` applicato al DP di
una singola zona invece che al MAX globale. Per evitare di duplicare
`dp_margin_c` / `delta_surface_water_c` / `cool_supply_max_c` in due punti
che potrebbero disallinearsi nel tempo, questo modulo riusa l'istanza di
`DewGuardPolicy` già condivisa dal planner.

Isteresi e tempo minimo di permanenza (anti-chatter)
-----------------------------------------------------
Senza isteresi, un DP oscillante intorno alla soglia farebbe aprire e
chiudere ripetutamente l'elettrovalvola di zona (tempo di apertura tipico
2-3 min, §3.2/§5.2 del progetto). La FSM impone:
  - ingresso in LOCKOUT immediato (la sicurezza non va ritardata);
  - permanenza minima in LOCKOUT (`min_lockout_minutes`), analoga a
    MIN_OFF_TIME_MINUTES per il compressore;
  - uscita solo se il DP osservato, sommato al margine di isteresi
    (`hysteresis_c`), risulta ancora ammissibile, e solo DOPO il tempo
    minimo di permanenza.

Persistenza
-----------
`ZoneDpLockoutState` va mantenuto dal chiamante in un
`Dict[str, ZoneDpLockoutState]` chiave-zona (vedi `PlantDecisionPlanner`).
Non viene ripristinato al riavvio di Home Assistant: il primo tick utile
ricalcola la fase corretta dal DP osservato in quel momento. Questo è
fail-safe by design — se la zona è ancora a rischio, rientra subito in
LOCKOUT; l'unico costo è perdere il timer `min_lockout_minutes` già
trascorso prima del riavvio, accettabile per un evento raro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from ..config import ZoneDpLockoutConfig
from ..contracts import PlantMode
from .dew_guard import DewGuardPolicy


class ZoneDpLockoutPhase(str, Enum):
    """Fase del lockout DP per una singola zona."""

    SAFE = "safe"
    LOCKOUT = "lockout"


@dataclass(slots=True)
class ZoneDpLockoutState:
    """Stato persistente del lockout DP per una singola zona.

    Va mantenuto in un `Dict[str, ZoneDpLockoutState]` chiave-zona a cura
    del chiamante (`PlantDecisionPlanner._zone_dp_lockout`). Vedi la
    docstring di modulo per il comportamento al riavvio HA.
    """

    phase: ZoneDpLockoutPhase = ZoneDpLockoutPhase.SAFE
    entered_at: Optional[datetime] = None


def zone_dp_lockout_step(
    state: ZoneDpLockoutState,
    *,
    now: datetime,
    zone_dp_c: Optional[float],
    dew_guard: DewGuardPolicy,
    lockout_cfg: ZoneDpLockoutConfig,
) -> tuple[bool, list[str]]:
    """Step puro: aggiorna lo stato di lockout DP di UNA zona.

    Non esegue I/O. Muta `state` in-place (stesso pattern di `fsm_step` in
    `plant/control/fsm.py`).

    Args:
        state: stato persistente della zona (mutato in-place).
        now: timestamp corrente (tz-aware).
        zone_dp_c: dew point della zona (°C); None se non disponibile
            (fail-safe: trattato come zona non ammissibile).
        dew_guard: istanza di `DewGuardPolicy` condivisa con il planner
            (stessa configurazione/margini usati dal guard whole-plant —
            vedi nota di modulo su riuso della formula).
        lockout_cfg: isteresi e timer anti-chatter per questa zona.

    Returns:
        (admitted, reasons): `admitted=True` se la zona può restare nel
        circuito cooling in questo ciclo; `reasons` per diagnostica/log
        (formato compatibile con `dec.warnings` / `ZoneCommand.debug`).
    """
    reasons: list[str] = []

    def _raw_admitted(dp_for_test: Optional[float]) -> bool:
        if dp_for_test is None:
            return False
        result = dew_guard.evaluate(
            mode=PlantMode.COOLING,
            dp_max_c=float(dp_for_test),
            allow_dehum_assist=False,
        )
        return bool(result.radiant_allowed)

    if state.phase == ZoneDpLockoutPhase.SAFE:
        if _raw_admitted(zone_dp_c):
            return True, reasons

        state.phase = ZoneDpLockoutPhase.LOCKOUT
        state.entered_at = now
        reasons.append(f"zone_dp_lockout_enter:dp={zone_dp_c}")
        return False, reasons

    # state.phase == LOCKOUT
    entered_at = state.entered_at or now
    elapsed_min = (now - entered_at).total_seconds() / 60.0
    min_hold = float(lockout_cfg.min_lockout_minutes)

    if elapsed_min < min_hold:
        reasons.append(f"zone_dp_lockout_min_hold:{elapsed_min:.1f}/{min_hold:.1f}min")
        return False, reasons

    dp_with_hysteresis = (
        float(zone_dp_c) + float(lockout_cfg.hysteresis_c) if zone_dp_c is not None else None
    )
    if _raw_admitted(dp_with_hysteresis):
        state.phase = ZoneDpLockoutPhase.SAFE
        state.entered_at = None
        reasons.append(f"zone_dp_lockout_exit:dp={zone_dp_c}")
        return True, reasons

    reasons.append(f"zone_dp_lockout_hold:dp={zone_dp_c}")
    return False, reasons
