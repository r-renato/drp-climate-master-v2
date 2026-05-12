"""Policy layer for DRP Climate Master v2 comfort calculations.

Refactor goals (no logic changes)
--------------------------------
- Keep physics in ComfortBandCalculator.
- Keep policy decisions transparent (reasons).
- Remove duplicate room-classification logic by using domain.is_living().
- Keep existing public types/behavior.

Notes
-----
- Italian climate zones A..F are typically defined by degree-days (DPR 412/1993).
  This module intentionally does not implement municipality->zone lookup.
  Provide climate_zone in config (or compute upstream).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, Literal, Optional, Tuple

from ....domain.enums import HVACOperatingProfile

try:
    # Preferred in Home Assistant for correct timezone handling
    from homeassistant.util import dt as dt_util  # type: ignore
except Exception:  # pragma: no cover
    dt_util = None

from .config import (
    # CLO base stagionali
    CLO_BASE_SUMMER,
    CLO_BASE_SHOULDER,
    CLO_BASE_WINTER,
    # Correzioni CLO per profilo/stagione
    CLO_SLEEP_WINTER_DELTA,
    CLO_SLEEP_SHOULDER_DELTA,
    CLO_AWAY_VACATION_WINTER_DELTA,
    CLO_AWAY_VACATION_WINTER_FLOOR,
    CLO_CAP_SLEEP,
    CLO_CAP_DEFAULT,
    COLD_SNAP_CLO_FRACTION,
    # S4: interpolazione CLO shoulder con progresso stagionale
    CLO_SHOULDER_RAMP_ENABLED,
    CLO_SHOULDER_RAMP_START,
    # Tabella CLO per zona climatica
    CLO_WINTER_BY_ZONE,
    # MET per profilo
    MET_BASE,
    MET_SLEEP,
    MET_AWAY_VACATION,
    # Tabelle PMV e aggressività
    MODE_PMV_CENTER,
    MODE_PMV_BAND,
    MODE_CTRL_AGGRESSIVENESS,
    # Nudge PMV estate/ECO
    PMV_SUMMER_ECO_NUDGE_DELTA,
    PMV_SUMMER_ECO_CENTER_MAX,
    # Draft robustness
    DRAFT_ALPHA_LIVING,
    DRAFT_ALPHA_OTHER,
    DRAFT_ALPHA_SLEEP_DELTA,
    DRAFT_ALPHA_SLEEP_MIN,
    DRAFT_ALPHA_HIGH_SPEED_DELTA,
    VMC_SPEED_THR_DRAFT,
    VMC_SPEED_THR_LIVING_HI_SCALE,
    # v_air scaling SLEEP/WINTER
    V_AIR_HI_SLEEP_WINTER_MAX,
    # Scaling v_hi per portata alta VMC
    V_AIR_HI_SCALE_NON_LIVING_HIGH_SPEED,
    V_AIR_HI_SCALE_LIVING_HIGH_SPEED,
    # S3: adaptive CLO su running mean T_op
    T_RM_NEUTRAL_BY_SEASON,
    T_RM_SENSITIVITY_CLO,
    T_RM_CAP_DELTA_CLO,
    T_RM_SKIP_PROFILES,
    T_RM_MAX_DEVIATION_SUPPRESS_C,
)
from .model import PolicyContext, PolicyDecision, HumiditySolveMode, is_living, ClimateZoneIT, ComplianceMode


# -----------------------------
# Configuration / data model
# -----------------------------


@dataclass(slots=True)
class ConfortPolicyConfig:
    """Configurazione statica del policy layer per il comfort ISO 7730.

    I valori di default replicano le costanti definite in ``config.py``:
    in questo modo un'istanza costruita senza argomenti è già calibrata
    per l'impianto di riferimento (zona climatica D, met base 1.10).
    Per modificare i default a livello di impianto, agire su ``config.py``.
    """

    # Contesto geografico
    climate_zone: Optional[ClimateZoneIT] = None

    # Profilo comfort base (v. config.py sezione C e D)
    base_met: float = MET_BASE
    base_clo_summer: float = CLO_BASE_SUMMER
    base_clo_shoulder: float = CLO_BASE_SHOULDER
    default_clo_winter: float = CLO_BASE_WINTER

    # Se True e climate_zone è impostata, applica il delta CLO zonale invernale.
    zone_clo_delta_enabled: bool = True

    # Calibrazione draft/aria (scaling v_hi per step VMC alto)
    # Vedi config.py sezione B per il razionale.
    non_living_high_speed_hi_scale: float = V_AIR_HI_SCALE_NON_LIVING_HIGH_SPEED
    living_high_speed_hi_scale: float = V_AIR_HI_SCALE_LIVING_HIGH_SPEED

    # Compliance gating
    compliance_mode: ComplianceMode = ComplianceMode.OFF
    heating_allowed_from: Optional[time] = None
    heating_allowed_to: Optional[time] = None
    cooling_allowed_from: Optional[time] = None
    cooling_allowed_to: Optional[time] = None

    # ---------------------------------------------------------------------
    # Helper methods
    # ---------------------------------------------------------------------

    def clo_for_season(self, season: Literal["summer", "shoulder", "winter"]) -> float:
        if season == "summer":
            return self.base_clo_summer
        if season == "shoulder":
            return self.base_clo_shoulder
        return self.default_clo_winter

    def winter_clo(self, *, zone_delta: Optional[float] = None) -> float:
        clo = self.default_clo_winter
        if self.zone_clo_delta_enabled and zone_delta is not None:
            clo += zone_delta
        return clo

    def draft_hi_scale(self, *, is_living_room: bool) -> float:
        return self.living_high_speed_hi_scale if is_living_room else self.non_living_high_speed_hi_scale

    def allowed_window(self, mode: Literal["heating", "cooling"]) -> tuple[Optional[time], Optional[time]]:
        if mode == "heating":
            return self.heating_allowed_from, self.heating_allowed_to
        return self.cooling_allowed_from, self.cooling_allowed_to

    def is_within_allowed_window(self, *, mode: Literal["heating", "cooling"], now_local: time) -> bool:
        """Return True if now_local is inside the configured window.

        Semantics preserved:
        - If window not configured (start or end is None) => True.
        - Supports overnight windows.
        """
        start, end = self.allowed_window(mode)
        if start is None or end is None:
            return True
        if start <= end:
            return start <= now_local < end
        return now_local >= start or now_local < end

    def compliance_enabled(self) -> bool:
        return self.compliance_mode in (ComplianceMode.WARN, ComplianceMode.ENFORCE)

    def validate(self) -> None:
        if self.base_met <= 0:
            raise ValueError("base_met must be > 0")
        for name, v in (
            ("base_clo_summer", self.base_clo_summer),
            ("base_clo_shoulder", self.base_clo_shoulder),
            ("default_clo_winter", self.default_clo_winter),
            ("non_living_high_speed_hi_scale", self.non_living_high_speed_hi_scale),
            ("living_high_speed_hi_scale", self.living_high_speed_hi_scale),
        ):
            if v <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.compliance_mode not in (ComplianceMode.OFF, ComplianceMode.WARN, ComplianceMode.ENFORCE):
            raise ValueError("compliance_mode must be 'off', 'warn' or 'enforce'")


# Le tabelle di lookup per zona climatica, PMV target e aggressività controllo
# sono definite in ``config.py`` di questo modulo e importate direttamente.
# Non sono più replicate qui: un'unica source of truth evita disallineamenti.


# -----------------------------
# Implementation
# -----------------------------

class ComfortPolicyLayer:
    """Policy layer that selects comfort-model inputs."""

    def __init__(self, cfg: ConfortPolicyConfig) -> None:
        self._cfg = cfg

    def decide(self, ctx: PolicyContext) -> PolicyDecision:
        reasons: list[str] = []

        # 0) Validate inputs lightly
        vmc_speed = ctx.vmc_speed
        if not isinstance(vmc_speed, int):
            reasons.append(f"vmc_speed:invalid_type={type(vmc_speed).__name__}")
            try:
                vmc_speed = int(vmc_speed)  # best effort
            except Exception:
                vmc_speed = 0
        if vmc_speed < 0 or vmc_speed > 5:
            reasons.append(f"vmc_speed:clamped_from={vmc_speed}")
            vmc_speed = max(0, min(5, vmc_speed))

        # 1) met (mode-aware)
        met = float(self._cfg.base_met)
        if ctx.mode == HVACOperatingProfile.SLEEP:
            met = MET_SLEEP
            reasons.append(f"met:sleep={MET_SLEEP:.2f}")
        elif ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
            met = MET_AWAY_VACATION
            reasons.append(f"met:away/vacation={MET_AWAY_VACATION:.2f}")

        # 2) clo (season + climate zone + mode)
        clo = self._clo_for(ctx, reasons)

        # 3) PMV targets from mode
        pmv_center, pmv_band = self._pmv_targets(ctx, reasons)

        # 3b) Controller aggressiveness (decoupled from PMV)
        ctrl_aggr = float(MODE_CTRL_AGGRESSIVENESS.get(ctx.mode, 1.0))
        reasons.append(f"ctrl:aggr={ctrl_aggr:.2f}")

        # 4) v_air scaling (draft calibration)
        v_best_s, v_hi_s, v_lo_override = self._v_air_policy(ctx, vmc_speed, reasons)

        # Draft robustness: blending v_best→v_hi per il bound freddo (t_op_min).
        # Vedi config.py sezione B per il razionale termotecnico.
        living = is_living(ctx.room)
        draft_alpha = DRAFT_ALPHA_LIVING if living else DRAFT_ALPHA_OTHER
        if ctx.mode == HVACOperatingProfile.SLEEP:
            # Notte: riduce il conservativismo (VMC a passo basso, draft reale minimo)
            # → abbassa t_op_min, evitando riscaldamento prematuro nelle ore notturne.
            draft_alpha = max(DRAFT_ALPHA_SLEEP_MIN, draft_alpha + DRAFT_ALPHA_SLEEP_DELTA)
            reasons.append(f"draft:sleep_delta={DRAFT_ALPHA_SLEEP_DELTA:+.2f} -> {draft_alpha:.2f}")
        if (vmc_speed >= VMC_SPEED_THR_DRAFT) and (not living):
            # VMC ad alta portata in ambienti non-living: la turbolenza è già
            # rappresentata nelle tabelle V_AIR_HI_OTHER → correzione per evitare
            # doppio conteggio dell'effetto draft.
            draft_alpha = max(0.0, draft_alpha + DRAFT_ALPHA_HIGH_SPEED_DELTA)
            reasons.append(f"draft:high_speed_delta={DRAFT_ALPHA_HIGH_SPEED_DELTA:+.2f} -> {draft_alpha:.2f}")

        # 4b) Humidity solving mode for comfort-band bounds (policy choice)
        # Default strategy:
        # - SUMMER/WINTER: favor PA_CONST (more physical for typical homes without tight RH control)
        # - SHOULDER: AUTO (lets the calculator fall back to RH_CONST if no anchor temperature is available)
        from ....domain.models.season import OperativeSeason

        if ctx.season in (OperativeSeason.SUMMER, OperativeSeason.WINTER):
            h_mode = HumiditySolveMode.PA_CONST
        else:
            h_mode = HumiditySolveMode.AUTO
        reasons.append(f"humidity_solve_mode={h_mode}")

        # 5) optional compliance gating
        heating_allowed, cooling_allowed = self._compliance(ctx, reasons)

        return PolicyDecision(
            met=float(met),
            clo=float(clo),
            pmv_center=float(pmv_center),
            pmv_band=float(pmv_band),
            ctrl_aggressiveness=float(ctrl_aggr),
            v_air_best_scale=float(v_best_s),
            v_air_hi_scale=float(v_hi_s),
            v_air_lo_override=v_lo_override,
            draft_robustness=draft_alpha,
            humidity_solve_mode=h_mode,
            heating_allowed=heating_allowed,
            cooling_allowed=cooling_allowed,
            reasons=tuple(reasons),
        )

    # -------------------------
    # Internals
    # -------------------------

    def _clo_for(self, ctx: PolicyContext, reasons: list[str]) -> float:
        from ....domain.models.season import OperativeSeason

        # Base per stagione (vedi config.py sezione C)
        if ctx.season == OperativeSeason.SUMMER:
            clo = float(self._cfg.base_clo_summer)
            reasons.append(f"clo:summer={clo:.2f}")
        elif ctx.season == OperativeSeason.SHOULDER:
            clo = float(self._cfg.base_clo_shoulder)
            reasons.append(f"clo:shoulder={clo:.2f}")
            # cold_snap: interpola clo verso inverno.
            # Le persone si vestono più pesante nei giorni freddi per la stagione;
            # il PMV calcolato con clo più alto è fisicamente più corretto.
            # Frazione di interpolazione: config.COLD_SNAP_CLO_FRACTION.
            if ctx.cold_snap:
                if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                    clo_winter = float(CLO_WINTER_BY_ZONE.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                else:
                    clo_winter = float(self._cfg.default_clo_winter)
                clo = clo + COLD_SNAP_CLO_FRACTION * (clo_winter - clo)
                reasons.append(f"clo:cold_snap:interp={clo:.2f}")

            # S4 - Interpolazione progressiva CLO shoulder -> target stagionale.
            # Attiva solo se abilitata in config e il progresso è disponibile.
            # spring -> CLO_BASE_SUMMER (meno vestiti verso estate)
            # autumn -> CLO invernale per zona climatica (più vestiti verso inverno)
            # La rampa inizia a CLO_SHOULDER_RAMP_START% e arriva a target al 100%.
            # Applicata DOPO cold_snap: il cold_snap è una correzione giornaliera
            # rispetto al base, S4 corregge il base stesso su scala stagionale.
            if CLO_SHOULDER_RAMP_ENABLED and ctx.season_progress is not None and ctx.shoulder_direction is not None:
                _ramp_start = float(CLO_SHOULDER_RAMP_START)
                _progress = float(ctx.season_progress)
                if _progress > _ramp_start:
                    blend = (_progress - _ramp_start) / (100.0 - _ramp_start)
                    blend = max(0.0, min(1.0, blend))
                    if ctx.shoulder_direction == "spring":
                        _clo_target = float(self._cfg.base_clo_summer)
                    else:
                        # autumn -> inverno
                        if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                            _clo_target = float(CLO_WINTER_BY_ZONE.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                        else:
                            _clo_target = float(self._cfg.default_clo_winter)
                    clo_s4 = float(self._cfg.base_clo_shoulder) + blend * (_clo_target - float(self._cfg.base_clo_shoulder))
                    # cold_snap preservato: S4 agisce sul base, il delta cold_snap
                    # viene ricalcolato sulla nuova base interpolata.
                    if ctx.cold_snap:
                        clo = clo_s4 + COLD_SNAP_CLO_FRACTION * (_clo_target - clo_s4)
                    else:
                        clo = clo_s4
                    reasons.append(
                        f"clo:s4:{ctx.shoulder_direction}"
                        f":progress={_progress:.1f}%"
                        f":blend={blend:.3f}"
                        f":target={_clo_target:.2f}"
                        f" -> {clo:.3f}"
                    )
        else:
            # Inverno: usa la tabella per zona climatica se abilitata
            if self._cfg.climate_zone and self._cfg.zone_clo_delta_enabled:
                clo = float(CLO_WINTER_BY_ZONE.get(self._cfg.climate_zone, self._cfg.default_clo_winter))
                reasons.append(f"clo:winter:zone={self._cfg.climate_zone}={clo:.2f}")
            else:
                clo = float(self._cfg.default_clo_winter)
                reasons.append(f"clo:winter:default={clo:.2f}")

        # Correzioni per profilo/stagione (vedi config.py sezione C)
        if ctx.mode == HVACOperatingProfile.SLEEP:
            season_name = ctx.season.name.lower()
            if season_name == "winter":
                # Coperte invernali: pigiama + piumino pesante
                clo += CLO_SLEEP_WINTER_DELTA
                reasons.append(f"clo:sleep:winter:+{CLO_SLEEP_WINTER_DELTA:.2f} -> {clo:.2f}")
            elif season_name == "shoulder":
                # Coperta primaverile/autunnale: piumino leggero o coperta singola.
                # In mezza stagione a Roma gli occupanti usano comunque coperture;
                # senza questo delta il modello produce PMV~-0.4 a 23 gradi (deficit spurio).
                clo += CLO_SLEEP_SHOULDER_DELTA
                reasons.append(f"clo:sleep:shoulder:+{CLO_SLEEP_SHOULDER_DELTA:.2f} -> {clo:.2f}")

        if ctx.mode in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION) and ctx.season.name.lower() == "winter":
            # Assenza: abbigliamento leggero; floor a CLO_AWAY_VACATION_WINTER_FLOOR
            clo = max(CLO_AWAY_VACATION_WINTER_FLOOR, clo + CLO_AWAY_VACATION_WINTER_DELTA)
            reasons.append(f"clo:away:{CLO_AWAY_VACATION_WINTER_DELTA:+.2f}:floor={CLO_AWAY_VACATION_WINTER_FLOOR:.2f} -> {clo:.2f}")

        # S3 — Correzione adattiva CLO su running mean T_op (ASHRAE 55 adaptive)
        # Soppresso per SLEEP/AWAY/VACATION (vedi config.T_RM_SKIP_PROFILES).
        # Soppresso se t_op_running_mean è None (tracker in warm-up → fail-safe).
        profile_name = ctx.mode.value.lower() if hasattr(ctx.mode, "value") else str(ctx.mode).lower()
        t_rm = ctx.t_op_running_mean
        if t_rm is not None and profile_name not in T_RM_SKIP_PROFILES:
            deviation = abs(ctx.t_op_current - float(t_rm)) if ctx.t_op_current is not None else 0.0
            if deviation > T_RM_MAX_DEVIATION_SUPPRESS_C:
                # Rientro da assenza: T_rm troppo distante da T_op corrente.
                # S3 soppressa → CLO stagionale baseline (fail-safe neutro).
                reasons.append(
                    f"clo:trm:suppressed:deviation={deviation:.1f}C"
                    f">threshold={T_RM_MAX_DEVIATION_SUPPRESS_C:.1f}C"
                )
            else:
                season_key = ctx.season.value.lower() if hasattr(ctx.season, "value") else str(ctx.season).lower()
                t_rm_neutral = float(T_RM_NEUTRAL_BY_SEASON.get(season_key, T_RM_NEUTRAL_BY_SEASON["shoulder"]))
                delta_raw = (t_rm_neutral - float(t_rm)) * T_RM_SENSITIVITY_CLO
                delta_clo = max(-T_RM_CAP_DELTA_CLO, min(T_RM_CAP_DELTA_CLO, delta_raw))
                if abs(delta_clo) >= 0.005:
                    clo = clo + delta_clo
                    reasons.append(
                        f"clo:trm:t_rm={float(t_rm):.1f}C"
                        f":neutral={t_rm_neutral:.1f}C"
                        f":delta={delta_clo:+.3f}"
                        f" -> {clo:.3f}"
                    )

        # Cap superiore fisicamente plausibile per profilo
        clo_cap = CLO_CAP_SLEEP if ctx.mode == HVACOperatingProfile.SLEEP else CLO_CAP_DEFAULT
        clo = min(clo_cap, clo)
        return float(clo)

    def _pmv_targets(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[float, float]:
        from ....domain.models.season import OperativeSeason

        # Legge centro e banda dalla tabella per profilo (config.py sezione E).
        # Fallback su COMFORT se il profilo non è in tabella.
        fallback_center = MODE_PMV_CENTER[HVACOperatingProfile.COMFORT]
        fallback_band   = MODE_PMV_BAND[HVACOperatingProfile.COMFORT]
        pmv_center = float(MODE_PMV_CENTER.get(ctx.mode, fallback_center))
        pmv_band   = float(MODE_PMV_BAND.get(ctx.mode, fallback_band))
        reasons.append(f"pmv:mode={ctx.mode}:center={pmv_center:+.2f},band={pmv_band:.2f}")

        # Nudge estate/ECO: alza leggermente il centro per ridurre il raffreddamento.
        # Vedi config.py PMV_SUMMER_ECO_NUDGE_DELTA e PMV_SUMMER_ECO_CENTER_MAX.
        if ctx.season == OperativeSeason.SUMMER and ctx.mode == HVACOperatingProfile.ECO:
            pmv_center = min(PMV_SUMMER_ECO_CENTER_MAX, pmv_center + PMV_SUMMER_ECO_NUDGE_DELTA)
            reasons.append(f"pmv:summer_eco:nudge+{PMV_SUMMER_ECO_NUDGE_DELTA:.2f} -> {pmv_center:+.2f}")

        return pmv_center, pmv_band

    def _v_air_policy(self, ctx: PolicyContext, vmc_speed: int, reasons: list[str]) -> Tuple[float, float, Optional[float]]:
        v_best_s = 1.0
        v_hi_s = 1.0
        v_lo_override: Optional[float] = None

        living = is_living(ctx.room)

        # Step VMC alto in ambienti non-living: riduce lo scaling v_hi per non
        # sovrastimare la velocità (già rappresentata nelle tabelle V_AIR_HI_OTHER).
        if vmc_speed >= VMC_SPEED_THR_DRAFT and not living:
            v_hi_s = float(self._cfg.non_living_high_speed_hi_scale)
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (speed>={VMC_SPEED_THR_DRAFT} non-living)")

        # Step VMC moderato/alto in living: aumenta leggermente v_hi per riflettere
        # la maggiore dispersione in ambienti aperti (tabella LIVING più alta).
        if living and vmc_speed >= VMC_SPEED_THR_LIVING_HI_SCALE:
            v_hi_s = max(v_hi_s, float(self._cfg.living_high_speed_hi_scale))
            reasons.append(f"v_air_hi:scale={v_hi_s:.2f} (living speed>={VMC_SPEED_THR_LIVING_HI_SCALE})")

        # SLEEP/WINTER: azzera l'amplificazione v_hi (VMC a passo basso di notte;
        # il draft reale è già gestito dall'alpha, non serve scaling aggiuntivo).
        if ctx.mode == HVACOperatingProfile.SLEEP and ctx.season.name.lower() == "winter":
            v_hi_s = min(v_hi_s, V_AIR_HI_SLEEP_WINTER_MAX)
            reasons.append(f"v_air_hi:sleep_winter:cap={V_AIR_HI_SLEEP_WINTER_MAX:.2f}")

        return float(v_best_s), float(v_hi_s), v_lo_override

    def _compliance(self, ctx: PolicyContext, reasons: list[str]) -> Tuple[Optional[bool], Optional[bool]]:
        from ....domain.models.season import OperativeSeason

        if self._cfg.compliance_mode == ComplianceMode.OFF:
            return None, None

        now_local: datetime = ctx.now
        if dt_util is not None:
            now_local = dt_util.as_local(ctx.now)
        elif ctx.now.tzinfo is not None:
            now_local = ctx.now.astimezone()

        h_ok = self._time_in_window_eval(now_local, self._cfg.heating_allowed_from, self._cfg.heating_allowed_to)
        c_ok = self._time_in_window_eval(now_local, self._cfg.cooling_allowed_from, self._cfg.cooling_allowed_to)

        heating_allowed: Optional[bool] = None
        cooling_allowed: Optional[bool] = None

        if ctx.season == OperativeSeason.WINTER:
            heating_allowed = h_ok
            if heating_allowed is not None:
                reasons.append(f"compliance:heating_allowed={heating_allowed}")
        elif ctx.season == OperativeSeason.SUMMER:
            cooling_allowed = c_ok
            if cooling_allowed is not None:
                reasons.append(f"compliance:cooling_allowed={cooling_allowed}")

        return heating_allowed, cooling_allowed

    @staticmethod
    def _time_in_window_eval(now: datetime, start: Optional[time], end: Optional[time]) -> Optional[bool]:
        """Return whether now.local_time is within [start, end). Supports overnight windows.

        Semantics preserved:
        - If start or end is None => returns None (not evaluable)
        - 'now' should already be in local timezone.
        """
        if start is None or end is None:
            return None

        t = now.timetz().replace(tzinfo=None)
        if start <= end:
            return start <= t < end
        return (t >= start) or (t < end)


# -----------------------------
# Builder
# -----------------------------


def build_policy_layer(
    *,
    climate_zone: Optional[ClimateZoneIT] = ClimateZoneIT.D,
    compliance_mode: ComplianceMode = ComplianceMode.OFF,
) -> ComfortPolicyLayer:
    """Convenience builder (no behavior change; no automatic validate)."""

    if climate_zone is None:
        cz = None
    elif isinstance(climate_zone, ClimateZoneIT):
        cz = climate_zone
    else:
        v = str(climate_zone).strip().upper()
        cz = ClimateZoneIT.from_str(v) if ClimateZoneIT.is_member(v) else None

    cfg = ConfortPolicyConfig(climate_zone=cz, compliance_mode=compliance_mode)
    return ComfortPolicyLayer(cfg)
