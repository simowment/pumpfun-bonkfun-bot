# Repeat-Rugger Sniper

## Goal

Build one Pump.fun system that can:

1. qualify a repeat rug operator from completed launches;
2. follow wallet changes using funding, transfer, role, and profit-flow evidence;
3. watch the operator's next launch and flag only transaction position 0 or 1;
4. paper-enter with a fixed size only when a full exit is executable; and
5. backtest the exact same strategy without future-data leakage.

Unknown operators and incomplete state always abstain. Live transaction submission
is outside the current scope.

## Strategy

Qualification uses only launches completed before the launch being evaluated.
Start with roughly 10 to 20 launches from the same operator and regime. Require
repeated pump-and-dump behavior, a sensible positive-trade rate, consistent
entry market caps and holding times, limited launch spam, and positive net
expectancy after fees, latency, price impact, failed fills, and failed exits.

Wallet identity is an evidence-weighted cluster, not a creator-address equality.
Track creator, creation submitter, fee payer, funder, early buyer, inventory
holder, dumper, relay, and profit collector. A new wallet inherits suspicion
only through point-in-time funding, transfer, timing, or consolidation evidence.
Shared services and common transaction templates are insufficient on their own.

For a known operator's future launch:

- detect through the cheapest available stream and reconcile through finalized
  HTTP RPC;
- require a pinned decoder and canonical transaction ordering;
- accept only transaction index 0 or 1;
- buy once, never re-enter, and never copy the operator's sell;
- use fixed notional while comparing one coherent trade universe;
- reject sizes that cannot be sold in one stressed full-position quote;
- cap size by bankroll loss, pool depth, independent volume, and exit capacity;
- paper trade until out-of-sample evidence supports any later decision.

## Exit Calibration

For each historical launch, measure the maximum executable net PnL from the
copied entry after measured copy latency and before the adverse event. Average
those peaks to form the initial anchor. Scan take-profit thresholds downward
from that anchor and replay every threshold across the same launches.

Select the threshold with the highest mean net PnL after all costs. Win rate is
an optional minimum and a tie-breaker, not the objective. Five trades at +150%
may therefore beat six trades at +100% when the full sample has higher net
profit. A non-positive result abstains.

## Architecture

Only these paths are core:

```text
optional fast stream or finalized HTTP/JSONL
  -> immutable RawChainObservation
  -> pinned Pump decoder and integer market state
  -> point-in-time wallet/operator evidence
  -> known-operator matcher
  -> fixed-size entry and historical exit policy
  -> observe/paper decision
  -> backtest report
```

Online and offline differ only in observation acquisition. They share the raw
observation contract, decoder, state reducers, strategy functions, paper
simulation, and evaluator. A fast source may emit an observe-only provisional
signal, but finalized HTTP evidence must reconcile it before it becomes
canonical. gRPC, LaserStream, and ShredStream remain optional transports.

The useful pattern from
[`zero-block-sniper`](https://github.com/anshuman008/zero-block-sniper) is:

- converge multiple event sources before one creator-wallet filter;
- deduplicate the same mint across sources;
- merge the outer create instruction with its CPI event; and
- derive the paper transaction state from decoded event evidence without a
  hot-path RPC call.

This project uses the durable evidence identity instead of an in-memory TTL.
It also rejects guessed initial reserves, missing address-table keys, defaulted
token programs, and floating-point financial configuration.

## Current Work

- [x] Finalized HTTP wallet observation with durable restart cursor.
- [x] Immutable JSONL observations and handled-evidence ledger.
- [x] Pinned Pump create/trade decoders and integer quote primitives.
- [x] Point-in-time wallet graph, operator profile, and wallet churn features.
- [x] Known-wallet transaction-position 0/1 candidate logic.
- [x] Historical peak-based fixed-size exit calibration.
- [x] Runnable JSON backtest command.
- [x] Wire finalized HTTP Pump create decoding into wallet watch mode.
- [x] Expose one durable, non-signing wallet watch command.
- [x] Expose bounded wallet history, direct-transfer graph, and linked-creator
      wallet-switch candidate report.
- [x] Acquire a bounded known-operator wallet window and explicitly attributed
      launch-mint observations through the finalized HTTP path.
- [x] Reuse canonical launch/trade joins for the operator-wallet backtest
      command; missing case proofs remain a typed abstention.
- [x] Add a Textual TUI for wallet stats, launch history, and graph inspection.
- [x] Decode and merge the create CPI event to prove transaction-slot reserves.
- [x] Feed those exact reserves into the existing integer quote engine and
      paper execution port when point-in-time protocol and mint proofs exist.
- [ ] Resolve those protocol/fee/mint proofs in the runtime watcher and inject
      the exact paper port automatically; missing proofs must remain ABSTAIN.
- [ ] Add one optional processed/confirmed source only after the paper path is
      executable; finalized HTTP remains the reconciliation source.
- [ ] Build complete historical launch/trade datasets directly from finalized
      RPC evidence; automatic launch/trade joins, mint-history lookup, and a
      pure trajectory/outcome producer now exist, but metadata resolution,
      wallet attribution, and point-in-time case assembly are not wired into
      one RPC backtest command.
- [ ] Replay exact historical account observations and typed entity evidence
      into complete launch-case proofs; standard `getAccountInfo` cannot prove
      arbitrary past account bytes from a later slot.
- [ ] Produce one end-to-end wallet qualification and out-of-sample paper run.
- [x] Add pure operator qualification metrics and fail-closed thresholds.
- [x] Add volume/liquidity-aware integer sizing with one-shot exit capacity.
- [x] Add a non-signing paper position lifecycle for TP/SL/trailing exits.
- [x] Add stressed full-exit quotes to every calibrated backtest trade.
- [x] Extend wallet intelligence with typed SPL transfer and multi-hop evidence.
- [x] Add a pure finalized outcome-trajectory builder that accepts only
      point-in-time market state and executable full-exit quotes.
- [x] Add a pure finalized Pump metadata resolver requiring explicit
      account/mint proofs and pinned registry artifacts.
- [x] Compose qualification, finalized dataset validation, and OOS evaluation
      behind one fail-closed typed boundary.

## Rules

- No signing keys in observe, paper, or backtest processes.
- No live submissions during development.
- No floats for financial values, reserves, fees, or PnL.
- Every feature and decision carries an `as_of_slot`.
- No RPC or storage calls inside pure strategy functions.
- Missing, stale, malformed, conflicting, or undecoded state returns `ABSTAIN`.
- Raw evidence stays immutable and re-decodable.
- No backward compatibility, migrations, application versioning, dashboards,
  readiness frameworks, provider frameworks, or duplicate implementations.
- Add code only when it enables wallet qualification, watch mode, paper
  execution, or leakage-safe backtesting.
