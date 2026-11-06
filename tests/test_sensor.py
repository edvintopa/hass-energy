"""Tests for the sensors."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hass_energy.const import (
    CONF_BATTERY_SOC,
    MEASURED_REFRESH_SECOND,
    PLAN_EXPIRY_SECOND,
)
from custom_components.hass_energy.models import Activity, Command, PlanPeriod

from .common import (
    AVERAGE_CONSUMPTION,
    EXPECTED_AVERAGE_KW,
    PLAN_OPTIONS,
    async_setup_integration,
    set_soc,
)

BATTERY_COMMAND = "sensor.hass_energy_battery_command"


def _period(command: Command) -> PlanPeriod:
    """A plan period with `command`, for the sensor to show."""
    now = dt_util.now()
    return PlanPeriod(
        start=now,
        end=now,
        command=command,
        power=0.0,
        solar=0.0,
        home=0.0,
        battery=0.0,
        grid=0.0,
        soc_start=50.0,
        soc_end=50.0,
        cost=0.0,
        activity=Activity.IDLE,
    )


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


@pytest.mark.parametrize("command", list(Command))
async def test_battery_command(hass: HomeAssistant, command: Command) -> None:
    """The sensor shows the command of the plan's first period."""
    entry = await async_setup_integration(hass)
    coordinator = entry.runtime_data

    later = Command.HOLD if command is Command.CHARGE else Command.CHARGE
    plan = (_period(command), _period(later))
    coordinator.async_set_updated_data(replace(coordinator.data, plan=plan))
    await hass.async_block_till_done()

    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == command.value
    assert state.attributes["options"] == [command.value for command in Command]


async def test_battery_command_without_a_plan(hass: HomeAssistant) -> None:
    """Without a plan the inverter is told to run on its own."""
    await async_setup_integration(hass)

    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.AUTO.value


async def test_battery_command_back_to_auto(hass: HomeAssistant) -> None:
    """A plan that goes away doesn't leave the last command standing."""
    entry = await async_setup_integration(hass)
    coordinator = entry.runtime_data
    plan = (_period(Command.DISCHARGE),)
    coordinator.async_set_updated_data(replace(coordinator.data, plan=plan))
    await hass.async_block_till_done()

    coordinator.async_set_updated_data(replace(coordinator.data, plan=()))
    await hass.async_block_till_done()

    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.AUTO.value


@pytest.mark.usefixtures("energy_dashboard")
async def test_battery_command_follows_the_replan(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """The plan made at a new quarter reaches the sensor."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    with patch(
        "custom_components.hass_energy.coordinator.plan_battery",
        return_value=(_period(Command.CHARGE),),
    ):
        await async_setup_integration(hass, options=PLAN_OPTIONS)
    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.CHARGE.value

    target = dt_util.utcnow().replace(minute=15, second=MEASURED_REFRESH_SECOND)
    stockholm_afternoon.move_to(target)
    with patch(
        "custom_components.hass_energy.coordinator.plan_battery",
        return_value=(_period(Command.HOLD),),
    ):
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.HOLD.value


async def test_battery_command_after_a_failed_refresh(hass: HomeAssistant) -> None:
    """A failed refresh leaves the command showing, unlike the other sensors."""
    entry = await async_setup_integration(hass)
    coordinator = entry.runtime_data
    plan = (_period(Command.DISCHARGE),)
    coordinator.async_set_updated_data(replace(coordinator.data, plan=plan))

    with patch(
        "custom_components.hass_energy.coordinator.async_get_consumption",
        side_effect=RuntimeError,
    ):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.DISCHARGE.value
    average = hass.states.get(AVERAGE_CONSUMPTION)
    assert average is not None
    assert average.state == STATE_UNAVAILABLE


async def test_battery_command_expires(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """With the replan stuck, a command that ended goes back to auto."""
    entry = await async_setup_integration(hass)
    coordinator = entry.runtime_data
    period = _period(Command.DISCHARGE)
    period = replace(period, end=period.start + timedelta(minutes=15))
    coordinator.async_set_updated_data(replace(coordinator.data, plan=(period,)))

    target = dt_util.utcnow().replace(minute=15, second=PLAN_EXPIRY_SECOND)
    stockholm_afternoon.move_to(target)
    with patch.object(coordinator, "_async_refresh_measured", AsyncMock()):
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(BATTERY_COMMAND)
    assert state is not None
    assert state.state == Command.AUTO.value


async def test_battery_command_id_ignores_the_language(hass: HomeAssistant) -> None:
    """Automations can name the sensor the same way on every install."""
    hass.config.language = "sv"
    await async_setup_integration(hass)

    assert hass.states.get(BATTERY_COMMAND) is not None
