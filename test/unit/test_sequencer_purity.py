"""Test di purezza per `run_staging_cycle` (P-07a).

Verifica che `run_staging_cycle`:
- non muti l'oggetto `StagingState` ricevuto in ingresso dal chiamante,
  nemmeno nel ciclo idronico "pieno" (quello che attraversa `fsm_step`,
  `update_boiler_ready` e `compute_zone_valves_plan` — i tre punti che
  mutavano `stage` in place prima di P-07a);
- restituisca un oggetto distinto (nuova identità) i cui campi riflettono
  correttamente l'esito del ciclo corrente.

Nota sul device_io
-------------------
Il percorso di test usa `mode=heating` senza alcuna domanda attiva
(PDC/valvole/pompe tutte a riposo), quindi `apply_pdc`/`apply_vmc` vengono
comunque invocati (passo 3 del sequencer) ma come no-op: usiamo un doppio
minimale (`_NoopDeviceIO`) invece di un vero `PlantDeviceIO`, che richiede
un'istanza `HomeAssistant` reale e i driver hardware.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from custom_components.drp_climate_master_v2.plant.control.config import (
    PlantActuatorConfig,
)
from custom_components.drp_climate_master_v2.plant.control.model import (
    PlantFsmState,
    PlantPhase,
    StagingState,
)
from custom_components.drp_climate_master_v2.plant.control.sequencer import (
    run_staging_cycle,
)
from custom_components.drp_climate_master_v2.plant.decision.contracts import (
    GatingDiagnostics,
    PlantDecision,
    PlantMode,
)
from custom_components.drp_climate_master_v2.plant.monitor.plant import PlantSnapshot


class _NoopDeviceIO:
    """Doppio minimale di `PlantDeviceIO`: nessun I/O reale, solo no-op async."""

    async def apply_pdc(self, *_a, **_k) -> None:
        return None

    async def apply_vmc(self, *_a, **_k) -> None:
        return None

    async def apply_zone_valves(self, *_a, **_k) -> None:
        return None

    async def apply_supply(self, *_a, **_k) -> None:
        return None


def _heating_no_demand_decision(now: datetime) -> PlantDecision:
    """Decisione con mode=heating ma nessuna domanda attiva (PDC/valvole/pompe a riposo).

    Sceglie deliberatamente `mode=heating` (non `off`/`vent_only`/`iaq_only`)
    per attraversare l'intero ciclo di staging (gate OFF e bypass VMC non si
    applicano), arrivando quindi a `update_boiler_ready`, `fsm_step` e
    `compute_zone_valves_plan` — i tre punti che mutavano `stage` in place.
    """
    return PlantDecision(
        ts=now,
        mode=PlantMode.HEATING,
        reason="test_full_cycle_no_demand",
        gating=GatingDiagnostics(user_hvac_mode="heat"),
    )


def test_run_staging_cycle_does_not_mutate_input_stage() -> None:
    now = datetime.now(timezone.utc)

    # Stato di ingresso "non banale": se la funzione mutasse l'oggetto
    # ricevuto (comportamento pre-P-07a), questi valori cambierebbero
    # per effetto di fsm_step (che scrive sempre `last_request_on` e,
    # con richiesta assente, transita RUNNING->STOPPING).
    original_fsm = PlantFsmState(phase=PlantPhase.RUNNING, entered_at=now, last_request_on=True)
    input_stage = StagingState(boiler_ready=True, fsm=original_fsm)
    input_stage_id = id(input_stage)
    input_fsm_id = id(input_stage.fsm)

    snapshot = PlantSnapshot(timestamp=now)
    decision = _heating_no_demand_decision(now)
    cfg = PlantActuatorConfig()

    result = asyncio.run(
        run_staging_cycle(
            snapshot=snapshot,
            decision=decision,
            stage=input_stage,
            cfg=cfg,
            runtime_areas=[],
            supply_configured=False,
            device_io=_NoopDeviceIO(),
        )
    )

    # L'oggetto passato dal chiamante non deve essere stato toccato:
    # né i campi scalari di StagingState, né i campi di PlantFsmState
    # (che fsm_step scrive sempre, incondizionatamente).
    assert input_stage.boiler_ready is True
    assert input_stage.fsm.phase == PlantPhase.RUNNING
    assert input_stage.fsm.last_request_on is True
    assert id(input_stage) == input_stage_id
    assert id(input_stage.fsm) == input_fsm_id

    # Il valore restituito è un oggetto distinto (nuova identità) e riflette
    # l'esito reale del tick corrente (nessuna domanda attiva -> FSM ha
    # processato request_on=False).
    assert result is not input_stage
    assert result.fsm is not input_stage.fsm
    assert result.fsm.last_request_on is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
