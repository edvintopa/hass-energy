"""Seed the dev Home Assistant with 14 days of demo energy statistics.

Simulates a house with solar and a battery, imports hourly grid, solar and battery
statistics, and sets them as the Energy dashboard sources (replacing any there).

Usage:
    HA_TOKEN=<long-lived access token> uv run python scripts/seed_demo.py

HA_URL defaults to http://localhost:8123.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
import os
import random
import sys

import aiohttp

DAYS = 14
SOURCE = "hass_energy_demo"
BATTERY_KWH = 10.0
BATTERY_MAX_KW = 3.0

STATS = {
    "grid_import": "Demo grid import",
    "grid_export": "Demo grid export",
    "solar": "Demo solar production",
    "battery_in": "Demo battery charge",
    "battery_out": "Demo battery discharge",
}


def simulate(hours: list[datetime]) -> dict[str, list[float]]:
    """Return kWh per hour for each statistic, balanced so home usage is the load."""
    rng = random.Random(42)
    flows: dict[str, list[float]] = {key: [] for key in STATS}
    clouds: dict[object, float] = {}
    soc = BATTERY_KWH / 2

    for hour in hours:
        local = hour.astimezone()
        h = local.hour + 0.5
        load = 0.4 + rng.uniform(0, 0.2)
        if 6 <= h < 9:
            load += 1.0
        if 17 <= h < 22:
            load += 1.5

        cloud = clouds.setdefault(local.date(), rng.uniform(0.2, 1.0))
        sun = max(0.0, math.sin(math.pi * (h - 7) / 11)) if 7 <= h <= 18 else 0.0
        solar = 4.0 * sun * cloud

        surplus = solar - load
        charge = discharge = 0.0
        if surplus > 0:
            charge = min(surplus, BATTERY_MAX_KW, BATTERY_KWH - soc)
            soc += charge
        else:
            discharge = min(-surplus, BATTERY_MAX_KW, soc - BATTERY_KWH * 0.1)
            discharge = max(discharge, 0.0)
            soc -= discharge

        flows["solar"].append(solar)
        flows["battery_in"].append(charge)
        flows["battery_out"].append(discharge)
        flows["grid_export"].append(max(0.0, surplus - charge))
        flows["grid_import"].append(max(0.0, -surplus - discharge))

    return flows


class Client:
    """Minimal Home Assistant websocket client."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._id = 0

    async def call(self, msg_type: str, **data: object) -> object:
        self._id += 1
        await self._ws.send_json({"id": self._id, "type": msg_type, **data})
        while True:
            msg = await self._ws.receive_json()
            if msg.get("id") == self._id and msg["type"] == "result":
                if not msg["success"]:
                    raise RuntimeError(f"{msg_type} failed: {msg['error']}")
                return msg.get("result")


async def main() -> None:
    token = os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("Set HA_TOKEN to a long-lived access token (HA profile > Security).")
    url = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")

    end = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    hours = [end - timedelta(hours=i) for i in range(DAYS * 24, 0, -1)]
    flows = simulate(hours)

    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(f"{url}/api/websocket") as ws,
    ):
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json())["type"] != "auth_ok":
            sys.exit("Authentication failed; check HA_TOKEN.")
        client = Client(ws)

        for key, name in STATS.items():
            total = 0.0
            stats = []
            for hour, kwh in zip(hours, flows[key], strict=True):
                total += kwh
                stats.append({"start": hour.isoformat(), "state": total, "sum": total})
            await client.call(
                "recorder/import_statistics",
                metadata={
                    "mean_type": 0,
                    "has_sum": True,
                    "name": name,
                    "source": SOURCE,
                    "statistic_id": f"{SOURCE}:{key}",
                    "unit_class": "energy",
                    "unit_of_measurement": "kWh",
                },
                stats=stats,
            )

        # Link any Forecast.Solar entries, which HASS Energy reads its forecast from.
        entries = await client.call("config_entries/get", domain="forecast_solar")
        forecasts = [entry["entry_id"] for entry in entries]
        await client.call(
            "energy/save_prefs",
            energy_sources=[
                {
                    "type": "grid",
                    "stat_energy_from": f"{SOURCE}:grid_import",
                    "stat_energy_to": f"{SOURCE}:grid_export",
                    "cost_adjustment_day": 0,
                },
                {
                    "type": "solar",
                    "stat_energy_from": f"{SOURCE}:solar",
                    "config_entry_solar_forecast": forecasts or None,
                },
                {
                    "type": "battery",
                    "stat_energy_from": f"{SOURCE}:battery_out",
                    "stat_energy_to": f"{SOURCE}:battery_in",
                },
            ],
        )

    # Each hour starts at `hour`, so the last 240 are the sensor's window.
    window = slice(-240, None)
    home = sum(
        sum(flows[key][window]) * sign
        for key, sign in (
            ("grid_import", 1),
            ("solar", 1),
            ("battery_out", 1),
            ("grid_export", -1),
            ("battery_in", -1),
        )
    )
    print(f"Imported {len(hours)} hours ({hours[0]:%Y-%m-%d %H:%M} to {end:%H:%M}).")
    print(f"Expected 10-day average consumption: {home / 240:.3f} kW")
    print(f"Linked {len(forecasts)} Forecast.Solar entries to the solar source.")


if __name__ == "__main__":
    asyncio.run(main())
