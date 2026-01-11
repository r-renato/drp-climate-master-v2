# custom_components/drp_climate_master_v2/config_flow.py
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

try:
    # Newer HA
    from homeassistant.config_entries import ConfigFlowResult, OptionsFlowResult  # type: ignore
except Exception:  # pragma: no cover
    # Older compatibility
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult  # type: ignore
    from homeassistant.data_entry_flow import FlowResult as OptionsFlowResult  # type: ignore

from . import const as c

_LOGGER = logging.getLogger(__name__)

DOMAIN: str = c.DOMAIN
INTEGRATION_NAME: str = getattr(c, "INTEGRATION_NAME", DOMAIN)

# Conservative list of sensitive keys that should never be displayed in clear in the UI.
_SENSITIVE_KEYS = {
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "bearer",
    "authorization",
}


def _redact(obj: Any) -> Any:
    """Recursively redact sensitive keys."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).strip().lower() in _SENSITIVE_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _dump(obj: Any) -> str:
    """Stable pretty dump for UI display."""
    try:
        return json.dumps(_redact(obj), ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        # Fallback if something is not JSON-serializable
        return str(obj)


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _extract_first_climate_payload(import_config: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a single 'climate config dict' from possible import payload shapes.

    Supported shapes:
      1) hub_cfg (direct) -> {"name": ..., "unique_id": ..., "areas": ..., ...}
      2) wrapped -> {DOMAIN: [hub_cfg, ...]}
      3) ensure_list style -> [{"climate": [hub_cfg, ...]}, ...]
      4) wrapper -> {"climate": [hub_cfg, ...]}

    Note: a single flow can create only one entry; we intentionally pick the first.
    Your YAML sync can (and should) launch one flow per hub.
    """

    payload: Any = import_config

    # If wrapped by domain
    if isinstance(payload, dict) and DOMAIN in payload:
        payload = payload[DOMAIN]

    # Make list of items
    items: list[Any] = payload if isinstance(payload, list) else [payload]

    for item in items:
        if not isinstance(item, dict):
            continue

        # If item has a climate list wrapper
        if "climate" in item and isinstance(item.get("climate"), list):
            climates = [x for x in item.get("climate", []) if isinstance(x, dict)]
            if climates:
                return climates[0]
            continue

        # Otherwise assume this dict is already the climate config
        return item

    return None


class DrpClimateMasterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow.

    Target behavior requested:
      - No editable UI configuration.
      - YAML-only, with UI used only for read-only verification.

    Implementation:
      - async_step_user() always aborts (prevents UI setup).
      - async_step_import() creates ConfigEntry from YAML payload.
      - OptionsFlow provides read-only screens that display current entry config.
    """

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Hard block UI configuration.
        return self.async_abort(reason="yaml_only")

    async def async_step_import(self, import_config: dict[str, Any]) -> ConfigFlowResult:
        """Import from YAML."""

        climate_cfg = _extract_first_climate_payload(import_config)
        if not climate_cfg:
            return self.async_abort(reason="invalid_yaml")

        if not isinstance(climate_cfg, dict):
            return self.async_abort(reason="invalid_yaml")

        # Determine unique_id (preferred) and title.
        unique_id = str(climate_cfg.get("unique_id") or "").strip() or None
        name = str(climate_cfg.get("name") or "").strip()

        if unique_id:
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

        # Extra duplicate protection (legacy) - match by name too.
        if name:
            self._async_abort_entries_match({"name": name})

        title = f"{INTEGRATION_NAME} - {name}" if name else INTEGRATION_NAME

        # IMPORTANT: We keep entry.data == raw YAML climate dict.
        # This aligns with YAML-sync logic that compares/updates entry.data with hub_cfg.
        return self.async_create_entry(title=title, data=climate_cfg)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return DrpClimateMasterOptionsFlowHandler(config_entry)


class DrpClimateMasterOptionsFlowHandler(config_entries.OptionsFlow):
    """Read-only Options Flow for verification.

    Design:
      - Forms have an empty schema (no fields) => user cannot change values.
      - We display the current configuration through description placeholders.

    NOTE:
      - To actually see the JSON blob in the UI, add translations with {config} placeholders
        under translations/<lang>.json for each step.
    """

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    def _config_for_key(self, key: str) -> Any:
        """Return config for a top-level key, searching both entry.data and entry.options."""
        if isinstance(self.entry.data, Mapping) and key in self.entry.data:
            return self.entry.data.get(key)
        if isinstance(self.entry.options, Mapping) and key in self.entry.options:
            return self.entry.options.get(key)
        return None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "summary",
                "areas",
                "devices",
                "weather",
                "historical_data",
                "scenarios",
                "apt_windows",
                "confort_zones",
                "raw",
            ],
        )

    async def async_step_summary(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        summary = {
            "title": self.entry.title,
            "entry_id": self.entry.entry_id,
            "unique_id": self.entry.unique_id,
            "source": self.entry.source,
            "data": dict(self.entry.data) if isinstance(self.entry.data, Mapping) else self.entry.data,
            "options": dict(self.entry.options) if isinstance(self.entry.options, Mapping) else self.entry.options,
        }
        return self.async_show_form(
            step_id="summary",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(summary)},
        )

    async def async_step_areas(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="areas",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("areas") or [])},
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("devices") or {})},
        )

    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="weather",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("weather") or {})},
        )

    async def async_step_historical_data(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="historical_data",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("historical_data") or {})},
        )

    async def async_step_scenarios(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="scenarios",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("scenarios") or {})},
        )

    async def async_step_apt_windows(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="apt_windows",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("apt_windows") or {})},
        )

    async def async_step_confort_zones(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        return self.async_show_form(
            step_id="confort_zones",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(self._config_for_key("confort_zones") or {})},
        )

    async def async_step_raw(self, user_input: dict[str, Any] | None = None) -> OptionsFlowResult:
        """Raw dump (useful for debugging)."""
        raw = {
            "data": dict(self.entry.data) if isinstance(self.entry.data, Mapping) else self.entry.data,
            "options": dict(self.entry.options) if isinstance(self.entry.options, Mapping) else self.entry.options,
        }
        return self.async_show_form(
            step_id="raw",
            data_schema=vol.Schema({}),
            description_placeholders={"config": _dump(raw)},
        )
