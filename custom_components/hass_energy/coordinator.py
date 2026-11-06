"""Data coordinator for HASS Energy."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_REQUIRED,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_MAX_DISCHARGE,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_WEAR_COST,
    CONF_EXPORT_COMPENSATION,
    CONF_IMPORT_FEE,
    CONF_NORDPOOL_AREA,
    CONF_NORDPOOL_ENTRY,
    CONF_VAT,
    CONSUMPTION_WINDOW,
    DOMAIN,
    LIVE_COOLDOWN,
    LOGGER,
    MEASURED_REFRESH_SECOND,
    PLAN_DEFAULTS,
    PLAN_EXPIRY_SECOND,
    REFRESH_MINUTE,
    REPLAN_CUTOFF,
    SIGNAL_DATA_UPDATED,
    SIGNAL_ENERGY_PREFS_UPDATED,
    SIGNAL_LIVE_UPDATED,
)
from .consumption import async_get_consumption
from .measured import async_get_measured, live_entities, with_live_values
from .models import (
    EnergyData,
    Measured,
    PlanPeriod,
    PricePeriod,
    Prices,
    SolarPeriod,
)
from .nordpool import async_get_prices, delivery_days, may_be_published
from .planner import Battery, Tariff, plan_battery, slots
from .solar import async_get_solar_forecast, async_linked_forecast_entries

type HassEnergyConfigEntry = ConfigEntry[HassEnergyCoordinator]


def _battery(options: Mapping[str, Any]) -> Battery | None:
    """Return the battery set in the options, or None without its size."""
    if not all(options.get(key) for key in BATTERY_REQUIRED):
        return None
    settings = {**PLAN_DEFAULTS, **options}
    return Battery(
        capacity=settings[CONF_BATTERY_CAPACITY],
        min_soc=settings[CONF_BATTERY_MIN_SOC],
        max_soc=settings[CONF_BATTERY_MAX_SOC],
        max_charge=settings[CONF_BATTERY_MAX_CHARGE],
        max_discharge=settings[CONF_BATTERY_MAX_DISCHARGE],
        round_trip_efficiency=settings[CONF_BATTERY_EFFICIENCY] / 100,
        wear_cost=settings[CONF_BATTERY_WEAR_COST],
    )


def _tariff(options: Mapping[str, Any]) -> Tariff:
    """Return the prices set in the options."""
    settings = {**PLAN_DEFAULTS, **options}
    return Tariff(
        import_fee=settings[CONF_IMPORT_FEE],
        vat=settings[CONF_VAT],
        export_compensation=settings[CONF_EXPORT_COMPENSATION],
    )


def _soc_now(measured: Measured) -> float | None:
    """Return the battery's charge now, or as last measured, in %."""
    if measured.soc is None:
        return None
    if measured.soc.now is not None:
        return measured.soc.now
    return measured.soc.periods[-1].value if measured.soc.periods else None


def _quarter_start(now: datetime) -> datetime:
    """Return the start of the quarter-hour that holds `now`."""
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def _chart_range(now: datetime) -> tuple[datetime, datetime]:
    """Return the local today and tomorrow, which the panel's chart shows."""
    return (
        dt_util.start_of_local_day(now),
        dt_util.start_of_local_day(now.date() + timedelta(days=2)),
    )


class HassEnergyCoordinator(DataUpdateCoordinator[EnergyData]):
    """Fetch energy data once per hour, after the recorder's hourly statistics."""

    config_entry: HassEnergyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HassEnergyConfigEntry) -> None:
        """Initialize."""
        # No interval: refreshes are aligned to the hour by async_track_hourly.
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN)
        nordpool = hass.config_entries.async_get_entry(
            entry.data.get(CONF_NORDPOOL_ENTRY, "")
        )
        if nordpool is None or CONF_NORDPOOL_AREA not in entry.data:
            # Entries from before prices, or whose Nord Pool entry was removed.
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="nordpool_not_configured"
            )
        self._nordpool_entry = nordpool.entry_id
        self._area: str = entry.data[CONF_NORDPOOL_AREA]
        self._currency: str = nordpool.data["currency"]
        # Published prices never change, so each delivery day (CET) is fetched
        # until it has some.
        self._prices_by_day: dict[date, list[PricePeriod]] = {}
        # The linked forecast entries, and the listener on each one's
        # coordinator (with that coordinator), so their updates show at once.
        self._forecast_entries: list[str] = []
        self._forecast_listeners: dict[
            str, tuple[DataUpdateCoordinator, CALLBACK_TYPE]
        ] = {}
        # When a plan was last made, so it's made at most once a quarter.
        self._planned_at: datetime | None = None
        # One plan at a time, so a plan started later is also sent later.
        self._plan_lock = asyncio.Lock()
        # Set once the entry unloads, so refreshes still running then send
        # nothing on.
        self._unloaded = False

    async def _async_update_data(self) -> EnergyData:
        now = dt_util.now()
        start, end = _chart_range(now)
        (average, by_hour), prices, solar, measured = await asyncio.gather(
            async_get_consumption(self.hass, CONSUMPTION_WINDOW),
            self._async_prices(now, start, end),
            self._async_solar(start, end),
            self._async_measured(start, now),
        )
        data = EnergyData(
            average_consumption=average,
            consumption_by_hour=by_hour,
            prices=prices,
            solar_forecast=solar,
            measured=measured,
            plan=(),
        )
        return replace(data, plan=await self._async_plan(data))

    async def _async_plan(self, data: EnergyData) -> tuple[PlanPeriod, ...]:
        """Return the battery plan from now on, made from `data`.

        Empty without the battery's size, its charge, prices from now on or
        the home's consumption, or if planning fails, so the rest of the data
        still updates.
        """
        async with self._plan_lock:
            now = dt_util.now()
            try:
                battery = _battery(self.config_entry.options)
                soc = _soc_now(data.measured)
                if battery is None or soc is None or not data.consumption_by_hour:
                    return ()
                plan_slots = slots(
                    data.prices,
                    data.solar_forecast,
                    data.consumption_by_hour,
                    _tariff(self.config_entry.options),
                    now,
                    dt_util.get_default_time_zone(),
                )
                if not plan_slots:
                    return ()
                plan = await self.hass.async_add_executor_job(
                    plan_battery, plan_slots, battery, soc
                )
            except Exception:  # A bug in planning mustn't stop the rest.
                LOGGER.exception("Could not plan the battery")
                return ()
            # Only now: without a plan, the next measured refresh tries again.
            self._planned_at = now
        LOGGER.debug(
            "Planned %d periods from %.0f %% to %s, costing %.2f %s",
            len(plan),
            soc,
            plan[-1].end.isoformat() if plan else None,
            sum(period.cost for period in plan),
            data.prices.currency,
        )
        return plan

    async def _async_measured(self, start: datetime, now: datetime) -> Measured:
        """Return what was measured from `start` to `now`.

        If the recorder can't be read, what was measured before is kept, so
        the rest of the data still updates.
        """
        try:
            return await async_get_measured(
                self.hass, self.config_entry.options, start, now
            )
        except Exception as err:  # The recorder's database: anything can fail.
            LOGGER.warning("Could not read the measured data: %s", err)
            return self.data.measured if self.data else Measured()

    @callback
    def _async_set_partial_data(self, data: EnergyData) -> None:
        """Send data that changed between refreshes.

        Unlike async_set_updated_data, this leaves whether the last refresh
        succeeded as it is, so a failed one isn't hidden.
        """
        self.data = data
        self.async_update_listeners()

    async def _async_refresh_measured(self) -> None:
        """Re-read only the measured data and send it on."""
        if self.data is None:
            return
        now = dt_util.now()
        start, _ = _chart_range(now)
        measured = await self._async_measured(start, now)
        if self._planned_at is not None and self._planned_at >= _quarter_start(now):
            self._async_set_partial_data(replace(self.data, measured=measured))
            return
        # A new quarter: plan again from the battery's charge now, and send
        # both at once.
        plan = await self._async_plan(replace(self.data, measured=measured))
        self._async_set_partial_data(replace(self.data, measured=measured, plan=plan))

    @callback
    def _async_refresh_live(self) -> None:
        """Read the measured values now again and send only those on.

        They change every few seconds, so the entities, which don't show them,
        aren't updated, and the panel gets them without the rest of the data.
        """
        if self.data is None:
            return
        measured = with_live_values(
            self.hass, self.config_entry.options, self.data.measured
        )
        self.data = replace(self.data, measured=measured)
        async_dispatcher_send(self.hass, SIGNAL_LIVE_UPDATED, measured)

    @callback
    def async_track_measured(self) -> CALLBACK_TYPE:
        """Follow the measured sensors. Returns the unsubscribe callback.

        The periods are re-read after each 5-minute statistics run, and the
        values now when the sensors change, at most once per LIVE_COOLDOWN.
        """

        async def _refresh(_now: datetime) -> None:
            await self._async_refresh_measured()

        @callback
        def _expire_plan(now: datetime) -> None:
            # A stuck refresh mustn't leave the inverter on an old command.
            data = self.data
            if data is not None and data.plan and data.plan[0].end <= now:
                self._async_set_partial_data(replace(data, plan=()))

        live = Debouncer(
            self.hass,
            LOGGER,
            cooldown=LIVE_COOLDOWN.total_seconds(),
            immediate=True,
            function=self._async_refresh_live,
        )

        @callback
        def _changed(_event: Event) -> None:
            live.async_schedule_call()

        unsubs = [
            async_track_time_change(
                self.hass,
                _refresh,
                minute=list(range(0, 60, 5)),
                second=MEASURED_REFRESH_SECOND,
            ),
            async_track_time_change(
                self.hass,
                _expire_plan,
                minute=list(range(0, 60, 15)),
                second=PLAN_EXPIRY_SECOND,
            ),
            async_track_state_change_event(
                self.hass, live_entities(self.config_entry.options), _changed
            ),
        ]

        @callback
        def _unsubscribe() -> None:
            for unsub in unsubs:
                unsub()
            live.async_shutdown()

        return _unsubscribe

    async def _async_solar(
        self, start: datetime, end: datetime
    ) -> tuple[SolarPeriod, ...]:
        """Return the linked forecasts, and follow the entries they come from."""
        entry_ids = await async_linked_forecast_entries(self.hass)
        self._forecast_entries = entry_ids
        self._watch_forecast_coordinators()
        return await async_get_solar_forecast(self.hass, entry_ids, start, end)

    @callback
    def _watch_forecast_coordinators(self) -> None:
        """Listen to each linked entry's coordinator, if it has one.

        Coordinators that are already listened to are left alone, so their
        refresh schedule isn't disturbed.
        """
        current: dict[str, DataUpdateCoordinator] = {}
        for entry_id in self._forecast_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            source = getattr(entry, "runtime_data", None)
            if isinstance(source, DataUpdateCoordinator):
                current[entry_id] = source
        for entry_id, (source, unsub) in list(self._forecast_listeners.items()):
            if current.get(entry_id) is not source:
                unsub()
                del self._forecast_listeners[entry_id]
        for entry_id, source in current.items():
            if entry_id not in self._forecast_listeners:
                unsub = source.async_add_listener(self._schedule_solar_refresh)
                self._forecast_listeners[entry_id] = (source, unsub)

    @callback
    def _schedule_solar_refresh(self) -> None:
        self.config_entry.async_create_background_task(
            self.hass, self._async_refresh_solar(), f"{DOMAIN} solar refresh"
        )

    async def _async_refresh_solar(self) -> None:
        """Re-read only the solar forecast and send it on."""
        if self.data is None:
            return
        start, end = _chart_range(dt_util.now())
        solar = await self._async_solar(start, end)
        now = dt_util.now()
        plan = self.data.plan
        if _quarter_start(now + REPLAN_CUTOFF) == _quarter_start(now):
            plan = await self._async_plan(replace(self.data, solar_forecast=solar))
        self._async_set_partial_data(
            replace(self.data, solar_forecast=solar, plan=plan)
        )

    @callback
    def async_track_solar(self) -> CALLBACK_TYPE:
        """Re-read the forecast when it may have changed.

        That is when the Energy dashboard is saved, when a linked entry changes
        state (it may load after this one at startup), and when its coordinator
        updates. Returns the unsubscribe callback.
        """

        @callback
        def _entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
            if entry.entry_id in self._forecast_entries:
                self._schedule_solar_refresh()

        unsubs = [
            async_dispatcher_connect(
                self.hass, SIGNAL_ENERGY_PREFS_UPDATED, self._schedule_solar_refresh
            ),
            async_dispatcher_connect(
                self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed
            ),
        ]

        @callback
        def _unsubscribe() -> None:
            for unsub in unsubs:
                unsub()
            for _, unsub in self._forecast_listeners.values():
                unsub()
            self._forecast_listeners.clear()

        return _unsubscribe

    async def async_shutdown(self) -> None:
        """Stop sending data on once the entry unloads.

        A refresh or plan still running then would otherwise finish after the
        reloaded entry's first one and show the panel the old entry's data.
        """
        self._unloaded = True
        await super().async_shutdown()

    @callback
    def async_update_listeners(self) -> None:
        """Update entities, then tell the panel, which outlives entry reloads."""
        if self._unloaded:
            return
        super().async_update_listeners()
        if self.data is not None:
            async_dispatcher_send(self.hass, SIGNAL_DATA_UPDATED, self.data)

    async def _async_prices(
        self, now: datetime, start: datetime, end: datetime
    ) -> Prices:
        """Return prices for [start, end), the local today and tomorrow.

        Only delivery days that are missing and may be published are fetched.
        A failed fetch leaves the day out until a later refresh, so the rest
        of the data still updates.
        """
        days = delivery_days(start, end)
        self._prices_by_day = {
            day: periods
            for day, periods in self._prices_by_day.items()
            if day >= days[0]
        }
        for day in days:
            if self._prices_by_day.get(day) or not may_be_published(day, now):
                continue
            try:
                self._prices_by_day[day] = await async_get_prices(
                    self.hass, self._nordpool_entry, self._area, day
                )
            except HomeAssistantError as err:
                LOGGER.warning("Could not get Nord Pool prices for %s: %s", day, err)

        return Prices(
            area=self._area,
            currency=self._currency,
            periods=tuple(
                period
                for day in days
                for period in self._prices_by_day.get(day, ())
                if start <= period.start and period.end <= end
            ),
        )

    @callback
    def async_track_hourly(self) -> CALLBACK_TYPE:
        """Refresh every hour at REFRESH_MINUTE. Returns the unsubscribe callback."""

        async def _refresh(_now: datetime) -> None:
            await self.async_refresh()

        return async_track_time_change(
            self.hass, _refresh, minute=REFRESH_MINUTE, second=0
        )
