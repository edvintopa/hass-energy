"""Shared fixtures."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import Recorder, migration
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy.orm.session import Session

from .common import ENERGY_PREFERENCES, HOURLY_KWH, import_hourly_statistics


@pytest.fixture
def recorder_type_checking_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose names HA's recorder only imports for type checking.

    The recorder test fixture autospecs recorder functions, which on Python 3.14
    evaluates their annotations and fails on these names. Undone after each test.
    """
    for module in (migration, recorder_helper):
        monkeypatch.setattr(module, "Recorder", Recorder, raising=False)
        monkeypatch.setattr(module, "Session", Session, raising=False)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_type_checking_names: None,
    recorder_mock: Recorder,
    enable_custom_integrations: None,
) -> None:
    """Load integrations from custom_components/, with the recorder we depend on."""


@pytest.fixture
async def stockholm_afternoon(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> FrozenDateTimeFactory:
    """Stockholm time, frozen at 14:00, after the next day's prices are out."""
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(datetime(2026, 10, 19, 14, tzinfo=ZoneInfo("Europe/Stockholm")))
    return freezer


@pytest.fixture
async def energy_dashboard(
    hass: HomeAssistant, stockholm_afternoon: FrozenDateTimeFactory
) -> None:
    """Configure the Energy dashboard with 10+ days of hourly statistics.

    They end at the frozen afternoon, which is set up first: imported up to
    the real time, they would miss the frozen window once it's 10 days past.
    """
    for statistic_id, kwh_per_hour in HOURLY_KWH.items():
        import_hourly_statistics(hass, statistic_id, kwh_per_hour, hours=250)
    await async_wait_recording_done(hass)

    manager = await async_get_manager(hass)
    await manager.async_update(ENERGY_PREFERENCES)
