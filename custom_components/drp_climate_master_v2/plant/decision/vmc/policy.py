from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from ....domain.enums import HVACOperatingProfile
from ....domain.models.plant import PlantSnapshot
from ....helpers.psychrometric import dew_point_celsius
from ....helpers.utils import as_float, clamp

from ..config import VmcConfig

from .state import VmcState

@dataclass(slots=True)
class VmcDemand:
    """Policy output for VMC demand (requests + DP control thresholds)."""

    # DP control (commanded / effective on device)
    dp_sp_c: Optional[float]
    ddp_cmd_c: Optional[float]
    dehum_on_thr_c: Optional[float]
    dehum_off_thr_c: Optional[float]
    dehum_feasible: Optional[bool]

    # DP control (raw, pre-quantization)
    dp_sp_raw_c: Optional[float]

    # Requests (towards hydronics / plant)
    req_heating: bool
    req_cooling: bool
    req_dehumidif: bool
    req_water: bool

    # Useful diagnostics
    operative_season: str
    rh_target_pct: float
    t_ref_c: float
    raw_req_dehumidif: bool


class VmcPolicy:
    """VMC policy domain.

    Responsibilities
    - resolve operative season bucket (winter/summer/shoulder)
    - resolve RH target from config (season + profile)
    - compute DP setpoint (psychrometric or fixed)
    - compute DP hysteresis thresholds
    - evaluate dehumidification feasibility (water coil vs vent-only)
    - apply hysteresis memory to decide dehumidification request
    - decide whether heat/cool boost requests are allowed
    """

    def __init__(self, cfg: VmcConfig, *, state: VmcState) -> None:
        self.cfg = cfg
        self._state = state

    # -----------------
    # High-level API
    # -----------------

    def compute(
        self,
        *,
        snapshot: PlantSnapshot,
        profile: HVACOperatingProfile,
        heat_def_max_c: float,
        heat_def_wmean_c: float,
        heat_cov: float,
        cool_sur_max_c: float,
        cool_sur_wmean_c: float,
        cool_cov: float,
        dp_dehum_c: Optional[float],
        dp_max_c: Optional[float],
        outdoor_dp_c: Optional[float],
    ) -> VmcDemand:
        operative = self.infer_operative_bucket(snapshot)

        # reference indoor temperature + RH target
        t_ref_c = float(self.get_indoor_reference_temp_c(snapshot))
        rh_target_pct = float(self.rh_target_pct(operative, profile))

        # dp setpoint + hysteresis thresholds
        # NOTE: the device may support ΔDP only in coarse steps (e.g. 1°C).
        # We preserve the intended ON threshold by adjusting the *commanded*
        # DP setpoint when quantizing ΔDP for the device.
        dp_sp_raw_c = float(self.compute_dp_setpoint_c_from(t_ref_c, rh_target_pct))
        ddp_policy = float(self.cfg.dehum.setpoint_ddp_c)
        step = max(1e-9, float(getattr(self.cfg.dehum, "ddp_device_step_c", 1.0)))
        if ddp_policy <= 0.0:
            ddp_cmd = 0.0
        else:
            # Quantize UP to avoid triggering dehumidification earlier than intended.
            ddp_cmd = step * math.ceil(ddp_policy / step - 1e-12)
        # Adjust commanded DP setpoint so that dp_sp_cmd + ddp_cmd ~= dp_sp_raw + ddp_policy
        dp_sp_cmd_c = float(dp_sp_raw_c) + float(ddp_policy) - float(ddp_cmd)
        dp_sp_cmd_c = float(clamp(dp_sp_cmd_c, float(self.cfg.dehum.dp_sp_min_c), float(self.cfg.dehum.dp_sp_max_c)))
        hyst = max(0.0, float(self.cfg.dehum.hysteresis_c))
        on_thr = float(dp_sp_cmd_c) + float(ddp_cmd)
        off_thr = float(on_thr) - hyst

        # Raw device request (best effort)
        vmc = getattr(snapshot, "vmc", None)
        raw_req_dehum = bool(getattr(vmc, "request_dehumidification", False)) if vmc else False

        # Feasibility
        dehum_feasible: Optional[bool] = None
        if bool(self.cfg.dehum.water_on_for_dehumid):
            dehum_feasible = True
        elif outdoor_dp_c is not None and dp_dehum_c is not None:
            headroom = float(self.cfg.dehum.outdoor_dp_headroom_c)
            dehum_feasible = float(outdoor_dp_c) <= (float(dp_dehum_c) - headroom)

        # DP control uses robust dp_dehum, fallback to dp_max
        dp_current = dp_dehum_c if dp_dehum_c is not None else dp_max_c
        need_dehum = self.need_dehumidification(dp_current, on_thr, off_thr)

        # Boost eligibility
        allow_heat = self.allow_heat_boost(snapshot, heat_def_max_c, heat_def_wmean_c, heat_cov)
        allow_cool = self.allow_cool_boost(snapshot, cool_sur_max_c, cool_sur_wmean_c, cool_cov)

        req_heat = bool(allow_heat)
        req_cool = bool(allow_cool)
        req_dehum = bool(need_dehum or raw_req_dehum) and (dehum_feasible is not False)
        req_water = bool(req_heat or req_cool or (req_dehum and bool(self.cfg.dehum.water_on_for_dehumid)))

        return VmcDemand(
            dp_sp_c=float(dp_sp_cmd_c),
            ddp_cmd_c=float(ddp_cmd),
            dehum_on_thr_c=float(on_thr),
            dehum_off_thr_c=float(off_thr),
            dehum_feasible=dehum_feasible,
            dp_sp_raw_c=float(dp_sp_raw_c),
            req_heating=req_heat,
            req_cooling=req_cool,
            req_dehumidif=req_dehum,
            req_water=req_water,
            operative_season=str(operative),
            rh_target_pct=float(rh_target_pct),
            t_ref_c=float(t_ref_c),
            raw_req_dehumidif=bool(raw_req_dehum),
        )

    # -----------------
    # Pure-ish helpers
    # -----------------

    def infer_operative_bucket(self, snapshot: PlantSnapshot) -> str:
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)
        if season_val == "winter":
            return "winter"
        if season_val == "summer":
            return "summer"
        return "shoulder"

    def rh_target_pct(self, operative: Optional[str], profile: HVACOperatingProfile) -> float:
        return float(self.cfg.dehum.rh_target_pct(operative, profile))

    def get_indoor_reference_temp_c(self, snapshot: PlantSnapshot) -> float:
        z = snapshot.global_indoor_zone
        if z is not None:
            for av in (getattr(z, "t_op", None), getattr(z, "temperature", None)):
                v = as_float(getattr(av, "value", None))
                if v is not None:
                    return float(v)
        return float(self.cfg.setpoint_t_c)

    def compute_dp_setpoint_c_from(self, t_c: float, rh_pct: float) -> float:
        if bool(self.cfg.dehum.dp_setpoint_from_psychrometrics):
            try:
                dp = float(dew_point_celsius(float(t_c), float(rh_pct)))
            except Exception:
                dp = float(self.cfg.dehum.setpoint_dp_c)
        else:
            dp = float(self.cfg.dehum.setpoint_dp_c)
        return float(clamp(dp, float(self.cfg.dehum.dp_sp_min_c), float(self.cfg.dehum.dp_sp_max_c)))

    def need_dehumidification(self, dp_current_c: Optional[float], on_thr_c: float, off_thr_c: float) -> bool:
        """Dehumidification hysteresis (anti-flapping) using minimal memory."""
        if dp_current_c is None:
            self._state.dehum_on = False
            return False
        cur = float(dp_current_c)
        prev = self._state.dehum_on
        if prev is True:
            keep = cur > float(off_thr_c)
            self._state.dehum_on = bool(keep)
            return bool(keep)
        turn_on = cur > float(on_thr_c)
        self._state.dehum_on = bool(turn_on)
        return bool(turn_on)

    def allow_heat_boost(self, snapshot: PlantSnapshot, heat_def_max_c: float, heat_def_wmean_c: float, heat_cov: float) -> bool:
        if not bool(self.cfg.boost.enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if heat_def_max_c >= float(self.cfg.boost.heat_def_max_thr_c):
            return True
        if heat_def_wmean_c >= float(self.cfg.boost.heat_def_wmean_thr_c) and float(heat_cov) >= 0.6:
            return True
        return False

    def allow_cool_boost(self, snapshot: PlantSnapshot, cool_sur_max_c: float, cool_sur_wmean_c: float, cool_cov: float) -> bool:
        if not bool(self.cfg.boost.enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if cool_sur_max_c >= float(self.cfg.boost.cool_sur_max_thr_c):
            return True
        if cool_sur_wmean_c >= float(self.cfg.boost.cool_sur_wmean_thr_c) and float(cool_cov) >= 0.6:
            return True
        return False
