# HASS Energy

A Home Assistant custom integration that plans home battery charging and discharging from electricity prices (Nordpool) and PV production forecasts.

> **Status:** rebuilt from scratch, step by step. Right now the integration adds one sensor, `sensor.hass_energy_average_consumption_10_days`: the average home consumption per hour (kW) over the last 10 days, computed from the sources set up in the Energy dashboard (grid import + solar + battery discharge − grid export − battery charge).

## Installation

### HACS (custom repository)
1. HACS → ⋮ → *Custom repositories* → add this repo's URL, category **Integration**.
2. Install **HASS Energy** and restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **HASS Energy**.

### Manual
Copy `custom_components/hass_energy` into your Home Assistant `config/custom_components/` folder, then restart.

## Development

Requires [uv](https://docs.astral.sh/uv/). It installs the right Python (3.14) automatically.

```bash
scripts/setup     # install Home Assistant + dev tooling into .venv
scripts/develop   # run a dev Home Assistant at http://localhost:8123
scripts/lint      # ruff format + ruff check --fix
uv run pytest     # tests with coverage
```

Locally, `uv sync` also installs the `hass-dev` dependency group. It holds C extensions that core integrations need when you run the dev instance. CI doesn't install it. On macOS, if building them fails with `tapi error: ... unknown architecture`, your Xcode and Command Line Tools versions don't match. Run setup with `SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"` pointing at Xcode's SDK, or update both so they match.

### CI
GitHub Actions runs ruff, pytest, [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) and the [HACS action](https://hacs.xyz/docs/publish/action/) on every push and PR. Pushing a tag `vX.Y.Z` that matches `manifest.json`'s `version` creates a GitHub release.
