// The grid power line on the energy chart.
// Set `grid` ([{ start: Date, end: Date, power: kW, estimated: boolean }]);
// the chart sets `localize` (it reads the grid_line_ strings) and `locale`.
// Drawn like every power line (see power-line.js), dashed where estimated.
//
// Importing is positive (a source feeding the home), exporting negative.
// The readout says which, with the power unsigned.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { PowerLine } = await import(`./power-line.js${version}`);

export class GridLine extends PowerLine {
  // `scale`: the PowerScale it shares with the other power lines.
  constructor(scale) {
    super({
      name: "grid",
      prefix: "grid_line_",
      color: "#a04ad9",
      scale,
      directions: { up: "importing", down: "exporting" },
    });
  }

  set grid(grid) {
    this._setPeriods(grid);
  }
}
