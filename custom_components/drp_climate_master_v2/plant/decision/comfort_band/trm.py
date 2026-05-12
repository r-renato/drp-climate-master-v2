"""Running mean della temperatura operante per zona (Adaptive CLO — S3).

Scopo
-----
Traccia la media mobile esponenziale (EWMA) della T_op interna per ciascuna
zona.  Il valore prodotto (``t_rm``) viene consumato dal policy layer per
modulare il CLO stagionale: se la settimana è stata più fredda della media
attesa, gli occupanti si vestono più pesante → CLO sale → la comfort band si
sposta verso temperature più basse → il sistema scalda meno.

Riferimento normativo
---------------------
ASHRAE 55-2017 §5.4.1 e Appendice A definiscono il "prevailing mean outdoor
temperature" come EWMA della T_ext su una finestra di 7-30 giorni.  Qui si
applica lo stesso principio alla **T_op interna** per catturare l'adattamento
comportamentale degli occupanti (abbigliamento) in funzione del microclima
domestico percepito.

Differenza rispetto alla running_mean_outdoor in sensor_aggregator
------------------------------------------------------------------
- Quella traccia T_ext con tau=giorni, per rilevare il regime stagionale.
- Questa traccia T_op interna per zona, con tau configurabile (default 7gg),
  per modulare il CLO nella comfort band.
- Scope diverso, oggetto diverso, nessuna dipendenza incrociata.

Stato e persistenza
-------------------
Lo stato è **in memoria** nel ``PlantDecisionPlanner``.  Non viene persistito
nel recorder HA: al riavvio si riparte dal warm-up.  Questo è intenzionale:
- Evita letture DB nel loop asincrono.
- Il warm-up (144 tick ≈ 2h a 30s) è sufficientemente breve da non creare
  discomfort operativo significativo.
- Alla fine del warm-up il CLO torna al valore stagionale base (fail-safe).

Concorrenza
-----------
``ZoneTrmTracker.update()`` è una funzione pura senza lock: viene chiamata
dal planner nel ciclo sincrono di decisione, che è già serializzato dal
``_decider_lock`` del supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import exp
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Stato per singola zona
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ZoneTrmState:
    """Stato EWMA della T_op per una singola zona.

    Attributi
    ---------
    t_rm : float
        Valore corrente della running mean (°C).
    ts_last : datetime
        Timestamp dell'ultimo aggiornamento (tz-aware UTC).
    n_updates : int
        Numero di tick validi accumulati (usato per il warm-up guard).
        Un tick è "valido" se ``t_op`` era disponibile (non None).
    """

    t_rm: float
    ts_last: datetime
    n_updates: int = 0


# ---------------------------------------------------------------------------
# Tracker (logica EWMA isolata)
# ---------------------------------------------------------------------------

class ZoneTrmTracker:
    """Gestisce le running mean T_op per un insieme di zone.

    Progettato per essere tenuto come campo del ``PlantDecisionPlanner``
    (stessa vita del planner, reset al riavvio HA).

    Parametri
    ---------
    tau_hours : float
        Costante di tempo dell'EWMA in ore.  Default 168h (7 giorni).
        Valori più bassi → risposta più rapida ma più rumore.
        Valori più alti → risposta più lenta ma più stabile.
    warmup_ticks : int
        Numero minimo di tick validi prima di esporre una running mean
        utilizzabile dalla policy.  Durante il warm-up ``get_t_rm()``
        restituisce ``None`` → la policy usa il CLO stagionale base.
    t_rm_clamp_min, t_rm_clamp_max : float
        Clamp difensivi sul valore T_op accettato (°C).
        Evitano che letture spurie (sensore guasto, valori impossibili)
        inquinino la running mean con una singola osservazione.
    """

    def __init__(
        self,
        *,
        tau_hours: float = 168.0,
        warmup_ticks: int = 144,
        t_rm_clamp_min: float = 10.0,
        t_rm_clamp_max: float = 35.0,
    ) -> None:
        self._tau_s = max(3600.0, float(tau_hours) * 3600.0)
        self._warmup = max(1, int(warmup_ticks))
        self._clamp_min = float(t_rm_clamp_min)
        self._clamp_max = float(t_rm_clamp_max)
        self._states: Dict[str, ZoneTrmState] = {}

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    def update(
        self,
        zone_id: str,
        t_op: Optional[float],
        ts: Optional[datetime] = None,
    ) -> None:
        """Aggiorna la running mean per ``zone_id`` con il campione ``t_op``.

        Se ``t_op`` è ``None`` (sensore unavailable) lo stato viene mantenuto
        ma ``n_updates`` non viene incrementato: il warm-up non avanza.

        Parametri
        ---------
        zone_id : str
            Identificatore zona (deve corrispondere a ZoneSnapshot.name).
        t_op : float | None
            Temperatura operante corrente (°C).  None = dato non disponibile.
        ts : datetime | None
            Timestamp del campione.  Se None usa ``datetime.now(UTC)``.
        """
        now = _ensure_tz(ts or datetime.now(timezone.utc))
        prev = self._states.get(zone_id)

        if t_op is None:
            # Nessun dato: mantieni lo stato precedente invariato (n_updates
            # fermo → warm-up non avanza; t_rm non modificato).
            if prev is not None:
                # Aggiorna solo il timestamp per evitare dt enormi al prossimo campione
                self._states[zone_id] = ZoneTrmState(
                    t_rm=prev.t_rm,
                    ts_last=now,
                    n_updates=prev.n_updates,
                )
            return

        # Clamp difensivo
        t_clamped = max(self._clamp_min, min(self._clamp_max, float(t_op)))

        if prev is None:
            # Prima osservazione: inizializza con il valore corrente
            self._states[zone_id] = ZoneTrmState(
                t_rm=t_clamped,
                ts_last=now,
                n_updates=1,
            )
            return

        dt_s = max(1.0, (now - prev.ts_last).total_seconds())
        alpha = 1.0 - exp(-dt_s / self._tau_s)
        t_rm_new = alpha * t_clamped + (1.0 - alpha) * prev.t_rm

        self._states[zone_id] = ZoneTrmState(
            t_rm=float(t_rm_new),
            ts_last=now,
            n_updates=prev.n_updates + 1,
        )

    def get_t_rm(self, zone_id: str) -> Optional[float]:
        """Restituisce la running mean per ``zone_id``, o ``None`` se in warm-up.

        ``None`` segnala al policy layer di usare il CLO stagionale base
        senza correzione adattiva: fail-safe esplicito.
        """
        s = self._states.get(zone_id)
        if s is None or s.n_updates < self._warmup:
            return None
        return float(s.t_rm)

    def state_for(self, zone_id: str) -> Optional[ZoneTrmState]:
        """Restituisce lo stato grezzo per commissioning/diagnostica."""
        return self._states.get(zone_id)

    def reset(self, zone_id: Optional[str] = None) -> None:
        """Reset stato: zona specifica o tutte le zone.

        Utile per test e per il riavvio manuale in caso di sensore sostituito.
        """
        if zone_id is not None:
            self._states.pop(zone_id, None)
        else:
            self._states.clear()

    @property
    def zone_ids(self) -> list[str]:
        """Zone attualmente tracciate."""
        return list(self._states.keys())


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _ensure_tz(dt: datetime) -> datetime:
    """Garantisce che il datetime sia tz-aware (UTC se naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
