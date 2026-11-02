"""Tests for the sensors."""

from __future__ import annotations

from homeassistant.const import STATE_UNKNOWN, UnitOfPower
from homeassistant.core import HomeAssistant
import pytest

from .common import AVERAGE_CONSUMPTION, EXPECTED_AVERAGE_KW, async_setup_integration


@pytest.mark.usefixtures("energy_dashboard")
async def test_average_consumption(hass: HomeAssistant) -> None:
    """The sensor shows the 10-day average home consumption in kW."""
    await async_setup_integration(hass)

    state = hass.states.get(AVERAGE_CONSUMPTION)
    assert state is not None
    # Rounded to 3 decimals, so float noise from summing doesn't reach the state.
    assert state.state == str(EXPECTED_AVERAGE_KW)
    assert state.attributes["unit_of_measurement"] == UnitOfPower.KILO_WATT


async def test_average_consumption_without_dashboard(hass: HomeAssistant) -> None:
    """Without an Energy dashboard the sensor is unknown."""
    await async_setup_integration(hass)

    state = hass.states.get(AVERAGE_CONSUMPTION)
    assert state is not None
    assert state.state == STATE_UNKNOWN
