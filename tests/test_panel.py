"""Tests for the sidebar panel."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.hass_energy import async_unload_entry
from custom_components.hass_energy.const import PANEL_URL_PATH

from .common import async_setup_integration


async def test_panel_registered_and_removed(hass: HomeAssistant) -> None:
    """The panel is in the sidebar while the entry is loaded."""
    entry = await async_setup_integration(hass)
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.sidebar_title == "HASS Energy"
    assert panel.config["_panel_custom"]["name"] == "hass-energy-panel"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]


async def test_panel_module_served(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The panel's JS module is served at its registered URL."""
    await async_setup_integration(hass)
    module_url = hass.data[DATA_PANELS][PANEL_URL_PATH].config["_panel_custom"][
        "module_url"
    ]

    # Versioned by the files' content, so HA's service worker can't serve an
    # old copy after a change.
    assert "?v=" in module_url

    client = await hass_client()
    response = await client.get(module_url)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-cache"
    assert 'customElements.define("hass-energy-panel"' in await response.text()


async def test_frontend_serves_only_js_modules(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Other files in the integration and unknown names are not served."""
    await async_setup_integration(hass)
    client = await hass_client()

    for path in ("manifest.json", "panel.py", "missing.js"):
        response = await client.get(f"/hass_energy_frontend/{path}")
        assert response.status == 404, path


async def test_panel_kept_when_unload_fails(hass: HomeAssistant) -> None:
    """The panel stays while the entry stays loaded."""
    entry = await async_setup_integration(hass)

    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await async_unload_entry(hass, entry)
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]

    # Unload for real so nothing lingers after the test.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("language", "price", "today"), [("en", "Price", "Today"), ("sv", "Pris", "Idag")]
)
async def test_panel_translations(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    language: str,
    price: str,
    today: str,
) -> None:
    """The panel's strings load in the user's language."""
    await async_setup_integration(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": "frontend/get_translations",
            "language": language,
            "category": "common",
            "integration": ["hass_energy"],
        }
    )
    response = await client.receive_json()

    resources = response["result"]["resources"]
    assert resources["component.hass_energy.common.price_line_price"] == price
    assert (
        resources["component.hass_energy.common.price_line_price_at"]
        == f"{price} {{period}}"
    )
    assert resources["component.hass_energy.common.timeline_today"] == today
