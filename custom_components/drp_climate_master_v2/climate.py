#
from __future__ import annotations

import logging
from typing import Any, Mapping
from enum import Enum

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode, HVACAction, ClimateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import (
    CONF_NAME,
    CONF_UNIQUE_ID,
    UnitOfTemperature,
)

from custom_components.drp_climate_master_v2.plant.monitor.plant import PlantSnapshot

from .controller.coordinator import ClimateCoordinator

from .helpers.logger import log_info

from .helpers.utils import slugify

from .controller.supervisor import ClimateSupervisor
from .const import (
    COORDINATOR,
    DEFAULT_CLIMATE_NAME,
    DOMAIN,
    INTEGRATION_MANUFACTURER,
    INTEGRATION_NAME,
    INTEGRATION_VERSION,
    SUPERVISOR,
)
from .domain.enums import HVACOperatingProfile

_LOGGER = logging.getLogger(__name__)

class DewPointPerception(Enum):
    """Human perception categories for dew point in °C."""

    _description: str
    _icon: str

    DRY = ("dry", "Dry", "mdi:emoticon-cool-outline")
    VERY_COMFORTABLE = (
        "very_comfortable",
        "Very comfortable",
        "mdi:emoticon-happy-outline",
    )
    COMFORTABLE = (
        "comfortable",
        "Comfortable",
        "mdi:emoticon-outline",
    )
    OK_BUT_HUMID = (
        "ok_but_humid",
        "Ok but humid",
        "mdi:emoticon-neutral-outline",
    )
    SOMEWHAT_UNCOMFORTABLE = (
        "somewhat_uncomfortable",
        "Somewhat uncomfortable",
        "mdi:emoticon-sad-outline",
    )
    QUITE_UNCOMFORTABLE = (
        "quite_uncomfortable",
        "Quite uncomfortable",
        "mdi:emoticon-angry-outline",
    )
    EXTREMELY_UNCOMFORTABLE = (
        "extremely_uncomfortable",
        "Extremely uncomfortable",
        "mdi:emoticon-cry-outline",
    )
    SEVERELY_HIGH = (
        "severely_high",
        "Severely high",
        "mdi:emoticon-dead-outline",
    )

    def __new__(
        cls,
        unique_id: str,
        description: str,
        icon: str,
    ) -> DewPointPerception:
        obj = object.__new__(cls)
        obj._value_ = unique_id
        return obj

    def __init__(self, unique_id: str, description: str, icon: str) -> None:
        self._description = description
        self._icon = icon

    @property
    def unique_id(self) -> str:
        return self.value

    @property
    def description(self) -> str:
        return self._description

    @property
    def icon(self) -> str:
        return self._icon
    
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """..."""
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator: ClimateCoordinator = store[COORDINATOR]
    supervisor: ClimateSupervisor = store[SUPERVISOR]
    
    async_add_entities([ClimateMasterEntity(hass, coordinator, supervisor, entry)], update_before_add=False)

    log_info(_LOGGER, "%s: setup entry '%s' completato.", DOMAIN, entry.entry_id)

class ClimateMasterEntity(CoordinatorEntity[ClimateCoordinator], ClimateEntity):
    """..."""
    # _attr_name = "Home Climate Master"
    # _attr_unique_id = "home_climate_master"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
    _attr_supported_features = ClimateEntityFeature.PRESET_MODE
    _attr_preset_modes = HVACOperatingProfile.values()

    _attr_preset_mode: str = HVACOperatingProfile.COMFORT.value
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS

    def __init__(
            self, 
            hass: HomeAssistant, 
            coordinator: ClimateCoordinator, 
            supervisor: ClimateSupervisor, 
            entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)

        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._supervisor = supervisor

        self._attr_name = entry.options.get(CONF_NAME, DEFAULT_CLIMATE_NAME)
        self._attr_unique_id = slugify(entry.options.get( CONF_UNIQUE_ID, f"""{self._attr_name}-uid""" ))

        self._preset_mode = HVACOperatingProfile.COMFORT
        coordinator.set_preset_mode( HVACOperatingProfile.COMFORT )
        self.map_on_hvac_mode = self._attr_hvac_mode = HVACMode.AUTO
        coordinator.set_hvac_mode( HVACMode.AUTO )

        self._target_temp = 22.0
        self._attr_min_temp = 16.0
        self._attr_max_temp = 26.0
        # self._attr_temperature_unit = self.hass.config.units.temperature_unit
        # log_debug(_LOGGER, "xyz %s", entry.data)
        # log_debug(_LOGGER, "xyz %s", entry.options)
        log_info(
            _LOGGER,
            "Initialized (id=%s) entry=%s source=%s",
            hex(id(self)),
            entry.entry_id,
            entry.source,
        )

    # @property
    # def hvac_mode(self) -> HVACMode:
    #     return self._supervisor.current_hvac_mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Imposta la modalità HVAC e aggiorna immediatamente la UI."""
        self.map_on_hvac_mode = hvac_mode
        self._attr_hvac_mode = hvac_mode
        self._coordinator.set_hvac_mode(hvac_mode)
        self.async_write_ha_state()
    
    def set_preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode
        self._attr_preset_mode = preset_mode

        profile = HVACOperatingProfile.from_value(preset_mode)
        if profile is not None:
            self._coordinator.set_preset_mode( profile )

    @property
    def hvac_action(self) -> HVACAction:
        """ OK """
        self._attr_hvac_action: HVACAction = self._supervisor.current_hvac_action
        return self._attr_hvac_action

    @property
    def current_temperature(self) -> float | None:
        """ OK """
        if self.coordinator.plant_snapshot is None:
            return None
        
        snap: PlantSnapshot = self.coordinator.plant_snapshot
        if snap.global_indoor_zone is not None and snap.global_indoor_zone.temperature is not None:
            return snap.global_indoor_zone.temperature.value

    @property
    def target_temperature(self) -> float | None:
        self._attr_target_temperature_high
        return self._target_temp

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, int | float | str | None]:
    # def device_info(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(super().extra_state_attributes or {})

        # data["identifiers"] = {(DOMAIN, self._entry.entry_id)}
        data["integration"] = INTEGRATION_NAME
        data["manufacturer"] = INTEGRATION_MANUFACTURER
        data["sw_version"] = INTEGRATION_VERSION

        plant_snapshot = self._coordinator.plant_snapshot
        if plant_snapshot is not None and plant_snapshot.global_indoor_zone is not None:
            if plant_snapshot.global_indoor_zone.dew_point is not None:
                human_perception = self._human_perception(plant_snapshot.global_indoor_zone.dew_point.value)
                if human_perception is not None:
                    data["Human Perception"] = human_perception.description
                    data["Human Perception Icon"] = human_perception.icon

        return data

    @property
    def _entry_store(self) -> dict[str, Any]:
        domain_store = self._hass.data.setdefault(DOMAIN, {})
        return domain_store.setdefault(self._entry.entry_id, {})
    
    def _human_perception(self, dewpoint: float | None) -> DewPointPerception | None:
        """Return the dew point perception category for a dew point in °C."""

        _DEW_POINT_THRESHOLDS: tuple[tuple[float, DewPointPerception], ...] = (
            (10.0, DewPointPerception.DRY),
            (13.0, DewPointPerception.VERY_COMFORTABLE),
            (16.0, DewPointPerception.COMFORTABLE),
            (18.0, DewPointPerception.OK_BUT_HUMID),
            (21.0, DewPointPerception.SOMEWHAT_UNCOMFORTABLE),
            (24.0, DewPointPerception.QUITE_UNCOMFORTABLE),
            (26.0, DewPointPerception.EXTREMELY_UNCOMFORTABLE),
        )

        if dewpoint is None:
            return None

        for upper_bound, perception in _DEW_POINT_THRESHOLDS:
            if dewpoint < upper_bound:
                return perception

        return DewPointPerception.SEVERELY_HIGH
