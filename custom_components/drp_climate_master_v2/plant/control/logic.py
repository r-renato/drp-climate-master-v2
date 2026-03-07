from __future__ import annotations

"""API pubblico di `plant.control`.

Questo package contiene la logica di staging idraulico + FSM e i piani di attuazione.
Per rendere il codice più leggibile e manutenibile, la logica è stata separata in
moduli dedicati:

- `signals.py`: derivazione segnali (requested vs observed) e contesto tipizzato
- `readiness.py`: boiler readiness con isteresi
- `fsm.py`: macchina a stati per anti-chatter + gating forte
- `plans.py`: piani (valvole / pompe-miscelatrice)
- `observed_phase.py`: stima diagnostica della fase osservata

Questo file re-esporta le funzioni principali per retro-compatibilità e discoverability.
"""

from .signals import (  # noqa: F401
    BoilerSignals,
    PdcSignals,
    PlantControlContext,
    SupplyDemand,
    build_control_context,
    compute_best_plant_t_target_c,
    compute_operation_plant_t_ready_ref_c,
    desired_zone_valves,
    mode_value,
    pdc_requested_on,
)

from .readiness import update_boiler_ready  # noqa: F401
from .fsm import PlantFsmConfig, PlantFsmInputs, fsm_step  # noqa: F401
from .plans import compute_supply_plan, compute_zone_valves_plan  # noqa: F401
from .observed_phase import ObservedPlantPhase, estimate_observed_phase  # noqa: F401
