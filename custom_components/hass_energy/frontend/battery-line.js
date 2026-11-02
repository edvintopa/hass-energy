// The battery power line on the energy chart.
// Set `battery` ([{ start: Date, end: Date, power: kW, estimated: boolean }]);
// the chart sets `localize` (it reads the battery_line_ strings) and `locale`.
// Drawn like every power line (see power-line.js), dashed where planned.
//
// Discharging is positive (a source feeding the home), charging negative.
// The readout says which, with the power unsigned.

// Import siblings with this module's ?v=<hash of the files>, like the panel.
const version = new URL(import.meta.url).search;
const { PowerLine } = await import(`./power-line.js${version}`);

export class BatteryLine extends PowerLine {
  // `scale`: the PowerScale it shares with the other power lines.
  constructor(scale) {
    super({
      name: "battery",
      prefix: "battery_line_",
      color: "#3e47cc",
      scale,
      directions: { up: "discharging", down: "charging" },
    });
  }

  set battery(battery) {
    this._setPeriods(battery);
  }
}
