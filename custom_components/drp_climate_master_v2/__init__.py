# custom_components/drp_climate_master_v2/__init__.py
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import Platform, SERVICE_RELOAD
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from custom_components.drp_climate_master_v2.controller.weather_coordinator import WeatherCoordinator

from .const import COORDINATOR, DOMAIN, ENTITIES_STATE, PLATFORMS, SUPERVISOR, WEATHER_COORDINATOR
from .controller.coordinator import ClimateCoordinator
from .controller.supervisor import ClimateSupervisor
from .helpers.logger import log_debug, log_info, log_warning
from .helpers.utils import as_list

_LOGGER = logging.getLogger(__name__)


def _hub_key_from_cfg(hub_cfg: dict[str, Any]) -> str:
    """Stable key for a hub in YAML."""
    return str(hub_cfg.get("unique_id") or hub_cfg.get("name") or "").strip()


def _hub_key_from_entry(entry: ConfigEntry) -> str:
    """Stable key for an existing ConfigEntry.

    Prefer ConfigEntry.unique_id, then entry.data["unique_id"], then title, then entry_id.
    """
    data_uid = None
    if isinstance(entry.data, dict):
        data_uid = entry.data.get("unique_id")

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
                # tolerate a direct hub dict
                hubs.append(item)
        return hubs

    return hubs


async def _run_import_flow(hass: HomeAssistant, hub_cfg: dict[str, Any], key: str) -> None:
    """Start the SOURCE_IMPORT flow safely and log any abort/errors.

    We try two payload shapes to be compatible with different async_step_import implementations:
      1) data={DOMAIN: [hub_cfg]}
      2) data=hub_cfg

    This prevents the classic "import expects wrapped DOMAIN key" mismatch.
    """
    try:
        # Attempt 1: wrapped payload (common pattern)
        res = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={DOMAIN: [hub_cfg]},
        )

        rtype = getattr(res, "get", lambda _k, _d=None: None)("type")
        reason = getattr(res, "get", lambda _k, _d=None: None)("reason")

        log_debug(_LOGGER, "%s: import flow result (wrapped) for '%s': type=%s reason=%s", DOMAIN, key, rtype, reason)

        # Fallback only for the most common mismatch
        if rtype == "abort" and reason in {"invalid_yaml", "invalid_config", "invalid_input"}:
            res2 = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=hub_cfg,
            )
            rtype2 = getattr(res2, "get", lambda _k, _d=None: None)("type")
            reason2 = getattr(res2, "get", lambda _k, _d=None: None)("reason")
            log_debug(
                _LOGGER,
                "%s: import flow result (direct) for '%s': type=%s reason=%s",
                DOMAIN,
                key,
                rtype2,
                reason2,
            )

    except Exception as err:  # noqa: BLE001
        # If config_flow is missing/misconfigured, this will surface here.
        log_warning(_LOGGER, "%s: import flow crashed for hub '%s': %s", DOMAIN, key, err, exc_info=True)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up from YAML (sync-to-ConfigEntries).

    Behavior:
      - Read DOMAIN YAML (supports dict or list)
      - One ConfigEntry per hub
      - Update existing entries when YAML changes
      - Remove entries no longer present
      - Register DOMAIN.reload service to re-read YAML and re-sync
    """

    store: dict[str, Any] = hass.data.setdefault(DOMAIN, {})

    domain_cfg = config.get(DOMAIN)
    if domain_cfg is None:
        return True

    hubs = _iter_yaml_hubs(domain_cfg)
    if not hubs:
        log_warning(_LOGGER, "Domain '%s' has no '%s' hubs configured in YAML.", DOMAIN, Platform.CLIMATE)
        return True

    # Avoid repeating the initial sync more than once per HA start.
    if store.get("_yaml_sync_started"):
        log_debug(_LOGGER, "%s: YAML sync already started; skip", DOMAIN)
        return True
    store["_yaml_sync_started"] = True

    existing_entries = hass.config_entries.async_entries(DOMAIN)
    by_key = {_hub_key_from_entry(e): e for e in existing_entries}

    yaml_keys: set[str] = set()

    # Create / update
    for hub_cfg in hubs:
        key = _hub_key_from_cfg(hub_cfg)
        if not key:
            log_warning(_LOGGER, "%s: skipping hub without unique_id/name: %s", DOMAIN, hub_cfg)
            continue

        yaml_keys.add(key)

        try:
            if key in by_key:
                entry = by_key[key]

                # Update entry.data if changed (best effort). If your entry.data is NOT raw YAML,
                # consider removing this compare/update and handling updates inside your coordinator.
                if isinstance(entry.data, dict) and dict(entry.data) != hub_cfg:
                    log_info(_LOGGER, "%s: updating existing entry '%s' from YAML.", DOMAIN, key)
                    hass.config_entries.async_update_entry(entry, data=hub_cfg)
                    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
                else:
                    log_debug(_LOGGER, "%s: entry '%s' already matches YAML; no update.", DOMAIN, key)
            else:
                log_info(_LOGGER, "%s: creating new entry from YAML for hub '%s'.", DOMAIN, key)
                hass.async_create_task(_run_import_flow(hass, hub_cfg, key))

        except Exception as err:  # noqa: BLE001
            log_warning(_LOGGER, "%s: YAML sync error for hub '%s': %s", DOMAIN, key, err, exc_info=True)

    # Remove entries no longer in YAML
    for entry in existing_entries:
        key = _hub_key_from_entry(entry)
        if key and key not in yaml_keys:
            log_info(_LOGGER, "%s: removing entry '%s' not present in YAML anymore.", DOMAIN, key)
            try:
                await hass.config_entries.async_remove(entry.entry_id)
            except Exception as err:  # noqa: BLE001
                log_warning(_LOGGER, "%s: failed removing entry '%s': %s", DOMAIN, key, err, exc_info=True)

    # Register reload service once
    if not store.get("_reload_service_registered"):
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_RELOAD,  # DOMAIN.reload
            lambda call: _reload_config(hass, call),
        )
        store["_reload_service_registered"] = True

    log_info(_LOGGER, "%s: YAML sync completed (%d hubs).", DOMAIN, len(yaml_keys))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a ConfigEntry."""

    # Lazy import to avoid circular deps (kept even if already imported above)
    from homeassistant import config_entries

    # Optional: UI placeholder entry
    if entry.source == config_entries.SOURCE_USER and isinstance(entry.data, dict) and entry.data.get("yaml_only") is True:
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

    hub_key = str(entry.unique_id or (entry.data.get("unique_id") if isinstance(entry.data, dict) else None) or entry.title or entry.entry_id)

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
        await supervisor.async_start()

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

        raise ConfigEntryNotReady(str(err)) from err


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    try:
        _LOGGER.debug("%s: options updated for entry_id=%s -> reload requested", DOMAIN, entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("%s: failed to reload entry %s after options update: %s", DOMAIN, entry.entry_id, err)


async def _reload_config(hass: HomeAssistant, call: Event | ServiceCall) -> None:
    """Admin service handler: reload YAML and re-sync entries."""

    log_info(_LOGGER, "%s: reload requested; re-reading YAML and syncing entries…", DOMAIN)

    reloaded: dict[str, Any] | None = await async_integration_yaml_config(hass, DOMAIN)
    if not reloaded or DOMAIN not in reloaded:
        log_warning(_LOGGER, "%s: domain not present in YAML after reload. Nothing to sync.", DOMAIN)
        return

    domain_cfg = reloaded.get(DOMAIN)
    hubs = _iter_yaml_hubs(domain_cfg)
    if not hubs:
        log_warning(_LOGGER, "%s: no '%s' hubs found after YAML reload.", DOMAIN, Platform.CLIMATE)
        return

    existing_entries = hass.config_entries.async_entries(DOMAIN)
    by_key = {_hub_key_from_entry(e): e for e in existing_entries}

    yaml_keys: set[str] = set()

    for hub_cfg in hubs:
        key = _hub_key_from_cfg(hub_cfg)
        if not key:
            log_warning(_LOGGER, "%s: skipping hub without unique_id/name after reload: %s", DOMAIN, hub_cfg)
            continue

        yaml_keys.add(key)

        if key in by_key:
            entry = by_key[key]
            if isinstance(entry.data, dict) and dict(entry.data) != hub_cfg:
                log_info(_LOGGER, "%s: updating entry '%s' from reloaded YAML and reloading.", DOMAIN, key)
                hass.config_entries.async_update_entry(entry, data=hub_cfg)
                await hass.config_entries.async_reload(entry.entry_id)
        else:
            log_info(_LOGGER, "%s: creating new entry from reloaded YAML for hub '%s'.", DOMAIN, key)
            await _run_import_flow(hass, hub_cfg, key)

    for entry in existing_entries:
        key = _hub_key_from_entry(entry)
        if key and key not in yaml_keys:
            log_info(_LOGGER, "%s: removing entry '%s' not present after YAML reload.", DOMAIN, key)
            await hass.config_entries.async_remove(entry.entry_id)

    log_info(_LOGGER, "%s: reload+sync completed (%d hubs).", DOMAIN, len(yaml_keys))
