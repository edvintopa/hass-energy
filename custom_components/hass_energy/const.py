"""Constants for HASS Energy."""

from datetime import timedelta
import logging

DOMAIN = "hass_energy"
LOGGER = logging.getLogger(__package__)

# The Nord Pool entry and area that prices come from.
NORDPOOL_DOMAIN = "nordpool"
CONF_NORDPOOL_ENTRY = "nordpool_entry"
CONF_NORDPOOL_AREA = "nordpool_area"

# The sensors measured data is read from, in the entry's options. All optional.
CONF_BATTERY_SOC = "battery_soc"
CONF_BATTERY_CHARGE_POWER = "battery_charge_power"
CONF_BATTERY_DISCHARGE_POWER = "battery_discharge_power"
CONF_PV_POWER = "pv_power"
CONF_LOAD_POWER = "load_power"
CONF_GRID_POWER = "grid_power"
CONF_GRID_EXPORT_POWER = "grid_export_power"
CONF_BATTERY_INPUT_ENERGY = "battery_input_energy"
CONF_BATTERY_OUTPUT_ENERGY = "battery_output_energy"
CONF_SOLAR_ENERGY = "solar_energy"
CONF_GRID_IMPORT_ENERGY = "grid_import_energy"
CONF_GRID_EXPORT_ENERGY = "grid_export_energy"
CONF_LOAD_ENERGY = "load_energy"

# The battery and prices the plan is made with, in the entry's options. All
# optional, but there's no plan without the capacity and maximum powers.
CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_MAX_SOC = "battery_max_soc"
CONF_BATTERY_MAX_CHARGE = "battery_max_charge"
CONF_BATTERY_MAX_DISCHARGE = "battery_max_discharge"
CONF_BATTERY_EFFICIENCY = "battery_efficiency"
CONF_BATTERY_WEAR_COST = "battery_wear_cost"
CONF_IMPORT_FEE = "import_fee"
CONF_VAT = "vat"
CONF_EXPORT_COMPENSATION = "export_compensation"
# The battery is only planned with all of these set.
BATTERY_REQUIRED = (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_MAX_CHARGE,
    CONF_BATTERY_MAX_DISCHARGE,
)
# What the plan uses for the ones that aren't set.
PLAN_DEFAULTS = {
    CONF_BATTERY_MIN_SOC: 10.0,
    CONF_BATTERY_MAX_SOC: 100.0,
    # Round trip, in %.
    CONF_BATTERY_EFFICIENCY: 90.0,
    CONF_BATTERY_WEAR_COST: 0.0,
    CONF_IMPORT_FEE: 0.0,
    CONF_VAT: 25.0,
    CONF_EXPORT_COMPENSATION: 0.0,
}

# Sent with the new EnergyData after every refresh, for the panel.
SIGNAL_DATA_UPDATED = f"{DOMAIN}_data_updated"
# Sent with the Measured whose values now were read again, for the panel.
SIGNAL_LIVE_UPDATED = f"{DOMAIN}_live_updated"
# Sent when the Energy dashboard's preferences are saved.
SIGNAL_ENERGY_PREFS_UPDATED = f"{DOMAIN}_energy_prefs_updated"

# How far back the average consumption looks.
CONSUMPTION_WINDOW = timedelta(days=10)

# The recorder compiles hourly statistics a few minutes past the hour,
# so refresh after that.
REFRESH_MINUTE = 15
# It compiles 5-minute statistics just after every fifth minute; measured data
# is re-read at this second of those minutes.
MEASURED_REFRESH_SECOND = 30
# The live values are sent at most this often while the sensors change.
LIVE_COOLDOWN = timedelta(seconds=10)

# Sidebar panel.
PANEL_URL_PATH = "hass-energy"
PANEL_TITLE = "HASS Energy"
PANEL_ICON = "mdi:home-battery"
