"""Tests for the panel's websocket commands."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.hass_energy.const import CONF_BATTERY_SOC, CONF_PV_POWER
from custom_components.hass_energy.models import MeasuredLine, MeasuredPeriod

from .common import (
    AREA,
    CURRENCY,
    HOURLY_MWH_PRICES,
    PLAN_OPTIONS,
    SENSOR_OPTIONS,
    WH_HOURS,
    WH_HOURS_KW,
    async_add_fake_solar_forecasts,
    async_link_solar_forecasts,
    async_setup_integration,
    set_power,
    set_soc,
)

pytestmark = pytest.mark.usefixtures("stockholm_afternoon")


async def test_subscribe_data(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Prices and solar are sent on subscribe and again after every refresh."""
    [forecast] = await async_add_fake_solar_forecasts(hass, WH_HOURS)
    await async_link_solar_forecasts(hass, [forecast.entry.entry_id])
    entry = await async_setup_integration(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    assert (await client.receive_json())["success"]

    event = (await client.receive_json())["event"]
    prices = event["prices"]
    assert prices["area"] == AREA
    assert prices["currency"] == CURRENCY
    assert len(prices["prices"]) == 2 * 96
    first = prices["prices"][0]
    assert first["price"] == HOURLY_MWH_PRICES[0] / 1000
    assert first["start"] == entry.runtime_data.data.prices.periods[0].start.isoformat()

    assert [period["power"] for period in event["solar"]] == WH_HOURS_KW
    first_solar = entry.runtime_data.data.solar_forecast[0]
    assert event["solar"][0]["start"] == first_solar.start.isoformat()
    assert event["solar"][0]["end"] == first_solar.end.isoformat()
    # No battery is set, so there's no plan.
    assert event["plan"] == []

    await entry.runtime_data.async_refresh()
    assert (await client.receive_json())["event"] == event


async def test_subscribe_data_not_loaded(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Without a loaded entry the subscription is refused."""
    entry = await async_setup_integration(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    response = await client.receive_json()

    assert not response["success"]
    assert response["error"]["code"] == "not_loaded"


async def test_subscribe_data_survives_reload(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """After a reload the subscription gets the new coordinator's prices."""
    entry = await async_setup_integration(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    assert (await client.receive_json())["success"]
    event = (await client.receive_json())["event"]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await client.receive_json())["event"] == event

    await entry.runtime_data.async_refresh()
    assert (await client.receive_json())["event"] == event


async def test_subscribe_data_measured(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Measured lines are sent with their periods and value now; unset ones as null."""
    set_power(hass, SENSOR_OPTIONS[CONF_PV_POWER], 1500)
    set_soc(hass, SENSOR_OPTIONS[CONF_BATTERY_SOC], 40)
    entry = await async_setup_integration(
        hass,
        options={key: SENSOR_OPTIONS[key] for key in (CONF_PV_POWER, CONF_BATTERY_SOC)},
    )
    period = MeasuredPeriod(
        start=dt_util.now().replace(minute=0, second=0, microsecond=0),
        end=dt_util.now().replace(minute=15, second=0, microsecond=0),
        value=1.25,
    )
    coordinator = entry.runtime_data
    coordinator.async_set_updated_data(
        replace(
            coordinator.data,
            measured=replace(
                coordinator.data.measured,
                solar=MeasuredLine((period,), 1.5),
            ),
        )
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    assert (await client.receive_json())["success"]
    measured = (await client.receive_json())["event"]["measured"]
    assert measured == {
        "solar": {
            "periods": [
                {
                    "start": period.start.isoformat(),
                    "end": period.end.isoformat(),
                    "power": 1.25,
                }
            ],
            "now": 1.5,
        },
        "battery": None,
        "grid": None,
        "home": None,
        "soc": {"periods": [], "now": 40.0},
    }


async def test_subscribe_data_live_values(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A sensor change sends only the values now, not the whole data again."""
    pv = SENSOR_OPTIONS[CONF_PV_POWER]
    set_power(hass, pv, 1000)
    await async_setup_integration(hass, options={CONF_PV_POWER: pv})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    assert (await client.receive_json())["success"]
    assert "prices" in (await client.receive_json())["event"]

    set_power(hass, pv, 2500)
    await hass.async_block_till_done()
    assert (await client.receive_json())["event"] == {
        "now": {"solar": 2.5, "battery": None, "grid": None, "home": None, "soc": None}
    }


@pytest.mark.usefixtures("energy_dashboard")
async def test_subscribe_data_plan(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The battery plan is sent with the rest, from now on."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "hass_energy/subscribe_data"})
    assert (await client.receive_json())["success"]
    plan = (await client.receive_json())["event"]["plan"]

    first = entry.runtime_data.data.plan[0]
    assert len(plan) == len(entry.runtime_data.data.plan)
    assert dt_util.parse_datetime(plan[0]["start"]) == dt_util.now()
    assert plan[0] == {
        "start": first.start.isoformat(),
        "end": first.end.isoformat(),
        "command": first.command.value,
        "power": first.power,
        "solar": first.solar,
        "home": first.home,
        "battery": first.battery,
        "grid": first.grid,
        "soc_start": 50.0,
        "soc_end": first.soc_end,
        "cost": first.cost,
        "activity": first.activity.value,
    }
