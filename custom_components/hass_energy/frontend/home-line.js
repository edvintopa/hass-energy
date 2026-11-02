// The home power line on the energy chart: what the home uses.
// Set `home` ([{ start: Date, end: Date, power: kW, estimated: boolean }]);
// the chart sets `localize` (it reads the home_line_ strings) and `locale`.
// Drawn like every power line (see power-line.js), dashed where estimated.
//
// Always positive: it's where solar, battery and grid power goes.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { PowerLine } = await import(`./power-line.js${version}`);

export class HomeLine extends PowerLine {
  // `scale`: the PowerScale it shares with the other power lines.
  constructor(scale) {
    super({ name: "home", prefix: "home_line_", color: "#d03e3e", scale });
  }

  set home(home) {
    this._setPeriods(home);
  }
}
