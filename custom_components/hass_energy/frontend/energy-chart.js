// Chart of lines over time, e.g. the electricity price and solar power.
// Set `lines` (ChartLine objects, see chart-line.js) and optionally
// `localize` (key, values) => string over the `common` strings (it reads the
// energy_chart_ ones, and passes it on to its lines), `locale`, `dark` and
// `axis` (false leaves out the hour labels, for when a strip under the chart
// has them).
//
// The chart owns the plot and the lines draw into it, each on its own scale
// around the plot's middle. The first line is the main one: it sets the time
// range and the periods the arrow keys step through, and is drawn on top.
// Above the plot go each line's readout (its value at the marker), the main
// line's on a row of its own. What's before now is dimmed, and a marker sits on the current
// time, or on the hovered one, with a dot on every line. Pointing at a line
// lights it up along its whole length and grays out the others.
//
// Fires `marker-change` with where the marker is ({ time, now }: the current
// time, or the hovered time) or null, so charts below it can follow. Set
// `hoverTime` to hover a time from outside, or null to stop.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const {
  AXIS_HEIGHT,
  TIMELINE_STYLE,
  TimelineElement,
  drawTimeAxis,
  formatTime,
  pastAndFuture,
  svgEl,
  timeRange,
  timeScale,
} = await import(`./timeline.js${version}`);
const { LINE_STYLE } = await import(`./chart-line.js${version}`);

// Room above the lines for the marker's label, and under them for the hour
// labels (when shown) or the lines' stroke.
const MARGIN = { top: 28, bottom: AXIS_HEIGHT, bottomWithoutAxis: 4 };
// How close, in px, the pointer must be to a line to light it up.
const LINE_HIT = 14;

// The main line's readout has a row of its own. Every other one has a fixed
// width, room for its widest value and label, so changing values never rewrap
// the row: how many fit on it depends only on the chart's width.
const STYLE = `
  ${TIMELINE_STYLE}
  ${LINE_STYLE}
  .readouts {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 8px 24px;
    margin-bottom: 16px;
  }
  .readouts > * {
    flex: 0 0 var(--energy-chart-readout-width, 120px);
    min-width: 0;
  }
  .readouts > :first-child {
    flex-basis: 100%;
  }
  .readouts > [hidden] {
    display: none;
  }
  .plot {
    position: relative;
    height: var(--energy-chart-height, 280px);
  }
  :host([no-axis]) .plot {
    height: calc(var(--energy-chart-height, 280px) - ${AXIS_HEIGHT - MARGIN.bottomWithoutAxis}px);
  }
  svg:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 4px;
    border-radius: 4px;
  }
  .grid {
    stroke: var(--chart-grid);
    stroke-width: 1;
    shape-rendering: crispEdges;
  }
  .series {
    transition: opacity 0.15s, filter 0.15s;
  }
  .series .past {
    transition: opacity 0.15s;
  }
  .series.muted {
    opacity: 0.35;
    filter: grayscale(1);
  }
  .series.active {
    filter: brightness(1.15);
  }
  .series.active .past,
  .series.muted .past {
    opacity: 1;
  }
  .series.active path {
    stroke-width: 3.5;
  }
  .dot {
    stroke: var(--card-background-color, #fff);
    stroke-width: 2;
  }
  .marker-pill rect {
    fill: var(--chart-text);
  }
  .marker-pill text {
    font-size: 11px;
    font-weight: 600;
    fill: var(--card-background-color, #fff);
  }
`;

class HassEnergyChart extends TimelineElement {
  constructor() {
    super();
    this._lines = [];
    this._activeLine = null;
    this._locale = undefined;
    this._localize = () => "";
    this._hoverTime = null;
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="readouts"></div>
      <div class="plot">
        <svg tabindex="0" role="img"></svg>
      </div>
    `;
    this._svg = this.shadowRoot.querySelector("svg");
    // New data keeps the hovered time while the main line still has it, so
    // a refresh doesn't snap the marker back to now mid-read.
    this._onLineChange = () => {
      if (this._hoverTime && !this._main?.periodAt(this._hoverTime)) this._hoverTime = null;
      this._requestRender();
    };

    this._svg.addEventListener("pointermove", (ev) => this._onPointer(ev));
    this._svg.addEventListener("pointerdown", (ev) => this._onPointer(ev));
    // A touch leaves as soon as the finger lifts, so it keeps what it marked
    // until the chart loses focus.
    this._svg.addEventListener("pointerleave", (ev) => {
      if (ev.pointerType !== "touch") this._leave();
    });
    this._svg.addEventListener("blur", () => this._leave());
    this._svg.addEventListener("keydown", (ev) => this._onKey(ev));
  }

  set lines(lines) {
    for (const line of this._lines) {
      line.removeEventListener("change", this._onLineChange);
    }
    this._lines = lines ?? [];
    const root = this.shadowRoot;
    root.querySelectorAll("style.line-style").forEach((el) => el.remove());
    const readouts = root.querySelector(".readouts");
    readouts.replaceChildren();
    for (const line of this._lines) {
      const style = document.createElement("style");
      style.className = "line-style";
      style.textContent = line.style;
      root.insertBefore(style, readouts);
      readouts.append(line.readout);
      line.localize = this._localize;
      line.locale = this._locale;
      line.addEventListener("change", this._onLineChange);
    }
    this._requestRender();
  }

  set localize(localize) {
    this._localize = localize;
    for (const line of this._lines) line.localize = localize;
    this._render();
  }

  set locale(locale) {
    this._locale = locale;
    for (const line of this._lines) line.locale = locale;
    this._render();
  }

  set dark(dark) {
    this.toggleAttribute("dark", Boolean(dark));
  }

  set axis(axis) {
    this.toggleAttribute("no-axis", !axis);
    this._requestRender();
  }

  set hoverTime(time) {
    this._setHovered(time);
  }

  // The main line: it sets the range and the periods keys step through.
  get _main() {
    return this._lines[0];
  }

  _hasData() {
    return Boolean(this._main?.periods.length);
  }

  // This component's strings, under energy_chart_ in `common`.
  _t(key, values) {
    return this._localize(`energy_chart_${key}`, values);
  }

  _render() {
    const shown = this._lines.filter((line) => line.periods.length);
    for (const line of this._lines) {
      line.readout.hidden = !line.periods.length;
    }
    const labels = new Intl.ListFormat(this._locale, { type: "conjunction" });
    this._svg.setAttribute("aria-label", labels.format(shown.map((line) => line.label)));
    this._renderPlot();
    this._scheduleTick();
  }

  _renderPlot() {
    const svg = this._svg;
    const { width, height } = svg.getBoundingClientRect();
    svg.replaceChildren();
    this._layout = null;
    this._marker = null;
    // One "now" per render, so the marker's place only changes on a render.
    this._now = new Date();
    if (!width || !height || !this._hasData()) {
      this._setHovered(this._hoverTime);
      return;
    }

    // The lines use the full width; there's no y-axis.
    const axis = !this.hasAttribute("no-axis");
    const plotHeight = height - MARGIN.top - (axis ? MARGIN.bottom : MARGIN.bottomWithoutAxis);
    const baseline = MARGIN.top + plotHeight;
    const range = timeRange(this._main.periods);
    const x = timeScale(range, width);
    const plot = {
      x,
      width,
      top: MARGIN.top,
      middle: MARGIN.top + plotHeight / 2,
      half: plotHeight / 2,
      baseline,
      clipHeight: baseline + 4,
    };

    // Each line in its own group, to light up or gray out, with what's before
    // now dimmed. The main line goes last, on top.
    const defs = svgEl("defs", {}, svg);
    const back = svgEl("g", {}, svg);
    const split = x(this._now);
    const series = [];
    for (const line of [...this._lines].reverse()) {
      if (!line.periods.length) continue;
      const group = svgEl("g", { class: "series" }, svg);
      const groups = pastAndFuture(group, defs, { split, width, height: plot.clipHeight, id: `${line.name}-clip` });
      const yAt = line.draw({ back, defs, groups, plot });
      if (yAt) series.push({ line, group, yAt });
      else group.remove();
    }

    if (axis) {
      drawTimeAxis(svg, { range, width, top: baseline, localize: this._localize, locale: this._locale });
    }

    // Marker on the hovered time, or on now: a line from a label at the top
    // down to the baseline (or on to the bottom, to meet the strip below),
    // with a dot on each line.
    const markerLine = svgEl("line", { class: "marker-line", y1: 20, y2: axis ? baseline : height }, svg);
    const dots = series.map((s) => ({ series: s, el: svgEl("circle", { class: "dot", r: 5 }, svg) }));
    const pill = svgEl("g", { class: "marker-pill" }, svg);
    const pillRect = svgEl("rect", { y: 2, height: 18, rx: 9 }, pill);
    const pillText = svgEl("text", { y: 11, "text-anchor": "middle", "dominant-baseline": "middle" }, pill);
    this._marker = { line: markerLine, dots, pill, pillRect, pillText };
    this._layout = { width, x, series };
    this._setActiveLine(this._activeLine);

    this._setHovered(this._hoverTime);
  }

  _onPointer(ev) {
    if (!this._layout) return;
    const rect = this._svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    // Light up the line nearest the pointer, if it's close enough.
    let nearest = null;
    let nearestDistance = LINE_HIT;
    for (const { line, yAt } of this._layout.series) {
      const y = yAt(px);
      if (y === null) continue;
      const distance = Math.abs(y - py);
      if (distance <= nearestDistance) {
        nearest = line.name;
        nearestDistance = distance;
      }
    }
    this._setActiveLine(nearest);
    this._setHovered(this._layout.x.invert(px));
  }

  _leave() {
    this._setActiveLine(null);
    this._setHovered(null);
  }

  // Lights up the line called `name` and grays out the rest, or shows them
  // all as usual when it's null.
  _setActiveLine(name) {
    this._activeLine = name;
    for (const { line, group } of this._layout?.series ?? []) {
      group.classList.toggle("active", line.name === name);
      group.classList.toggle("muted", name !== null && line.name !== name);
    }
  }

  // Arrow keys step through the main line's periods, marking the middle of
  // each.
  _onKey(ev) {
    const periods = this._main?.periods ?? [];
    const indexAt = (time) => periods.findIndex((p) => p.start <= time && time < p.end);
    const last = periods.length - 1;
    const hovered = this._hoverTime ? indexAt(this._hoverTime) : -1;
    const from = hovered >= 0 ? hovered : Math.max(0, indexAt(Date.now()));
    const moves = { ArrowLeft: from - 1, ArrowRight: from + 1, Home: 0, End: last };
    if (!(ev.key in moves) || last < 0) return;
    ev.preventDefault();
    const index = Math.min(last, Math.max(0, hovered < 0 ? from : moves[ev.key]));
    const { start, end } = periods[index];
    this._setHovered(new Date((start.getTime() + end.getTime()) / 2));
  }

  // Marks `time` and shows each line's value there, or now when it's null.
  _setHovered(time) {
    this._hoverTime = time;
    const isNow = !time;
    const markerTime = time ?? this._now ?? new Date();
    for (const line of this._lines) line.show(markerTime, isNow);
    const shown = this._main?.periodAt(markerTime);

    // Tell the charts below, only when the marker moved.
    const detail = shown ? { time: markerTime, now: isNow } : null;
    const key = detail && `${detail.time.getTime()} ${detail.now}`;
    if (key !== this._markerKey) {
      this._markerKey = key;
      this.dispatchEvent(new CustomEvent("marker-change", { detail }));
    }

    const marker = this._marker;
    if (!this._layout || !marker) return;
    const visibility = shown ? "visible" : "hidden";
    marker.line.setAttribute("visibility", visibility);
    marker.pill.setAttribute("visibility", visibility);
    if (!shown) {
      for (const dot of marker.dots) dot.el.setAttribute("visibility", "hidden");
      return;
    }

    const { width, x } = this._layout;
    const cx = x(markerTime);
    marker.line.setAttribute("x1", cx);
    marker.line.setAttribute("x2", cx);
    for (const { series, el } of marker.dots) {
      const color = series.line.dotColor(markerTime);
      const cy = color && series.yAt(cx);
      el.setAttribute("visibility", cy == null ? "hidden" : "visible");
      if (cy == null) continue;
      el.setAttribute("cx", cx);
      el.setAttribute("cy", cy);
      el.setAttribute("fill", color);
    }

    // Label "Now", or the hovered time; kept inside the chart.
    marker.pillText.textContent = isNow ? this._t("now") : formatTime(markerTime, this._locale);
    const pillWidth = (marker.pillText.getComputedTextLength() || 24) + 16;
    const px = Math.min(Math.max(cx - pillWidth / 2, 0), width - pillWidth);
    marker.pillRect.setAttribute("x", px);
    marker.pillRect.setAttribute("width", pillWidth);
    marker.pillText.setAttribute("x", px + pillWidth / 2);
  }
}

customElements.define("hass-energy-chart", HassEnergyChart);
