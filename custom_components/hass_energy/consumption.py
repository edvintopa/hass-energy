"""Home consumption from the Energy dashboard's recorder statistics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.energy.data import EnergyPreferences, async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

# Seconds in one hourly statistics row.
HOUR = 3600


def home_consumption_statistics(prefs: EnergyPreferences) -> dict[str, int]:
    """Map each statistic to the sign it adds to home consumption.

    Home consumption is what the Energy dashboard shows as "Home":
    grid import + solar + battery discharge - grid export - battery charge.
    Meters that aren't configured are skipped.
    """
    signs: dict[str, int] = {}
    for source in prefs["energy_sources"]:
        match source["type"]:
            case "grid" | "battery":
                pairs = (
                    (source.get("stat_energy_from"), 1),
                    (source.get("stat_energy_to"), -1),
                )
            case "solar":
                pairs = ((source.get("stat_energy_from"), 1),)
            case _:
                continue
        signs.update((stat_id, sign) for stat_id, sign in pairs if stat_id)
    return signs


async def async_get_consumption(
    hass: HomeAssistant, window: timedelta
) -> tuple[float | None, tuple[float, ...]]:
    """Return average home consumption in kW over the last complete hours.

    Averages over the hours in the window whose use is known, so a shorter
    history isn't diluted. Also returns the average in each local hour of the
    day (24 values), where an hour with no statistics gets the overall average.
    Returns None and no hours when there is nothing to average.
    """
    prefs = (await async_get_manager(hass)).data
    signs = home_consumption_statistics(prefs) if prefs else {}
    if not signs:
        return None, ()

    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    by_hour = await get_instance(hass).async_add_executor_job(
        _hourly_change, hass, signs, end - window, end
    )
    if not by_hour:
        return None, ()
    average = sum(by_hour.values()) / len(by_hour)
    # Each hour's kWh by local hour of day.
    by_hour_of_day: defaultdict[int, list[float]] = defaultdict(list)
    for start, kwh in by_hour.items():
        by_hour_of_day[dt_util.as_local(dt_util.utc_from_timestamp(start)).hour].append(
            kwh
        )
    return average, tuple(
        sum(values) / len(values) if (values := by_hour_of_day.get(hour)) else average
        for hour in range(24)
    )


def hourly_change(
    signs: Mapping[str, int], rows: Mapping[str, list[Mapping[str, Any]]], start: float
) -> dict[float, float]:
    """Return the signed kWh change in each hour from `start`, by its start.

    A row that doesn't follow the one before it by an hour, the first one
    included, doesn't say how much its meter grew in that hour: its change is
    0 for a new meter, or all the growth since the last reading after a gap.
    The hours with such a row are left out, as their total isn't known.
    """
    by_hour: defaultdict[float, float] = defaultdict(float)
    unknown: set[float] = set()
    for stat_id, stat_rows in rows.items():
        previous: float | None = None
        for row in stat_rows:
            follows = previous is not None and row["start"] - previous == HOUR
            previous = row["start"]
            if row["start"] < start:
                continue
            if not follows or (change := row.get("change")) is None:
                unknown.add(row["start"])
                continue
            by_hour[row["start"]] += signs[stat_id] * change
    return {hour: kwh for hour, kwh in by_hour.items() if hour not in unknown}


def _hourly_change(
    hass: HomeAssistant, signs: dict[str, int], start: datetime, end: datetime
) -> dict[float, float]:
    """Return the signed kWh change in each known hour, by its start.

    Runs in the recorder thread; all statistics are fetched in one query, with
    one row more before `start` to tell whether the first row follows a gap.
    """
    rows = statistics_during_period(
        hass,
        start - timedelta(seconds=HOUR),
        end,
        set(signs),
        "hour",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR},
        {"change"},
    )
    return hourly_change(signs, rows, start.timestamp())
