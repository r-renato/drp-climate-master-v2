# custom_components/drp_climate_master_v2/__init__.py
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import Platform, SERVICE_RELOAD
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import STARTUP_MESSAGE, COORDINATOR, DOMAIN, ENTITIES_STATE, PLATFORMS, SUPERVISOR, WEATHER_COORDINATOR
from .controller.coordinator import ClimateCoordinator
from .controller.supervisor import ClimateSupervisor
from .controller.weather_coordinator import WeatherCoordinator
from .helpers.logger import log_debug, log_info, log_warning
from .helpers.utils import as_list

_LOGGER = logging.getLogger(__name__)


# ------------------------------- helpers -------------------------------------

def _hub_key_from_cfg(hub_cfg: dict[str, Any]) -> str:
    """Stable key for a hub in YAML."""
    return str(hub_cfg.get("unique_id") or hub_cfg.get("name") or "").strip()


def _hub_key_from_entry(entry: ConfigEntry) -> str:
    """Stable key for an existing ConfigEntry.

    Prefer ConfigEntry.unique_id, then entry.data["unique_id"], then title, then entry_id.

    NOTE: in Home Assistant, entry.data is typically a MappingProxyType (Mapping),
    so NEVER use isinstance(entry.data, dict).
    """
    data_uid: str | None = None
    if isinstance(entry.data, Mapping):
        data_uid = entry.data.get("unique_id")  # type: ignore[assignment]

    return str(entry.unique_id or data_uid or entry.title or entry.entry_id).strip()


def _iter_yaml_hubs(domain_cfg: Any) -> list[dict[str, Any]]:
    """Return a flat list of hub dicts from YAML.

    Supports:
      - dict: {"climate": [hub, ...]}
      - list: [{"climate": [hub, ...]}, ...]   (common with cv.ensure_list)
      - list: [hub, hub, ...]                   (rare but tolerated)
    """
    hubs: list[dict[str, Any]] = []

    if isinstance(domain_cfg, dict):
        hubs_raw = as_list(domain_cfg.get(Platform.CLIMATE))
        hubs.extend([h for h in hubs_raw if isinstance(h, dict)])
        return hubs

    if isinstance(domain_cfg, list):
        for item in domain_cfg:
            if isinstance(item, dict) and Platform.CLIMATE in item:
                hubs_raw = as_list(item.get(Platform.CLIMATE))
                hubs.extend([h for h in hubs_raw if isinstance(h, dict)])
            elif isinstance(item, dict):
                hubs.append(item)
        return hubs

    return hubs


def _normalize(obj: Any) -> Any:
    """Normalize YAML/entry structures to comparable plain-Python types.

    - MappingProxyType / OrderedDict / any Mapping -> dict
    - list/tuple -> list (recursive)
    """
    if isinstance(obj, Mapping):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def _pick_best_entry(entries: list[ConfigEntry]) -> ConfigEntry:
    """If multiple entries share the same hub key, pick the most likely 'real' one."""

    def score(e: ConfigEntry) -> tuple[int, int, int, int]:
        data = e.data if isinstance(e.data, Mapping) else {}
        yaml_only = 1 if bool(data.get("yaml_only") is True) else 0
        has_devices = 1 if "devices" in data else 0
        is_import = 1 if e.source == SOURCE_IMPORT else 0
        has_uid = 1 if bool(e.unique_id) else 0
        # prefer: has_uid, import, has_devices, NOT yaml_only
        return (has_uid, is_import, has_devices, 1 - yaml_only)

    return sorted(entries, key=score, reverse=True)[0]


async def _run_import_flow(hass: HomeAssistant, hub_cfg: dict[str, Any], key: str) -> None:
    """Start the SOURCE_IMPORT flow safely and log any abort/errors.

    We try two payload shapes to be compatible with different async_step_import implementations:
      1) data={DOMAIN: [hub_cfg]}
      2) data=hub_cfg

    This prevents the classic "import expects wrapped DOMAIN key" mismatch.
    """
    try:
        res = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={DOMAIN: [hub_cfg]},
        )

        rtype = res.get("type")
        reason = res.get("reason")
        log_debug(_LOGGER, "%s: import flow result (wrapped) for '%s': type=%s reason=%s", DOMAIN, key, rtype, reason)

        if rtype == "abort" and reason in {"invalid_yaml", "invalid_config", "invalid_input"}:
            res2 = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=hub_cfg,
            )
            log_debug(
                _LOGGER,
                "%s: import flow result (direct) for '%s': type=%s reason=%s",
                DOMAIN,
                key,
                res2.get("type"),
                res2.get("reason"),
            )

    except Exception as err:  # noqa: BLE001
        log_warning(_LOGGER, "%s: import flow crashed for hub '%s': %s", DOMAIN, key, err, exc_info=True)


# ------------------------------- setup ---------------------------------------

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up from YAML (sync-to-ConfigEntries)."""

    log_info(_LOGGER, f"{STARTUP_MESSAGE}")
    
    domain_cfg = config.get(DOMAIN)
    if domain_cfg is None:
        return True

    log_info(_LOGGER, "%s: YAML real domain config: %s", DOMAIN, domain_cfg)

    await _sync_yaml_to_entries(hass, domain_cfg, force=False)

    # Register reload service once (DOMAIN.reload)
    domain_store: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    if not domain_store.get("_reload_service_registered"):
        async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _handle_reload_service)
        domain_store["_reload_service_registered"] = True

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a ConfigEntry."""

    # Lazy import to avoid circular deps
    from homeassistant import config_entries

    # Optional: UI placeholder entry
    if (
        entry.source == config_entries.SOURCE_USER
        and isinstance(entry.data, Mapping)
        and entry.data.get("yaml_only") is True
    ):
        log_info(
            _LOGGER,
            "%s: UI placeholder entry '%s' (configurazione gestita da YAML). Nessun setup runtime.",
            DOMAIN,
            entry.title,
        )
        return True

    domain_store: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    entry_store: dict[str, Any] = domain_store.setdefault(entry.entry_id, {})

    if entry_store.get("ready") and entry_store.get(COORDINATOR) and entry_store.get(SUPERVISOR):
        log_debug(_LOGGER, "%s: entry %s already ready; skipping setup.", DOMAIN, entry.entry_id)
        return True

    entry_store.setdefault(ENTITIES_STATE, {})

    coordinator: ClimateCoordinator | None = None
    supervisor: ClimateSupervisor | None = None
    weather_coordinator: WeatherCoordinator | None = None

    hub_key = str(
        entry.unique_id
        or (entry.data.get("unique_id") if isinstance(entry.data, Mapping) else None)
        or entry.title
        or entry.entry_id
    )

    try:
        log_info(_LOGGER, "%s: setup entry %s (hub=%s, source=%s)", DOMAIN, entry.entry_id, hub_key, entry.source)

        coordinator = ClimateCoordinator(hass=hass, entry=entry)
        supervisor = ClimateSupervisor(hass=hass, coordinator=coordinator)
        weather_coordinator = WeatherCoordinator(hass=hass, coordinator=coordinator)

        # Gate platforms behind first refresh
        await coordinator.async_config_entry_first_refresh()

        entry_store[COORDINATOR] = coordinator
        entry_store[SUPERVISOR] = supervisor
        entry_store[WEATHER_COORDINATOR] = weather_coordinator

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # await supervisor.async_start()

        entry.async_on_unload(entry.add_update_listener(_options_updated))

        entry_store["ready"] = True
        log_info(_LOGGER, "%s: setup entry %s completed (hub=%s)", DOMAIN, entry.entry_id, hub_key)
        return True

    except ConfigEntryNotReady:
        raise

    except Exception as err:  # noqa: BLE001
        log_warning(
            _LOGGER,
            "%s: setup entry %s failed (hub=%s): %s",
            DOMAIN,
            entry.entry_id,
            hub_key,
            err,
            exc_info=True,
        )

        # best-effort cleanup
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: failed unloading platforms after setup failure", DOMAIN)

        if weather_coordinator is not None and hasattr(weather_coordinator, "async_stop"):
            try:
                await weather_coordinator.async_stop()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: failed stopping weather_coordinator after setup failure", DOMAIN)

        if supervisor is not None and hasattr(supervisor, "async_stop"):
            try:
                await supervisor.async_stop()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: failed stopping supervisor after setup failure", DOMAIN)

        if coordinator is not None:
            for method in ("async_stop", "async_close"):
                if hasattr(coordinator, method):
                    try:
                        await getattr(coordinator, method)()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("%s: failed stopping coordinator after setup failure", DOMAIN)
                    break

        entry_store.pop("ready", None)
        entry_store.pop(COORDINATOR, None)
        entry_store.pop(SUPERVISOR, None)
        entry_store.pop(WEATHER_COORDINATOR, None)

        raise ConfigEntryNotReady(str(err)) from err


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    try:
        _LOGGER.debug("%s: options updated for entry_id=%s -> reload requested", DOMAIN, entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("%s: failed to reload entry %s after options update: %s", DOMAIN, entry.entry_id, err)


# ---------------------------- reload + yaml sync -----------------------------

async def _handle_reload_service(call: ServiceCall) -> None:
    """DOMAIN.reload: re-read YAML (integration config) and re-sync entries."""

    log_info(_LOGGER, "%s: reload requested; re-reading YAML and syncing entries…", DOMAIN)

    reloaded: dict[str, Any] | None = await async_integration_yaml_config(call.hass, DOMAIN)
    if not reloaded or DOMAIN not in reloaded:
        log_warning(_LOGGER, "%s: domain not present in YAML after reload. Nothing to sync.", DOMAIN)
        return

    await _sync_yaml_to_entries(call.hass, reloaded.get(DOMAIN), force=True)


async def _sync_yaml_to_entries(hass: HomeAssistant, domain_cfg: Any, *, force: bool = False) -> None:
    """Sync YAML hubs to ConfigEntries.

    Insertions/removals/updates are logged explicitly.

    IMPORTANT: ConfigEntry.data is a MappingProxyType (Mapping), so we normalize and compare
    against YAML using plain dicts (deep-normalized).
    """

    store: dict[str, Any] = hass.data.setdefault(DOMAIN, {})

    if not force and store.get("_yaml_sync_started"):
        log_debug(_LOGGER, "%s: YAML sync already started; skip", DOMAIN)
        return
    store["_yaml_sync_started"] = True

    hubs = _iter_yaml_hubs(domain_cfg)
    if not hubs:
        log_warning(_LOGGER, "Domain '%s' has no '%s' hubs configured in YAML.", DOMAIN, Platform.CLIMATE)
        return

    existing_entries = hass.config_entries.async_entries(DOMAIN)

    # Handle duplicates by hub key
    grouped: dict[str, list[ConfigEntry]] = {}
    for e in existing_entries:
        k = _hub_key_from_entry(e)
        grouped.setdefault(k, []).append(e)

    by_key: dict[str, ConfigEntry] = {}
    for k, lst in grouped.items():
        if len(lst) > 1:
            log_warning(_LOGGER, "%s: multiple entries share hub key '%s' -> picking best (%s)", DOMAIN, k, [e.entry_id for e in lst])
        by_key[k] = _pick_best_entry(lst)

    yaml_keys: set[str] = set()
    reload_tasks: list[asyncio.Task] = []
    create_tasks: list[asyncio.Task] = []

    for hub_cfg in hubs:
        key = _hub_key_from_cfg(hub_cfg)
        if not key:
            log_warning(_LOGGER, "%s: skipping hub without unique_id/name: %s", DOMAIN, hub_cfg)
            continue

        yaml_keys.add(key)

        # DEBUG hooks (safe)
        yaml_rs = (
            hub_cfg.get("devices", {})
            .get("radiant", {})
            .get("sensors", None)
            if isinstance(hub_cfg, dict)
            else None
        )

        if key in by_key:
            entry = by_key[key]

            entry_data_norm = _normalize(entry.data) if isinstance(entry.data, Mapping) else {}
            hub_cfg_norm = _normalize(hub_cfg)

            entry_rs = (
                entry_data_norm.get("devices", {})
                .get("radiant", {})
                .get("sensors", None)
            )

            log_debug(_LOGGER, "SYNC[%s] YAML radiant.sensors=%s", key, yaml_rs)
            log_debug(_LOGGER, "SYNC[%s] ENTRY radiant.sensors=%s", key, entry_rs)

            must_update = force or (entry_data_norm != hub_cfg_norm)
            if must_update:
                log_info(_LOGGER, "%s: updating existing entry '%s' from YAML (force=%s).", DOMAIN, key, force)
                hass.config_entries.async_update_entry(entry, data=hub_cfg_norm)
                reload_tasks.append(hass.async_create_task(hass.config_entries.async_reload(entry.entry_id)))
            else:
                log_debug(_LOGGER, "%s: entry '%s' already matches YAML; no update.", DOMAIN, key)
        else:
            log_info(_LOGGER, "%s: creating new entry from YAML for hub '%s'.", DOMAIN, key)
            create_tasks.append(hass.async_create_task(_run_import_flow(hass, hub_cfg, key)))

    # Remove entries no longer in YAML
    for entry in existing_entries:
        key = _hub_key_from_entry(entry)
        if key and key not in yaml_keys:
            log_info(_LOGGER, "%s: removing entry '%s' not present in YAML anymore (entry_id=%s).", DOMAIN, key, entry.entry_id)
            try:
                await hass.config_entries.async_remove(entry.entry_id)
            except Exception as err:  # noqa: BLE001
                log_warning(_LOGGER, "%s: failed removing entry '%s': %s", DOMAIN, key, err, exc_info=True)

    if create_tasks:
        await asyncio.gather(*create_tasks, return_exceptions=True)

    if reload_tasks:
        await asyncio.gather(*reload_tasks, return_exceptions=True)

    log_info(_LOGGER, "%s: YAML sync completed (%d hubs, reloaded=%d).", DOMAIN, len(yaml_keys), len(reload_tasks))



async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a ConfigEntry and fully cleanup runtime objects.

    This is critical to ensure YAML reloads truly rebuild runtime state.
    """
    domain_store: dict[str, Any] = hass.data.get(DOMAIN, {})
    entry_store: dict[str, Any] = domain_store.get(entry.entry_id, {})

    supervisor: Any = entry_store.get(SUPERVISOR)
    coordinator: Any = entry_store.get(COORDINATOR)
    weather_coordinator: Any = entry_store.get(WEATHER_COORDINATOR)

    # Stop runtime components first (best-effort)
    if supervisor is not None and hasattr(supervisor, "async_stop"):
        try:
            await supervisor.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: failed stopping supervisor on unload", DOMAIN)

    if weather_coordinator is not None and hasattr(weather_coordinator, "async_stop"):
        try:
            await weather_coordinator.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: failed stopping weather coordinator on unload", DOMAIN)

    if coordinator is not None:
        for method in ("async_stop", "async_close"):
            if hasattr(coordinator, method):
                try:
                    await getattr(coordinator, method)()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("%s: failed stopping coordinator on unload", DOMAIN)
                break

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # IMPORTANT: Clear entry store so next setup_entry cannot short-circuit.
    try:
        domain_store.pop(entry.entry_id, None)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("%s: failed clearing entry store on unload", DOMAIN)

    log_debug(_LOGGER, "%s: unload entry %s -> %s", DOMAIN, entry.entry_id, unload_ok)
    return unload_ok

