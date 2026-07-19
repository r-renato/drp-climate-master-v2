from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from ...helpers.logger import log_debug, log_info

from .device_io import PlantDeviceIO
from .sequencer import run_staging_cycle

from ...plant.monitor.plant import PlantSnapshot
from ...domain.models.runtime_schema import RuntimeConfig

from ..decision.contracts import PlantDecision

from .config import PlantActuatorConfig
from .model import PlantPhase, StagingState

_LOGGER = logging.getLogger(__name__)


class PlantActuator:
    """Attuatore di plant: traduce `PlantDecision` in comandi ai dispositivi.

    Architettura
    ------------
    - `model.py`: value-objects e stato persistente (staging + FSM)
    - `logic.py`: logica pura (calcoli, piano valvole/pompe, FSM)
    - `actuator.py` (questo file): I/O e orchestrazione

    Obiettivo principale
    --------------------
    Usare una macchina a stati (FSM) sopra lo staging per evitare stati anomali
    di accensione/spegnimento dovuti a flap sensori e dinamiche lente.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        runtime_cfg: Optional[RuntimeConfig] = None,
        *,
        runtime: Optional[RuntimeConfig] = None,
        cfg: Optional[PlantActuatorConfig] = None,
    ) -> None:
        """Crea l'attuatore di impianto.

        Parametri
        ---------
        hass:
            Istanza Home Assistant.
        runtime_cfg / runtime:
            Configurazione runtime dell'integrazione.

        Nota
        ----
        `runtime` è un alias per compatibilità con chiamanti legacy.
        """

        if runtime_cfg is None:
            runtime_cfg = runtime
        if runtime_cfg is None:
            raise ValueError("runtime_cfg (o runtime) è obbligatorio")

        self._cfg = cfg or PlantActuatorConfig()
        self._cfg.validate()

        self._snapshot: Optional[PlantSnapshot] = None
        self._apply_lock = asyncio.Lock()

        self._runtime = runtime_cfg

        # I/O dispositivi: strato puro, separato dall'orchestrazione
        self._device_io = PlantDeviceIO.build(hass=hass, runtime_cfg=runtime_cfg)

        # supply_cfg: determina se i comandi pompe vengono attuati
        self._supply_configured = getattr(runtime_cfg.climate.devices, "supply_units", None) is not None

        # Stato persistente staging
        self._stage = StagingState()

    # ------------------------- orchestrator -------------------------
    async def async_apply(self, snapshot: PlantSnapshot, decision: PlantDecision) -> None:
        """Applica una `PlantDecision` allo stato reale dell'impianto.

        Delega l'intera orchestrazione a `run_staging_cycle` (sequencer.py),
        proteggendo con `_apply_lock` contro invocazioni concorrenti.
        La logica di staging (FSM, valvole, pompe, readiness) è in sequencer.py;
        l'I/O verso i driver è in device_io.py.
        """
        async with self._apply_lock:
            self._stage = await run_staging_cycle(
                snapshot=snapshot,
                decision=decision,
                stage=self._stage,
                cfg=self._cfg,
                runtime_areas=self._runtime.climate.areas or [],
                supply_configured=self._supply_configured,
                device_io=self._device_io,
            )
