"""Tests for reading measured data from the sensors set in the options."""

from __future__ import annotations

from datetime import datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.hass_energy.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_SOC,
    CONF_GRID_EXPORT_ENERGY,
    CONF_GRID_IMPORT_ENERGY,
    CONF_GRID_POWER,
    CONF_PV_POWER,
    CONF_SOLAR_ENERGY,
)
from custom_components.hass_energy.measured import (
    async_get_measured,
    combine,
    live_entities,
    quarters_from_changes,
    quarters_from_means,
    with_live_values,
)
from custom_components.hass_energy.models import Measured, MeasuredLine

from .common import SENSOR_OPTIONS, set_energy, set_power, set_soc

# A quarter that starts on the hour, as a timestamp.
Q = 1_800_000_000.0


def _rows(key: str, *values: float | None, start: float = Q) -> list[dict]:
    """5-minute statistics rows from `start`, one per value."""
    return [{"start": start + 300 * i, key: value} for i, value in enumerate(values)]


def test_quarters_from_changes() -> None:
    """kWh per quarter becomes kW, a partial quarter over the time it has.

    The first row only tells whether the next follows a gap.
    """
    rows = _rows("change", 9.9, 0.1, 0.1, 0.1, 0.05, None, start=Q - 300)
    assert quarters_from_changes(rows) == pytest.approx({Q: 1.2, Q + 900: 0.6})


def test_quarters_from_changes_after_gap() -> None:
    """The growth over a gap isn't counted as 5 minutes' worth.

    Readings stop at Q + 5 min and resume at Q + 1 h with 230 kWh more; only
    the rows that follow on are kept.
    """
    rows = [
        *_rows("change", 0.1, 0.1),
        *_rows("change", 230.0, 0.1, 0.1, start=Q + 3600),
    ]
    assert quarters_from_changes(rows) == pytest.approx({Q: 1.2, Q + 3600: 1.2})


def test_quarters_from_means() -> None:
    """The 5-minute means in a quarter are averaged."""
    rows = _rows("mean", 1.0, 2.0, 3.0, None, 4.0)
    assert quarters_from_means(rows) == {Q: 2.0, Q + 900: 4.0}


def test_combine() -> None:
    """Quantities are summed with their signs, over the quarters they share."""
    signs = {"battery_out": 1, "battery_in": -1}
    quarters = {
        "battery_out": {Q: 2.0, Q + 900: 0.0},
        "battery_in": {Q: 0.5, Q + 900: 1.5, Q + 1800: 1.0},
    }
    assert combine(signs, quarters) == {Q: 1.5, Q + 900: -1.5}


def test_combine_missing_quantity() -> None:
    """A quantity with no sensor counts as zero."""
    signs = {"grid_import": 1, "grid_export": -1}
    assert combine(signs, {"grid_import": {Q: 3.0}}) == {Q: 3.0}
    assert combine(signs, {}) == {}


def test_live_entities() -> None:
    """The values now come from the power sensors and the charge."""
    options = {
        CONF_PV_POWER: "sensor.pv",
        CONF_SOLAR_ENERGY: "sensor.solar_total",
        CONF_BATTERY_SOC: "sensor.soc",
    }
    assert sorted(live_entities(options)) == ["sensor.pv", "sensor.soc"]


async def test_live_values(hass: HomeAssistant) -> None:
    """States are read in kW (or %), and an unknown one leaves the line unknown."""
    options = {
        key: SENSOR_OPTIONS[key]
        for key in (
            CONF_BATTERY_CHARGE_POWER,
            CONF_BATTERY_DISCHARGE_POWER,
            CONF_GRID_POWER,
            CONF_GRID_EXPORT_ENERGY,
            CONF_PV_POWER,
            CONF_BATTERY_SOC,
        )
    }
    set_power(hass, options[CONF_BATTERY_CHARGE_POWER], 1500)
    set_power(hass, options[CONF_BATTERY_DISCHARGE_POWER], 0)
    set_power(hass, options[CONF_GRID_POWER], 4000)
    set_power(hass, options[CONF_PV_POWER], "unavailable")
    set_soc(hass, options[CONF_BATTERY_SOC], 13)
    empty = MeasuredLine((), None)
    measured = Measured(solar=empty, battery=empty, grid=empty, soc=empty)

    live = with_live_values(hass, options, measured)
    assert live.battery.now == -1.5
    assert live.soc.now == 13
    # PV is unavailable.
    assert live.solar.now is None
    # Export is only a total, so there's no value now to subtract.
    assert live.grid.now is None
    assert live.home is None


async def test_live_values_converts_units(hass: HomeAssistant) -> None:
    """kW states are taken as they are, and unknown units are left out."""
    options = {CONF_PV_POWER: "sensor.pv", CONF_GRID_POWER: "sensor.grid"}
    hass.states.async_set("sensor.pv", "2.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.grid", "3", {"unit_of_measurement": "A"})
    empty = MeasuredLine((), None)

    live = with_live_values(hass, options, Measured(solar=empty, grid=empty))
    assert live.solar.now == 2.5
    assert live.grid.now is None


async def test_nothing_set(hass: HomeAssistant) -> None:
    """Without sensors there are no lines."""
    now = dt_util.now()
    assert await async_get_measured(hass, {}, now - timedelta(hours=1), now) == (
        Measured()
    )


async def test_get_measured(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """Periods come from the totals' growth, or the power sensors' means.

    From 13:00 to 13:30 the solar total grows 0.1 kWh every 5 minutes (1.2 kW),
    grid import 0.05 kWh with no export (0.6 kW), and the battery charges at
    a steady 2 kW at 80 %.
    """
    freezer = stockholm_afternoon
    # Its recorder platform compiles the sensors' statistics.
    assert await async_setup_component(hass, "sensor", {})
    options = {
        key: SENSOR_OPTIONS[key]
        for key in (
            CONF_SOLAR_ENERGY,
            CONF_PV_POWER,
            CONF_GRID_IMPORT_ENERGY,
            CONF_GRID_EXPORT_ENERGY,
            CONF_BATTERY_CHARGE_POWER,
            CONF_BATTERY_SOC,
        )
    }
    start = dt_util.now().replace(hour=13)
    for i in range(7):
        freezer.move_to(start + timedelta(minutes=5 * i, seconds=1))
        set_energy(hass, options[CONF_SOLAR_ENERGY], 100 + 0.1 * i)
        set_energy(hass, options[CONF_GRID_IMPORT_ENERGY], 50 + 0.05 * i)
        set_energy(hass, options[CONF_GRID_EXPORT_ENERGY], 20)
        set_power(hass, options[CONF_PV_POWER], 1200)
        set_power(hass, options[CONF_BATTERY_CHARGE_POWER], 2000)
        set_soc(hass, options[CONF_BATTERY_SOC], 80)
        await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=31))
    for i in range(6):
        do_adhoc_statistics(
            hass, start=dt_util.as_utc(start) + timedelta(minutes=5 * i)
        )
        await async_wait_recording_done(hass)

    measured = await async_get_measured(
        hass, options, dt_util.start_of_local_day(), dt_util.now()
    )

    def values(line: MeasuredLine) -> dict[datetime, float]:
        return {period.start: period.value for period in line.periods}

    second = start + timedelta(minutes=15)
    assert values(measured.solar)[second] == pytest.approx(1.2)
    assert values(measured.grid)[second] == pytest.approx(0.6)
    assert values(measured.battery)[second] == pytest.approx(-2.0)
    assert values(measured.soc)[second] == pytest.approx(80)
    assert measured.solar.periods[-1].end == start + timedelta(minutes=30)
    # Now comes from the power sensors; the grid has only totals.
    assert measured.solar.now == pytest.approx(1.2)
    assert measured.battery.now == pytest.approx(-2.0)
    assert measured.grid.now is None
    assert measured.soc.now == 80
    assert measured.home is None
