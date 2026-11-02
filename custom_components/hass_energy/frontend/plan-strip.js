// Strip of what the battery does over time, one coloured segment per run of
// the same activity, with the hour axis under it. Pointing at the bar shows
// the activity's name next to the pointer.
// Set `plan` ([{ start: Date, end: Date, command, activity }]), `range`
// ({ start: Date, end: Date }, the time the strip spans, matching the chart
// above it) and optionally `localize` (key, values) => string over the
// `common` strings (it reads the plan_strip_ ones), `locale` and
// `dark`. Set `marker` ({ time, now } or null) to where the chart above marks;
// the strip carries its marker on down.
//
// Fires `hover-time` with the time under the pointer, or null when it leaves,
// so the chart above can follow.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const {
  AXIS_HEIGHT,
  TIMELINE_STYLE,
  TimelineElement,
  drawTimeAxis,
  formatRange,
  pastAndFuture,
  svgEl,
  timeScale,
} = await import(`./timeline.js${version}`);

// The bar: its distance from the top (the gap to the chart above, which the
// marker crosses), its height, and the gap left between two activities.
const BAR = { top: 8, height: 12, split: 2 };
// How far, in px, above or below the bar still counts as pointing at it.
const BAR_HIT = 6;
// Where the name sits from the pointer, in px.
const TIP_OFFSET = { x: 12, y: -28 };
// How long, in ms, the name stays after a tap, as a touch leaves as soon as
// the finger lifts.
const TOUCH_TIP_MS = 3000;
// Hold's diagonal hatching on the bar: a stripe of `stripe` px every `size`
// px, turned `angle` degrees.
const HATCH = { stripe: 1.5, size: 4, angle: 45 };

const STYLE = `
  ${TIMELINE_STYLE}
  :host {
    --activity-grid_charge: #a04ad9;
    --activity-solar_charge: #ff9800;
    --activity-discharge: #d03e3e;
    --activity-export: #3e47cc;
    --activity-idle: var(--chart-grid);
    --activity-hatch: var(--chart-text-muted);
  }
  .plot {
    position: relative;
    height: ${BAR.top + BAR.height + AXIS_HEIGHT}px;
  }
  .tip {
    position: absolute;
    top: 0;
    left: 0;
    padding: 2px 8px;
    border-radius: 9px;
    background: var(--chart-text);
    color: var(--card-background-color, #fff);
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    pointer-events: none;
  }
`;

// Back-to-back periods doing the same, merged into runs, drawn as one segment
// and labelled with when they start and end.
function planRuns(plan) {
  const runs = [];
  for (const period of plan) {
    const last = runs[runs.length - 1];
    if (
      last?.activity === period.activity &&
      last.end.getTime() === period.start.getTime()
    ) {
      last.end = period.end;
    } else {
      runs.push({ ...period });
    }
  }
  return runs;
}

class HassEnergyPlanStrip extends TimelineElement {
  constructor() {
    super();
    this._runs = [];
    this._range = null;
    this._marker = null;
    this._locale = undefined;
    this._localize = () => "";
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="plot">
        <svg role="img"></svg>
        <div class="tip" hidden></div>
      </div>
    `;
    this._svg = this.shadowRoot.querySelector("svg");
    this._tip = this.shadowRoot.querySelector(".tip");

    const hover = (time) => this.dispatchEvent(new CustomEvent("hover-time", { detail: { time } }));
    const onPointer = (ev) => {
      if (!this._x) return;
      clearTimeout(this._tipTimer);
      const rect = this._svg.getBoundingClientRect();
      this._pointer = { px: ev.clientX - rect.left, py: ev.clientY - rect.top, width: rect.width };
      hover(this._x.invert(this._pointer.px));
      this._renderTip();
    };
    const clearTip = () => {
      this._pointer = null;
      this._renderTip();
    };
    this._svg.addEventListener("pointermove", onPointer);
    this._svg.addEventListener("pointerdown", onPointer);
    this._svg.addEventListener("pointerleave", (ev) => {
      hover(null);
      if (ev.pointerType === "touch") this._tipTimer = setTimeout(clearTip, TOUCH_TIP_MS);
      else clearTip();
    });
  }

  set plan(plan) {
    this._runs = planRuns(plan ?? []);
    this._requestRender();
  }

  set range(range) {
    this._range = range;
    this._requestRender();
  }

  set marker(marker) {
    this._marker = marker;
    this._renderMarker();
  }

  set localize(localize) {
    this._localize = localize;
    this._requestRender();
  }

  set locale(locale) {
    this._locale = locale;
    this._requestRender();
  }

  set dark(dark) {
    this.toggleAttribute("dark", Boolean(dark));
  }

  _hasData() {
    return this._runs.length > 0;
  }

  // This component's strings, under plan_strip_ in `common`.
  _t(key, values) {
    return this._localize(`plan_strip_${key}`, values);
  }

  _render() {
    // The tooltip is for pointers, so the label also lists every run.
    const runs = this._runs.map((run) =>
      this._t("run", {
        activity: this._t(`activity_${run.activity}`),
        period: formatRange(run.start, run.end, this._locale, { weekday: true }),
      })
    );
    const list = new Intl.ListFormat(this._locale, { type: "unit" }).format(runs);
    this._svg.setAttribute("aria-label", runs.length ? this._t("described", { runs: list }) : this._t("label"));
    this._renderPlot();
    this._scheduleTick();
  }

  _renderPlot() {
    const svg = this._svg;
    const { width } = svg.getBoundingClientRect();
    svg.replaceChildren();
    this._x = null;
    this._markerLine = null;
    const runs = this._runs;
    if (!width || !runs.length || !this._range) {
      this._renderMarker();
      this._renderTip();
      return;
    }

    const x = timeScale(this._range, width);
    this._x = x;
    const barBottom = BAR.top + BAR.height;

    // Hold is the idle track with diagonal hatching.
    const defs = svgEl("defs", {}, svg);
    const { stripe, size, angle } = HATCH;
    const hatch = svgEl(
      "pattern",
      { id: "hold-hatch", patternUnits: "userSpaceOnUse", width: size, height: size, patternTransform: `rotate(${angle})` },
      defs
    );
    svgEl("rect", { width: size, height: size, style: "fill: var(--activity-idle)" }, hatch);
    svgEl("rect", { width: stripe, height: size, style: "fill: var(--activity-hatch)" }, hatch);

    // Dim what's already happened, up to now.
    const groups = pastAndFuture(svg, defs, { split: x(Date.now()), width, height: barBottom, id: "bar-clip" });
    for (const group of groups) {
      runs.forEach((run, k) => {
        // Leave a gap where one activity meets the next.
        const x0 = x(run.start) + (k > 0 ? BAR.split / 2 : 0);
        const x1 = x(run.end) - (k < runs.length - 1 ? BAR.split / 2 : 0);
        if (x1 <= x0) return;
        svgEl(
          "rect",
          {
            x: x0,
            y: BAR.top,
            width: x1 - x0,
            height: BAR.height,
            rx: 3,
            fill: run.activity === "hold" ? "url(#hold-hatch)" : `var(--activity-${run.activity})`,
          },
          group
        );
      });
    }

    drawTimeAxis(svg, {
      range: this._range,
      width,
      top: barBottom,
      localize: this._localize,
      locale: this._locale,
    });

    // The chart's marker, carried on down through the bar. It starts a pixel
    // early so no seam shows between the two.
    this._markerLine = svgEl("line", { class: "marker-line", y1: -1, y2: barBottom }, svg);
    this._renderMarker();
    this._renderTip();
  }

  // Shows the name of the activity under the pointer next to it while it's
  // over the bar, kept inside the strip. Also called after a redraw, so the
  // name follows new data under a still pointer.
  _renderTip() {
    const pointer = this._pointer;
    const time = pointer && this._x?.invert(pointer.px);
    const run = time && this._runs.find((r) => r.start <= time && time < r.end);
    const overBar = pointer && pointer.py >= BAR.top - BAR_HIT && pointer.py <= BAR.top + BAR.height + BAR_HIT;
    this._tip.hidden = !run || !overBar;
    if (this._tip.hidden) return;
    this._tip.textContent = this._t(`activity_${run.activity}`);
    const { px, width } = pointer;
    const left = Math.max(0, Math.min(px + TIP_OFFSET.x, width - this._tip.offsetWidth));
    this._tip.style.transform = `translate(${left}px, ${BAR.top + TIP_OFFSET.y}px)`;
  }

  // Moves the marker.
  _renderMarker() {
    const marker = this._marker;
    const line = this._markerLine;
    if (!line) return;
    if (!marker) {
      line.setAttribute("visibility", "hidden");
      return;
    }
    const cx = this._x(marker.time);
    line.setAttribute("x1", cx);
    line.setAttribute("x2", cx);
    line.setAttribute("visibility", "visible");
  }

}

customElements.define("hass-energy-plan-strip", HassEnergyPlanStrip);
