// The electricity price line on the energy chart.
// Set `prices` ([{ start: Date, end: Date, price: number }]) and `unit`; the
// chart sets `localize` (it reads the price_line_ strings) and `locale`.
//
// Drawn hourly: centred on the average, scaled so the hourly price furthest
// from it reaches the edge, in the theme's text colour, thinner than the power lines. Its readout shows the 15-minute
// price at the marker, as it's what's paid.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { formatRange, svgEl } = await import(`./timeline.js${version}`);
const { ChartLine, monotonePath, periodPoints } = await import(`./chart-line.js${version}`);

const STYLE = `
  .price-readout .readout-value {
    font-size: 48px;
  }
  .price-line {
    fill: none;
    stroke: var(--chart-text);
    stroke-width: 1.5;
    stroke-linejoin: round;
    stroke-linecap: round;
  }
`;

export class PriceLine extends ChartLine {
  constructor() {
    super();
    this.name = "price";
    this.style = STYLE;
    this.valueKeys = ["price"];
    this._unit = "";
    this._average = 0;
    this.readout.className = "price-readout";
    this.readout.innerHTML = `
      <div class="readout-label"></div>
      <div class="readout-value"><span class="value"></span> <span class="readout-unit"></span></div>
    `;
  }

  set prices(prices) {
    const values = (prices ?? []).map((p) => p.price);
    this._average = values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : 0;
    this._setPeriods(prices);
  }

  set unit(unit) {
    this._unit = unit;
    this._render();
  }

  // This line's strings, under price_line_ in `common`.
  _t(key, values) {
    return this._localize(`price_line_${key}`, values);
  }

  get label() {
    return this._t("label");
  }

  _formatPrice(price) {
    return price.toLocaleString(this._locale, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  _render() {
    this.readout.querySelector(".readout-unit").textContent = this._unit;
  }

  show(time, now) {
    const price = this.periodAt(time);
    const current = this.periodAt(new Date());
    this.readout.querySelector(".readout-label").textContent =
      price && (now || price === current)
        ? this._t("price")
        : price
          ? this._t("price_at", { period: formatRange(price.start, price.end, this._locale, { weekday: true }) })
          : "";
    this.readout.querySelector(".value").textContent = price ? this._formatPrice(price.price) : this._t("no_value");
  }

  dotColor(time) {
    return this.periodAt(time) ? "var(--chart-text)" : null;
  }

  draw({ back, groups, plot }) {
    const { x, width, middle, half } = plot;
    const values = this.hours.map((h) => h.price);
    const reach = Math.max(...values.map((v) => Math.abs(v - this._average))) || 1;
    const y = (value) => middle - ((value - this._average) / reach) * half;

    // With negative prices (any quarter), mark zero so above and below read
    // clearly.
    if (this.periods.some((p) => p.price < 0)) {
      svgEl("line", { class: "grid", x1: 0, x2: width, y1: y(0), y2: y(0) }, back);
    }

    const points = periodPoints(this.hours, x, (p) => y(p.price), width);
    if (!points.length) return null;
    const { d, yAt } = monotonePath(points);
    for (const group of groups) {
      svgEl("path", { class: "price-line", d }, group);
    }
    return yAt;
  }
}
