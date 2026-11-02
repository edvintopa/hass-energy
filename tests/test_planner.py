"""Tests for the battery planner, which needs no Home Assistant."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.hass_energy.models import (
    Activity,
    Command,
    PlanPeriod,
    PricePeriod,
    Prices,
    SolarPeriod,
)
from custom_components.hass_energy.planner import (
    Battery,
    Slot,
    Step,
    Tariff,
    activity,
    plan_battery,
    simulate,
    slots,
    terminal_value,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
MIDNIGHT = datetime(2026, 10, 19, tzinfo=STOCKHOLM)
QUARTER = timedelta(minutes=15)
# 10 kWh kept between 1 and 10 kWh, 5 kW each way, 90 % efficient each way.
BATTERY = Battery(
    capacity=10,
    min_soc=10,
    max_soc=100,
    max_charge=5,
    max_discharge=5,
    round_trip_efficiency=0.81,
    wear_cost=0,
)


def _slot(
    *, solar: float = 0.0, home: float = 1.0, buy: float = 1.0, sell: float = 0.5
) -> Slot:
    return Slot(MIDNIGHT, MIDNIGHT + QUARTER, buy, sell, solar, home)


def _day(
    buy: Sequence[float],
    *,
    sell: Sequence[float] | None = None,
    solar: Sequence[float] | None = None,
    home: Sequence[float] | None = None,
) -> list[Slot]:
    """Four quarter slots per hour from MIDNIGHT, from hourly values."""
    return [
        Slot(
            start=MIDNIGHT + quarter * QUARTER,
            end=MIDNIGHT + (quarter + 1) * QUARTER,
            buy=buy[quarter // 4],
            sell=(sell or [0.0] * len(buy))[quarter // 4],
            solar=(solar or [0.0] * len(buy))[quarter // 4],
            home=(home or [1.0] * len(buy))[quarter // 4],
        )
        for quarter in range(4 * len(buy))
    ]


def _commands(plan: Sequence[PlanPeriod]) -> set[Command]:
    return {period.command for period in plan}


def test_battery_limits() -> None:
    """Stored energy is kept between the charges set; each way is √ efficient."""
    assert BATTERY.min_soe == 1
    assert BATTERY.max_soe == 10
    assert BATTERY.efficiency == pytest.approx(0.9)


def test_tariff() -> None:
    """Buying adds the fee, then VAT; selling adds the compensation."""
    tariff = Tariff(import_fee=0.8, vat=25, export_compensation=0.05)
    assert tariff.buy(1.2) == pytest.approx(2.5)
    assert tariff.sell(1.2) == pytest.approx(1.25)
    assert tariff.sell(-0.3) == pytest.approx(-0.25)


def test_auto_covers_the_shortfall() -> None:
    """auto runs the home from the battery, losing some on the way out."""
    step = simulate(Command.AUTO, 0, 5, _slot(home=2), BATTERY)
    assert step.battery == pytest.approx(2)
    assert step.grid == pytest.approx(0)
    assert step.next_soe == pytest.approx(5 - 2 * 0.25 / 0.9)
    assert step.cost == pytest.approx(0)


def test_auto_stores_the_surplus() -> None:
    """auto stores what solar makes beyond the home, losing some on the way in."""
    step = simulate(Command.AUTO, 0, 5, _slot(solar=3, home=1), BATTERY)
    assert step.battery == pytest.approx(-2)
    assert step.grid == pytest.approx(0)
    assert step.next_soe == pytest.approx(5 + 2 * 0.25 * 0.9)


def test_auto_stops_at_the_lowest_charge() -> None:
    """Near the lowest charge, the grid covers what the battery can't."""
    step = simulate(Command.AUTO, 0, 1.1, _slot(home=2), BATTERY)
    assert step.battery == pytest.approx(0.1 * 0.9 / 0.25)
    assert step.grid == pytest.approx(2 - 0.36)
    assert step.next_soe == pytest.approx(1)


def test_auto_stops_when_full_or_at_max_power() -> None:
    """The surplus beyond the room left or the maximum power is exported."""
    full = simulate(Command.AUTO, 0, 9.9, _slot(solar=3, home=1, sell=0.5), BATTERY)
    assert full.battery == pytest.approx(-0.1 / (0.9 * 0.25))
    assert full.next_soe == pytest.approx(10)
    assert full.grid < 0
    assert full.cost == pytest.approx(full.grid * 0.25 * 0.5)

    fast = simulate(Command.AUTO, 0, 5, _slot(solar=8, home=1), BATTERY)
    assert fast.battery == pytest.approx(-5)
    assert fast.grid == pytest.approx(-2)


def test_hold() -> None:
    """hold leaves the battery alone; the grid covers the home."""
    step = simulate(Command.HOLD, 0, 5, _slot(home=2, buy=2), BATTERY)
    assert step == Step(battery=0, grid=2, next_soe=5, cost=2 * 0.25 * 2)


def test_charge_from_the_grid_with_wear() -> None:
    """charge draws its power from the grid on top of the home, and wears."""
    battery = Battery(10, 10, 100, 5, 5, 0.81, wear_cost=0.1)
    step = simulate(Command.CHARGE, 3, 5, _slot(home=1, buy=2), battery)
    assert step.battery == pytest.approx(-3)
    assert step.grid == pytest.approx(4)
    assert step.next_soe == pytest.approx(5 + 3 * 0.25 * 0.9)
    assert step.cost == pytest.approx(4 * 0.25 * 2 + 3 * 0.25 * 0.1)


def test_discharge_exports_beyond_the_home() -> None:
    """discharge runs the home, then sends the rest to the grid."""
    step = simulate(Command.DISCHARGE, 4, 5, _slot(home=1, sell=0.5), BATTERY)
    assert step.battery == pytest.approx(4)
    assert step.grid == pytest.approx(-3)
    assert step.cost == pytest.approx(-3 * 0.25 * 0.5)


@pytest.mark.parametrize(
    ("command", "power"),
    [
        (Command.AUTO, 0),
        (Command.HOLD, 0),
        (Command.CHARGE, 2.5),
        (Command.DISCHARGE, 2.5),
    ],
)
@pytest.mark.parametrize(("solar", "home"), [(0, 1.5), (4, 1), (1, 1)])
@pytest.mark.parametrize("soe", [1, 5, 10])
def test_power_balances(
    command: Command, power: float, solar: float, home: float, soe: float
) -> None:
    """solar + battery + grid = home, and the charge stays within its limits."""
    step = simulate(command, power, soe, _slot(solar=solar, home=home), BATTERY)
    assert solar + step.battery + step.grid == pytest.approx(home)
    assert BATTERY.min_soe - 1e-9 <= step.next_soe <= BATTERY.max_soe + 1e-9


@pytest.mark.parametrize(
    ("command", "battery", "solar", "home", "expected"),
    [
        (Command.CHARGE, -3, 0, 1, Activity.GRID_CHARGE),
        # Charging no faster than the surplus is solar charging, whatever
        # the command.
        (Command.CHARGE, -2, 3, 1, Activity.SOLAR_CHARGE),
        (Command.AUTO, -2, 3, 1, Activity.SOLAR_CHARGE),
        (Command.DISCHARGE, 4, 0, 1, Activity.EXPORT),
        (Command.DISCHARGE, 1, 0, 1, Activity.DISCHARGE),
        (Command.AUTO, 1, 0, 1, Activity.DISCHARGE),
        (Command.HOLD, 0, 0, 1, Activity.HOLD),
        (Command.AUTO, 0, 0, 1, Activity.IDLE),
        (Command.AUTO, 0.01, 0, 1, Activity.IDLE),
    ],
)
def test_activity(
    command: Command, battery: float, solar: float, home: float, expected: Activity
) -> None:
    """What the battery is seen doing comes from where its power goes."""
    step = Step(battery=battery, grid=home - solar - battery, next_soe=5, cost=0)
    assert activity(command, step, _slot(solar=solar, home=home)) == expected


def _prices(start: datetime, quarters: int) -> Prices:
    """Prices of a tenth of the local hour, per quarter from `start`."""
    start = start.astimezone(UTC)
    return Prices(
        "SE3",
        "SEK",
        tuple(
            PricePeriod(
                start + i * QUARTER,
                start + (i + 1) * QUARTER,
                (start + i * QUARTER).astimezone(STOCKHOLM).hour / 10,
            )
            for i in range(quarters)
        ),
    )


def test_slots() -> None:
    """Quarters from now to the end of the prices, with solar and home use.

    The first starts now; each takes the price and forecast holding its start.
    """
    solar = (
        SolarPeriod(MIDNIGHT + timedelta(hours=14), MIDNIGHT + timedelta(hours=15), 2),
    )
    home_by_hour = [hour / 100 for hour in range(24)]
    tariff = Tariff(import_fee=1, vat=0, export_compensation=0)
    now = MIDNIGHT + timedelta(hours=14, minutes=7)

    result = slots(
        _prices(MIDNIGHT, 2 * 96), solar, home_by_hour, tariff, now, STOCKHOLM
    )

    assert result[0] == Slot(
        start=now,
        end=MIDNIGHT + timedelta(hours=14, minutes=15),
        buy=1 + 1.4,
        sell=1.4,
        solar=2,
        home=0.14,
    )
    assert result[0].hours == pytest.approx(8 / 60)
    assert result[3].solar == 2
    assert result[4].start == MIDNIGHT + timedelta(hours=15)
    assert result[4].solar == 0
    assert result[4].home == 0.15
    assert result[-1].end == MIDNIGHT + timedelta(days=2)
    assert len(result) == 2 * 96 - 14 * 4


def test_slots_stop_at_missing_prices() -> None:
    """A plan needs prices for every quarter, so it ends at the first gap."""
    prices = _prices(MIDNIGHT, 96)
    gap = Prices("SE3", "SEK", prices.periods[:40] + prices.periods[44:])
    tariff = Tariff(0, 0, 0)
    assert len(slots(gap, (), [1.0] * 24, tariff, MIDNIGHT, STOCKHOLM)) == 40
    late = MIDNIGHT + timedelta(days=1)
    assert slots(prices, (), [1.0] * 24, tariff, late, STOCKHOLM) == []


def test_slots_on_a_long_day() -> None:
    """When summer time ends, the home's use follows the local hour twice."""
    day = datetime(2026, 10, 25, tzinfo=STOCKHOLM)
    home_by_hour = [float(hour) for hour in range(24)]
    result = slots(_prices(day, 100), (), home_by_hour, Tariff(0, 0, 0), day, STOCKHOLM)
    assert len(result) == 100
    assert [slot.home for slot in result].count(2.0) == 8


def test_plan_stores_solar_for_the_evening() -> None:
    """With buying dearer than selling, solar is stored, not the grid's power."""
    solar = [0.0] * 9 + [2.0] * 6 + [0.0] * 9
    plan = plan_battery(_day([1.0] * 24, sell=[0.3] * 24, solar=solar), BATTERY, 10)

    assert _commands(plan) == {Command.AUTO}
    assert {p.activity for p in plan[36:60]} == {Activity.SOLAR_CHARGE}
    assert plan[60].activity is Activity.DISCHARGE
    assert all(period.grid >= -1e-9 for period in plan[36:60])


def test_plan_charges_cheap_for_dear_hours() -> None:
    """It charges from the grid at night to run the home in the dear evening."""
    buy = [0.5] * 6 + [1.0] * 11 + [3.0] * 4 + [1.0] * 3
    plan = plan_battery(_day(buy), BATTERY, 10)

    night, evening = plan[:24], plan[68:84]
    assert Command.CHARGE in _commands(night)
    assert {p.activity for p in night} >= {Activity.GRID_CHARGE}
    assert all(period.grid == pytest.approx(0) for period in evening)
    assert Command.DISCHARGE not in _commands(plan)


def test_plan_skips_a_spread_below_the_losses() -> None:
    """Buying at 1 to save 1.2 later loses money at 81 % round trip."""
    buy = [1.0] * 6 + [1.1] * 11 + [1.2] * 4 + [1.1] * 3
    plan = plan_battery(_day(buy), BATTERY, 10)
    assert Command.CHARGE not in _commands(plan)


def test_plan_counts_wear() -> None:
    """A spread that covers the losses but not the wear isn't worth it."""
    buy = [1.0] * 6 + [1.1] * 11 + [1.5] * 4 + [1.1] * 3
    assert Command.CHARGE in _commands(plan_battery(_day(buy), BATTERY, 10))
    worn = Battery(10, 10, 100, 5, 5, 0.81, wear_cost=0.3)
    assert Command.CHARGE not in _commands(plan_battery(_day(buy), worn, 10))


def test_plan_charges_when_paid_to() -> None:
    """At negative prices it fills up from the grid."""
    buy = [1.0] * 12 + [-0.5] * 2 + [1.0] * 10
    plan = plan_battery(_day(buy, sell=buy), BATTERY, 10)
    assert {p.command for p in plan[48:56]} == {Command.CHARGE}
    assert plan[55].soc_end > 50


def test_plan_keeps_energy_at_the_end() -> None:
    """With nothing dearer ahead, the charge isn't sold off before the end."""
    plan = plan_battery(_day([1.0] * 24, sell=[0.8] * 24, home=[0.0] * 24), BATTERY, 80)
    assert _commands(plan) == {Command.AUTO}
    assert plan[-1].soc_end == pytest.approx(80)


def test_terminal_value() -> None:
    """A kWh left is worth the median price to buy, less losses taking it out,
    but never more than buying it back at the cheapest price.
    """
    assert terminal_value(_day([2.0, 2.0, 9.0]), BATTERY) == pytest.approx(1.8)
    assert terminal_value(_day([1.0, 2.0, 9.0]), BATTERY) == pytest.approx(1 / 0.9)
    worn = Battery(10, 10, 100, 5, 5, 0.81, wear_cost=0.2)
    assert terminal_value(_day([1.0, 2.0, 9.0]), worn) == pytest.approx(1.2 / 0.9)
    assert terminal_value(_day([-1.0]), BATTERY) == 0


def test_plan_doesnt_buy_for_the_end() -> None:
    """Cheap hours at the end aren't used to fill up just to have it after."""
    buy = [3.0] * 20 + [1.0] * 4
    plan = plan_battery(_day(buy, home=[0.0] * 24), BATTERY, 10)
    assert _commands(plan) == {Command.AUTO}


def test_plan_keeps_charge_above_the_highest() -> None:
    """Charge above the highest set is saved for the dear evening, not sold."""
    battery = Battery(10, 10, 90, 5, 5, 0.81, wear_cost=0)
    plan = plan_battery(
        _day(
            [1.0] * 12 + [3.0] * 12,
            sell=[0.05] * 24,
            home=[0.0] * 12 + [1.0] * 12,
        ),
        battery,
        100,
    )
    assert all(period.battery == 0 for period in plan[:48])
    assert plan[47].soc_end == pytest.approx(100)
    assert plan[-1].soc_end < 90


def test_plan_adds_up() -> None:
    """The charge carries on from period to period, stays in its limits, and
    each period's cost is what its flows cost.
    """
    buy = [0.5] * 6 + [1.5] * 3 + [0.8] * 8 + [3.0] * 4 + [1.0] * 3
    solar = [0.0] * 8 + [3.0] * 8 + [0.0] * 8
    battery = Battery(10, 10, 90, 5, 4, 0.81, wear_cost=0.05)
    plan = plan_battery(_day(buy, sell=[0.4] * 24, solar=solar), battery, 50)

    assert len(plan) == 96
    assert plan[0].soc_start == pytest.approx(50)
    for before, after in pairwise(plan):
        assert after.soc_start == pytest.approx(before.soc_end)
    for period in plan:
        assert 10 - 1e-6 <= period.soc_end <= 90 + 1e-6
        assert period.solar + period.battery + period.grid == pytest.approx(period.home)
        price = buy[period.start.hour] if period.grid > 0 else 0.4
        wear = max(0.0, -period.battery) * 0.25 * 0.05
        assert period.cost == pytest.approx(period.grid * 0.25 * price + wear)
        assert period.power == 0 or period.command in (
            Command.CHARGE,
            Command.DISCHARGE,
        )


def test_plan_from_below_the_lowest_charge() -> None:
    """A battery below its lowest charge isn't discharged."""
    plan = plan_battery(_day([1.0] * 3 + [3.0]), BATTERY, 5)
    assert all(period.battery <= 1e-9 for period in plan[:12])


def test_no_plan_without_slots_or_room() -> None:
    """Nothing to plan over, or a battery with no range, gives no plan."""
    assert plan_battery([], BATTERY, 50) == ()
    stuck = Battery(10, 50, 50, 5, 5, 0.81, 0)
    assert plan_battery(_day([1.0]), stuck, 50) == ()
