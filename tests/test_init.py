"""Tests for setting up, refreshing and unloading the integration."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.hass_energy.const import DOMAIN, REFRESH_MINUTE

from .common import AVERAGE_CONSUMPTION, async_setup_integration


async def test_load_and_unload(hass: HomeAssistant) -> None:
    """The entry loads and unloads cleanly."""
    entry = await async_setup_integration(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_hourly_refresh(hass: HomeAssistant) -> None:
    """Data refreshes every hour at REFRESH_MINUTE."""
    with patch(
        "custom_components.hass_energy.coordinator.async_get_consumption",
        return_value=(1.0, ()),
    ) as consumption:
        await async_setup_integration(hass)
        assert hass.states.get(AVERAGE_CONSUMPTION).state == "1.0"

        consumption.return_value = (2.0, ())
        next_refresh = dt_util.utcnow().replace(
            minute=REFRESH_MINUTE, second=0, microsecond=0
        ) + timedelta(hours=1)
        async_fire_time_changed(hass, next_refresh)
        # Time listeners run as background tasks.
        await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(AVERAGE_CONSUMPTION).state == "2.0"


async def test_entry_without_nordpool(hass: HomeAssistant) -> None:
    """An entry from before prices fails setup with a clear error, not a crash."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    # HA drops the message's trailing period.
    assert entry.reason == (
        "Remove and re-add HASS Energy to pick the Nord Pool prices to use"
    )
