// HASS Energy sidebar panel. Plain web components, no build step.
// Home Assistant sets `hass`, `narrow`, `route` and `panel` on it.

// Import siblings with this module's ?v=<hash of the files>: HA's service
// worker serves cached copies first, so a changed file needs a new URL.
const version = new URL(import.meta.url).search;
const { timeRange } = await import(`./timeline.js${version}`);
const { PriceLine } = await import(`./price-line.js${version}`);
const { PowerScale } = await import(`./power-line.js${version}`);
const { SolarLine } = await import(`./solar-line.js${version}`);
const { BatteryLine } = await import(`./battery-line.js${version}`);
const { GridLine } = await import(`./grid-line.js${version}`);
const { HomeLine } = await import(`./home-line.js${version}`);
const { SocLine } = await import(`./soc-line.js${version}`);
await import(`./energy-chart.js${version}`);
await import(`./plan-strip.js${version}`);

const DOMAIN = "hass_energy";
// How often the lines move their measured/estimated split (and, when narrow,
// where they start) to now.
const SPLIT_INTERVAL = 60000;
const QUARTER = 15 * 60000;

// Fetches this integration's `common` strings from translations/<lang>.json
// and returns a localize(key, values) function over them. The keys are grouped
// per component by prefix (panel_, energy_chart_, price_line_, solar_line_,
// battery_line_, grid_line_, home_line_, soc_line_, plan_strip_, timeline_): HA only allows flat keys there.
async function loadLocalize(hass) {
  const { resources } = await hass.callWS({
    type: "frontend/get_translations",
    language: hass.language,
    category: "common",
    integration: [DOMAIN],
  });
  const prefix = `component.${DOMAIN}.common.`;
  return (key, values = {}) =>
    (resources[prefix + key] ?? "").replace(/\{(\w+)\}/g, (match, name) =>
      name in values ? values[name] : match
    );
}

// A measured line's periods before `quarter` (the one that holds now), then
// that quarter with the value now, or as measured so far when that's unknown.
// Empty for a line that isn't set.
function measuredPeriods(line, quarter, key) {
  if (!line) return [];
  const past = line.periods.filter((period) => period.end <= quarter.start);
  const value = line.now ?? line.periods.find((period) => period.start >= quarter.start)?.[key];
  return value == null ? past : [...past, { ...quarter, [key]: value, estimated: false }];
}

// The periods that end after `start`, or all of them when it's null.
function startingAt(periods, start) {
  return start ? periods.filter((period) => period.end > start) : periods;
}

class HassEnergyPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100%;
          background: var(--primary-background-color);
          color: var(--primary-text-color);
        }
        header {
          display: flex;
          align-items: center;
          gap: 8px;
          height: var(--header-height, 56px);
          padding: 0 12px;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, var(--text-primary-color));
          font-size: 20px;
        }
        :host(:not([narrow])) ha-menu-button {
          display: none;
        }
        main {
          padding: 16px;
        }
        .card {
          padding: 16px;
          border-radius: var(--ha-card-border-radius, 12px);
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
        }
        h2 {
          margin: 0 0 12px;
          font-size: 16px;
          font-weight: 500;
          color: var(--secondary-text-color);
        }
        .message {
          margin: 0 0 12px;
          color: var(--secondary-text-color);
        }
      </style>
      <header>
        <ha-menu-button></ha-menu-button>
        <span data-key="panel_title"></span>
      </header>
      <main>
        <section class="card">
          <h2 id="title"></h2>
          <p class="message" hidden></p>
          <hass-energy-chart></hass-energy-chart>
          <hass-energy-plan-strip hidden></hass-energy-plan-strip>
        </section>
      </main>
    `;
    this._menuButton = this.shadowRoot.querySelector("ha-menu-button");
    this._chart = this.shadowRoot.querySelector("hass-energy-chart");
    // The price line is the chart's main one. The power lines share one kW
    // scale, so their sizes compare. The battery's charge goes last, so its
    // fill is under all the others.
    this._priceLine = new PriceLine();
    const power = new PowerScale();
    this._solarLine = new SolarLine(power);
    this._batteryLine = new BatteryLine(power);
    this._gridLine = new GridLine(power);
    this._homeLine = new HomeLine(power);
    this._socLine = new SocLine();
    this._chart.lines = [
      this._priceLine,
      this._solarLine,
      this._batteryLine,
      this._gridLine,
      this._homeLine,
      this._socLine,
    ];
    this._strip = this.shadowRoot.querySelector("hass-energy-plan-strip");
    // The strip marks what the chart marks, and hovering the strip hovers the
    // chart.
    this._chart.addEventListener("marker-change", (ev) => {
      this._strip.marker = ev.detail;
    });
    this._strip.addEventListener("hover-time", (ev) => {
      this._chart.hoverTime = ev.detail.time;
    });
    this._title = this.shadowRoot.getElementById("title");
    this._message = this.shadowRoot.querySelector(".message");
    this._localize = () => "";
    this._prices = [];
    // The solar forecast as last sent, hourly, the battery plan from now on,
    // and the measured lines (null for those whose sensors aren't set).
    this._forecast = [];
    this._plan = [];
    this._measured = {};
    this._area = "";
    // "loading", "loaded" or "error".
    this._status = "loading";

    // HA can set properties before this element is defined. Those land as
    // plain fields that hide the setters below, so re-apply them.
    for (const prop of ["hass", "narrow"]) {
      if (Object.hasOwn(this, prop)) {
        const value = this[prop];
        delete this[prop];
        this[prop] = value;
      }
    }
  }

  connectedCallback() {
    if (this._hass) this._subscribe();
    // Now moves on between backend messages, and with it where the measured
    // past ends and the estimates start.
    this._splitTimer = setInterval(() => this._renderLines(), SPLIT_INTERVAL);
  }

  disconnectedCallback() {
    this._unsubscribe();
    clearInterval(this._splitTimer);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._subscribe();
    this._menuButton.hass = hass;
    this._chart.dark = hass.themes?.darkMode;
    this._strip.dark = hass.themes?.darkMode;
    if (this._language !== hass.language) {
      this._language = hass.language;
      this._chart.locale = hass.language;
      this._strip.locale = hass.language;
      this._loadTranslations(hass);
    }
  }

  // A narrow (mobile) view has no room for the past, so its lines start at
  // the hour that holds now.
  set narrow(narrow) {
    this.toggleAttribute("narrow", narrow);
    this._menuButton.narrow = narrow;
    if (this._narrow === Boolean(narrow)) return;
    this._narrow = Boolean(narrow);
    this._split = undefined;
    this._renderLines();
  }

  async _loadTranslations(hass) {
    const language = hass.language;
    const localize = await loadLocalize(hass);
    // Drop the result if the language changed again while loading.
    if (language !== this._language) return;
    for (const el of this.shadowRoot.querySelectorAll("[data-key]")) {
      el.textContent = localize(el.dataset.key);
    }
    this._localize = localize;
    this._chart.localize = localize;
    this._strip.localize = localize;
    this._renderText();
  }

  // Prices, solar, the measured lines and the plan arrive now and again after
  // every backend refresh or new plan, and when the measured values now change.
  _subscribe() {
    if (this._subscription) return;
    const subscription = this._hass.connection.subscribeMessage(
      (message) => this._onData(message),
      { type: `${DOMAIN}/subscribe_data` }
    );
    this._subscription = subscription;
    subscription.catch((err) => {
      // An attempt that was already dropped must not clear a newer one.
      if (this._subscription !== subscription) return;
      console.error("HASS Energy: could not subscribe to its data", err);
      this._subscription = undefined;
      this._status = "error";
      this._renderText();
    });
  }

  _unsubscribe() {
    const subscription = this._subscription;
    this._subscription = undefined;
    subscription?.then(
      (unsubscribe) => unsubscribe(),
      () => {} // Never subscribed, so nothing to undo.
    );
  }

  // Between refreshes only the measured values now arrive, as { now }.
  _onData(message) {
    if (message.now) {
      for (const [name, now] of Object.entries(message.now)) {
        if (this._measured[name]) this._measured[name].now = now;
      }
      this._split = undefined;
      this._renderLines();
      return;
    }
    const { prices: { area, currency, prices }, solar, measured, plan } = message;
    this._area = area;
    this._status = "loaded";
    this._prices = prices.map(({ start, end, price }) => ({
      start: new Date(start),
      end: new Date(end),
      price,
    }));
    this._currency = currency;
    // The plan from now on. The strip has the hour labels when it's shown,
    // under both.
    this._plan = plan.map(({ start, end, ...values }) => ({
      start: new Date(start),
      end: new Date(end),
      ...values,
    }));
    this._strip.plan = this._plan;
    this._strip.hidden = !this._plan.length;
    this._chart.axis = !this._plan.length;
    this._forecast = solar.map(({ start, end, power }) => ({
      start: new Date(start),
      end: new Date(end),
      power,
      estimated: true,
    }));
    this._measured = Object.fromEntries(
      Object.entries(measured).map(([name, line]) => [
        name,
        line && {
          now: line.now,
          periods: line.periods.map(({ start, end, ...values }) => ({
            start: new Date(start),
            end: new Date(end),
            ...values,
            estimated: false,
          })),
        },
      ])
    );
    this._split = undefined;
    this._renderLines();
    this._renderText();
  }

  // Each line is measured up to the end of the quarter that holds now, which
  // shows the value now. After it, solar is the forecast, and battery, grid,
  // home and the battery's charge are what the plan predicts (all estimated,
  // so dashed; the charge halfway through each period). Lines whose sensors
  // aren't set only have the part after now. When narrow, every line (and so
  // the strip's range) starts at the hour that holds now, a whole hour as the
  // lines draw hourly averages. Nothing is redrawn until the split reaches
  // another quarter (hours start on one), narrow changes or new data arrives.
  _renderLines() {
    const split = (Math.floor(Date.now() / QUARTER) + 1) * QUARTER;
    if (split === this._split) return;
    this._split = split;
    const shownFrom = this._narrow ? new Date(split - QUARTER) : null;
    shownFrom?.setMinutes(0, 0, 0);
    // The lines draw hourly averages and read out these 15-minute periods.
    const prices = startingAt(this._prices, shownFrom);
    this._priceLine.prices = prices;
    // The strip spans the same time as the chart above it.
    this._strip.range = timeRange(prices);
    const quarter = { start: new Date(split - QUARTER), end: new Date(split) };
    const forecast = this._forecast
      .filter((period) => period.end > quarter.end)
      .map((period) => (period.start < quarter.end ? { ...period, start: quarter.end } : period));
    const planned = this._plan.filter((period) => period.start >= quarter.end);
    const flow = (key) =>
      planned.map(({ start, end, [key]: power }) => ({ start, end, power, estimated: true }));
    const { solar, battery, grid, home, soc } = this._measured;
    const measured = (line, key) => startingAt(measuredPeriods(line, quarter, key), shownFrom);
    this._solarLine.solar = [...measured(solar, "power"), ...forecast];
    this._batteryLine.battery = [...measured(battery, "power"), ...flow("battery")];
    this._gridLine.grid = [...measured(grid, "power"), ...flow("grid")];
    this._homeLine.home = [...measured(home, "power"), ...flow("home")];
    this._socLine.soc = [
      ...measured(soc, "soc"),
      ...planned.map(({ start, end, soc_start, soc_end }) => ({
        start,
        end,
        soc: (soc_start + soc_end) / 2,
        estimated: true,
      })),
    ];
  }

  _renderText() {
    this._title.textContent = this._localize("panel_card_title", { area: this._area }).trim();
    this._priceLine.unit = this._currency
      ? this._localize("panel_price_unit", { currency: this._currency })
      : "";
    const message =
      this._status === "error"
        ? this._localize("panel_prices_error")
        : this._status === "loaded" && !this._prices.length
          ? this._localize("panel_no_prices")
          : "";
    this._message.textContent = message;
    this._message.hidden = !message;
  }
}

customElements.define("hass-energy-panel", HassEnergyPanel);
