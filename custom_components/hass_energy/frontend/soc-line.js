// The battery's state of charge on the energy chart.
// Set `soc` ([{ start: Date, end: Date, soc: %, estimated: boolean }]); the
// chart sets `localize` (it reads the soc_line_ strings) and `locale`.
//
// Unlike the power lines it isn't centred on the middle: it has a fixed
// 0–100 % scale from the plot's baseline to its top, so a level always sits
// at the same height, and a fill under it down to the baseline. Drawn hourly,
// solid where measured and dashed from the first estimated period on. Its
// readout shows the 15-minute charge at the marker.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { svgEl } = await import(`./timeline.js${version}`);
const { ChartLine, badgeStyle, drawSplitPath, lineStrokeStyle, monotonePath, periodPoints } = await import(`./chart-line.js${version}`);

// The fill's opacity at the top of the plot and at the baseline.
const FILL_TOP = 0.3;
const FILL_BOTTOM = 0.03;

export class SocLine extends ChartLine {
  constructor() {
    super();
    this.name = "soc";
    this.valueKeys = ["soc"];
    this.style = `
      :host {
        --soc: #62c355;
      }
      ${lineStrokeStyle("soc-line", "var(--soc)")}
      .soc-fill {
        stroke: none;
      }
      .soc-readout .readout-value {
        ${badgeStyle("var(--soc)")}
      }
    `;
    this.readout.className = "soc-readout";
    this.readout.innerHTML = `
      <div class="readout-label" data-key="name"></div>
      <div class="readout-value"><span class="value"></span> <span class="readout-unit" data-key="unit"></span></div>
    `;
  }

  set soc(soc) {
    this._setPeriods(soc);
  }

  // This line's strings, under soc_line_ in `common`.
  _t(key, values) {
    return this._localize(`soc_line_${key}`, values);
  }

  get label() {
    return this._t("label");
  }

  _render() {
    for (const el of this.readout.querySelectorAll("[data-key]")) {
      el.textContent = this._t(el.dataset.key);
    }
  }

  show(time) {
    const period = this.periodAt(time);
    this.readout.querySelector(".value").textContent = period
      ? Math.round(period.soc).toLocaleString(this._locale)
      : this._t("no_value");
  }

  dotColor(time) {
    return this.periodAt(time) ? "var(--soc)" : null;
  }

  draw({ defs, groups, plot }) {
    const { x, width, top, baseline } = plot;
    const y = (soc) => baseline - (Math.min(100, Math.max(0, soc)) / 100) * (baseline - top);
    const points = periodPoints(this.hours, x, (p) => y(p.soc), width);
    if (!points.length) return null;
    const { d, yAt } = monotonePath(points);

    // The fill fades from the top of the plot down to the baseline.
    const gradient = svgEl(
      "linearGradient",
      { id: "soc-fill-gradient", gradientUnits: "userSpaceOnUse", x1: 0, x2: 0, y1: top, y2: baseline },
      defs
    );
    for (const [offset, opacity] of [
      [0, FILL_TOP],
      [1, FILL_BOTTOM],
    ]) {
      svgEl("stop", { offset, style: `stop-color: var(--soc); stop-opacity: ${opacity}` }, gradient);
    }
    // The fill closes down to the baseline under the line's own ends.
    const fill = `${d} L${points[points.length - 1].x},${baseline} L${points[0].x},${baseline} Z`;
    for (const group of groups) {
      // The gradient is set as an attribute: url(#…) in a shadow-root
      // stylesheet doesn't resolve in every browser.
      svgEl("path", { class: "soc-fill", d: fill, fill: "url(#soc-fill-gradient)" }, group);
    }
    drawSplitPath({ defs, groups, d, name: this.name, cls: "soc-line", periods: this.periods, plot });
    return yAt;
  }
}
