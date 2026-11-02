"""Measured power and battery charge from the sensors set in the entry's options.

Each quantity has a lifetime kWh total and a power sensor, both optional. A
15-minute period's average kW comes from how much the total grew over it, or
from the power sensor's average when no total is set. The value now always
comes from the power sensor. Both are read from the recorder's 5-minute
statistics, which it keeps for 10 days.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_INPUT_ENERGY,
    CONF_BATTERY_OUTPUT_ENERGY,
    CONF_BATTERY_SOC,
    CONF_GRID_EXPORT_ENERGY,
    CONF_GRID_EXPORT_POWER,
    CONF_GRID_IMPORT_ENERGY,
    CONF_GRID_POWER,
    CONF_LOAD_ENERGY,
    CONF_LOAD_POWER,
    CONF_PV_POWER,
    CONF_SOLAR_ENERGY,
)
from .models import Measured, MeasuredLine, MeasuredPeriod

QUARTER = 15 * 60
# Seconds and hours in one 5-minute statistics row.
ROW_SECONDS = 5 * 60
ROW_HOURS = ROW_SECONDS / 3600

# Each quantity's kWh total and power sensor (or charge in %), as option keys.
QUANTITIES: dict[str, tuple[str | None, str]] = {
    "pv": (CONF_SOLAR_ENERGY, CONF_PV_POWER),
    "battery_in": (CONF_BATTERY_INPUT_ENERGY, CONF_BATTERY_CHARGE_POWER),
    "battery_out": (CONF_BATTERY_OUTPUT_ENERGY, CONF_BATTERY_DISCHARGE_POWER),
    "grid_import": (CONF_GRID_IMPORT_ENERGY, CONF_GRID_POWER),
    "grid_export": (CONF_GRID_EXPORT_ENERGY, CONF_GRID_EXPORT_POWER),
    "load": (CONF_LOAD_ENERGY, CONF_LOAD_POWER),
    "soc": (None, CONF_BATTERY_SOC),
}
# Each line of Measured as the signed sum of its quantities. A quantity with no
# sensor set counts as 0, so a net grid meter set as the grid power, with no
# export sensor, gives the net grid power.
LINES: dict[str, dict[str, int]] = {
    "solar": {"pv": 1},
    "battery": {"battery_out": 1, "battery_in": -1},
    "grid": {"grid_import": 1, "grid_export": -1},
    "home": {"load": 1},
    "soc": {"soc": 1},
}

type Quarters = dict[float, float]


def live_entities(options: Mapping[str, Any]) -> list[str]:
    """Return the sensors the values now come from."""
    return [options[power] for _, power in QUANTITIES.values() if options.get(power)]


def quarters_from_changes(rows: list[Mapping[str, Any]]) -> Quarters:
    """Return the average kW in each quarter from 5-minute kWh changes.

    A row that doesn't follow the one before it by 5 minutes is left out, as is
    the first: after a gap (the sensor or HA was down), its change is all the
    growth since the last reading, not 5 minutes'. A quarter with fewer rows
    (the current one, or around a gap) is averaged over the time they cover.
    """
    total: defaultdict[float, float] = defaultdict(float)
    count: defaultdict[float, int] = defaultdict(int)
    previous: float | None = None
    for row in rows:
        follows = previous is not None and row["start"] - previous == ROW_SECONDS
        previous = row["start"]
        if not follows or (change := row.get("change")) is None:
            continue
        quarter = row["start"] - row["start"] % QUARTER
        total[quarter] += change
        count[quarter] += 1
    return {quarter: total[quarter] / (count[quarter] * ROW_HOURS) for quarter in total}


def quarters_from_means(rows: list[Mapping[str, Any]]) -> Quarters:
    """Return the average of the 5-minute means in each quarter."""
    means: defaultdict[float, list[float]] = defaultdict(list)
    for row in rows:
        if (mean := row.get("mean")) is not None:
            means[row["start"] - row["start"] % QUARTER].append(mean)
    return {quarter: sum(values) / len(values) for quarter, values in means.items()}


def combine(signs: Mapping[str, int], quarters: Mapping[str, Quarters]) -> Quarters:
    """Return the signed sum of the quantities that are set, per quarter.

    Only quarters every one of them has data for are kept.
    """
    present = [name for name in signs if name in quarters]
    if not present:
        return {}
    common = set.intersection(*(set(quarters[name]) for name in present))
    return {
        quarter: sum(signs[name] * quarters[name][quarter] for name in present)
        for quarter in sorted(common)
    }


def _live_value(hass: HomeAssistant, entity_id: str, key: str) -> float | None:
    """Return a sensor's current value in kW, or in % for the charge."""
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if key == CONF_BATTERY_SOC:
        return value
    unit = state.attributes.get("unit_of_measurement")
    if unit not in PowerConverter.VALID_UNITS:
        return None
    return PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)


def _live_line(
    hass: HomeAssistant, options: Mapping[str, Any], signs: Mapping[str, int]
) -> float | None:
    """Return a line's value now, or None when one of its sensors can't say."""
    total = 0.0
    for name, sign in signs.items():
        energy, power = QUANTITIES[name]
        if not options.get(power):
            if energy and options.get(energy):
                return None
            continue
        if (value := _live_value(hass, options[power], power)) is None:
            return None
        total += sign * value
    return total


def with_live_values(
    hass: HomeAssistant, options: Mapping[str, Any], measured: Measured
) -> Measured:
    """Return `measured` with each line's value now read again."""
    lines = {field.name: getattr(measured, field.name) for field in fields(measured)}
    return replace(
        measured,
        **{
            name: replace(line, now=_live_line(hass, options, LINES[name]))
            for name, line in lines.items()
            if line is not None
        },
    )


async def async_get_measured(
    hass: HomeAssistant, options: Mapping[str, Any], start: datetime, end: datetime
) -> Measured:
    """Return what was measured from `start` to `end`, and the values now."""
    # The statistic each quantity's periods come from, and whether it's a total.
    sources = {
        name: (options[energy], True)
        if energy and options.get(energy)
        else (options[power], False)
        for name, (energy, power) in QUANTITIES.items()
        if (energy and options.get(energy)) or options.get(power)
    }
    if not sources:
        return Measured()

    # One row more, before `start`, which tells whether the first row follows
    # a gap.
    rows = await get_instance(hass).async_add_executor_job(
        _statistics,
        hass,
        {stat_id for stat_id, _ in sources.values()},
        start - timedelta(seconds=ROW_SECONDS),
        end,
    )
    quarters = {
        name: (quarters_from_changes if is_total else quarters_from_means)(
            rows.get(stat_id, [])
        )
        for name, (stat_id, is_total) in sources.items()
    }
    lines: dict[str, MeasuredLine] = {}
    for name, signs in LINES.items():
        if not any(quantity in sources for quantity in signs):
            continue
        periods = tuple(
            MeasuredPeriod(
                start=dt_util.utc_from_timestamp(quarter),
                end=dt_util.utc_from_timestamp(quarter + QUARTER),
                value=value,
            )
            for quarter, value in combine(signs, quarters).items()
            if quarter >= start.timestamp()
        )
        lines[name] = MeasuredLine(periods, _live_line(hass, options, signs))
    return Measured(**lines)


def _statistics(
    hass: HomeAssistant, statistic_ids: set[str], start: datetime, end: datetime
) -> dict[str, list[Any]]:
    """Return the 5-minute statistics; runs in the recorder thread, in one query."""
    return statistics_during_period(
        hass,
        start,
        end,
        statistic_ids,
        "5minute",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR, "power": UnitOfPower.KILO_WATT},
        {"change", "mean"},
    )
