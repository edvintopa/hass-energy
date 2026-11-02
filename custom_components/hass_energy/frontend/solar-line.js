// The solar generation line on the energy chart.
// Set `solar` ([{ start: Date, end: Date, power: kW, estimated: boolean }]);
// the chart sets `localize` (it reads the solar_line_ strings) and `locale`.
// Drawn and read out like every power line (see power-line.js). Generating
// is positive.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { PowerLine } = await import(`./power-line.js${version}`);

export class SolarLine extends PowerLine {
  // `scale`: the PowerScale it shares with the other power lines.
  constructor(scale) {
    super({ name: "solar", prefix: "solar_line_", color: "#ff9800", scale });
  }

  set solar(solar) {
    this._setPeriods(solar);
  }
}
