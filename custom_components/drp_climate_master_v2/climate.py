#
from __future__ import annotations

import logging
from typing import Any

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

from custom_components.drp_climate_master_v2.domain.models.plant import PlantSnapshot

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

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        self.map_on_hvac_mode = hvac_mode
        self._attr_hvac_mode = hvac_mode
        self._coordinator.set_hvac_mode( hvac_mode )
    
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

    # @property
    # def preset_mode(self) -> str | None:
    #     return self._supervisor.current_profile.value if self._supervisor.current_profile else None

    # @property
    # def extra_state_attributes(self) -> dict[str, Any]:
    #     snap = self.coordinator.snapshot
    #     return {
    #         "dewpoint_guard": snap.dew_guard_active,
    #         "free_cooling_possible": snap.free_cooling_possible,
    #         "faults": snap.faults,
    #         "vmc_on": snap.vmc_on,
    #         "pdc_on": snap.pdc_on,
    #     }

    # async def async_set_temperature(self, **kwargs: Any) -> None:
    #     temp = kwargs.get("temperature")
    #     if temp is None:
    #         return
    #     self._target_temp = float(temp)
    #     await self._supervisor.async_set_target_temperature(self._target_temp)
    #     self.async_write_ha_state()

    # async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
    #     await self._supervisor.async_set_hvac_mode(hvac_mode)
    #     self.async_write_ha_state()

    # async def async_set_preset_mode(self, preset_mode: str) -> None:
    #     await self._supervisor.async_set_profile(preset_mode)
    #     self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": INTEGRATION_MANUFACTURER,
            "sw_version" : INTEGRATION_VERSION,
            # "config_version": str(getattr(self._entry, "version", "v2")),
        }

    @property
    def _entry_store(self) -> dict[str, Any]:
        domain_store = self._hass.data.setdefault(DOMAIN, {})
        return domain_store.setdefault(self._entry.entry_id, {})
    