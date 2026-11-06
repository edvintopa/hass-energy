"""Tests for how the coordinator fetches prices."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import time
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hass_energy.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_SOC,
    CONF_PV_POWER,
    LIVE_COOLDOWN,
    MEASURED_REFRESH_SECOND,
    PLAN_EXPIRY_SECOND,
    REFRESH_MINUTE,
    SIGNAL_DATA_UPDATED,
)
from custom_components.hass_energy.models import EnergyData, Measured, MeasuredLine
from custom_components.hass_energy.planner import plan_battery

from .common import (
    AREA,
    AVERAGE_CONSUMPTION,
    CURRENCY,
    EXPECTED_AVERAGE_KW,
    PLAN_OPTIONS,
    SENSOR_OPTIONS,
    WH_HOURS,
    WH_HOURS_KW,
    add_fake_nordpool,
    async_add_fake_solar_forecasts,
    async_link_solar_forecasts,
    async_setup_integration,
    set_power,
    set_soc,
)

pytestmark = pytest.mark.usefixtures("stockholm_afternoon")


async def _next_refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Move time to the next hourly refresh and let it run."""
    now = dt_util.utcnow()
    target = now.replace(minute=REFRESH_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(hours=1)
    freezer.move_to(target)
    async_fire_time_changed(hass, target)
    # Time listeners run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_today_and_tomorrow(hass: HomeAssistant) -> None:
    """Prices cover the local today and tomorrow, in currency/kWh."""
    entry = await async_setup_integration(hass)

    prices = entry.runtime_data.data.prices
    assert prices.area == AREA
    assert prices.currency == CURRENCY
    assert len(prices.periods) == 2 * 96
    today = dt_util.now().date()
    assert prices.periods[0].start == dt_util.start_of_local_day(today)
    assert prices.periods[-1].end == dt_util.start_of_local_day(
        today + timedelta(days=2)
    )


async def test_local_days_outside_cet(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """In Helsinki the chart still covers local days, from three CET days."""
    await hass.config.async_set_time_zone("Europe/Helsinki")
    nordpool = add_fake_nordpool(hass)
    entry = await async_setup_integration(hass, nordpool)

    periods = entry.runtime_data.data.prices.periods
    today = dt_util.now().date()
    assert len(periods) == 2 * 96
    assert periods[0].start == dt_util.start_of_local_day(today)
    assert periods[-1].end == dt_util.start_of_local_day(today + timedelta(days=2))
    assert nordpool.calls == [
        today - timedelta(days=1),
        today,
        today + timedelta(days=1),
    ]


async def test_published_days_fetched_once(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """Days are kept once published, and tomorrow isn't asked for too early."""
    freezer = stockholm_afternoon
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=11))
    nordpool = add_fake_nordpool(hass, tomorrow=False)
    entry = await async_setup_integration(hass, nordpool)
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    assert len(entry.runtime_data.data.prices.periods) == 96

    await _next_refresh(hass, freezer)  # 11:15, before publication
    await _next_refresh(hass, freezer)  # 12:15
    assert nordpool.calls == [today]

    await _next_refresh(hass, freezer)  # 13:15, not published yet
    assert nordpool.calls == [today, tomorrow]

    nordpool.published.add(tomorrow)
    await _next_refresh(hass, freezer)
    assert len(entry.runtime_data.data.prices.periods) == 2 * 96

    await _next_refresh(hass, freezer)
    assert nordpool.calls == [today, tomorrow, tomorrow]


async def test_old_days_dropped(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """After midnight yesterday's prices go and the new today is already cached."""
    freezer = stockholm_afternoon
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=23, minutes=30))
    nordpool = add_fake_nordpool(hass)
    entry = await async_setup_integration(hass, nordpool)
    today = dt_util.now().date()

    await _next_refresh(hass, freezer)

    new_today = today + timedelta(days=1)
    periods = entry.runtime_data.data.prices.periods
    # The new tomorrow can't be out before 12:45, so only the new today is left.
    assert len(periods) == 96
    assert periods[0].start == dt_util.start_of_local_day(new_today)
    assert nordpool.calls == [today, new_today]


async def test_failure_keeps_other_data(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """A failed price fetch leaves the day out but the refresh still succeeds."""
    nordpool = add_fake_nordpool(hass)
    nordpool.failing = True
    entry = await async_setup_integration(hass, nordpool)

    assert entry.runtime_data.last_update_success
    assert entry.runtime_data.data.prices.periods == ()
    assert hass.states.get(AVERAGE_CONSUMPTION).state != "unavailable"

    nordpool.failing = False
    await _next_refresh(hass, stockholm_afternoon)
    assert len(entry.runtime_data.data.prices.periods) == 2 * 96


async def test_solar_forecast(hass: HomeAssistant) -> None:
    """Linking a forecast in the Energy dashboard shows it without a refresh."""
    [forecast] = await async_add_fake_solar_forecasts(hass, WH_HOURS)
    entry = await async_setup_integration(hass)
    # Not linked in the Energy dashboard yet, so there's none.
    assert entry.runtime_data.data.solar_forecast == ()
    assert len(entry.runtime_data.data.prices.periods) == 2 * 96

    await async_link_solar_forecasts(hass, [forecast.entry.entry_id])
    await hass.async_block_till_done()
    solar = entry.runtime_data.data.solar_forecast
    assert [period.power for period in solar] == WH_HOURS_KW


async def test_solar_forecast_follows_its_entry(hass: HomeAssistant) -> None:
    """A forecast that loads later, or updates, is read again at once."""
    [forecast] = await async_add_fake_solar_forecasts(hass, WH_HOURS)
    await async_link_solar_forecasts(hass, [forecast.entry.entry_id])
    # Still setting up when HASS Energy starts, as after a restart.
    forecast.entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    entry = await async_setup_integration(hass)
    assert entry.runtime_data.data.solar_forecast == ()

    forecast.entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()
    solar = entry.runtime_data.data.solar_forecast
    assert [period.power for period in solar] == WH_HOURS_KW

    forecast.wh_hours = {"2026-10-19T12:00:00+02:00": 3000}
    forecast.coordinator.async_update_listeners()
    await hass.async_block_till_done()
    solar = entry.runtime_data.data.solar_forecast
    assert [(period.start.hour, period.power) for period in solar if period.power] == [
        (dt_util.as_utc(dt_util.start_of_local_day() + timedelta(hours=11)).hour, 3.0)
    ]

    # After unloading, its updates are no longer followed.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not forecast.coordinator._listeners


async def test_measured_refresh_every_five_minutes(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """After each 5-minute statistics run only the measured data is re-read."""
    nordpool = add_fake_nordpool(hass)
    entry = await async_setup_integration(
        hass, nordpool, options={CONF_PV_POWER: SENSOR_OPTIONS[CONF_PV_POWER]}
    )
    calls = len(nordpool.calls)
    measured = Measured(solar=MeasuredLine((), 9.0))

    with patch(
        "custom_components.hass_energy.coordinator.async_get_measured",
        return_value=measured,
    ) as get_measured:
        target = dt_util.utcnow().replace(minute=5, second=MEASURED_REFRESH_SECOND)
        stockholm_afternoon.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done(wait_background_tasks=True)

    get_measured.assert_called_once()
    assert entry.runtime_data.data.measured == measured
    assert len(nordpool.calls) == calls


async def test_live_values_debounced(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """A change shows at once; a burst after it shows once the cooldown ends."""
    pv = SENSOR_OPTIONS[CONF_PV_POWER]
    set_power(hass, pv, 0)
    entry = await async_setup_integration(hass, options={CONF_PV_POWER: pv})
    coordinator = entry.runtime_data
    assert coordinator.data.measured.solar.now == 0

    set_power(hass, pv, 1000)
    await hass.async_block_till_done()
    assert coordinator.data.measured.solar.now == 1.0

    set_power(hass, pv, 2000)
    set_power(hass, pv, 3000)
    await hass.async_block_till_done()
    assert coordinator.data.measured.solar.now == 1.0

    target = dt_util.utcnow() + LIVE_COOLDOWN
    stockholm_afternoon.move_to(target)
    async_fire_time_changed(hass, target)
    await hass.async_block_till_done()
    assert coordinator.data.measured.solar.now == 3.0


async def test_measured_failure_keeps_the_rest(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """A failed measured read keeps the last measured data; the rest updates."""
    entry = await async_setup_integration(
        hass, options={CONF_PV_POWER: SENSOR_OPTIONS[CONF_PV_POWER]}
    )
    coordinator = entry.runtime_data
    measured = Measured(solar=MeasuredLine((), 9.0))
    coordinator.data = replace(coordinator.data, measured=measured)

    with patch(
        "custom_components.hass_energy.coordinator.async_get_measured",
        side_effect=RuntimeError("database is locked"),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.measured == measured
    assert len(coordinator.data.prices.periods) == 2 * 96


async def test_partial_updates_keep_a_failed_refresh(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """Measured and live updates don't mark a failed refresh as a success."""
    pv = SENSOR_OPTIONS[CONF_PV_POWER]
    entry = await async_setup_integration(hass, options={CONF_PV_POWER: pv})
    coordinator = entry.runtime_data
    coordinator.last_update_success = False

    await coordinator._async_refresh_measured()
    set_power(hass, pv, 1000)
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert coordinator.data.measured.solar.now == 1.0


@pytest.mark.usefixtures("energy_dashboard")
async def test_plan(hass: HomeAssistant) -> None:
    """With a battery, its charge, prices and consumption, there's a plan.

    It runs from now to the end of tomorrow's prices.
    """
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)

    plan = entry.runtime_data.data.plan
    assert plan[0].start == dt_util.now()
    assert plan[0].soc_start == pytest.approx(50)
    assert plan[-1].end == dt_util.start_of_local_day(
        dt_util.now().date() + timedelta(days=2)
    )
    assert len(plan) == 40 + 96
    assert entry.runtime_data.data.consumption_by_hour == pytest.approx(
        [EXPECTED_AVERAGE_KW] * 24
    )


@pytest.mark.parametrize(
    "missing", [CONF_BATTERY_CAPACITY, CONF_BATTERY_MAX_CHARGE, CONF_BATTERY_SOC]
)
@pytest.mark.usefixtures("energy_dashboard")
async def test_no_plan_without_the_battery(hass: HomeAssistant, missing: str) -> None:
    """The battery's size, maximum powers and charge are all needed."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    options = {key: value for key, value in PLAN_OPTIONS.items() if key != missing}
    entry = await async_setup_integration(hass, options=options)
    assert entry.runtime_data.data.plan == ()


async def test_no_plan_without_consumption(hass: HomeAssistant) -> None:
    """Without the home's consumption there's nothing to plan for."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    assert entry.runtime_data.data.plan == ()


@pytest.mark.usefixtures("energy_dashboard")
async def test_plan_again_each_quarter(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """The plan is made again from the charge now at each quarter only."""
    soc = PLAN_OPTIONS[CONF_BATTERY_SOC]
    set_soc(hass, soc, 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    coordinator = entry.runtime_data
    set_soc(hass, soc, 70)

    for minute, expected in ((5, 50), (15, 70)):
        target = dt_util.utcnow().replace(minute=minute, second=MEASURED_REFRESH_SECOND)
        stockholm_afternoon.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.data.plan[0].soc_start == pytest.approx(expected)

    assert coordinator.data.plan[0].start == dt_util.now()


@pytest.mark.usefixtures("energy_dashboard")
@pytest.mark.parametrize("failing", ["slots", "plan_battery"])
async def test_planning_failure_keeps_the_rest(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    """A failing planner leaves the plan out; the rest still updates."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    with patch(
        f"custom_components.hass_energy.coordinator.{failing}",
        side_effect=RuntimeError("bug"),
    ):
        entry = await async_setup_integration(hass, options=PLAN_OPTIONS)

    assert entry.runtime_data.last_update_success
    assert entry.runtime_data.data.plan == ()
    assert len(entry.runtime_data.data.prices.periods) == 2 * 96
    assert "Could not plan the battery" in caplog.text


@pytest.mark.usefixtures("energy_dashboard")
async def test_no_plan_without_prices(hass: HomeAssistant) -> None:
    """With no prices from now on there's nothing to plan over."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    nordpool = add_fake_nordpool(hass)
    nordpool.failing = True
    entry = await async_setup_integration(hass, nordpool, options=PLAN_OPTIONS)
    assert entry.runtime_data.data.plan == ()


@pytest.mark.usefixtures("energy_dashboard")
async def test_plan_once_per_quarter(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """The plan is made once a quarter and sent with the data that changed.

    At :15 the hourly refresh plans, so the measured refresh after it doesn't.
    """
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    await async_setup_integration(hass, options=PLAN_OPTIONS)
    sent: list[EnergyData] = []
    async_dispatcher_connect(hass, SIGNAL_DATA_UPDATED, sent.append)

    with patch(
        "custom_components.hass_energy.coordinator.plan_battery",
        wraps=plan_battery,
    ) as planned:
        # Each step: the time, then how many plans and updates it adds.
        for minute, second, plans, updates in (
            (10, MEASURED_REFRESH_SECOND, 0, None),
            (15, 0, 1, 1),
            (15, MEASURED_REFRESH_SECOND, 0, 1),
            (15, PLAN_EXPIRY_SECOND, 0, 0),
            (30, MEASURED_REFRESH_SECOND, 1, 1),
        ):
            before = (planned.call_count, len(sent))
            target = dt_util.utcnow().replace(minute=minute, second=second)
            stockholm_afternoon.move_to(target)
            async_fire_time_changed(hass, target)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert planned.call_count - before[0] == plans
            if updates is not None:
                assert len(sent) - before[1] == updates

    assert sent[-1].plan[0].start == dt_util.now()


@pytest.mark.usefixtures("energy_dashboard")
async def test_nothing_sent_after_unload(hass: HomeAssistant) -> None:
    """A plan that finishes after the entry unloads isn't sent to the panel.

    Otherwise, after a reload, it would replace the new entry's data with data
    made from the old options.
    """
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    coordinator = entry.runtime_data
    sent: list[EnergyData] = []
    async_dispatcher_connect(hass, SIGNAL_DATA_UPDATED, sent.append)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await coordinator._async_refresh_solar()

    assert sent == []


@pytest.mark.usefixtures("energy_dashboard")
async def test_plan_retried_within_the_quarter(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """Without a plan the next measured refresh tries again."""
    soc = PLAN_OPTIONS[CONF_BATTERY_SOC]
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    assert entry.runtime_data.data.plan == ()

    set_soc(hass, soc, 50)
    target = dt_util.utcnow().replace(minute=5, second=MEASURED_REFRESH_SECOND)
    stockholm_afternoon.move_to(target)
    async_fire_time_changed(hass, target)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.data.plan


@pytest.mark.usefixtures("energy_dashboard")
async def test_late_forecast_waits_for_the_next_quarter(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """A forecast just before a quarter ends keeps the plan, but is kept itself."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    coordinator = entry.runtime_data
    plan = coordinator.data.plan

    stockholm_afternoon.move_to(dt_util.utcnow().replace(minute=14, second=30))
    with patch(
        "custom_components.hass_energy.coordinator.plan_battery", wraps=plan_battery
    ) as planned:
        await coordinator._async_refresh_solar()

    planned.assert_not_called()
    assert coordinator.data.plan == plan


@pytest.mark.usefixtures("energy_dashboard")
async def test_plans_run_one_at_a_time(hass: HomeAssistant) -> None:
    """A plan started later waits, so it can't be overwritten by an earlier one."""
    set_soc(hass, PLAN_OPTIONS[CONF_BATTERY_SOC], 50)
    entry = await async_setup_integration(hass, options=PLAN_OPTIONS)
    coordinator = entry.runtime_data
    running = 0
    most = 0

    def _plan(*args: object) -> tuple:
        nonlocal running, most
        running += 1
        most = max(most, running)
        time.sleep(0.01)
        running -= 1
        return ()

    with patch("custom_components.hass_energy.coordinator.plan_battery", _plan):
        await asyncio.gather(
            coordinator._async_plan(coordinator.data),
            coordinator._async_plan(coordinator.data),
        )

    assert most == 1
