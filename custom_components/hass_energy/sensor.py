"""Sensors for HASS Energy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import HassEnergyConfigEntry
from .entity import HassEnergyEntity
from .models import EnergyData

# All data comes from the coordinator, so entities never update on their own.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HassEnergySensorEntityDescription(SensorEntityDescription):
    """Describes a HASS Energy sensor."""

    value_fn: Callable[[EnergyData], StateType]


SENSORS: tuple[HassEnergySensorEntityDescription, ...] = (
    HassEnergySensorEntityDescription(
        key="average_consumption",
        translation_key="average_consumption",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
        value_fn=lambda data: _round(data.average_consumption),
    ),
)


def _round(value: float | None) -> float | None:
    """Round for the state; history and automations don't need float noise."""
    return None if value is None else round(value, 3)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HassEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        HassEnergySensor(coordinator, description) for description in SENSORS
    )


class HassEnergySensor(HassEnergyEntity, SensorEntity):
    """A sensor whose value is read from the coordinator's data."""

    entity_description: HassEnergySensorEntityDescription

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)
