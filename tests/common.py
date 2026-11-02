"""Test helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.energy.data import EnergyPreferences, async_get_manager
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.hass_energy.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_INPUT_ENERGY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_MAX_DISCHARGE,
    CONF_BATTERY_OUTPUT_ENERGY,
    CONF_BATTERY_SOC,
    CONF_GRID_EXPORT_ENERGY,
    CONF_GRID_EXPORT_POWER,
    CONF_GRID_IMPORT_ENERGY,
    CONF_GRID_POWER,
    CONF_LOAD_ENERGY,
    CONF_LOAD_POWER,
    CONF_NORDPOOL_AREA,
    CONF_NORDPOOL_ENTRY,
    CONF_PV_POWER,
    CONF_SOLAR_ENERGY,
    DOMAIN,
    NORDPOOL_DOMAIN,
)
from custom_components.hass_energy.nordpool import NORDPOOL_TIME_ZONE

AVERAGE_CONSUMPTION = "sensor.hass_energy_average_consumption_10_days"

# kWh per hour for each statistic in ENERGY_PREFERENCES.
HOURLY_KWH = {
    "sensor.grid_import": 1.0,
    "sensor.grid_export": 0.3,
    "sensor.solar": 0.5,
    "sensor.battery_out": 0.2,
    "sensor.battery_in": 0.1,
}
# 1.0 import + 0.5 solar + 0.2 discharge - 0.3 export - 0.1 charge
EXPECTED_AVERAGE_KW = 1.3

ENERGY_PREFERENCES: EnergyPreferences = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_import",
            "stat_energy_to": "sensor.grid_export",
            "stat_cost": None,
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0,
        },
        {
            "type": "solar",
            "stat_energy_from": "sensor.solar",
            "config_entry_solar_forecast": None,
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.battery_out",
            "stat_energy_to": "sensor.battery_in",
        },
        {
            "type": "gas",
            "stat_energy_from": "sensor.gas",
            "stat_cost": None,
            "entity_energy_price": None,
            "number_energy_price": None,
        },
    ],
    "device_consumption": [],
}


def import_hourly_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    kwh_per_hour: float | Callable[[datetime], float],
    hours: int,
) -> None:
    """Import an ever-increasing kWh meter covering the last `hours` hours.

    It grows the same every hour, or by `kwh_per_hour(start of the hour)`. The
    first row only gives the others a reading to grow from.
    """
    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    rows = []
    total = 0.0
    for i in range(hours + 1):
        hour = start + timedelta(hours=i)
        if i:
            total += kwh_per_hour(hour) if callable(kwh_per_hour) else kwh_per_hour
        rows.append({"start": hour, "state": total, "sum": total})
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        rows,
    )


AREA = "SE4"
CURRENCY = "SEK"
# Nord Pool's price per MWh in each 15-minute period of a fake day, by hour.
HOURLY_MWH_PRICES = [100.0 + 10 * hour for hour in range(24)]


def day_response(day: date, area: str = AREA) -> dict[str, list[dict[str, Any]]]:
    """A get_prices_for_date response with 96 quarter-hours for CET `day`."""
    midnight = datetime.combine(day, datetime.min.time(), NORDPOOL_TIME_ZONE)
    rows = []
    for quarter in range(96):
        start = midnight + timedelta(minutes=15 * quarter)
        rows.append(
            {
                "start": dt_util.as_utc(start).isoformat(),
                "end": dt_util.as_utc(start + timedelta(minutes=15)).isoformat(),
                "price": HOURLY_MWH_PRICES[quarter // 4],
            }
        )
    return {area: rows}


@dataclass
class FakeNordPool:
    """Stands in for the Nord Pool integration's get_prices_for_date service."""

    entry: MockConfigEntry
    # Delivery days (CET) with published prices; others return an empty list.
    published: set[date] = field(default_factory=set)
    failing: bool = False
    calls: list[date] = field(default_factory=list)

    async def async_get_prices(self, call: ServiceCall) -> ServiceResponse:
        day = date.fromisoformat(str(call.data["date"]))
        self.calls.append(day)
        if self.failing:
            raise ServiceValidationError("connection error")
        area = call.data["areas"][0]
        return day_response(day, area) if day in self.published else {area: []}


def add_fake_nordpool(hass: HomeAssistant, *, tomorrow: bool = True) -> FakeNordPool:
    """Add a Nord Pool entry and a fake price service.

    Yesterday and today (CET) are published, and tomorrow unless told not to.
    """
    entry = MockConfigEntry(
        domain=NORDPOOL_DOMAIN, data={"areas": [AREA], "currency": CURRENCY}
    )
    entry.add_to_hass(hass)
    today = dt_util.now(NORDPOOL_TIME_ZONE).date()
    published = {today - timedelta(days=1), today}
    if tomorrow:
        published.add(today + timedelta(days=1))
    fake = FakeNordPool(entry, published)
    hass.services.async_register(
        NORDPOOL_DOMAIN,
        "get_prices_for_date",
        fake.async_get_prices,
        supports_response=SupportsResponse.ONLY,
    )
    return fake


async def async_setup_integration(
    hass: HomeAssistant,
    nordpool: FakeNordPool | None = None,
    *,
    options: dict[str, Any] | None = None,
    expect_loaded: bool = True,
) -> MockConfigEntry:
    """Add and set up a config entry, with a fake Nord Pool unless one is given."""
    nordpool = nordpool or add_fake_nordpool(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id, CONF_NORDPOOL_AREA: AREA},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is expect_loaded
    await hass.async_block_till_done()
    return entry


# Every measured-data sensor, as set in the entry's options.
SENSOR_OPTIONS = {
    CONF_BATTERY_SOC: "sensor.inverter_battery_soc",
    CONF_BATTERY_CHARGE_POWER: "sensor.inverter_battery_charge_power",
    CONF_BATTERY_DISCHARGE_POWER: "sensor.inverter_battery_discharge_power",
    CONF_PV_POWER: "sensor.inverter_pv_power",
    CONF_LOAD_POWER: "sensor.inverter_load_power",
    CONF_GRID_POWER: "sensor.inverter_grid_power",
    CONF_GRID_EXPORT_POWER: "sensor.inverter_grid_export_power",
    CONF_BATTERY_INPUT_ENERGY: "sensor.inverter_battery_input_energy",
    CONF_BATTERY_OUTPUT_ENERGY: "sensor.inverter_battery_output_energy",
    CONF_SOLAR_ENERGY: "sensor.inverter_solar_energy",
    CONF_GRID_IMPORT_ENERGY: "sensor.inverter_grid_import_energy",
    CONF_GRID_EXPORT_ENERGY: "sensor.inverter_grid_export_energy",
    CONF_LOAD_ENERGY: "sensor.inverter_load_energy",
}


# A battery the plan can be made for, with its charge sensor.
PLAN_OPTIONS = {
    CONF_BATTERY_SOC: SENSOR_OPTIONS[CONF_BATTERY_SOC],
    CONF_BATTERY_CAPACITY: 10.0,
    CONF_BATTERY_MAX_CHARGE: 5.0,
    CONF_BATTERY_MAX_DISCHARGE: 5.0,
}


def set_power(hass: HomeAssistant, entity_id: str, watts: float | str) -> None:
    """Set a power sensor's state in W, as a measurement with statistics."""
    hass.states.async_set(
        entity_id,
        str(watts),
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
    )


def set_energy(hass: HomeAssistant, entity_id: str, kwh: float) -> None:
    """Set a lifetime energy total's state in kWh."""
    hass.states.async_set(
        entity_id,
        str(kwh),
        {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
        },
    )


def set_soc(hass: HomeAssistant, entity_id: str, percent: float) -> None:
    """Set a battery's state of charge in %."""
    hass.states.async_set(
        entity_id,
        str(percent),
        {
            "device_class": "battery",
            "state_class": "measurement",
            "unit_of_measurement": "%",
        },
    )


# Forecast.Solar keys each period by its end; other platforms by its start.
FORECAST_SOLAR = "forecast_solar"
OTHER_FORECAST = "other_forecast"
# Forecast.Solar-style Wh on the frozen test day (2026-10-19, CEST), each for
# the period ending at its key: 0 at sunrise, then hourly, then up to sunset.
# The last one is outside the chart's two days.
WH_HOURS = {
    "2026-10-19T07:33:00+02:00": 0,
    "2026-10-19T08:00:00+02:00": 50,
    "2026-10-19T09:00:00+02:00": 400,
    "2026-10-19T10:00:00+02:00": 1200,
    "2026-10-19T17:41:00+02:00": 150,
    "2026-10-22T10:00:00+02:00": 999,
}
# kW in each local hour of that day: the night is 0, and the next day, with no
# values, is left out.
WH_HOURS_KW = [{7: 0.05, 8: 0.4, 9: 1.2, 17: 0.15}.get(hour, 0.0) for hour in range(24)]


@dataclass
class FakeSolarForecast:
    """A loaded forecast entry whose energy platform returns `wh_hours`.

    Its runtime data is a coordinator, as in most forecast integrations: call
    `async_update_listeners()` on it to announce a new forecast.
    """

    entry: MockConfigEntry
    wh_hours: dict[str, float]
    failing: bool = False

    @property
    def coordinator(self) -> DataUpdateCoordinator:
        return self.entry.runtime_data


async def async_add_fake_solar_forecasts(
    hass: HomeAssistant, *wh_hours: dict[str, float], domain: str = FORECAST_SOLAR
) -> list[FakeSolarForecast]:
    """Add one forecast entry per `wh_hours` behind a fake `domain` platform."""
    fakes = []
    for hours in wh_hours:
        entry = MockConfigEntry(domain=domain, state=ConfigEntryState.LOADED)
        entry.runtime_data = DataUpdateCoordinator(
            hass, logging.getLogger(domain), config_entry=entry, name=domain
        )
        fakes.append(FakeSolarForecast(entry, dict(hours)))
    by_entry_id = {fake.entry.entry_id: fake for fake in fakes}

    async def async_get_solar_forecast(
        hass: HomeAssistant, config_entry_id: str
    ) -> dict[str, dict[str, float]] | None:
        fake = by_entry_id.get(config_entry_id)
        if fake is None:
            return None
        if fake.failing:
            raise ConnectionError("rate limited")
        return {"wh_hours": fake.wh_hours}

    platform = MockPlatform()
    platform.async_get_solar_forecast = async_get_solar_forecast
    mock_integration(hass, MockModule(domain))
    mock_platform(hass, f"{domain}.energy", platform)
    assert await async_setup_component(hass, domain, {})
    for fake in fakes:
        fake.entry.add_to_hass(hass)
    return fakes


async def async_link_solar_forecasts(hass: HomeAssistant, *sources: list[str]) -> None:
    """Set the Energy dashboard to one solar source per list of forecast entries."""
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_energy_from": f"sensor.solar_{i}",
                    "config_entry_solar_forecast": entry_ids,
                }
                for i, entry_ids in enumerate(sources)
            ]
        }
    )
