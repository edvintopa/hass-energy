// What the power lines on the energy chart share (solar, battery, grid, home).
// Subclasses set the periods ([{ start: Date, end: Date, power: kW,
// estimated: boolean }]) through `_setPeriods`; the chart sets `localize`
// (each line reads the strings under its own prefix) and `locale`.
//
// Signs: up is a source feeding the home, down is power leaving it, so
// solar + battery + grid = home at any moment (see the line modules).
//
// Drawn hourly, centred on zero so it can go negative, on a `PowerScale` that
// lines can share: the hourly power furthest from zero over all its lines
// reaches the edge, so their sizes compare. One colour (`--<name>`), solid where
// measured and dashed from the first estimated period on. Its readout shows
// the 15-minute power at the marker, and for lines with `directions`, which way it
// flows.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { ChartLine, badgeStyle, drawSplitPath, lineStrokeStyle, monotonePath, periodPoints } = await import(`./chart-line.js${version}`);

// Room beyond the furthest power from zero, as a fraction of it.
const HEADROOM = 0.1;
// Below this many kW either way, a line with directions counts as idle.
const IDLE_KW = 0.05;

/** A kW scale centred on zero, shared by the power lines added to it. */
export class PowerScale {
  constructor() {
    this._lines = [];
    this._reach = null;
  }

  add(line) {
    this._lines.push(line);
    line.addEventListener("change", () => {
      this._reach = null;
    });
  }

  // The power at the plot's edge, worked out again only after a line's data
  // changes.
  reach() {
    if (this._reach === null) {
      const powers = this._lines.flatMap((line) => line.hours.map((h) => Math.abs(h.power)));
      this._reach = Math.max(0, ...powers) * (1 + HEADROOM) || 1;
    }
    return this._reach;
  }
}

/**
 * Base for a line of power in kW. `name` names its classes, clip paths and
 * colour variable; `prefix` its strings in `common` (e.g. "solar_line_");
 * `color` its colour (in light and dark mode); `scale` the `PowerScale` it shares
 * (its own by default). The readout is labelled with the line's `name`
 * string; `directions` ({ up, down }, string keys) labels it with which way
 * the power flows instead (or `idle`), and shows the power unsigned. The
 * chart's marker already says whether it's now, and the dashes whether it's
 * estimated, so the label doesn't.
 */
export class PowerLine extends ChartLine {
  constructor({ name, prefix, color, scale = new PowerScale(), directions }) {
    super();
    this.name = name;
    this.valueKeys = ["power"];
    this._prefix = prefix;
    this._directions = directions;
    this.scale = scale;
    scale.add(this);
    this.style = `
      :host {
        --${name}: ${color};
      }
      ${lineStrokeStyle(`${name}-line`, `var(--${name})`)}
      .${name}-readout .readout-value {
        ${badgeStyle(`var(--${name})`)}
      }
    `;
    this.readout.className = `${name}-readout`;
    this.readout.innerHTML = `
      <div class="readout-label"></div>
      <div class="readout-value"><span class="value"></span> <span class="readout-unit" data-key="power_unit"></span></div>
    `;
  }

  // This line's strings, under its prefix in `common`.
  _t(key, values) {
    return this._localize(`${this._prefix}${key}`, values);
  }

  get label() {
    return this._t("label");
  }

  _render() {
    for (const el of this.readout.querySelectorAll("[data-key]")) {
      el.textContent = this._t(el.dataset.key);
    }
  }

  // The readout's label for `period` (maybe none).
  _labelFor(period) {
    if (!this._directions || !period) return this._t("name");
    const { up, down } = this._directions;
    return this._t(period.power >= IDLE_KW ? up : period.power <= -IDLE_KW ? down : "idle");
  }

  // The power the readout shows for `period`: unsigned when the label says
  // the direction.
  _shownPower(period) {
    return this._directions ? Math.abs(period.power) : period.power;
  }

  // The power at the marker's time rather than over a price period, to match
  // the dot.
  show(time) {
    const period = this.periodAt(time);
    this.readout.querySelector(".readout-label").textContent = this._labelFor(period);
    this.readout.querySelector(".value").textContent = period
      ? this._shownPower(period).toLocaleString(this._locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 })
      : this._t("no_value");
  }

  dotColor(time) {
    return this.periodAt(time) ? `var(--${this.name})` : null;
  }

  draw({ defs, groups, plot }) {
    const { x, width, middle, half } = plot;
    const reach = this.scale.reach();
    const points = periodPoints(this.hours, x, (p) => middle - (p.power / reach) * half, width);
    if (!points.length) return null;
    const { d, yAt } = monotonePath(points);

    // Solid up to the first estimated 15-minute period, dashed from there.
    drawSplitPath({ defs, groups, d, name: this.name, cls: `${this.name}-line`, periods: this.periods, plot });
    return yAt;
  }
}
