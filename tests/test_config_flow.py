"""Tests for the config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hass_energy.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_MAX_DISCHARGE,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC,
    CONF_EXPORT_COMPENSATION,
    CONF_IMPORT_FEE,
    CONF_LOAD_POWER,
    CONF_NORDPOOL_AREA,
    CONF_NORDPOOL_ENTRY,
    CONF_PV_POWER,
    CONF_SOLAR_ENERGY,
    CONF_VAT,
    DOMAIN,
    NORDPOOL_DOMAIN,
)

from .common import (
    AREA,
    SENSOR_OPTIONS,
    add_fake_nordpool,
    async_setup_integration,
)


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """Prices, then sensors: the sensors set are stored in the options."""
    nordpool = add_fake_nordpool(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    user_input = {
        CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id,
        CONF_NORDPOOL_AREA: AREA,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors"
    assert set(result["data_schema"].schema) == {
        "battery",
        "power",
        "energy",
        "battery_settings",
        "prices",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "battery": {CONF_BATTERY_SOC: SENSOR_OPTIONS[CONF_BATTERY_SOC]},
            "power": {CONF_PV_POWER: SENSOR_OPTIONS[CONF_PV_POWER]},
            "energy": {CONF_SOLAR_ENERGY: SENSOR_OPTIONS[CONF_SOLAR_ENERGY]},
            "battery_settings": {},
            "prices": {},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"HASS Energy {AREA}"
    assert result["data"] == user_input
    assert result["result"].options == {
        key: SENSOR_OPTIONS[key]
        for key in (CONF_BATTERY_SOC, CONF_PV_POWER, CONF_SOLAR_ENERGY)
    }


async def test_sensors_are_optional(hass: HomeAssistant) -> None:
    """With no sensors picked, the entry has no options."""
    nordpool = add_fake_nordpool(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id, CONF_NORDPOOL_AREA: AREA},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "battery": {},
            "power": {},
            "energy": {},
            "battery_settings": {},
            "prices": {},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options == {}


@pytest.mark.usefixtures("stockholm_afternoon")
async def test_options_flow_changes_sensors(hass: HomeAssistant) -> None:
    """Configure shows the sensors set, saves the new ones and reloads."""
    entry = await async_setup_integration(
        hass, options={CONF_PV_POWER: SENSOR_OPTIONS[CONF_PV_POWER]}
    )
    coordinator = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    power = result["data_schema"].schema["power"].schema.schema
    suggested = {
        str(key): (key.description or {}).get("suggested_value") for key in power
    }
    assert suggested[CONF_PV_POWER] == SENSOR_OPTIONS[CONF_PV_POWER]
    assert suggested[CONF_LOAD_POWER] is None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "battery": {},
            "power": {CONF_LOAD_POWER: SENSOR_OPTIONS[CONF_LOAD_POWER]},
            "energy": {},
            "battery_settings": {},
            "prices": {},
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_LOAD_POWER: SENSOR_OPTIONS[CONF_LOAD_POWER]}
    assert entry.runtime_data is not coordinator


async def test_options_flow_battery_and_prices(hass: HomeAssistant) -> None:
    """The battery and prices are saved, 0 included, and shown again."""
    entry = await async_setup_integration(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "battery": {},
            "power": {},
            "energy": {},
            "battery_settings": {
                CONF_BATTERY_CAPACITY: 10.0,
                CONF_BATTERY_MAX_CHARGE: 5.0,
                CONF_BATTERY_MAX_DISCHARGE: 5.0,
            },
            "prices": {CONF_IMPORT_FEE: 0.75, CONF_VAT: 0.0},
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_BATTERY_CAPACITY: 10.0,
        CONF_BATTERY_MAX_CHARGE: 5.0,
        CONF_BATTERY_MAX_DISCHARGE: 5.0,
        CONF_IMPORT_FEE: 0.75,
        CONF_VAT: 0.0,
    }

    result = await hass.config_entries.options.async_init(entry.entry_id)
    prices = result["data_schema"].schema["prices"].schema.schema
    suggested = {
        str(key): (key.description or {}).get("suggested_value") for key in prices
    }
    assert suggested == {
        CONF_IMPORT_FEE: 0.75,
        CONF_VAT: 0.0,
        CONF_EXPORT_COMPENSATION: None,
    }


async def test_area_must_belong_to_entry(hass: HomeAssistant) -> None:
    """An area from a different Nord Pool entry is refused."""
    nordpool = add_fake_nordpool(hass)
    MockConfigEntry(
        domain=NORDPOOL_DOMAIN, data={"areas": ["SE3"], "currency": "SEK"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id, CONF_NORDPOOL_AREA: "SE3"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NORDPOOL_AREA: "area_not_in_entry"}


async def test_nordpool_required(hass: HomeAssistant) -> None:
    """Without a Nord Pool entry there is nothing to set up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "nordpool_missing"


async def test_only_one_instance(hass: HomeAssistant) -> None:
    """A second setup is refused."""
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_nordpool_without_areas_ignored(hass: HomeAssistant) -> None:
    """A Nord Pool entry with no areas can't be picked."""
    MockConfigEntry(domain=NORDPOOL_DOMAIN, data={"currency": "SEK"}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "nordpool_missing"


async def test_defaults_belong_together(hass: HomeAssistant) -> None:
    """The default area is one of the default Nord Pool entry's areas."""
    nordpool = add_fake_nordpool(hass)
    MockConfigEntry(
        domain=NORDPOOL_DOMAIN, data={"areas": ["DK1"], "currency": "DKK"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == {
        CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id,
        CONF_NORDPOOL_AREA: AREA,
    }


@pytest.mark.parametrize(
    ("battery_settings", "error"),
    [
        ({CONF_BATTERY_CAPACITY: 10.0}, "battery_incomplete"),
        (
            {
                CONF_BATTERY_CAPACITY: 10.0,
                CONF_BATTERY_MAX_CHARGE: 5.0,
                CONF_BATTERY_MAX_DISCHARGE: 5.0,
                CONF_BATTERY_MIN_SOC: 90.0,
                CONF_BATTERY_MAX_SOC: 20.0,
            },
            "soc_range",
        ),
        # The highest charge defaults to 100 %.
        ({CONF_BATTERY_MIN_SOC: 100.0}, "soc_range"),
    ],
)
async def test_battery_that_cant_be_planned(
    hass: HomeAssistant, battery_settings: dict[str, float], error: str
) -> None:
    """A battery that can't be planned isn't saved; the form says why."""
    entry = await async_setup_integration(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        "battery": {},
        "power": {},
        "energy": {},
        "battery_settings": battery_settings,
        "prices": {},
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    settings = result["data_schema"].schema["battery_settings"].schema.schema
    suggested = {
        str(key): (key.description or {}).get("suggested_value") for key in settings
    }
    assert suggested[CONF_BATTERY_CAPACITY] == battery_settings.get(
        CONF_BATTERY_CAPACITY
    )
    assert entry.options == {}


async def test_setup_checks_the_battery(hass: HomeAssistant) -> None:
    """Setup shows the same errors, and keeps what was entered."""
    nordpool = add_fake_nordpool(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NORDPOOL_ENTRY: nordpool.entry.entry_id, CONF_NORDPOOL_AREA: AREA},
    )
    sections = {
        "battery": {},
        "power": {},
        "energy": {},
        "battery_settings": {CONF_BATTERY_MAX_CHARGE: 5.0},
        "prices": {},
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sections)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "battery_incomplete"}

    sections["battery_settings"] = {}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sections)
    assert result["type"] is FlowResultType.CREATE_ENTRY
