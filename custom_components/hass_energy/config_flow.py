"""Config flow for HASS Energy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    ConfigEntrySelector,
    ConfigEntrySelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    Selector,
    SelectSelector,
    SelectSelectorConfig,
)
import probatio

from .const import (
    BATTERY_REQUIRED,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_INPUT_ENERGY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_MAX_DISCHARGE,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_OUTPUT_ENERGY,
    CONF_BATTERY_SOC,
    CONF_BATTERY_WEAR_COST,
    CONF_EXPORT_COMPENSATION,
    CONF_GRID_EXPORT_ENERGY,
    CONF_GRID_EXPORT_POWER,
    CONF_GRID_IMPORT_ENERGY,
    CONF_GRID_POWER,
    CONF_IMPORT_FEE,
    CONF_LOAD_ENERGY,
    CONF_LOAD_POWER,
    CONF_NORDPOOL_AREA,
    CONF_NORDPOOL_ENTRY,
    CONF_PV_POWER,
    CONF_SOLAR_ENERGY,
    CONF_VAT,
    DOMAIN,
    NORDPOOL_DOMAIN,
    PLAN_DEFAULTS,
)


def _sensor(device_class: SensorDeviceClass) -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain="sensor", device_class=device_class)
    )


def _number(
    low: float, high: float, step: float, unit: str | None = None
) -> NumberSelector:
    config = NumberSelectorConfig(
        min=low, max=high, step=step, mode=NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


# The options, by form section: the measured-data sensors, then the battery and
# prices the plan is made with. Prices are per kWh, in the Nord Pool currency.
SECTIONS: dict[str, dict[str, Selector]] = {
    "battery": {
        CONF_BATTERY_SOC: _sensor(SensorDeviceClass.BATTERY),
        CONF_BATTERY_CHARGE_POWER: _sensor(SensorDeviceClass.POWER),
        CONF_BATTERY_DISCHARGE_POWER: _sensor(SensorDeviceClass.POWER),
    },
    "power": {
        CONF_PV_POWER: _sensor(SensorDeviceClass.POWER),
        CONF_LOAD_POWER: _sensor(SensorDeviceClass.POWER),
        CONF_GRID_POWER: _sensor(SensorDeviceClass.POWER),
        CONF_GRID_EXPORT_POWER: _sensor(SensorDeviceClass.POWER),
    },
    "energy": {
        CONF_BATTERY_INPUT_ENERGY: _sensor(SensorDeviceClass.ENERGY),
        CONF_BATTERY_OUTPUT_ENERGY: _sensor(SensorDeviceClass.ENERGY),
        CONF_SOLAR_ENERGY: _sensor(SensorDeviceClass.ENERGY),
        CONF_GRID_IMPORT_ENERGY: _sensor(SensorDeviceClass.ENERGY),
        CONF_GRID_EXPORT_ENERGY: _sensor(SensorDeviceClass.ENERGY),
        CONF_LOAD_ENERGY: _sensor(SensorDeviceClass.ENERGY),
    },
    "battery_settings": {
        CONF_BATTERY_CAPACITY: _number(0.1, 1000, 0.1, UnitOfEnergy.KILO_WATT_HOUR),
        CONF_BATTERY_MIN_SOC: _number(0, 100, 1, PERCENTAGE),
        CONF_BATTERY_MAX_SOC: _number(0, 100, 1, PERCENTAGE),
        CONF_BATTERY_MAX_CHARGE: _number(0.1, 100, 0.1, UnitOfPower.KILO_WATT),
        CONF_BATTERY_MAX_DISCHARGE: _number(0.1, 100, 0.1, UnitOfPower.KILO_WATT),
        CONF_BATTERY_EFFICIENCY: _number(50, 100, 1, PERCENTAGE),
        CONF_BATTERY_WEAR_COST: _number(0, 100, 0.01),
    },
    "prices": {
        CONF_IMPORT_FEE: _number(0, 100, 0.001),
        CONF_VAT: _number(0, 100, 0.1, PERCENTAGE),
        CONF_EXPORT_COMPENSATION: _number(-100, 100, 0.001),
    },
}

OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(name): section(
            probatio.Schema(
                {probatio.Optional(key): selector for key, selector in fields.items()}
            )
        )
        for name, fields in SECTIONS.items()
    }
)


def _sections(options: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Group the stored options by form section, to fill the form with."""
    return {
        name: {key: options[key] for key in fields if key in options}
        for name, fields in SECTIONS.items()
    }


def _errors(options: Mapping[str, Any]) -> dict[str, str]:
    """Return why the battery set can't be planned, as form errors.

    Without these, the plan would just stay empty with no word why.
    """
    required = [key in options for key in BATTERY_REQUIRED]
    if any(required) and not all(required):
        return {"base": "battery_incomplete"}
    settings = {**PLAN_DEFAULTS, **options}
    if settings[CONF_BATTERY_MIN_SOC] >= settings[CONF_BATTERY_MAX_SOC]:
        return {"base": "soc_range"}
    return {}


def _options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the form's sections into the options, leaving out empty fields."""
    return {
        key: value
        for name in SECTIONS
        for key, value in user_input.get(name, {}).items()
        if value not in (None, "")
    }


class HassEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HassEnergyOptionsFlow:
        """Change the sensors, battery and prices after setup."""
        return HassEnergyOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the Nord Pool entry and area that prices come from."""
        nordpool_entries = [
            entry
            for entry in self.hass.config_entries.async_entries(NORDPOOL_DOMAIN)
            if entry.data.get("areas")
        ]
        if not nordpool_entries:
            return self.async_abort(reason="nordpool_missing")

        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(
                user_input[CONF_NORDPOOL_ENTRY]
            )
            area = user_input[CONF_NORDPOOL_AREA]
            if entry is None or area not in entry.data.get("areas", []):
                errors[CONF_NORDPOOL_AREA] = "area_not_in_entry"
            else:
                self._data = user_input
                return await self.async_step_sensors()

        areas = sorted(
            {area for entry in nordpool_entries for area in entry.data["areas"]}
        )
        default = nordpool_entries[0]
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_NORDPOOL_ENTRY, default=default.entry_id
                ): ConfigEntrySelector(
                    ConfigEntrySelectorConfig(integration=NORDPOOL_DOMAIN)
                ),
                probatio.Required(
                    CONF_NORDPOOL_AREA, default=default.data["areas"][0]
                ): SelectSelector(SelectSelectorConfig(options=areas)),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the sensors measured data is read from, and the battery and prices."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _options(user_input)
            if not (errors := _errors(options)):
                return self.async_create_entry(
                    title=f"HASS Energy {self._data[CONF_NORDPOOL_AREA]}",
                    data=self._data,
                    options=options,
                )
        return self.async_show_form(
            step_id="sensors",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, user_input),
            errors=errors,
        )


class HassEnergyOptionsFlow(OptionsFlowWithReload):
    """Change the sensors, battery and prices; the entry reloads with them."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the sensors, battery and prices as they're set now."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _options(user_input)
            if not (errors := _errors(options)):
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input or _sections(self.config_entry.options)
            ),
            errors=errors,
        )
