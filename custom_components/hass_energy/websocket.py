"""Websocket commands the panel reads its data through."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
import probatio

from .const import DOMAIN, SIGNAL_DATA_UPDATED, SIGNAL_LIVE_UPDATED
from .coordinator import HassEnergyConfigEntry
from .models import (
    EnergyData,
    Measured,
    MeasuredLine,
    PlanPeriod,
    Prices,
    SolarPeriod,
)


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register the commands once; they find the loaded entry per call."""
    websocket_api.async_register_command(hass, websocket_subscribe_data)


def prices_message(prices: Prices) -> dict[str, Any]:
    """Serialize prices for the panel."""
    return {
        "area": prices.area,
        "currency": prices.currency,
        "prices": [
            {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "price": period.price,
            }
            for period in prices.periods
        ],
    }


def solar_message(periods: tuple[SolarPeriod, ...]) -> list[dict[str, Any]]:
    """Serialize the solar forecast for the panel, power in kW."""
    return [
        {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "power": period.power,
        }
        for period in periods
    ]


def measured_line_message(line: MeasuredLine | None, key: str) -> dict[str, Any] | None:
    """Serialize a measured line, its values under `key`; None when not set."""
    if line is None:
        return None
    return {
        "periods": [
            {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                key: period.value,
            }
            for period in line.periods
        ],
        "now": line.now,
    }


def measured_message(measured: Measured) -> dict[str, Any]:
    """Serialize the measured lines: power in kW, the battery's charge in %."""
    return {
        field.name: measured_line_message(
            getattr(measured, field.name), "soc" if field.name == "soc" else "power"
        )
        for field in fields(measured)
    }


def live_message(measured: Measured) -> dict[str, Any]:
    """Serialize only each measured line's value now (None when not set)."""
    return {
        "now": {
            field.name: line.now if (line := getattr(measured, field.name)) else None
            for field in fields(measured)
        }
    }


def plan_message(plan: tuple[PlanPeriod, ...]) -> list[dict[str, Any]]:
    """Serialize the battery plan: power in kW, the battery's charge in %."""
    return [
        {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "command": period.command,
            "power": period.power,
            "solar": period.solar,
            "home": period.home,
            "battery": period.battery,
            "grid": period.grid,
            "soc_start": period.soc_start,
            "soc_end": period.soc_end,
            "cost": period.cost,
            "activity": period.activity,
        }
        for period in plan
    ]


def data_message(data: EnergyData) -> dict[str, Any]:
    """Serialize everything the panel shows."""
    return {
        "prices": prices_message(data.prices),
        "solar": solar_message(data.solar_forecast),
        "measured": measured_message(data.measured),
        "plan": plan_message(data.plan),
    }


@callback
@websocket_api.websocket_command(
    {probatio.Required("type"): f"{DOMAIN}/subscribe_data"}
)
def websocket_subscribe_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Send the panel's data now and after every refresh or new plan, across
    entry reloads.

    Between refreshes, values now that changed are sent on their own, as
    `{now}`.
    """
    entries: list[HassEnergyConfigEntry] = hass.config_entries.async_entries(DOMAIN)
    entry = next((e for e in entries if e.state is ConfigEntryState.LOADED), None)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "HASS Energy is not loaded")
        return

    @callback
    def send(data: EnergyData) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], data_message(data))
        )

    @callback
    def send_live(measured: Measured) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], live_message(measured))
        )

    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_DATA_UPDATED, send),
        async_dispatcher_connect(hass, SIGNAL_LIVE_UPDATED, send_live),
    ]

    @callback
    def _unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])
    send(entry.runtime_data.data)
