"""Solar forecasts linked to the Energy dashboard's solar sources.

Forecast integrations (Forecast.Solar, Solcast, …) give HA an energy platform
with `async_get_solar_forecast`, the same one the Energy dashboard reads:
Wh per period, keyed by a timestamp in it. Forecast.Solar keys each period by
its end; others are taken to key it by its start.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.energy.data import EnergyPreferences, async_get_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.integration_platform import (
    async_process_integration_platforms,
)
from homeassistant.helpers.singleton import singleton
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, SIGNAL_ENERGY_PREFS_UPDATED
from .models import SolarPeriod

HOUR = timedelta(hours=1)
ENERGY_DOMAIN = "energy"
# Platforms whose keys mark the end of each period: on the hour, or at
# sunrise/sunset, where Forecast.Solar adds a key (0 Wh at sunrise).
PERIOD_END_DOMAINS = {"forecast_solar"}

type GetSolarForecast = Callable[[HomeAssistant, str], Awaitable[dict[str, Any] | None]]


@singleton(f"{DOMAIN}_solar_forecast_platforms", async_=True)
async def _async_forecast_platforms(
    hass: HomeAssistant,
) -> dict[str, GetSolarForecast]:
    """Return each integration's async_get_solar_forecast, by domain.

    Kept up to date as integrations load, like the Energy dashboard's own list.
    """
    platforms: dict[str, GetSolarForecast] = {}

    @callback
    def _process(hass: HomeAssistant, domain: str, platform: Any) -> None:
        if hasattr(platform, "async_get_solar_forecast"):
            platforms[domain] = platform.async_get_solar_forecast

    await async_process_integration_platforms(
        hass, ENERGY_DOMAIN, _process, wait_for_platforms=True
    )
    return platforms


async def async_setup_solar(hass: HomeAssistant) -> None:
    """Send SIGNAL_ENERGY_PREFS_UPDATED whenever the Energy dashboard is saved.

    The manager keeps its listeners for good, so this is added once, in
    async_setup, and coordinators connect to the signal.
    """

    async def _prefs_updated() -> None:
        async_dispatcher_send(hass, SIGNAL_ENERGY_PREFS_UPDATED)

    (await async_get_manager(hass)).async_listen_updates(_prefs_updated)


def forecast_entries(prefs: EnergyPreferences) -> list[str]:
    """Return the forecast config entries linked to any solar source, once each."""
    entries: dict[str, None] = {}
    for source in prefs["energy_sources"]:
        if source["type"] == "solar":
            entries.update(
                dict.fromkeys(source.get("config_entry_solar_forecast") or ())
            )
    return list(entries)


async def async_linked_forecast_entries(hass: HomeAssistant) -> list[str]:
    """Return the forecast entries linked in the Energy dashboard."""
    prefs = (await async_get_manager(hass)).data
    return forecast_entries(prefs) if prefs else []


def parse_forecast(
    wh_hours: Mapping[str, float], *, period_end: bool
) -> dict[datetime, float]:
    """Return Wh per hour start (UTC, so adding an hour is safe over DST).

    With `period_end`, each value is the energy of the period ending at its
    timestamp, so it goes to the hour that ends at or after it. Otherwise it
    goes to the hour that contains its timestamp.
    """
    hours: dict[datetime, float] = {}
    for timestamp, wh in wh_hours.items():
        key = dt_util.parse_datetime(timestamp, raise_on_error=True)
        if period_end:
            key -= timedelta(microseconds=1)
        local = dt_util.as_local(key)
        hour = dt_util.as_utc(local.replace(minute=0, second=0, microsecond=0))
        hours[hour] = hours.get(hour, 0.0) + wh
    return hours


async def async_get_solar_forecast(
    hass: HomeAssistant, entry_ids: list[str], start: datetime, end: datetime
) -> tuple[SolarPeriod, ...]:
    """Return the summed forecast of these entries, hourly, within [start, end).

    `start` must be on the hour.

    A forecast that is missing, not loaded or fails is left out, so the rest
    still updates.
    """
    if not entry_ids:
        return ()

    platforms = await _async_forecast_platforms(hass)
    total: dict[datetime, float] = {}
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.state is not ConfigEntryState.LOADED
            or entry.domain not in platforms
        ):
            continue
        try:
            forecast = await platforms[entry.domain](hass, entry_id)
            hours = (
                parse_forecast(
                    forecast["wh_hours"],
                    period_end=entry.domain in PERIOD_END_DOMAINS,
                )
                if forecast
                else {}
            )
        except Exception as err:  # Another integration's code: anything can fail.
            LOGGER.warning(
                "Could not get the %s solar forecast: %s",
                entry.title or entry.domain,
                err,
            )
            continue
        for hour, wh in hours.items():
            total[hour] = total.get(hour, 0.0) + wh

    return _hourly_periods(total, start, end)


def _hourly_periods(
    wh_by_hour: Mapping[datetime, float], start: datetime, end: datetime
) -> tuple[SolarPeriod, ...]:
    """Return a period per hour in [start, end) on the days the forecast covers.

    Forecasts leave out the night, so hours without a value are 0 kW on those
    days. Days with no values at all stay out rather than being claimed dark.
    """
    days = {dt_util.as_local(hour).date() for hour in wh_by_hour}
    periods = []
    hour = dt_util.as_utc(start)
    while hour + HOUR <= end:
        if dt_util.as_local(hour).date() in days:
            # Wh over one hour is the average W over it.
            power = wh_by_hour.get(hour, 0.0) / 1000
            periods.append(SolarPeriod(start=hour, end=hour + HOUR, power=power))
        hour += HOUR
    return tuple(periods)
