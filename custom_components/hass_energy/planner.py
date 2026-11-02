"""The battery planner: prices and forecasts in, a plan of inverter commands out.

Pure Python with no Home Assistant imports, so it can be tested on its own and
swapped out later. The coordinator adapts Home Assistant's data to it.

It plans by dynamic programming over the battery's stored energy. Every choice
it weighs is a command the inverter can carry out (auto, hold, or charge or
discharge at a set power), and `_step` is the one place that works out what a
command does to the energy flows. Both passes use it, so the flows in the plan
are exactly the ones it priced.

Power is in kW, signed as on the panel: up is a source feeding the home, so the
battery is positive while discharging, the grid while importing, and
solar + battery + grid = home.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
import math
from statistics import median

from .models import Activity, Command, PlanPeriod, Prices, SolarPeriod

QUARTER = timedelta(minutes=15)
# Levels of stored energy the plan's value is worked out at, from the lowest
# allowed charge to the highest, and how many powers it tries each way.
SOE_LEVELS = 101
POWER_STEPS = 10
# Going forwards, a command must beat an earlier one in `_candidates` order by
# more than what this many kWh cost at the median price to buy. The cost to go
# is interpolated between levels, which is off by a little, and without this
# the plan switched to hold or charge for a single quarter on that noise. It's
# small, so real gains (storing the solar that sells cheapest, a last partial
# discharge at a peak) still win.
TIE_TOLERANCE = 0.005
# Battery power below this is no activity at all.
ACTIVITY_THRESHOLD_KW = 0.05


@dataclass(frozen=True, slots=True)
class Battery:
    """What the battery can store and do."""

    # kWh it holds when full.
    capacity: float
    # The charge it's kept between, in %.
    min_soc: float
    max_soc: float
    # The most it takes in or gives out, in kW.
    max_charge: float
    max_discharge: float
    # The share of the energy that comes back out, as a fraction.
    round_trip_efficiency: float
    # What wear costs per kWh charged, in the prices' currency.
    wear_cost: float
    # Worked out once from the above, since the planner reads them for every
    # state and choice: the lowest and highest stored energy allowed in kWh,
    # and the share kept each way, in and out (√ of the round trip).
    min_soe: float = field(init=False)
    max_soe: float = field(init=False)
    efficiency: float = field(init=False)

    def __post_init__(self) -> None:
        """Work out the stored energy limits and the efficiency each way."""
        object.__setattr__(self, "min_soe", self.capacity * self.min_soc / 100)
        object.__setattr__(self, "max_soe", self.capacity * self.max_soc / 100)
        object.__setattr__(self, "efficiency", math.sqrt(self.round_trip_efficiency))


@dataclass(frozen=True, slots=True)
class Tariff:
    """How the spot price becomes what a kWh costs to buy and earns to sell."""

    # Added to the spot price before VAT: supplier markup, grid fee, energy tax.
    import_fee: float
    # VAT on bought energy, in %.
    vat: float
    # Added to the spot price for sold energy.
    export_compensation: float

    def buy(self, spot: float) -> float:
        """What a kWh bought costs."""
        return (spot + self.import_fee) * (1 + self.vat / 100)

    def sell(self, spot: float) -> float:
        """What a kWh sold earns."""
        return spot + self.export_compensation


@dataclass(frozen=True, slots=True)
class Slot:
    """One period to plan, with what's expected over it."""

    start: datetime
    end: datetime
    # Per kWh, in the prices' currency.
    buy: float
    sell: float
    # Average power in kW.
    solar: float
    home: float
    # Its length in hours, worked out once.
    hours: float = field(init=False)

    def __post_init__(self) -> None:
        """Work out the slot's length."""
        object.__setattr__(
            self, "hours", (self.end - self.start).total_seconds() / 3600
        )


@dataclass(frozen=True, slots=True)
class Step:
    """What a command does over one slot."""

    battery: float
    grid: float
    # The battery's stored energy at the end, in kWh.
    next_soe: float
    cost: float


def slots(
    prices: Prices,
    solar: Sequence[SolarPeriod],
    home_by_hour: Sequence[float],
    tariff: Tariff,
    now: datetime,
    tz: tzinfo,
) -> list[Slot]:
    """Return 15-minute slots from `now` to the end of the known prices.

    The first runs from `now` to the end of its quarter. Each takes the price
    and solar forecast of the periods holding its quarter's start (no solar
    when none does), and the home's use in its local hour. The slots stop at
    the first quarter with no price.
    """
    # In UTC, so adding a quarter never skips or repeats local time.
    timestamp = now.timestamp()
    start = datetime.fromtimestamp(timestamp - timestamp % QUARTER.total_seconds(), UTC)
    result: list[Slot] = []
    price_index = solar_index = 0
    while True:
        while (
            price_index < len(prices.periods)
            and prices.periods[price_index].end <= start
        ):
            price_index += 1
        if (
            price_index == len(prices.periods)
            or prices.periods[price_index].start > start
        ):
            return result
        spot = prices.periods[price_index].price
        while solar_index < len(solar) and solar[solar_index].end <= start:
            solar_index += 1
        sun = (
            solar[solar_index].power
            if solar_index < len(solar) and solar[solar_index].start <= start
            else 0.0
        )
        result.append(
            Slot(
                start=max(start, now),
                end=start + QUARTER,
                buy=tariff.buy(spot),
                sell=tariff.sell(spot),
                solar=sun,
                home=home_by_hour[start.astimezone(tz).hour],
            )
        )
        start += QUARTER


def _step(
    command: Command, power: float, soe: float, slot: Slot, battery: Battery
) -> tuple[float, float, float, float]:
    """Return the battery and grid kW, the stored kWh after and the cost.

    The physics behind `simulate`, as a plain tuple, since the planner calls it
    for every state and choice.
    """
    hours = slot.hours
    net = slot.home - slot.solar
    if command is Command.HOLD:
        battery_kw = 0.0
    elif command is Command.DISCHARGE or (command is Command.AUTO and net > 0):
        available = min(
            battery.max_discharge,
            max(0.0, soe - battery.min_soe) * battery.efficiency / hours,
        )
        battery_kw = min(net if command is Command.AUTO else power, available)
    else:
        room = min(
            battery.max_charge,
            max(0.0, battery.max_soe - soe) / (battery.efficiency * hours),
        )
        battery_kw = -min(-net if command is Command.AUTO else power, room)
    grid = net - battery_kw
    if battery_kw > 0:
        next_soe = soe - battery_kw * hours / battery.efficiency
        wear = 0.0
    else:
        next_soe = soe - battery_kw * hours * battery.efficiency
        wear = -battery_kw * hours * battery.wear_cost
    energy_cost = grid * hours * (slot.buy if grid > 0 else slot.sell)
    return battery_kw, grid, next_soe, energy_cost + wear


def simulate(
    command: Command, power: float, soe: float, slot: Slot, battery: Battery
) -> Step:
    """Return what `command` (at `power` kW) does over `slot` from `soe` kWh.

    auto covers the home's shortfall from the battery, or stores the solar
    surplus. hold leaves the battery alone. charge and discharge run at
    `power`. All stop where the battery's power or charge limits do.
    """
    return Step(*_step(command, power, soe, slot, battery))


def activity(command: Command, step: Step, slot: Slot) -> Activity:
    """Return what the battery is seen doing, from its command and flows.

    Charging is grid charging when the solar surplus doesn't cover it, and
    discharging is exporting when the home's shortfall doesn't take it all.
    """
    if step.battery < -ACTIVITY_THRESHOLD_KW:
        surplus = max(0.0, slot.solar - slot.home)
        if -step.battery > surplus + ACTIVITY_THRESHOLD_KW:
            return Activity.GRID_CHARGE
        return Activity.SOLAR_CHARGE
    if step.battery > ACTIVITY_THRESHOLD_KW:
        shortfall = max(0.0, slot.home - slot.solar)
        if step.battery > shortfall + ACTIVITY_THRESHOLD_KW:
            return Activity.EXPORT
        return Activity.DISCHARGE
    return Activity.HOLD if command is Command.HOLD else Activity.IDLE


def _candidates(battery: Battery) -> list[tuple[Command, float]]:
    """Return the commands to weigh, in the order ties go to.

    This is where what the inverter can't do will be left out.
    """
    return [
        (Command.AUTO, 0.0),
        (Command.HOLD, 0.0),
        *(
            (Command.CHARGE, battery.max_charge * k / POWER_STEPS)
            for k in range(1, POWER_STEPS + 1)
        ),
        *(
            (Command.DISCHARGE, battery.max_discharge * k / POWER_STEPS)
            for k in range(1, POWER_STEPS + 1)
        ),
    ]


def terminal_value(slots: Sequence[Slot], battery: Battery) -> float:
    """Return what a kWh stored at the end of the plan is worth.

    It saves buying a kWh later at a typical price: the median one to buy over
    the plan, less what's lost taking it out. But it's never worth more than
    buying it back at the plan's cheapest price, wear and losses included, so
    the plan never buys energy just to have it at the end, which tomorrow's
    sun may fill for free.
    """
    typical = median(slot.buy for slot in slots) * battery.efficiency
    refill = (min(slot.buy for slot in slots) + battery.wear_cost) / battery.efficiency
    return max(0.0, min(typical, refill))


def _value_at(values: Sequence[float], soe: float, low: float, spacing: float) -> float:
    """Interpolate the cost to go at `soe`, holding the ends beyond the levels."""
    position = (soe - low) / spacing
    if position <= 0:
        return values[0]
    last = len(values) - 1
    if position >= last:
        return values[last]
    index = int(position)
    fraction = position - index
    return values[index] * (1 - fraction) + values[index + 1] * fraction


def _best(
    soe: float,
    slot: Slot,
    battery: Battery,
    candidates: Sequence[tuple[Command, float]],
    next_values: Sequence[float],
    low: float,
    spacing: float,
    tie: float,
) -> tuple[float, Command, float]:
    """Return the least cost from `soe` to the end, with the command for it.

    A command only beats one before it in `candidates` by more than `tie`.
    """
    best = (math.inf, Command.AUTO, 0.0)
    for command, power in candidates:
        _, _, next_soe, cost = _step(command, power, soe, slot, battery)
        total = cost + _value_at(next_values, next_soe, low, spacing)
        if total < best[0] - tie:
            best = (total, command, power)
    return best


def plan_battery(
    slots: Sequence[Slot], battery: Battery, soc: float
) -> tuple[PlanPeriod, ...]:
    """Return the cheapest plan over `slots`, starting at `soc` %.

    Works backwards from the end to find the least cost to go from each level
    of stored energy, then forwards from the real charge, picking the command
    with the least cost now plus after. Energy left at the end counts at
    `terminal_value`.
    """
    if not slots or battery.max_soe <= battery.min_soe:
        return ()
    soe = battery.capacity * soc / 100
    # The levels also reach a charge now outside the limits, so energy above
    # the highest charge is worth what it can still do, not nothing.
    low, high = min(battery.min_soe, soe), max(battery.max_soe, soe)
    spacing = (high - low) / (SOE_LEVELS - 1)
    levels = [low + i * spacing for i in range(SOE_LEVELS)]
    candidates = _candidates(battery)
    end_value = terminal_value(slots, battery)
    # Only the commands picked going forwards prefer auto on near ties; the
    # cost to go is worked out exactly.
    tie = TIE_TOLERANCE * median(abs(slot.buy) for slot in slots)

    # values[t][i]: the least cost from levels[i] at the start of slot t.
    # Energy below the lowest charge can't be used, so it's worth nothing.
    values = [[-end_value * max(0.0, level - battery.min_soe) for level in levels]]
    for slot in reversed(slots):
        next_values = values[-1]
        values.append(
            [
                _best(level, slot, battery, candidates, next_values, low, spacing, 0.0)[
                    0
                ]
                for level in levels
            ]
        )
    values.reverse()

    periods = []
    for index, slot in enumerate(slots):
        _, command, power = _best(
            soe, slot, battery, candidates, values[index + 1], low, spacing, tie
        )
        step = simulate(command, power, soe, slot, battery)
        periods.append(
            PlanPeriod(
                start=slot.start,
                end=slot.end,
                command=command,
                power=power,
                solar=slot.solar,
                home=slot.home,
                battery=step.battery,
                grid=step.grid,
                soc_start=soe / battery.capacity * 100,
                soc_end=step.next_soe / battery.capacity * 100,
                cost=step.cost,
                activity=activity(command, step, slot),
            )
        )
        soe = step.next_soe
    return tuple(periods)
