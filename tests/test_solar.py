"""Tests for reading solar forecasts linked in the Energy dashboard."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hass_energy.models import SolarPeriod
from custom_components.hass_energy.solar import (
    async_get_solar_forecast as async_get_forecast,
    async_linked_forecast_entries,
    forecast_entries,
    parse_forecast,
)

from .common import (
    ENERGY_PREFERENCES,
    OTHER_FORECAST,
    WH_HOURS,
    WH_HOURS_KW,
    async_add_fake_solar_forecasts,
    async_link_solar_forecasts,
)

pytestmark = pytest.mark.usefixtures("stockholm_afternoon")


def _today_and_tomorrow() -> tuple[datetime, datetime]:
    start = dt_util.start_of_local_day()
    return start, dt_util.start_of_local_day(start.date() + timedelta(days=2))


async def async_get_solar_forecast(
    hass: HomeAssistant, start: datetime, end: datetime
) -> tuple[SolarPeriod, ...]:
    """The forecast of the entries linked in the Energy dashboard."""
    entry_ids = await async_linked_forecast_entries(hass)
    return await async_get_forecast(hass, entry_ids, start, end)


def _local_hour(hour: int) -> datetime:
    return dt_util.start_of_local_day() + timedelta(hours=hour)


def test_forecast_entries_once_each() -> None:
    """Entries linked to several solar sources are read once, in order."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "config_entry_solar_forecast": ["a", "b"]},
            {"type": "grid", "config_entry_solar_forecast": ["c"]},
            {"type": "solar", "config_entry_solar_forecast": None},
            {"type": "solar", "config_entry_solar_forecast": ["b", "d"]},
        ],
        "device_consumption": [],
    }
    assert forecast_entries(prefs) == ["a", "b", "d"]
    assert forecast_entries(ENERGY_PREFERENCES) == []


def test_parse_forecast_period_end() -> None:
    """Keyed by period end, each value joins the hour it ends in (UTC start)."""
    hours = parse_forecast(
        {
            "2026-10-19T07:33:00+02:00": 0,
            "2026-10-19T08:00:00+02:00": 50,
            "2026-10-19T08:41:00+02:00": 25,
        },
        period_end=True,
    )
    assert hours == {
        dt_util.as_utc(_local_hour(7)): 50,
        dt_util.as_utc(_local_hour(8)): 25,
    }


def test_parse_forecast_period_start() -> None:
    """Keyed by period start, each value joins the hour that holds its key."""
    hours = parse_forecast(
        {"2026-10-19T08:00:00+02:00": 50, "2026-10-19T08:30:00+02:00": 25},
        period_end=False,
    )
    assert hours == {dt_util.as_utc(_local_hour(8)): 75}


async def test_hourly_kw_within_range(hass: HomeAssistant) -> None:
    """Wh become hourly kW over the forecast's days, with the night at 0 kW.

    Hours outside the range, and days without values, are left out.
    """
    [fake] = await async_add_fake_solar_forecasts(hass, WH_HOURS)
    await async_link_solar_forecasts(hass, [fake.entry.entry_id])

    periods = await async_get_solar_forecast(hass, *_today_and_tomorrow())

    assert [p.power for p in periods] == WH_HOURS_KW
    assert [p.start for p in periods] == [_local_hour(h) for h in range(24)]
    assert all(p.end - p.start == timedelta(hours=1) for p in periods)


async def test_entries_summed(hass: HomeAssistant) -> None:
    """Several arrays, on one or more solar sources, add up."""
    east, west = await async_add_fake_solar_forecasts(
        hass,
        {"2026-10-19T09:00:00+02:00": 400},
        {"2026-10-19T09:00:00+02:00": 100, "2026-10-19T16:00:00+02:00": 300},
    )
    await async_link_solar_forecasts(
        hass, [east.entry.entry_id], [west.entry.entry_id, east.entry.entry_id]
    )

    periods = await async_get_solar_forecast(hass, *_today_and_tomorrow())

    assert [(p.start, p.power) for p in periods if p.power] == [
        (_local_hour(8), 0.5),
        (_local_hour(15), 0.3),
    ]


async def test_failing_or_missing_entries_left_out(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing forecast is skipped and logged; unknown or unloaded ones too."""
    working, failing, loading = await async_add_fake_solar_forecasts(
        hass, WH_HOURS, WH_HOURS, WH_HOURS
    )
    failing.failing = True
    # Still setting up, as at startup: its platform can't answer yet.
    loading.entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    await async_link_solar_forecasts(
        hass,
        [
            working.entry.entry_id,
            failing.entry.entry_id,
            loading.entry.entry_id,
            "removed",
        ],
    )

    periods = await async_get_solar_forecast(hass, *_today_and_tomorrow())

    assert [p.power for p in periods] == WH_HOURS_KW
    assert f"Could not get the {failing.entry.title} solar forecast" in caplog.text


async def test_no_forecast_linked(hass: HomeAssistant) -> None:
    """Without Energy dashboard prefs or a linked forecast there is none."""
    assert await async_get_solar_forecast(hass, *_today_and_tomorrow()) == ()

    await async_link_solar_forecasts(hass, [])
    assert await async_get_solar_forecast(hass, *_today_and_tomorrow()) == ()


async def test_hours_over_dst_change(hass: HomeAssistant) -> None:
    """The day clocks go back has 25 hours, each its own period."""
    [fake] = await async_add_fake_solar_forecasts(
        hass, {"2026-10-25T12:00:00+01:00": 500}
    )
    await async_link_solar_forecasts(hass, [fake.entry.entry_id])
    start = dt_util.start_of_local_day(date(2026, 10, 25))
    end = dt_util.start_of_local_day(date(2026, 10, 26))

    periods = await async_get_solar_forecast(hass, start, end)

    assert len(periods) == 25
    assert all(p.end - p.start == timedelta(hours=1) for p in periods)
    assert [dt_util.as_local(p.start).hour for p in periods if p.power] == [11]


async def test_other_platforms_keyed_by_start(hass: HomeAssistant) -> None:
    """Platforms other than Forecast.Solar key each hour by its start."""
    [fake] = await async_add_fake_solar_forecasts(
        hass, {"2026-10-19T10:00:00+02:00": 2000}, domain=OTHER_FORECAST
    )
    await async_link_solar_forecasts(hass, [fake.entry.entry_id])

    periods = await async_get_solar_forecast(hass, *_today_and_tomorrow())

    assert [(p.start, p.power) for p in periods if p.power] == [(_local_hour(10), 2.0)]
