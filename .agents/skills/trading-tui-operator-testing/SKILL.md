---
name: trading-tui-operator-testing
description: Exercise and audit Textual/Python crypto trading TUIs as an operator. Use when implementing, testing, or reviewing tracker, backtester, or sniper/execution workflows that require keyboard-first interaction and readable visual evidence.
---

# Trading TUI Operator Testing

## Purpose

Validate a trading TUI through the actual application event loop, not only unit
tests. The deliverable is an operator-oriented audit: real keyboard workflows,
safe state transitions, and screenshots that a person can inspect.

## Safety boundary

- Never place a live order, sign a transaction, or load a private key.
- Use finalized fixtures, replay, paper, observe, or route-simulation modes.
- If a visual control could execute live trading, verify its label, guard, and
  cancel path only. Do not activate it.
- State clearly whether data came from a fixture, internal integration path,
  or real read-only transport.

## Workflow

1. Discover the real TUI contract before changing it.
   - Inspect bindings, actions, modal handlers, focus order, responsive CSS,
     state stores, and the event-to-widget update path.
   - Map the operator areas: Tracker (detection), Backtester (historical
     evidence), and Sniper (paper/simulated execution). Do not invent PnL or
     a backtest result when chronological price evidence is unavailable.

2. Define the operator contract.
   - Identify the primary task, primary action, starting focus, and safe
     cancellation route for each screen.
   - Make keyboard shortcuts visible where they are useful: persistent footer
     for global and contextual actions, focused panel for local navigation.
   - Verify `Enter`, `Escape`, arrows, tabs, modal confirmation, and every
     advertised shortcut. Bindings must still work when a table has focus.

3. Drive the application itself.
   - Prefer Textual `App.run_test()` and `Pilot` when native desktop automation
     is unavailable. This exercises the real event loop and widget tree.
   - Feed authentic project fixtures or the application's internal integration
     pipeline; do not call a widget method as a substitute for a workflow.
   - Cover at least: empty/startup, a selected tracked target, candidate,
     pending paper route, ignored/blocked, error, and a simulated position.
     Also cover stale/disconnected or risk-gated state when the application
     exposes it.
   - For Tracker, Backtester, and Sniper, test panel switching, focused row
     navigation, inspection, primary safe action, and return/cancel behavior.
   - For purchase/sell dialogs, test validation failure, confirmation path
     only in paper/simulation mode, and cancellation with both `Escape` and
     any displayed shortcut.

4. Capture and inspect visual evidence.
   - Capture representative 80x24 and 120x36 terminal states. Include every
     critical modal and narrow layout where it can be used.
   - Preserve the original SVG if supplied by the test harness, then render a
     readable PNG at at least 2x. For an 80x24 capture, target a PNG width of
     2,000 pixels or more. Inspect the rendered PNG, not SVG XML alone.
   - Store artifacts under a project-owned visual artifact directory when the
     repository has one; otherwise use the session visualization directory and
     name files by viewport and workflow (for example,
     `sniper-80x24-pending@3x.png`).
   - In the handoff, embed or link the high-resolution PNGs. A raw SVG alone is
     not sufficient visual proof.

5. Verify in the right order.
   - Direct interactive integration exercise and end-to-end replay first.
   - Running isolated unit tests does NOT prove that the bot or TUI is actually usable.
   - Treat an unavailable renderer, missing fixture, or untested destructive
   route as incomplete verification, not success.

## Project evidence convention

For this repository, keep inspected SVG and high-resolution PNG evidence in
`artifacts/tui/` and maintain `artifacts/tui/README.md` with the exact operator
paths, provenance, safety mode, and unresolved boundary. A screenshot is not
proof of wiring: pair it with a Textual `Pilot` workflow that observes the
durable SQLite or daemon state mutation caused by the advertised shortcut.

## Report

Report only executed evidence:

- **Operator path:** panels and keyboard workflows exercised.
- **Visual evidence:** viewport, PNG paths, and what was checked for clipping,
  focus, contrast, and shortcut legibility.
- **Safety mode:** fixture/paper/simulation/read-only status and confirmation
  that no live transaction was attempted.
- **Residual risk:** any state or integration boundary that could not safely
  be exercised, plus the next required check.

Use precise failure language. A visually clipped control, an inactive shortcut,
or a route that was only unit-tested is not verified.
