"""Spot prices from the Nord Pool integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import NORDPOOL_DOMAIN
from .models import PricePeriod

SERVICE_GET_PRICES = "get_prices_for_date"

# Delivery days are CET days. Integrations are imported in the executor, so
# loading the zone here doesn't block the event loop.
NORDPOOL_TIME_ZONE = ZoneInfo("Europe/Oslo")
# The next day's prices are published shortly before 13:00 CET.
PUBLISH_TIME = time(12, 45)


def delivery_days(start: datetime, end: datetime) -> list[date]:
    """Return the Nord Pool delivery days that cover [start, end)."""
    first = start.astimezone(NORDPOOL_TIME_ZONE).date()
    last = (end - timedelta(microseconds=1)).astimezone(NORDPOOL_TIME_ZONE).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def may_be_published(day: date, now: datetime) -> bool:
    """Return whether Nord Pool can have published a day's prices by now."""
    cet = now.astimezone(NORDPOOL_TIME_ZONE)
    tomorrow = cet.date() + timedelta(days=1)
    return day < tomorrow or (day == tomorrow and cet.time() >= PUBLISH_TIME)


def parse_prices(response: Mapping[str, Any], area: str) -> list[PricePeriod]:
    """Turn a get_prices_for_date response into periods, in currency/kWh.

    Nord Pool returns prices per MWh. A missing area gives no periods.
    """
    periods = [
        PricePeriod(
            start=dt_util.parse_datetime(row["start"], raise_on_error=True),
            end=dt_util.parse_datetime(row["end"], raise_on_error=True),
            price=row["price"] / 1000,
        )
        for row in response.get(area) or []
    ]
    return sorted(periods, key=lambda period: period.start)


async def async_get_prices(
    hass: HomeAssistant, entry_id: str, area: str, day: date
) -> list[PricePeriod]:
    """Fetch one delivery day's prices; empty until Nord Pool publishes them."""
    response = await hass.services.async_call(
        NORDPOOL_DOMAIN,
        SERVICE_GET_PRICES,
        {"config_entry": entry_id, "date": day.isoformat(), "areas": [area]},
        blocking=True,
        return_response=True,
    )
    return parse_prices(response or {}, area)
