// Shared pieces for the charts that run along the same time span (the price
// chart and the plan strip): SVG helpers, the time scale, the hour and day
// axis, past dimming, and the styles they have in common.

const SVG_NS = "http://www.w3.org/2000/svg";

// Height, in px, of the hour and day labels under a chart.
export const AXIS_HEIGHT = 44;

export const TIMELINE_STYLE = `
  :host {
    display: block;
    position: relative;
    --chart-grid: var(--divider-color, rgba(0, 0, 0, 0.12));
    --chart-text: var(--primary-text-color);
    --chart-text-muted: var(--secondary-text-color);
    color: var(--chart-text);
  }
  :host([hidden]) {
    display: none;
  }
  svg {
    display: block;
    width: 100%;
    height: 100%;
    overflow: visible;
    outline: none;
    touch-action: pan-y;
  }
  .axis-label {
    font-size: 11px;
    fill: var(--chart-text-muted);
    font-variant-numeric: tabular-nums;
  }
  .day-label {
    font-size: 12px;
    font-weight: 600;
    fill: var(--chart-text);
  }
  .past {
    opacity: 0.35;
  }
  .marker-line {
    stroke: var(--chart-text-muted);
    stroke-width: 1;
    shape-rendering: crispEdges;
  }
`;

export function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  parent?.appendChild(el);
  return el;
}

/** The time a list of periods ([{ start, end }]) spans, or null if empty. */
export function timeRange(periods) {
  return periods.length ? { start: periods[0].start, end: periods[periods.length - 1].end } : null;
}

/**
 * The start of the local hour that holds `date` (also in half-hour time
 * zones). It's taken back from the instant rather than set on the clock, so
 * in the hour repeated when clocks go back it stays in the right one.
 */
export function hourStart(date) {
  return new Date(
    date.getTime() - (date.getMinutes() * 60000 + date.getSeconds() * 1000 + date.getMilliseconds())
  );
}

/**
 * Maps a time to x over `range` ({ start, end }), kept inside `width`.
 * `.invert(x)` maps back to a time.
 */
export function timeScale(range, width) {
  const from = range.start.getTime();
  const span = range.end.getTime() - from;
  const x = (date) => Math.min(width, Math.max(0, ((date - from) / span) * width));
  x.invert = (px) => new Date(from + (px / width) * span);
  return x;
}

/**
 * Base for the timeline charts. Renders once per task however many properties
 * are set, re-renders every minute to move "now" along, and redraws the plot
 * on resize. Subclasses set `this._svg` in their constructor and implement
 * `_render()` (everything, ending with `_scheduleTick()`), `_renderPlot()`
 * (the SVG) and `_hasData()`.
 */
export class TimelineElement extends HTMLElement {
  connectedCallback() {
    this._resizeObserver ??= new ResizeObserver(() => this._renderPlot());
    this._resizeObserver.observe(this._svg);
    this._scheduleTick();
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
    clearTimeout(this._tick);
  }

  _requestRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    queueMicrotask(() => {
      this._renderQueued = false;
      this._render();
    });
  }

  _scheduleTick() {
    clearTimeout(this._tick);
    if (!this.isConnected || !this._hasData()) return;
    this._tick = setTimeout(() => this._render(), untilNextMinute());
  }
}

/**
 * Two groups in `svg`, clipped to before and after `split` and `height` tall.
 * Whatever is drawn into both shows dimmed before `split`.
 */
export function pastAndFuture(svg, defs, { split, width, height, id }) {
  return [
    [0, split, "past"],
    [split, width - split, ""],
  ].map(([x, w, cls], k) => {
    const clipId = `${id}-${k}`;
    const clip = svgEl("clipPath", { id: clipId }, defs);
    svgEl("rect", { x, y: 0, width: Math.max(0, w), height }, clip);
    return svgEl("g", { "clip-path": `url(#${clipId})`, class: cls }, svg);
  });
}

export function formatTime(date, locale) {
  return date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

/**
 * A time range written the locale's way, e.g. "07:30–09:30", or "tis 07:30–09:30"
 * with `weekday`. A range ending on another day gives both weekdays; one
 * ending exactly at midnight still counts as the same day.
 */
export function formatRange(start, end, locale, { weekday = false } = {}) {
  const time = { hour: "2-digit", minute: "2-digit" };
  const timeOnly = new Intl.DateTimeFormat(locale, time);
  const withDay = new Intl.DateTimeFormat(locale, { ...time, weekday: "short" });
  const sameDay = start.toDateString() === new Date(end - 1).toDateString();
  const [from, to] = sameDay
    ? [(weekday ? withDay : timeOnly).format(start), timeOnly.format(end)]
    : [withDay.format(start), withDay.format(end)];
  return `${from}${rangeSeparator(locale)}${to}`;
}

// The locale's own separator between the two ends of a range (e.g. "–" or
// " – "), as Intl writes it between two times on one day.
function rangeSeparator(locale) {
  const format = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" });
  const parts = format.formatRangeToParts(new Date(2000, 0, 1, 1), new Date(2000, 0, 1, 2));
  return parts.find((part) => part.source === "shared" && part.type === "literal")?.value ?? "–";
}

/** Milliseconds until the next whole minute, for re-rendering "now". */
export function untilNextMinute() {
  return 60000 - (Date.now() % 60000) + 50;
}

/**
 * Hour labels from `top`, each under the moment its hour starts and thinned
 * out so they don't collide, with day names under them from where each day
 * starts.
 */
export function drawTimeAxis(svg, { range, width, top, localize, locale }) {
  const x = timeScale(range, width);
  const hourWidth = (3600000 / (range.end - range.start)) * width;
  const labelEvery = [1, 2, 3, 4, 6, 12].find((n) => n * hourWidth >= 36) ?? 24;
  const hour = new Date(range.start);
  hour.setMinutes(0, 0, 0);
  if (hour < range.start) hour.setHours(hour.getHours() + 1);
  for (; hour < range.end; hour.setHours(hour.getHours() + 1)) {
    if (hour.getHours() % labelEvery === 0) {
      // Kept inside the chart at its edges.
      const at = x(hour);
      const anchor = at < 8 ? "start" : at > width - 8 ? "end" : "middle";
      svgEl("text", { class: "axis-label", x: at, y: top + 16, "text-anchor": anchor }, svg).textContent =
        String(hour.getHours()).padStart(2, "0");
    }
    if (hour.getHours() === 0 && hour > range.start) {
      svgEl("text", { class: "day-label", x: x(hour) + 4, y: top + 36 }, svg).textContent = dayName(
        hour,
        localize,
        locale
      );
    }
  }
  svgEl("text", { class: "day-label", x: 4, y: top + 36 }, svg).textContent = dayName(
    range.start,
    localize,
    locale
  );
}

function dayName(date, localize, locale) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((date - today) / 86400000);
  if (days === 0) return localize("timeline_today");
  if (days === 1) return localize("timeline_tomorrow");
  return date.toLocaleDateString(locale, { weekday: "long" });
}
