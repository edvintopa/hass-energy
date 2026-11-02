"""Tests for reading home consumption from recorder statistics."""

from __future__ import annotations

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.hass_energy.const import CONSUMPTION_WINDOW
from custom_components.hass_energy.consumption import (
    async_get_consumption,
    home_consumption_statistics,
    hourly_change,
)

from .common import ENERGY_PREFERENCES, EXPECTED_AVERAGE_KW, import_hourly_statistics


def test_home_consumption_statistics() -> None:
    """Imports and production add, exports and charging subtract, gas is ignored."""
    assert home_consumption_statistics(ENERGY_PREFERENCES) == {
        "sensor.grid_import": 1,
        "sensor.grid_export": -1,
        "sensor.solar": 1,
        "sensor.battery_out": 1,
        "sensor.battery_in": -1,
    }


def test_home_consumption_statistics_skips_missing_meters() -> None:
    """A grid without an export meter only contributes its import."""
    grid = {**ENERGY_PREFERENCES["energy_sources"][0], "stat_energy_to": None}
    prefs = {"energy_sources": [grid], "device_consumption": []}
    assert home_consumption_statistics(prefs) == {"sensor.grid_import": 1}


def test_home_consumption_statistics_tolerates_absent_keys() -> None:
    """A source missing a meter key is read as if that meter is not configured."""
    grid = {"type": "grid", "stat_energy_from": "sensor.grid_import"}
    prefs = {"energy_sources": [grid], "device_consumption": []}
    assert home_consumption_statistics(prefs) == {"sensor.grid_import": 1}


@pytest.mark.usefixtures("energy_dashboard")
async def test_average_consumption(hass: HomeAssistant) -> None:
    """The signed hourly changes are averaged over the window."""
    average, by_hour = await async_get_consumption(hass, CONSUMPTION_WINDOW)
    assert average == pytest.approx(EXPECTED_AVERAGE_KW)
    assert by_hour == pytest.approx([EXPECTED_AVERAGE_KW] * 24)


async def test_average_consumption_with_short_history(hass: HomeAssistant) -> None:
    """With less than 10 days of data, only the hours with data are averaged."""
    # 73 hourly rows in the window. The meter's first row only gives the
    # others a reading to grow from, so it's 72 kWh over 72 hours, not 240.
    import_hourly_statistics(hass, "sensor.grid_import", 1.0, hours=73)
    await async_wait_recording_done(hass)
    grid = {**ENERGY_PREFERENCES["energy_sources"][0], "stat_energy_to": None}
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": [grid], "device_consumption": []})

    average, _ = await async_get_consumption(hass, CONSUMPTION_WINDOW)
    assert average == pytest.approx(1.0)


async def test_average_consumption_without_dashboard(hass: HomeAssistant) -> None:
    """Without an Energy dashboard there is nothing to average."""
    assert await async_get_consumption(hass, CONSUMPTION_WINDOW) == (None, ())


@pytest.mark.usefixtures("stockholm_afternoon")
async def test_consumption_by_local_hour(hass: HomeAssistant) -> None:
    """Each local hour of the day averages its own hours.

    The meter grows 2 kWh in the hour from 18:00 local time and 1 kWh in the
    others, over the last 3 days.
    """
    import_hourly_statistics(
        hass,
        "sensor.grid_import",
        lambda start: 2.0 if dt_util.as_local(start).hour == 18 else 1.0,
        hours=73,
    )
    await async_wait_recording_done(hass)
    grid = {**ENERGY_PREFERENCES["energy_sources"][0], "stat_energy_to": None}
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": [grid], "device_consumption": []})

    _, by_hour = await async_get_consumption(hass, CONSUMPTION_WINDOW)
    assert by_hour[18] == pytest.approx(2.0)
    assert by_hour[3] == pytest.approx(1.0)


def test_hourly_change_leaves_out_unknown_hours() -> None:
    """An hour is left out when a meter's row there doesn't follow an earlier
    one: the first, or the first after a gap, whose change isn't that hour's.
    """
    signs = {"sensor.grid_import": 1, "sensor.solar": 1}
    rows = {
        # Before the window: only tells that the row at 3600 follows it.
        "sensor.grid_import": [
            {"start": 0.0, "change": 5.0},
            {"start": 3600.0, "change": 1.0},
            {"start": 7200.0, "change": 1.0},
            # After a 2-hour gap: all the growth since 7200.
            {"start": 18000.0, "change": 4.0},
            {"start": 21600.0, "change": 1.0},
        ],
        # Starts later: its first row's 0 change says nothing about that hour.
        "sensor.solar": [
            {"start": 7200.0, "change": 0.0},
            {"start": 10800.0, "change": 0.5},
            {"start": 14400.0, "change": None},
            {"start": 21600.0, "change": 0.5},
        ],
    }
    assert hourly_change(signs, rows, start=3600.0) == {
        3600.0: 1.0,
        10800.0: 0.5,
    }
