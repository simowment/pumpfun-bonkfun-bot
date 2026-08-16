# Architecture Review

Date: 2026-08-16

This review covers the current `rugbot` implementation, the configurable TUI
settings, the online/offline path, and the remaining legacy boundaries. Four
independent reviews were compared against the
[Refactoring.Guru design-pattern catalog](https://refactoring.guru/design-patterns/catalog).

## Decision

The TUI settings editor is useful as a configuration editor, but the current
implementation is not ready for live execution or for claiming that every
displayed filter is enforced. The core delivery remains observe/paper only
until the blocking issues below are resolved.

The most important distinction is between configuration storage and decision
behavior:

- `max_entry_transaction_index` currently reaches the watch decision path.
- Existing `rules` fields remain the effective source for the current market,
  timing, take-profit, stop-loss, and sizing decisions.
- The other new strategy fields are parsed and shown by the TUI, but are not
  yet backed by typed online evidence and do not currently change runtime
  decisions. They must either be wired through a fail-closed evidence boundary
  or be removed from the UI until that exists.

## Blocking Findings

### P0: Paper execution is not end to end

`run_watch_cycle` constructs a bare `PaperExecutionPort` without a simulator,
so paper submissions are rejected instead of producing executable simulated
fills. The existing exact paper-context resolver is not wired into this path.

References:

- `src/rugbot/runtime/cli.py:141`
- `src/rugbot/runtime/cli.py:513`
- `src/rugbot/execution/paper.py:17`
- `src/rugbot/runtime/paper_context.py:40`

The paper path must use the same finalized observation, pinned decoder, quote,
and state-reduction path as replay before it can be used as evidence.

### P0: Live submission can be retried after a partial failure

The handler performs execution before the handled-evidence ledger and source
acknowledgement are durably committed. A post-submit persistence failure can
cause the same pending observation and intent to be submitted again.

References:

- `src/rugbot/runtime/observation_loop.py:131`
- `src/rugbot/runtime/observation_loop.py:381`
- `src/rugbot/runtime/observation_loop.py:405`
- `src/rugbot/runtime/observation_loop.py:431`
- `src/rugbot/runtime/watch.py:359`
- `src/rugbot/execution/live.py:166`

This requires a transactional execution/state machine or an explicit durable
intent and reconciliation protocol. It is outside the safe paper-only scope,
but must block live execution.

## P1 Findings

### Configured filters are mostly inert

The TUI exposes volume, creator history, sample count, win rate, buy rate,
market-cap, entry deviation, bundle, double-signature, and prior-zero-balance
settings. The watch path currently consumes only
`max_entry_transaction_index` from this group. Tightening the other fields
does not change the decision.

References:

- `src/rugbot/runtime/config.py:79`
- `src/rugbot/runtime/config.py:283`
- `src/rugbot/runtime/watch.py:675`
- `src/rugbot/runtime/watch.py:849`
- `src/rugbot/runtime/wallet_tui.py:528`

There is also no canonical Axiom or Solscan evidence adapter. These external
metrics cannot be treated as decision inputs without provider provenance,
slot context, and fail-closed handling.

### Online evidence is not point-in-time correct

`PumpOnlineMarket` reads pool state with `commitment="processed"`, then labels
the result with the requested finalized observation slot. Newer processed
state can therefore be represented as if it were finalized launch-time state.
The same problem affects position evidence and breaks online/replay parity.

References:

- `src/rugbot/runtime/pump_market.py:54`
- `src/rugbot/runtime/pump_market.py:63`
- `src/rugbot/runtime/pump_market.py:102`
- `src/rugbot/runtime/pump_market.py:118`
- `src/rugbot/runtime/pump_market.py:143`

### Runtime, qualification, and backtest use separate strategy contracts

`StrategyFilterSettings`, `PlaybookRules`, `VolumeSizingPolicy`,
`CopyTradeConfig`, and `OperatorQualificationConfig` can describe different
strategies. A YAML edit can therefore change watch behavior without changing
qualification or backtest behavior.

References:

- `src/rugbot/runtime/config.py:95`
- `src/rugbot/backtest/copytrade.py:36`
- `src/rugbot/decision/operator_qualification.py:49`
- `src/rugbot/backtest/qualified_run.py:52`

The project needs one canonical typed strategy contract or an explicit,
validated projection shared by online and offline execution.

### Historical trajectory validation has a leakage gap

Trajectory points are bounded by a global cutoff, but are not required to be
no later than the individual history sample's `as_of_slot`. Post-sample points
can therefore influence qualification and exit calibration.

References:

- `src/rugbot/backtest/copytrade.py:349`
- `src/rugbot/backtest/copytrade.py:368`
- `src/rugbot/backtest/copytrade.py:405`
- `src/rugbot/backtest/copytrade.py:427`

### Wallet and entry state are not fully durable or isolated

- Single-wallet state is keyed by a bare state directory and market, so
  switching wallets can restore or manage another wallet's position.
- The handler is rebuilt each cycle, resetting entry cooldown and buy-once
  state.
- `max_consecutive_losses` is parsed but is not reliably wired to realized-PnL
  updates.

References:

- `src/rugbot/runtime/cli.py:141`
- `src/rugbot/runtime/cli.py:409`
- `src/rugbot/runtime/cli.py:467`
- `src/rugbot/runtime/watch.py:121`
- `src/rugbot/runtime/watch.py:138`
- `src/rugbot/runtime/watch.py:177`
- `src/rugbot/runtime/watch.py:700`
- `src/rugbot/storage/paper_position_store.py:79`

### Execution mode is validated after submission

The watcher can submit through an injected or resolved port before validating
that the receipt mode matches observe/paper expectations. A wrong port can
therefore submit a real transaction before being rejected.

Reference: `src/rugbot/runtime/watch.py:318-336`.

### Paper and online timing values are placeholders

Online entry evidence assigns received time to creation and event time, and
position evidence hardcodes inactivity to zero. Positive delays and inactivity
rules therefore do not represent real timing.

References:

- `src/rugbot/runtime/pump_market.py:62`
- `src/rugbot/runtime/pump_market.py:118`

### Configured entry index is broader than the stated policy

The written policy describes block/index 0 or 1, while the parser permits
values through 20 and both watch and backtest honor the larger value.

References:

- `TODO_RUG_RISK_SYSTEM.md:32`
- `src/rugbot/runtime/config.py:328`
- `src/rugbot/runtime/watch.py:850`
- `src/rugbot/backtest/copytrade.py:557`

### TUI persistence and reload behavior are misleading

- The TUI rewrites YAML with `yaml.safe_load`/`safe_dump`, which collapses
  duplicate keys before the strict loader can reject them and discards comments.
- The watcher loads configuration once; saving in the TUI does not affect an
  already-running watcher despite the UI suggesting next-cycle behavior.
- The TUI can scan wallet A while editing a config targeting wallet B.
- A wallet report can finish for wallet A after the input has been changed to
  wallet B.
- The subtitle still describes the UI as read-only even though Settings writes
  `watch.yaml`.

References:

- `src/rugbot/runtime/wallet_tui.py:64`
- `src/rugbot/runtime/wallet_tui.py:273`
- `src/rugbot/runtime/wallet_tui.py:391`
- `src/rugbot/runtime/wallet_tui.py:428`
- `src/rugbot/runtime/wallet_tui.py:556`
- `src/rugbot/runtime/wallet_tui.py:602`
- `src/rugbot/runtime/wallet_tui.py:616`
- `src/rugbot/runtime/cli.py:380`
- `src/rugbot/runtime/cli.py:464`

The smallest correction is a strict config-owned update/save function with
atomic replacement, plus an explicit “restart watcher to apply” message.

## Architecture and Pattern Critique

The Refactoring.Guru catalog is useful here as a naming aid, not as a reason to
add abstractions. The appropriate minimal shape is:

- **Strategy:** one pure, typed policy for matching, sizing, timing, and exit.
- **Observer:** the existing event/refresh flow is enough; no custom observer
  hierarchy is needed.
- **Memento:** durable observation and position state, with explicit wallet
  ownership and restart semantics.
- **Facade:** one strict configuration boundary used by both CLI and TUI.
- **Adapter:** provider data may be adapted only when slot provenance is
  preserved and validated.

The following are currently overextended or duplicated:

- `WatchSnipeHandler` combines matching, rules, sizing, risk counters,
  persistence, execution, and resolver callbacks.
- `PlatformRegistry`, `PlatformFactory`, the global factory, and
  `ListenerFactory` are plugin-style machinery for a private two-platform
  project.
- `rugbot` still reaches into the legacy `src/platforms` stack, creating two
  incompatible Pump protocol/quote implementations.
- `online_pipeline`, `qualified_run`, `rpc_dataset`, and CLI orchestration
  duplicate dataset construction and evaluation paths.
- The global `IDLManager` singleton adds hidden mutable state despite pinned
  core decoders.

Keep the boundaries that provide real value: finalized/replay observation
source, immutable observation storage, pinned decoder/state reducer, pure
decision functions, execution ports, and a small composition root.

## Verification

- Focused config/watch/TUI run: **42 passed, 1 failed**.
- The failure is the pre-existing contract mismatch where the test expects
  `live` mode parsing to fail, while the parser accepts it and the CLI defers
  the gate.
- Focused Ruff and formatting checks passed before the aggregate command was
  interrupted by that expected test failure.
- No live transaction was run.

## Required Order Before Live Authorization

1. Wire a real deterministic paper simulator through finalized decoders and
   quote/state reduction; prove an end-to-end backtest/paper result.
2. Make provider evidence slot-pinned and fail closed when provenance is
   unavailable.
3. Use one strategy contract for TUI, watch, qualification, and backtest.
4. Remove or hide inert TUI filters until their evidence path is implemented.
5. Persist wallet ownership and entry/risk state; validate execution mode before
   any submission.
6. Resolve the live idempotency/reconciliation problem only after paper and
   out-of-sample evidence exist.
