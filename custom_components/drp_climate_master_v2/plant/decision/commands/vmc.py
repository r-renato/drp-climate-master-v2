from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....helpers.utils import as_bool, as_float, clamp
from ....domain.enums import HVACOperatingProfile
from ....domain.models.plant import PlantSnapshot

from ..config import PlantPlannerConfig
from ..contracts import PlantDecision, PlantDemandSignals, PlantMode
from ..vmc.policy import VmcPolicy


@dataclass(slots=True)
class VmcCommandBuilder:
    """Build VMC commands based on PlantMode and VMC policy outputs."""

    cfg: PlantPlannerConfig
    vmc_policy: VmcPolicy

    def fill(self, dec: PlantDecision, snapshot: PlantSnapshot, demand: PlantDemandSignals) -> None:
        """Populate VMC commands.

        Rational:
        - La VMC serve per ventilare e deumidificare (via DP setpoint).
        - Il contributo termico (heating/cooling) è solo boost quando fuori comfort in modo significativo.
        - Il setpoint T neutro evita di trascinare la PDC per inseguire 24°C in inverno.
        """
        cfg = self.cfg
        v = dec.vmc

        # OFF means plant idle, including VMC (unless a different policy is implemented).
        if dec.mode == PlantMode.OFF:
            v.power = False
            v.mode = "off"
            v.air_speed = 0
            v.setpoint_t_c = None
            v.setpoint_rh_pct = None
            v.setpoint_dp_c = None
            v.setpoint_ddp_c = None
            v.debug.update({"reason": "plant_mode_off"})
            return

        if demand.operative_season == "winter":
            mode = cfg.vmc.mode_winter
        elif demand.operative_season == "summer":
            mode = cfg.vmc.mode_summer
        else:
            mode = getattr(snapshot.vmc, "processing_mode", None) or cfg.vmc.mode_winter

        t_ref_c = float(self.vmc_policy.get_indoor_reference_temp_c(snapshot))
        profile = HVACOperatingProfile.from_value(demand.user_profile, default=HVACOperatingProfile.COMFORT) or HVACOperatingProfile.COMFORT
        rh_target_pct = float(self.vmc_policy.rh_target_pct(demand.operative_season, profile))

        dp_sp_c = float(
            getattr(demand, "vmc_dp_sp_c", None)
            or self.vmc_policy.compute_dp_setpoint_c_from(t_ref_c, rh_target_pct)
        )
        ddp_sp_c = float(getattr(demand, "vmc_ddp_cmd_c", None) or cfg.vmc.dehum.setpoint_ddp_c)

        dp_current = getattr(demand, "dp_dehum_c", None) or demand.dp_max_c
        boost_active = bool(demand.vmc_req_heating or demand.vmc_req_cooling or demand.vmc_req_dehumidif)

        air_speed = self._compute_air_speed(snapshot, dp_current, dp_sp_c, boost_active)

        if demand.vmc_req_heating:
            t_sp = float(cfg.vmc.boost.setpoint_heat_c)
        elif demand.vmc_req_cooling:
            t_sp = float(cfg.vmc.boost.setpoint_cool_c)
        else:
            dead = float(cfg.vmc.temp_neutral_deadband_c)
            if mode == cfg.vmc.mode_winter:
                t_sp = t_ref_c - dead
            elif mode == cfg.vmc.mode_summer:
                t_sp = t_ref_c + dead
            else:
                t_sp = t_ref_c

        t_sp = clamp(float(t_sp), float(cfg.vmc.temp_min_c), float(cfg.vmc.temp_max_c))

        v.power = True
        v.mode = mode
        v.setpoint_t_c = round(t_sp, 1)
        v.setpoint_rh_pct = round(rh_target_pct, 0)
        v.setpoint_dp_c = round(dp_sp_c, 1)
        v.setpoint_ddp_c = int(round(ddp_sp_c, 0))
        v.air_speed = int(air_speed)

        # Diagnostics: report current vmc state
        if snapshot.vmc:
            vmc = snapshot.vmc
            v.debug.update(
                {
                    "recirculation": cfg.vmc.recirculation,
                    "device_power": getattr(vmc, "power_on", None),
                    "req_water": getattr(vmc, "request_water", None),
                    "req_heating": getattr(vmc, "request_heating", None),
                    "req_cooling": getattr(vmc, "request_cooling", None),
                    "req_dehumidif": getattr(vmc, "request_dehumidification", None),
                    "ambient_t_c": as_float(getattr(vmc, "sensor_t_ambient", None)),
                    "ambient_rh_pct": as_float(getattr(vmc, "sensor_h_ambient", None)),
                    "water_t_c": as_float(getattr(vmc, "sensor_t_water", None)),
                    "outdoor_t_c": as_float(getattr(vmc, "sensor_t_outdoor", None)),
                    "alarm_dew_point": getattr(vmc, "alarm_dew_point", None),
                    "alarm_general": getattr(vmc, "alarm_alarm", None),
                }
            )

    def _compute_air_speed(
        self,
        snapshot: PlantSnapshot,
        dp_current_c: Optional[float],
        dp_setpoint_c: float,
        boost: bool,
    ) -> int:
        windows_closed = as_bool(getattr(snapshot, "windows_close_state", None), default=True)
        if windows_closed is False:
            sp = int(self.cfg.vmc.speed.speed_windows_open)
        elif bool(snapshot.presence_vacation) or bool(snapshot.presence_nobodysin):
            sp = int(self.cfg.vmc.speed.speed_vacation)
        else:
            sp = int(self.cfg.vmc.speed.speed_base)

        if dp_current_c is not None:
            delta = float(dp_current_c) - float(dp_setpoint_c)
            if delta >= float(self.cfg.vmc.speed.dp_boost_step1_c):
                sp += 1
            if delta >= float(self.cfg.vmc.speed.dp_boost_step2_c):
                sp += 1
            if delta >= float(self.cfg.vmc.speed.dp_boost_step3_c):
                sp += 1

        if boost:
            sp = max(sp, int(self.cfg.vmc.boost.min_air_speed))

        return int(clamp(float(sp), float(self.cfg.vmc.speed.speed_min), float(self.cfg.vmc.speed.speed_max)))
