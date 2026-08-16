# Pump.fun Repeat-Rugger Bot

Private research bot for identifying repeat Pump.fun operators, watching their
next launch, and evaluating fixed-size entries in observe, paper, and backtest
modes.

The repository also retains the original configurable Pump.fun/letsbonk.fun
trader as an execution reference. Do not run it with real funds while developing
the repeat-rugger strategy.

## Setup

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```powershell
uv sync
```

Yellowstone Geyser is optional:

```powershell
uv sync --extra geyser
```

HTTP observation and backtesting do not require gRPC.

Environment variables used by the remaining tools:

```env
SOLANA_RPC_HTTP=https://...
SOLANA_NODE_WSS_ENDPOINT=wss://...
SOLANA_PRIVATE_KEY=...
```

Observe and paper processes use only the RPC endpoint. They must not load
`SOLANA_PRIVATE_KEY`.

## Commands

Run the leakage-safe demo backtest:

```powershell
uv run python -m rugbot.backtest.cli \
  --input fixtures/backtest/demo.json --pretty
```

Replay finalized observations through the same dataset boundary used by RPC
callers:

```powershell
uv run python -m rugbot.backtest.cli \
  --replay path/to/observations.jsonl --as-of-slot SLOT --pretty
```

Acquire one known operator and its explicitly attributed launch mints through
the same finalized HTTP path:

```powershell
uv run python -m rugbot.backtest.cli \
  --operator-wallet CREATOR_WALLET \
  --start-slot START_SLOT --end-slot END_SLOT \
  --max-transactions 1000 --pretty
```

This command evaluates the typed launch outcomes already present in the fixed
fixture and reports split metrics. It does not infer operator qualification or
fabricate cases from incomplete RPC data. The RPC acquisition and qualified
pipeline still return `ABSTAIN` until point-in-time entity evidence,
protocol/mint account proofs, and completed outcome proofs are supplied.

Inspect a wallet, linked wallets, wallet-switch candidates, and launch
positions:

```powershell
uv run rug_watch --intelligence --wallet CREATOR_WALLET --pretty
```

Watch one known wallet through finalized HTTP RPC evidence:

```powershell
uv run rug_watch --wallet CREATOR_WALLET
```

Watch a persistent portfolio of known creator wallets in paper mode:

```yaml
# watch-portfolio.yaml
wallets:
  - "CREATOR_WALLET_A"
  - "CREATOR_WALLET_B"
```

```powershell
uv run rug_watch --portfolio watch-portfolio.yaml --mode paper `
  --interval-seconds 5 --state-dir .state/portfolio
```

The portfolio file is strict: it accepts only the `wallets` list, requires
valid unique Solana public keys, and has no schema or version field. Each
wallet is polled through the same finalized observation and paper-execution
path and gets isolated durable state under
`.state/portfolio/wallets/<wallet>/`. A per-wallet abstention is reported in
that cycle without stopping the other wallets or the persistent loop. Use
`--once` for one real RPC pass while keeping the same state layout.

Inspect wallet history and linked-wallet evidence:

```powershell
uv run rug_wallet --wallet CREATOR_WALLET --pretty
```

Interactive terminal UI:

```powershell
uv run rug_wallet_tui --wallet CREATOR_WALLET --config watch.yaml
```

Press `r` to refresh, `f` to focus the wallet field, `1`/`2`/`3` to switch
between Overview, Launches, and Graph, and `q` to quit. The Launches tab has a
local filter for symbol, mint, creator, or signature. The dashboard shows the
bounded activity window, native SOL flow, creator and wallet-switch signals,
refresh deltas, and a compact directional graph before the detailed tables.
The UI refreshes every 30 seconds by default and uses the same bounded,
finalized-RPC report as the JSON command. The Settings tab edits the same
strict `watch.yaml` consumed by `rug_watch`; saving validates the whole file
and replaces it atomically. It exposes the volume, creator-history, win-rate,
frequency, entry-position, entry-deviation, bundle, double-signature, and
prior-zero-balance settings without creating a second runtime configuration.

The result contains `stats`, a `rug_evidence` summary, historical Pump creates,
and a `graph` payload with nodes and direct native-transfer edges. It also
includes `creator_history` from the official read-only GMGN CLI when available.
That section reports creator-wide creation count, open count, token list, and
ATH information; it is explicitly external/non-finalized and is not used as a
backtest or decision input. Install the provider once with:

```powershell
npm install -g gmgn-cli
```

The CLI uses GMGN's public read-only testing key by default. Set
`GMGN_API_KEY` to use a personal key. If the CLI is unavailable, the report
keeps the finalized RPC result and shows the provider as unavailable instead
of treating a bounded RPC scan as proof that the creator has no history. The rug
summary exposes repeat-launch evidence, position 0/1 launches, linked creator
wallets, wallet-switch candidates, proven fresh wallets, multi-hop paths, and
native flow. These are observed signals, not a scam score or proof of common
control. The scan is
bounded to 50 transactions and 8 counterparties by default; increase those
limits deliberately because each linked wallet adds finalized RPC requests.

For Helius endpoints, the observer uses `getTransactionsForAddress` with
`transactionDetails: "full"`, so one finalized page carries both transaction
history and raw transaction evidence. Standard Solana RPC keeps the bounded
`getSignaturesForAddress` plus `getTransaction` path. Neither path fabricates a
missing account snapshot, entity proof, or completed outcome; the backtest
returns `ABSTAIN` until those typed proofs exist. This uses the existing
Helius/Solana JSON-RPC endpoint and requires no second paid data provider.
Helius Enhanced Transactions can be useful for later display-only enrichment,
but the canonical graph continues to use raw finalized RPC evidence so
offline replay and online inspection cannot diverge.

[`watch.yaml`](watch.yaml) contains the fixed quote size and observe/paper
mode; its wallet is used when neither `--wallet` nor `--portfolio` is given.
The watcher persists immutable raw observations in
`.state/watch/observations.jsonl` and derived restart state in
`.state/watch/state.sqlite3` for a single wallet, or under the per-wallet
portfolio paths shown above. SQLite stores checkpoints, handled canonical
identities, and paper positions; raw observations remain re-decodable JSONL.
Restart uses that state and does not replay handled evidence. Existing legacy
JSON state files are not imported automatically. It detects pinned Pump
`create_v2` launches and emits block-position 0/1 candidates without signing or
submitting transactions.

The sell rules also accept up to three bounded `auto_sell_big_buy.levels`
ranges under `rules.sell`; each range uses integer quote base units and a
`sell_fraction_ppm`.

Run the original bot only after reviewing and enabling its YAML:

```powershell
uv run pump_bot
```

The default watch configuration uses `observe`. Changing it to `paper` fails
closed until an executable Pump quote simulator is connected to the same
decision path.

## Live execution

Live execution is disabled in the core watcher. The configuration parser rejects
`execution.mode: live` until the paper simulator and out-of-sample evaluation
have proven the strategy. The legacy live adapter remains isolated as a
reference implementation and is not part of the supported development path.

The core filters live in `watch.yaml`: `max_market_cap_quote_base_units`,
`max_token_age_minutes`, `buy_only_once`, `max_consecutive_losses`, and
`execution.max_slippage_bps`. Exits support multiple PnL take-profit and
stop-loss levels, market-cap-tiered trailing stops, inactivity exits, and
partial sells. Amounts are integer base units: SOL quote values are lamports
and PnL/percentage values use parts per million.

The live poll currently supplies curve-price evidence for TP/SL/trailing
decisions. Inactivity exits remain fail-closed until an activity timestamp is
available for the position.

Transaction account layouts follow the repository's pinned Pump IDL and the
current Pump fee-recipient requirements documented in the
[official Pump public docs](https://github.com/pump-fun/pump-public-docs).

## Layout

- `src/rugbot/ingest`: finalized HTTP observations and decoding inputs.
- `src/rugbot/protocol`: pinned Pump decoders, state, and integer quotes.
- `src/rugbot/graph`: point-in-time wallet and operator evidence.
- `src/rugbot/decision`: matching, sizing, timing, and exits.
- `src/rugbot/runtime`: shared observation loop and wallet watch mode.
- `src/rugbot/backtest`: historical calibration and evaluation.
- `src/rugbot/storage`: immutable JSONL evidence and the SQLite derived-state store.
- `fixtures`: finalized protocol and backtest evidence.
- `learning-examples`: only the small scripts still useful for core manual checks.

The strategy and remaining work are in
[`TODO_RUG_RISK_SYSTEM.md`](TODO_RUG_RISK_SYSTEM.md).

## Safety

- Unknown or stale protocol state abstains.
- Financial calculations use integer base units.
- Historical features are bounded by `as_of_slot`.
- Paper and observe modes never submit transactions.
- Test manual buys and sells only with minimal amounts after paper validation.
- Never commit RPC credentials or private keys.
