// What the lines on the energy chart share: the base class the chart drives,
// hourly averages, and the smooth path through their periods.
//
// A line is a plain object, not an element: the chart owns the SVG, the
// marker and the pointer, and asks each of its lines to draw itself, place
// its dot and show its value. Each line brings its own scale, colours
// (`style`) and readout, and fires `change` when its data changes.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { hourStart, svgEl } = await import(`./timeline.js${version}`);

/**
 * Base for the chart's lines. Subclasses set `name`, `style` and `valueKeys`
 * (the numeric fields of their periods), set their 15-minute periods
 * ([{ start: Date, end: Date, … }]) through `_setPeriods`, fill `readout`
 * (an element the chart places above the plot), and implement:
 * - `_render()`: refresh `readout` text (localize, locale or data changed);
 * - `draw({ back, defs, groups, plot })`: draw `hours` (the hourly averages
 *   of `periods`) into `groups` (the dimmed past and the future, both the
 *   same) on top of `back`, and return `yAt(x)`, or null when none of it is
 *   in the plot;
 * - `show(time, now)`: show the value at `time` in `readout` (`now` when the
 *   marker is on the current time; lines may ignore it, as the marker's own
 *   label already says so);
 * - `dotColor(time)`: the marker dot's colour at `time`, or null for none;
 * - `label`: what the line shows, for the chart's accessible name.
 * `plot` has `x` (time scale), `width`, `top`, `middle`, `half` (px from the
 * middle to the top or bottom edge), `baseline` and `clipHeight`.
 */
export class ChartLine extends EventTarget {
  constructor() {
    super();
    this.valueKeys = [];
    // What the readout shows: the 15-minute detail.
    this.periods = [];
    // What the line draws: their hourly averages.
    this.hours = [];
    this._localize = () => "";
    this._locale = undefined;
    this.readout = document.createElement("div");
  }

  set localize(localize) {
    this._localize = localize;
    this._render();
  }

  set locale(locale) {
    this._locale = locale;
    this._render();
  }

  // Sets the line's periods and tells the chart to redraw.
  _setPeriods(periods) {
    this.periods = periods ?? [];
    this.hours = hourlyAverages(this.periods, this.valueKeys);
    this._render();
    this.dispatchEvent(new Event("change"));
  }

  periodAt(time) {
    return this.periods.find((p) => p.start <= time && time < p.end);
  }
}

const HOUR = 3600000;

/**
 * Averages shorter periods into whole hours: each field in `keys` is
 * averaged, weighted by how long each period is (so a 45-minute period counts
 * three times a quarter), and an hour is `estimated` when any of its periods
 * is. Hours are
 * cut on the local hour (also for half-hour time zones) and keyed by their
 * instant, so the hour repeated when clocks go back stays apart.
 */
export function hourlyAverages(periods, keys) {
  const hours = new Map();
  for (const period of periods) {
    const key = hourStart(period.start).getTime();
    if (!hours.has(key)) hours.set(key, []);
    hours.get(key).push(period);
  }
  return [...hours].map(([key, inHour]) => {
    const hour = { start: new Date(key), end: new Date(key + HOUR), estimated: inHour.some((p) => p.estimated) };
    for (const key of keys) {
      hour[key] = inHour.reduce((sum, p) => sum + p[key] * (p.end - p.start), 0) / inHour.reduce((sum, p) => sum + (p.end - p.start), 0);
    }
    return hour;
  });
}

// A smooth path through the points that never overshoots them
// (monotone cubic, Fritsch-Carlson), so the line stays true to the values.
// Returns the path's `d` and `yAt(x)`, the y of the curve at x, or null where
// the path doesn't reach.
export function monotonePath(points) {
  const n = points.length;
  if (n < 2) return { d: "", yAt: () => null };
  const slopes = [];
  for (let k = 0; k < n - 1; k++) {
    slopes.push((points[k + 1].y - points[k].y) / (points[k + 1].x - points[k].x));
  }
  const tangents = points.map((_, k) => {
    if (k === 0) return slopes[0];
    if (k === n - 1) return slopes[n - 2];
    return slopes[k - 1] * slopes[k] <= 0 ? 0 : (slopes[k - 1] + slopes[k]) / 2;
  });
  for (let k = 0; k < n - 1; k++) {
    if (slopes[k] === 0) {
      tangents[k] = tangents[k + 1] = 0;
      continue;
    }
    const a = tangents[k] / slopes[k];
    const b = tangents[k + 1] / slopes[k];
    const sum = a * a + b * b;
    if (sum > 9) {
      const tau = 3 / Math.sqrt(sum);
      tangents[k] = tau * a * slopes[k];
      tangents[k + 1] = tau * b * slopes[k];
    }
  }
  // Each segment's control points split it into equal thirds of x, so x moves
  // linearly with t and the curve's y at x is the Bezier at t = x's fraction.
  const controls = (k) => {
    const p = points[k];
    const q = points[k + 1];
    const dx = (q.x - p.x) / 3;
    return [p.y, p.y + tangents[k] * dx, q.y - tangents[k + 1] * dx, q.y];
  };
  let d = `M${points[0].x},${points[0].y}`;
  for (let k = 0; k < n - 1; k++) {
    const p = points[k];
    const q = points[k + 1];
    const dx = (q.x - p.x) / 3;
    const [, y1, y2] = controls(k);
    d += ` C${p.x + dx},${y1} ${q.x - dx},${y2} ${q.x},${q.y}`;
  }
  const yAt = (x) => {
    if (x < points[0].x || x > points[n - 1].x) return null;
    const k = Math.max(0, Math.min(n - 2, points.findIndex((p) => p.x > x) - 1));
    const t = Math.min(1, Math.max(0, (x - points[k].x) / (points[k + 1].x - points[k].x || 1)));
    const [y0, y1, y2, y3] = controls(k);
    const u = 1 - t;
    return u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3;
  };
  return { d, yAt };
}

// Points at the middle of each period over the time scale `x`, held flat out
// to the start of the first and the end of the last (the plot's edges when
// they cover it all). Periods outside the scale are left out, so none pile up
// on an edge. Empty when none are inside.
export function periodPoints(periods, x, value, width) {
  const points = [];
  let first;
  let last;
  for (const period of periods) {
    const px = x(new Date((period.start.getTime() + period.end.getTime()) / 2));
    if (px > 0 && px < width) {
      points.push({ x: px, y: value(period) });
      first ??= period;
      last = period;
    }
  }
  if (!points.length) return points;
  return [{ x: x(first.start), y: points[0].y }, ...points, { x: x(last.end), y: points[points.length - 1].y }];
}

// Draws the path `d` into each of `groups` with class `cls`: solid up to the
// first estimated period of `periods`, and with `estimated` added from there
// (dashed by the line's style). `name` keeps the clip paths' ids apart.
export function drawSplitPath({ defs, groups, d, name, cls, periods, plot }) {
  const { x, width, clipHeight } = plot;
  const firstEstimated = periods.find((p) => p.estimated);
  const split = firstEstimated ? x(firstEstimated.start) : width;
  const parts = [
    [0, split, `${name}-measured`, cls],
    [split, width - split, `${name}-estimated`, `${cls} estimated`],
  ];
  for (const [from, size, id] of parts) {
    const clip = svgEl("clipPath", { id }, defs);
    svgEl("rect", { x: from, y: 0, width: Math.max(0, size), height: clipHeight }, clip);
  }
  for (const group of groups) {
    for (const [, , id, partCls] of parts) {
      svgEl("path", { class: partCls, d, "clip-path": `url(#${id})` }, group);
    }
  }
}

// The stroke of a line drawn with `drawSplitPath` under class `cls`, in
// `color`: dashed where estimated.
export function lineStrokeStyle(cls, color) {
  return `
    .${cls} {
      fill: none;
      stroke: ${color};
      stroke-width: 2.5;
      stroke-linejoin: round;
      stroke-linecap: round;
    }
    .${cls}.estimated {
      stroke-dasharray: 6 5;
    }
  `;
}

// A readout value shown as a badge in a light tint of the line's `color`, so it
// ties the value to its line without changing the text's own colour. It fills
// the readout's fixed width, so every badge is the same size.
export function badgeStyle(color) {
  return `
    padding: 2px 10px;
    border-radius: 10px;
    background: color-mix(in srgb, ${color} 22%, transparent);
  `;
}

// Styles the lines share: the readout's value and unit. Digits are all the
// same width and nothing wraps, so a readout keeps its size as values change
// (the chart gives each one a fixed width); a label too long for it is cut
// off with an ellipsis.
export const LINE_STYLE = `
  .readout-label {
    font-size: 14px;
    color: var(--chart-text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .readout-value {
    font-size: 28px;
    font-weight: 600;
    line-height: 1.1;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .readout-unit {
    font-size: 16px;
    font-weight: 400;
    color: var(--chart-text-muted);
  }
`;
