"""Sidebar panel: serves the frontend and registers it with Home Assistant."""

from __future__ import annotations

import hashlib
from pathlib import Path

from aiohttp import web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ICON, PANEL_TITLE, PANEL_URL_PATH

FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_URL = f"/{DOMAIN}_frontend"
WEBCOMPONENT_NAME = "hass-energy-panel"


class FrontendView(HomeAssistantView):
    """Serves the panel's JS modules.

    HA's service worker caches every file and serves its copy first, so the
    panel loads modules with a ?v=<hash of the files> that changes with them.
    `Cache-Control: no-cache` makes the worker's refresh revalidate (a cheap
    304) instead of reusing a copy the browser guessed was still fresh.
    """

    url = FRONTEND_URL + "/{filename}"
    name = f"{DOMAIN}:frontend"
    # Browsers load module scripts without HA's auth header.
    requires_auth = False

    def __init__(self, files: frozenset[str]) -> None:
        """Serve only these files from FRONTEND_DIR."""
        self._files = files

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        """Return one frontend file."""
        if filename not in self._files:
            raise web.HTTPNotFound
        return web.FileResponse(
            FRONTEND_DIR / filename, headers={"Cache-Control": "no-cache"}
        )


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the frontend files. HA can't unregister a view, so do this once."""
    files = await hass.async_add_executor_job(
        lambda: frozenset(path.name for path in FRONTEND_DIR.glob("*.js"))
    )
    hass.http.register_view(FrontendView(files))


def _frontend_hash() -> str:
    """Hash the frontend files, to version their URLs."""
    digest = hashlib.sha256()
    for path in sorted(FRONTEND_DIR.glob("*.js")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


async def async_register_panel(hass: HomeAssistant) -> None:
    """Add the panel to the sidebar."""
    version = await hass.async_add_executor_job(_frontend_hash)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT_NAME,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        # The panel passes its ?v= on to the modules it imports.
        module_url=f"{FRONTEND_URL}/{WEBCOMPONENT_NAME}.js?v={version}",
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
