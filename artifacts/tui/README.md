# TUI operator evidence

These artifacts are visual evidence for the project-owned
`trading-tui-operator-testing` skill. They contain local configuration and
SQLite state only; capture does not load a private key, sign, or submit a
transaction.

## Required viewports

- `80x24-dashboard@3x.png` and `80x24-sniper@3x.png`: compact workflow. The
  global and contextual shortcut rows must remain visible without scrolling.
- `120x36-dashboard@2x.png` and `120x36-sniper@2x.png`: expanded workflow. The
  Sniper position table must show every decision column through `STATE`
  without horizontal scrolling.
- Matching `.svg` files preserve Textual's original render output.

## Operator paths exercised

- `F1`, `F2`, `F3`, `F4`, then `Escape` through Textual's real event loop.
- `F3 -> H -> E` against an injected real `SniperDaemonService`: the durable
  SQLite position moves from 1000 to 500 base units, then closes at zero.
- The footer is asserted at 80x24 and 120x36, including the contextual Sniper
  actions.

## Safety and provenance

- Screenshots: local configuration and local SQLite state.
- Execution workflow: route-simulation/fixture ports only.
- Pump hot path: recorded finalized Pump V2 fixture decoded from processed log
  format, then reconciled through the internal HTTP hydration pipeline.
- Live orders: not attempted.
