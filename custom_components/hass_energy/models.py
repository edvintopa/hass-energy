"""Data models for HASS Energy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class PricePeriod:
    """The spot price for one delivery period."""

    start: datetime
    end: datetime
    # In the area's currency per kWh.
    price: float


@dataclass(frozen=True, slots=True)
class Prices:
    """Spot prices for one area, oldest first."""

    area: str
    currency: str
    periods: tuple[PricePeriod, ...]


@dataclass(frozen=True, slots=True)
class SolarPeriod:
    """Forecast solar generation over one period."""

    start: datetime
    end: datetime
    # Average power over the period, in kW.
    power: float


@dataclass(frozen=True, slots=True)
class MeasuredPeriod:
    """A measured value over one 15-minute period (or the part with data)."""

    start: datetime
    end: datetime
    # Average power in kW, or the battery's charge in %.
    value: float


@dataclass(frozen=True, slots=True)
class MeasuredLine:
    """What one of the panel's lines measured today, and its value now."""

    # Oldest first, up to the last compiled 5-minute statistics.
    periods: tuple[MeasuredPeriod, ...]
    # From the sensors' current states; None when one is unknown or not set.
    now: float | None


@dataclass(frozen=True, slots=True)
class Measured:
    """The measured lines, each None when none of its sensors are set.

    Up is a source feeding the home: solar, battery discharging, grid
    importing and home use are positive.
    """

    solar: MeasuredLine | None = None
    battery: MeasuredLine | None = None
    grid: MeasuredLine | None = None
    home: MeasuredLine | None = None
    # The battery's state of charge in %.
    soc: MeasuredLine | None = None


class Command(StrEnum):
    """What the inverter is told to do over a period."""

    # Its own self-consumption: solar feeds the home, the surplus charges the
    # battery, and the battery covers what solar doesn't.
    AUTO = "auto"
    # Charge at a set power, from solar and the grid.
    CHARGE = "charge"
    # Discharge at a set power, into the home and then the grid.
    DISCHARGE = "discharge"
    # Keep the battery's charge; the home runs on solar and the grid.
    HOLD = "hold"


class Activity(StrEnum):
    """What the battery is seen doing over a period, from its command and flows."""

    GRID_CHARGE = "grid_charge"
    SOLAR_CHARGE = "solar_charge"
    DISCHARGE = "discharge"
    EXPORT = "export"
    HOLD = "hold"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class PlanPeriod:
    """One period of the battery plan, with the flows it predicts.

    Power is the average over the period in kW. Up is a source feeding the
    home, so solar + battery + grid = home.
    """

    start: datetime
    end: datetime
    command: Command
    # The command's power in kW; 0 for auto and hold.
    power: float
    solar: float
    home: float
    battery: float
    grid: float
    # The battery's charge in % at the start and end of the period.
    soc_start: float
    soc_end: float
    # What the grid energy costs, in the prices' currency, plus battery wear;
    # negative when it earns.
    cost: float
    activity: Activity


@dataclass(frozen=True, slots=True)
class EnergyData:
    """Everything the integration knows after a refresh."""

    # Average home consumption in kW (kWh per hour), or None if unknown.
    average_consumption: float | None
    # Average home consumption in kW by local hour of day (24 values), or empty
    # if unknown.
    consumption_by_hour: tuple[float, ...]
    prices: Prices
    # Forecast solar generation, oldest first; empty without a linked forecast.
    solar_forecast: tuple[SolarPeriod, ...]
    # Measured today, from the sensors set in the entry's options.
    measured: Measured
    # The battery plan from now on, oldest first; empty when it can't be made.
    plan: tuple[PlanPeriod, ...]
