"""HASS Energy: plan battery usage from electricity prices and PV forecasts."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import HassEnergyConfigEntry, HassEnergyCoordinator
from .panel import (
    async_register_frontend,
    async_register_panel,
    async_unregister_panel,
)
from .solar import async_setup_solar
from .websocket import async_register_websocket_commands

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration once, before any entry."""
    await async_register_frontend(hass)
    await async_setup_solar(hass)
    async_register_websocket_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HassEnergyConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = HassEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_track_hourly())
    entry.async_on_unload(coordinator.async_track_solar())
    entry.async_on_unload(coordinator.async_track_measured())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HassEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_panel(hass)
    return unloaded
