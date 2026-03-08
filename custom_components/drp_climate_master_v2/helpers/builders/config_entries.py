# custom_components/drp_climate_master_v2/helpers/config_entries.py
from __future__ import annotations

import logging
from dataclasses import is_dataclass, fields
from datetime import timedelta
from typing import Any, Mapping, Callable, Iterable, Optional, Union, List

from homeassistant.core import HomeAssistant, Event, CALLBACK_TYPE, EventStateChangedData
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.config_entries import ConfigEntry
from homeassistant.util.unit_system import get_unit_system
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (
    CONF_SENSORS,
)

from ..logger import log_debug, log_info

from ..utils import as_int
from ...domain.models.runtime_schema import (
    AreaConfig,
    ClimateConfig,
    CompressorManagementConfig,
    CoolingManagementConfig,
    DevicesConfig,
    ForecastDataConfig,
    HistoricalDataConfig,
    InfluxdbHistoricalDataConfig,
    WindowsConfig,
    ModeConfig,
    PlantCapabilities,
    RadiantConfig,
    RadiantSensors,
    RadiantSurface,
    RuntimeConfig,
    ScenariosConfig,
    SeasonConfig,
    SensorPair,
    SetpointConfig,
    SupplyUnitSensors,
    SupplyUnitsConfig,
    VMCAlarmsConfig,
    VMCConfig,
    VMCRequestsConfig,
    VMCSensorsConfig,
    WeatherConfig
)
from ...const import (
    CONF_ADJUSTABLE_SUPPLY_UNIT,
    CONF_ALARMS,
    CONF_AREA,
    CONF_AREAS,
    CONF_BUCKET,
    CONF_CEILING,
    CONF_COMPRESSOR_MANAGEMENT,
    CONF_COOLING_DT_SETPOINT,
    CONF_COOLING_MANAGEMENT,
    CONF_COOLING_T_SETPOINT,
    CONF_DELTA_DEW_POINT_SETPOINT,
    CONF_DEVICES,
    CONF_DEW_POINT_SETPOINT,
    CONF_DIRECT_SUPPLY_UNIT,
    CONF_FM_POWER,
    CONF_FORCE_COOLING,
    CONF_FORCE_FREE_COOLING,
    CONF_FORCE_HEATING,
    CONF_FORECAST_DATA,
    CONF_H_SETPOINT,
    CONF_HEATING_DT_SETPOINT,
    CONF_HEATING_T_SETPOINT,
    CONF_HISTORICAL_DATA,
    CONF_INDOOR,
    CONF_INFLUXDB,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MODE,
    CONF_ORGANIZATION,
    CONF_POWER,
    CONF_PROVIDER,
    CONF_RADIANT,
    CONF_RADIANT_SURFACE,
    CONF_RADIANT_SURFACES,
    CONF_SURFACE_M2,
    CONF_VALVE_SWITCH,
    CONF_REQUESTS,
    CONF_SCENARIOS,
    CONF_SEASON,
    CONF_SPARE_SETPOINT,
    CONF_WINDOWS,
    CONF_SUPPLY_UNITS,
    CONF_T_SETPOINT,
    CONF_TCOLLECTOR,
    CONF_THREE_POINT_MIXING_VALVE,
    CONF_TOKEN,
    CONF_UNITS,
    CONF_CLOSED_STATE,
    CONF_VENT_RECIRCULATION,
    CONF_VMC,
    CONF_WEATHER,
    DEFAULT_INFLUXDB_URL,
    DEFAULT_UNITS,
    OPT_UPDATE_INTERVAL_S,
    OPT_UPDATE_MIN_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)

def subscribe_entity_state_changes(
    hass: HomeAssistant,
    callback: Callable[[Event[EventStateChangedData]], Any],
    entity_ids: Union[str, Iterable[str]],
    *,
    on_remove: Optional[Callable[[CALLBACK_TYPE], None]] = None,
) -> Optional[CALLBACK_TYPE]:
    """
    Sottoscrive gli eventi `state_changed` per uno o più `entity_id` e restituisce
    la **funzione di unsubscribe**.

    Questo helper è un thin-wrapper su `async_track_state_change_event` che:
    - accetta una stringa singola o un iterabile di `entity_id`;
    - normalizza e **deduplica** gli ID vuoti o ripetuti;
    - opzionalmente registra l’unsubscribe nel ciclo di vita passato in `on_remove`
      (es. `entry.async_on_unload`, `entity.async_on_remove`).

    Parametri
    ---------
    hass : HomeAssistant
        Istanza di Home Assistant.
    callback : Callable[[Event[EventStateChangedData]], Any]
        Handler invocato su ogni evento `state_changed` degli entity monitorati.
        Può essere sincrono (consigliato decorare con `@callback`) o `async def`.
        Firma tip-safe: `def handler(event: Event[EventStateChangedData]) -> None`.
    entity_ids : str | Iterable[str]
        Uno o più `entity_id` (es. `"sensor.t_living"` o `["sensor.t_living", "sensor.h_living"]`).
    on_remove : Callable[[CALLBACK_TYPE], None], opzionale
        Funzione alla quale passare la `unsubscribe` per legarla al ciclo di vita
        (es. `entry.async_on_unload`, `self.async_on_remove`).
    log : bool, opzionale
        Se `True` logga l’attivazione dell’ascolto.

    Ritorno
    -------
    Optional[CALLBACK_TYPE]
        La funzione di **unsubscribe** (richiamala per rimuovere il listener),
        oppure `None` se `entity_ids` non contiene elementi validi.

    Esempi
    -------
    >>> # In config entry setup:
    >>> unsub = subscribe_entity_state_changes(
    ...     hass,
    ...     callback=my_handler,                              # def my_handler(e: Event[EventStateChangedData]) -> None
    ...     entity_ids=["sensor.t_soggiorno", "sensor.h_soggiorno"],
    ...     on_remove=entry.async_on_unload,                  # si pulisce da solo allo unload dell'entry
    ... )
    ...
    >>> # Dentro una Entity:
    >>> self._unsub = subscribe_entity_state_changes(
    ...     self.hass, my_handler, "sensor.t_camera", on_remove=self.async_on_remove
    ... )

    Note
    ----
    - Preferisci handler **sincroni** con `@callback` per ridurre overhead.
    - La firma della callback è tipizzata come `Event[EventStateChangedData]` per
      essere compatibile con `async_track_state_change_event` su HA 2025.4.x+.
    """
    # Normalizza gli entity_id
    ids: List[str]
    if isinstance(entity_ids, str):
        ids = [entity_ids]
    else:
        ids = [e for e in entity_ids if isinstance(e, str) and e.strip()]

    if not ids:
        _LOGGER.error("setup_entity_change: nessun entity_id valido.")
        return None

    unsubscribe: CALLBACK_TYPE = async_track_state_change_event(hass, ids, callback)
    log_info(_LOGGER, "Ascolto attivo per %s", ", ".join(ids))

    if on_remove is not None:
        try:
            on_remove(unsubscribe)
        except Exception:
            _LOGGER.exception("setup_entity_change: on_remove ha generato un'eccezione.")

    return unsubscribe

def _infer_capabilities_from_devices(options: Mapping[str, Any]) -> tuple[bool, bool, bool, bool]:
    """
    Deduce heating/cooling/dehumidifying capabilities from devices config
    when the user did not specify them explicitly.
    """
    devices = options.get(CONF_DEVICES) or {}
    if not isinstance(devices, Mapping):
        return False, False, False, False  # fallback conservativo

    radiant = devices.get(CONF_RADIANT) or {}
    vmc = devices.get(CONF_VMC) or {}

    supports_heating = supports_cooling = bool(radiant)
    supports_dehumidifying = supports_ventilation  = bool(vmc)

    return supports_heating, supports_cooling, supports_dehumidifying, supports_ventilation

def build_runtime_config(entry: ConfigEntry) -> RuntimeConfig:
    # --- Merge config: prefer options over data (options = overrides) ---
    data = dict(entry.data) if isinstance(entry.data, Mapping) else {}
    opts = dict(entry.options) if isinstance(entry.options, Mapping) else {}

    log_debug(_LOGGER, "ENTRY.DATA radiant.sensors=%s",
            (entry.data.get("devices", {}).get("radiant", {}).get("sensors", {})
            if isinstance(entry.data, Mapping) else None))

    log_debug(_LOGGER, "ENTRY.OPTIONS radiant.sensors=%s",
            (entry.options.get("devices", {}).get("radiant", {}).get("sensors", {})
            if isinstance(entry.options, Mapping) else None))

    climate_cfg: dict[str, Any] = {**opts, **data}
    log_debug(_LOGGER, "Merged climate config: %s", climate_cfg)

    # --- update_interval (FIX: read from config, not constant) ---
    update_s = as_int(
        OPT_UPDATE_INTERVAL_S,
        default=OPT_UPDATE_MIN_INTERVAL_S,
        min_value=OPT_UPDATE_MIN_INTERVAL_S,
        max_value=300,
    ) or OPT_UPDATE_MIN_INTERVAL_S

    update_interval = timedelta(seconds=max(OPT_UPDATE_MIN_INTERVAL_S, update_s))

    # --- Capabilities ---
    supports_heating, supports_cooling, supports_dehumidifying, supports_ventilation = _infer_capabilities_from_devices(climate_cfg)

    # --- Helpers: required blocks with clearer errors ---
    def _require_mapping(parent: Mapping[str, Any], key: str, ctx: str) -> Mapping[str, Any]:
        val = parent.get(key)
        if not isinstance(val, Mapping):
            raise ConfigEntryNotReady(f"Missing/invalid '{key}' in {ctx}. Check YAML or re-import entry.")
        return val

    # --- Areas ---
    areas_cfg = climate_cfg.get(CONF_AREAS, [])
    if not isinstance(areas_cfg, list):
        raise ConfigEntryNotReady(f"Invalid '{CONF_AREAS}': expected list.")

    areas = []
    for a in areas_cfg:
        if not isinstance(a, Mapping):
            continue

        # ── Risoluzione radiant_surfaces ────────────────────────────────
        # Nuovo formato: radiant_surfaces è una lista di {valve_switch, surface_m2}.
        # Vecchio formato flat: thermal_collector_valve_switch + radiant_surface.
        # Se entrambi presenti, il nuovo formato ha precedenza.
        raw_surfaces = a.get(CONF_RADIANT_SURFACES)
        if raw_surfaces:
            radiant_surfaces = tuple(
                RadiantSurface(
                    valve_switch=s[CONF_VALVE_SWITCH],
                    surface_m2=float(s.get(CONF_SURFACE_M2, 0.0)),
                )
                for s in raw_surfaces
                if isinstance(s, Mapping) and s.get(CONF_VALVE_SWITCH)
            )
        elif a.get(CONF_TCOLLECTOR):
            # Retrocompatibilità: converte formato flat → RadiantSurface singola.
            radiant_surfaces = (
                RadiantSurface(
                    valve_switch=a[CONF_TCOLLECTOR],
                    surface_m2=float(a.get(CONF_RADIANT_SURFACE, 0.0)),
                ),
            )
        else:
            radiant_surfaces = ()

        areas.append(AreaConfig(
            name=a[CONF_AREA],
            indoor=a.get(CONF_INDOOR, True),
            radiant=a.get(CONF_RADIANT, True),
            sensors=SensorPair(**a[CONF_SENSORS]),
            radiant_surfaces=radiant_surfaces,
            ceiling=a.get(CONF_CEILING, None),
        ))

    # --- Devices (required for your runtime logic) ---
    dev_cfg = _require_mapping(climate_cfg, CONF_DEVICES, "climate config")
    su_cfg = _require_mapping(dev_cfg, CONF_SUPPLY_UNITS, f"{CONF_DEVICES}")

    # Supply units
    supply_units = SupplyUnitsConfig(
        direct_supply_unit=su_cfg[CONF_DIRECT_SUPPLY_UNIT],
        adjustable_supply_unit=su_cfg[CONF_ADJUSTABLE_SUPPLY_UNIT],
        three_point_mixing_valve=su_cfg[CONF_THREE_POINT_MIXING_VALVE],
        sensors=SupplyUnitSensors(**su_cfg[CONF_SENSORS]),
    )

    # Radiant (optional)
    radiant = None
    r_cfg = dev_cfg.get(CONF_RADIANT)
    if isinstance(r_cfg, Mapping):
        log_info(_LOGGER, "Radiant config: %s", r_cfg[CONF_SENSORS])
        radiant = RadiantConfig(
            fm_power=r_cfg[CONF_FM_POWER],
            power=r_cfg[CONF_POWER],
            mode=ModeConfig(**r_cfg[CONF_MODE]),
            heating_t_setpoint=SetpointConfig(**r_cfg[CONF_HEATING_T_SETPOINT]),
            heating_dt_setpoint=SetpointConfig(**r_cfg[CONF_HEATING_DT_SETPOINT]),
            cooling_t_setpoint=SetpointConfig(**r_cfg[CONF_COOLING_T_SETPOINT]),
            cooling_dt_setpoint=SetpointConfig(**r_cfg[CONF_COOLING_DT_SETPOINT]),
            sensors=RadiantSensors(**r_cfg[CONF_SENSORS]),
        )

    # VMC (optional)
    vmc = None
    v_cfg = dev_cfg.get(CONF_VMC)
    if isinstance(v_cfg, Mapping):
        vmc = VMCConfig(
            power=v_cfg[CONF_POWER],
            t_setpoint=v_cfg[CONF_T_SETPOINT],
            h_setpoint=v_cfg[CONF_H_SETPOINT],
            t_dew_point_setpoint=v_cfg[CONF_DEW_POINT_SETPOINT],
            delta_t_dew_point_setpoint=v_cfg[CONF_DELTA_DEW_POINT_SETPOINT],
            spare_setpoint=v_cfg[CONF_SPARE_SETPOINT],
            vent_recirculation=v_cfg[CONF_VENT_RECIRCULATION],
            force_heating=v_cfg[CONF_FORCE_HEATING],
            force_cooling=v_cfg[CONF_FORCE_COOLING],
            force_free_cooling=v_cfg[CONF_FORCE_FREE_COOLING],
            season=SeasonConfig(**v_cfg[CONF_SEASON]),
            compressor_management=CompressorManagementConfig(**v_cfg[CONF_COMPRESSOR_MANAGEMENT]),
            cooling_management=CoolingManagementConfig(**v_cfg[CONF_COOLING_MANAGEMENT]),
            requests=VMCRequestsConfig(**v_cfg[CONF_REQUESTS]),
            sensors=VMCSensorsConfig(**v_cfg[CONF_SENSORS]),
            alarms=VMCAlarmsConfig(**v_cfg[CONF_ALARMS]),
        )

    # --- Weather: prefer merged config, fallback to entry.data ---
    w_cfg = climate_cfg.get(CONF_WEATHER) or data.get(CONF_WEATHER)
    if not isinstance(w_cfg, Mapping):
        raise ConfigEntryNotReady(f"Missing/invalid '{CONF_WEATHER}' in config entry.")

    fd = ForecastDataConfig(provider=str(w_cfg[CONF_FORECAST_DATA][CONF_PROVIDER]).strip())
    h = w_cfg[CONF_HISTORICAL_DATA]
    hd = HistoricalDataConfig(
        provider=str(h[CONF_PROVIDER]).strip().lower(),
        token=str(h.get(CONF_TOKEN, "")).strip(),
        latitude=float(h[CONF_LATITUDE]),
        longitude=float(h[CONF_LONGITUDE]),
    )

    # --- InfluxDB historical data: from merged, fallback legacy ---
    ihd_cfg = climate_cfg.get(CONF_HISTORICAL_DATA) or data.get(CONF_HISTORICAL_DATA)
    organization=""
    bucket=""
    token=""
    url=DEFAULT_INFLUXDB_URL
    if isinstance(ihd_cfg, Mapping):
        influx = ihd_cfg.get(CONF_INFLUXDB)
        if isinstance(influx, Mapping):
            organization=influx.get(CONF_ORGANIZATION, "").strip()
            bucket=influx.get(CONF_BUCKET, "").strip()
            token=influx.get(CONF_TOKEN, "").strip()

    # --- windows: from merged, fallback legacy ---
    windows_cfg = climate_cfg.get(CONF_WINDOWS)
    windows = None
    if isinstance(windows_cfg, Mapping):
        state = windows_cfg.get(CONF_CLOSED_STATE)
        if isinstance(state, str) and state.strip():
            windows = WindowsConfig(closed_state=str(state).strip())

    # --- Scenarios (required by your schema) ---
    scenarios_cfg = climate_cfg.get(CONF_SCENARIOS)
    if not isinstance(scenarios_cfg, Mapping):
        raise ConfigEntryNotReady(f"Missing/invalid '{CONF_SCENARIOS}' in config entry.")

    # --- Name/unique_id mapping (RAW YAML vs legacy payload) ---
    climate_name = (
        str(data.get("climate_name") or climate_cfg.get("climate_name") or climate_cfg.get("name") or entry.title).strip()
    )
    climate_unique_id = (
        str(data.get("climate_unique_id") or climate_cfg.get("climate_unique_id") or climate_cfg.get("unique_id") or entry.unique_id or "").strip()
    )

    climate = ClimateConfig(
        name=climate_name,
        unique_id=climate_unique_id,
        units=climate_cfg.get(CONF_UNITS, DEFAULT_UNITS),
        areas=areas,
        devices=DevicesConfig(supply_units=supply_units, radiant=radiant, vmc=vmc),
        windows=windows,
        weather=WeatherConfig(forecast_data=fd, historical_data=hd),
        historical_data=InfluxdbHistoricalDataConfig(bucket=bucket, organization=organization, token=token, url=url),
        scenarios=ScenariosConfig(**scenarios_cfg),
        mean_apt=SensorPair("", ""),
        unit_system=get_unit_system("metric" if climate_cfg.get(CONF_UNITS, DEFAULT_UNITS) == "si" else "imperial"),
    )

    caps = PlantCapabilities(
        supports_heating=supports_heating,
        supports_cooling=supports_cooling,
        supports_dehumidifying=supports_dehumidifying,
        supports_ventilation=supports_ventilation,
        setpoint_step_c=0.5,
    )

    return RuntimeConfig(
        update_interval=update_interval,
        capabilities=caps,
        manual_override_minutes=90,
        climate=climate,
    )

def collect_entity_ids_for_state_changes(runtime: "RuntimeConfig") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def _looks_like_entity_id(s: str) -> bool:
        return "." in s and " " not in s and s[0].islower()

    def _add(e: object) -> None:
        if isinstance(e, str):
            s = e.strip()
            if s and _looks_like_entity_id(s) and s not in seen:
                seen.add(s)
                out.append(s)

    def _walk(obj: Any) -> None:
        """Raccoglie ricorsivamente stringhe che paiono entity_id."""
        if obj is None:
            return
        if isinstance(obj, str):
            _add(obj)
            return
        # SOLO istanze dataclass (non classi): evita l'errore di typing con asdict
        if is_dataclass(obj) and not isinstance(obj, type):
            for f in fields(obj):
                _walk(getattr(obj, f.name))
            return
        if isinstance(obj, Mapping):
            for v in obj.values():
                _walk(v)
            return
        if isinstance(obj, Iterable) and not isinstance(obj, (str, bytes, bytearray, dict)):
            for v in obj:
                _walk(v)
            return
        # altri tipi ignorati

    climate = getattr(runtime, "climate", None)
    if not climate:
        return out

    # --- AREE: sensori T/H + tutte le elettrovalvole delle superfici radianti
    for area in getattr(climate, "areas", []) or []:
        sensors = getattr(area, "sensors", None)
        if sensors:
            _add(getattr(sensors, "temperature", None))
            _add(getattr(sensors, "humidity", None))
        for vs in getattr(area, "valve_switches", lambda: ())():
            _add(vs)

    # --- SUPPLY UNITS: attuatori + TUTTI i sensori
    su = getattr(getattr(climate, "devices", None), "supply_units", None)
    if su:
        _add(getattr(su, "direct_supply_unit", None))
        _add(getattr(su, "adjustable_supply_unit", None))
        _add(getattr(su, "three_point_mixing_valve", None))
        _walk(getattr(su, "sensors", None))

    # --- RADIANT (se presente): top-level + mode/setpoint/sensors
    radiant = getattr(getattr(climate, "devices", None), "radiant", None)
    if radiant:
        _add(getattr(radiant, "fm_power", None))
        _add(getattr(radiant, "power", None))
        _walk(getattr(radiant, "mode", None))
        _walk(getattr(radiant, "heating_t_setpoint", None))
        _walk(getattr(radiant, "heating_dt_setpoint", None))
        _walk(getattr(radiant, "cooling_t_setpoint", None))
        _walk(getattr(radiant, "cooling_dt_setpoint", None))
        _walk(getattr(radiant, "sensors", None))

    # --- VMC (se presente): top-level + season/management/requests/sensors/alarms
    vmc = getattr(getattr(climate, "devices", None), "vmc", None)
    if vmc:
        for name in (
            "power",
            "t_setpoint",
            "h_setpoint",
            "t_dew_point_setpoint",
            "delta_t_dew_point_setpoint",
            "spare_setpoint",
            "vent_recirculation",
            "force_heating",
            "force_cooling",
            "force_free_cooling",
        ):
            _add(getattr(vmc, name, None))
        _walk(getattr(vmc, "season", None))
        _walk(getattr(vmc, "compressor_management", None))
        _walk(getattr(vmc, "cooling_management", None))
        _walk(getattr(vmc, "requests", None))
        _walk(getattr(vmc, "sensors", None))
        _walk(getattr(vmc, "alarms", None))

    # --- APT WINDOWS / WEATHER / SCENARIOS
    _walk(getattr(climate, "windows", None))
    _walk(getattr(climate, "weather", None))
    _walk(getattr(climate, "scenarios", None))

    return out


