"""Tests for reading prices from the Nord Pool integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.hass_energy.nordpool import (
    NORDPOOL_TIME_ZONE,
    async_get_prices,
    delivery_days,
    may_be_published,
    parse_prices,
)

from .common import AREA, HOURLY_MWH_PRICES, add_fake_nordpool, day_response


def test_parse_prices_in_kwh() -> None:
    """Prices per MWh become prices per kWh, in time order."""
    today = dt_util.now().date()
    response = day_response(today)
    response[AREA].reverse()

    periods = parse_prices(response, AREA)

    assert len(periods) == 96
    assert periods[0].start == datetime.combine(
        today, datetime.min.time(), NORDPOOL_TIME_ZONE
    )
    assert periods[0].end - periods[0].start == timedelta(minutes=15)
    assert periods[0].price == HOURLY_MWH_PRICES[0] / 1000
    assert periods[-1].price == HOURLY_MWH_PRICES[23] / 1000


def test_parse_prices_missing_area() -> None:
    """An area the response doesn't have gives no periods."""
    assert parse_prices(day_response(dt_util.now().date()), "SE3") == []
    assert parse_prices({AREA: []}, AREA) == []


async def test_get_prices_calls_nordpool(hass: HomeAssistant) -> None:
    """Prices come from Nord Pool's get_prices_for_date for one area and day."""
    nordpool = add_fake_nordpool(hass, tomorrow=False)
    today = dt_util.now(NORDPOOL_TIME_ZONE).date()
    tomorrow = today + timedelta(days=1)

    assert len(await async_get_prices(hass, nordpool.entry.entry_id, AREA, today)) == 96
    assert await async_get_prices(hass, nordpool.entry.entry_id, AREA, tomorrow) == []
    assert nordpool.calls == [today, tomorrow]


def test_delivery_days() -> None:
    """Local days map to the CET delivery days that cover them."""
    stockholm = dt_util.get_time_zone("Europe/Stockholm")
    helsinki = dt_util.get_time_zone("Europe/Helsinki")
    day = date(2026, 10, 19)

    start = datetime(2026, 10, 19, tzinfo=stockholm)
    assert delivery_days(start, start + timedelta(days=2)) == [
        day,
        day + timedelta(days=1),
    ]
    start = datetime(2026, 10, 19, tzinfo=helsinki)
    assert delivery_days(start, start + timedelta(days=2)) == [
        day - timedelta(days=1),
        day,
        day + timedelta(days=1),
    ]


def test_may_be_published() -> None:
    """Tomorrow can be out from 12:45 CET; later days never are."""
    day = date(2026, 10, 19)
    morning = datetime(2026, 10, 19, 12, 44, tzinfo=NORDPOOL_TIME_ZONE)
    afternoon = morning + timedelta(minutes=1)

    assert may_be_published(day, morning)
    assert not may_be_published(day + timedelta(days=1), morning)
    assert may_be_published(day + timedelta(days=1), afternoon)
    assert not may_be_published(day + timedelta(days=2), afternoon)
