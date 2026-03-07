from __future__ import annotations

from typing import Optional

from .model import BoilerReadinessDebug, BoilerReadinessUpdate, StagingState


def update_boiler_ready(
    stage: StagingState,
    *,
    mode: str,
    t_boiler_supply: Optional[float],
    t_target: Optional[float],
    on_margin_c: float,
    off_margin_c: float,
) -> BoilerReadinessUpdate:
    """Aggiorna `stage.boiler_ready` con isteresi e ritorna diagnostica tipizzata.

    Note termotecniche
    ------------------
    - In riscaldamento: ready quando `t_boiler_supply >= t_target - on_margin`
    - In raffrescamento: ready quando `t_boiler_supply <= t_target + on_margin`

    L'isteresi ON/OFF evita oscillazioni dovute a rumore sensori e inerzie idrauliche.
    """
    dbg = BoilerReadinessDebug(
        t_boiler_supply_c=t_boiler_supply,
        t_target_c=t_target,
        on_thr_c=None,
        off_thr_c=None,
    )

    if t_boiler_supply is None or t_target is None:
        return BoilerReadinessUpdate(ready=stage.boiler_ready, debug=dbg)

    on_margin = float(on_margin_c)
    off_margin = float(off_margin_c)

    if mode == "heating":
        on_thr = float(t_target) - on_margin
        off_thr = float(t_target) - off_margin
        dbg.on_thr_c = on_thr
        dbg.off_thr_c = off_thr

        if not stage.boiler_ready:
            if float(t_boiler_supply) >= on_thr:
                stage.boiler_ready = True
        else:
            if float(t_boiler_supply) < off_thr:
                stage.boiler_ready = False

    elif mode in ("cooling", "dehum_assist"):
        on_thr = float(t_target) + on_margin
        off_thr = float(t_target) + off_margin
        dbg.on_thr_c = on_thr
        dbg.off_thr_c = off_thr

        if not stage.boiler_ready:
            if float(t_boiler_supply) <= on_thr:
                stage.boiler_ready = True
        else:
            if float(t_boiler_supply) > off_thr:
                stage.boiler_ready = False

    else:
        stage.boiler_ready = False

    return BoilerReadinessUpdate(ready=stage.boiler_ready, debug=dbg)
