# Pump.fun Intelligence & Cabal Sniper Bot

Autonomous on-chain insider intelligence, funding cluster reconstruction, and asymmetric copy-sniping pipeline on Solana Pump.fun.

---

> 📖 **Operator Guide**: See [Memecoin Bible Operator Guide](docs/MEMECOIN_BIBLE_OPERATOR_GUIDE.md) for the complete target discovery, cluster reconstruction, Jito B0 bundle forensics, and mathematical risk playbook based on the *Memecoin Bible*.

---

## 1. Insider Cabal Sniping Strategy

On Solana Pump.fun, thousands of tokens launch daily and ~98% rug to zero within minutes. However, the profitable runners are rarely random: they are launched or heavily coordinated by **serial operator cabals**.

These operators:
1. Fund multiple fresh "burner" wallets from a central funding mother wallet or CEX dispersal node.
2. Buy into their token in the very first blocks (blocks 0–10) across 2 to 5 burner wallets simultaneously to simulate launch momentum and organic hype.
3. Push the market cap to $50k–$500k+ before selling.

**The Strategy**: Instead of predicting coins or following social media shills, the bot **reverse-engineers the early buyers of verified winners**, clusters them back to their common funding origin, and monitors those clustered wallets via low-latency WebSockets. When the cluster coordinates a new launch with high conviction, the bot paper-snipes the coin in the same early block window, de-risks 100% of the initial principal at $2.0\times$, and trails the rest.

### System Architecture

```text
src/rugbot/
├── discover/
│   └── cabal.py             # 1. Winner Ingestion, Early Buyer Extraction, Transitive Clustering, CabalStore
├── intelligence/
│   └── signal_filter.py     # 2. Age, Liquidity, Confluence, & Conviction Sizing Filters
├── execution/
│   ├── wallet_pool.py       # 3. Stealth Multi-Wallet Signer Pool & Rotation (Round-Robin / LRU)
│   └── cabal_executor.py    # 4. Paper Execution, Jito Tip Estimator, TP Ladders & Trailing Stops
├── runtime/
│   └── cabal_pipeline.py    # 5. WebSocket/RPC Monitor, Discord Webhook, Telegram Alerts Coordinator
└── interfaces/cli/
    └── cabal.py             # 6. Unified CLI: cabal / cabal_sniper (discover, list, watch)
```

---

## 2. Mathematical Expected Value & Unit Economics

The Expected Value equation for on-chain trading:
$$\text{EV} = \Big[P(\text{win}) \times \bar{R}(\text{win})\Big] + \Big[P(\text{loss}) \times \bar{R}(\text{loss})\Big] - \text{Friction}$$

### Head-to-Head Comparison (Per 0.50 SOL Position)

| Metric | Naive Copytrading (Blind Follow) | Filtered Confluence Sniping (Our Setup) |
| :--- | :--- | :--- |
| **Filtered Signals / Day** | 20 – 30 (Spam & dust) | 1 – 3 (High conviction) |
| **Winrate ($P \ge 2.0\times$)** | 12.0% | **56.0%** |
| **Net Win Payoff ($\bar{R}_{\text{win}}$)** | +110.0% | **+153.8%** (Tiered 2x/5x + Trailing) |
| **Net Loss Payoff ($\bar{R}_{\text{loss}}$)** | -95.0% | **-89.5%** (Adverse print exit) |
| **Roundtrip Fee + Jito Drag** | 0.0225 SOL / trade | 0.0225 SOL / trade |
| **Breakeven Winrate Required** | 68.5% | **36.8%** |
| **Net EV per 0.50 SOL Trade** | **-0.2607 SOL (-52.1%)** | **+0.2337 to +0.4402 SOL (+46.7% to +88.0%)** |
| **Margin of Safety** | Negative (-56.5%) | **+19.2% above breakeven** |

### Complete Fee Breakdown (Per 0.50 SOL Trade)
* **Pump.fun Buy Curve Fee**: 1.0% (0.0050 SOL)
* **Pump.fun Sell Curve Fee**: 1.0% (0.0050 SOL)
* **Jito Validator Tip (p75 priority)**: 0.0045 SOL roundtrip
* **Solana Base Priority Network Fee**: 0.0005 SOL
* **Entry Slippage**: 1.5% (0.0075 SOL)
* **Total Friction Drag**: **0.0225 SOL (4.50% of trade capital)**.

### Asymmetric Exit Rules
1. **Tier 1 Take-Profit**: Sell **50% at 2.0x (+100%)**. Returning 100% of initial principal ($0.50 \times 2.0 \times 0.50 = 0.50\text{ SOL}$). The position becomes mathematically risk-free house money.
2. **Tier 2 Take-Profit**: Sell **25% at 5.0x (+400%)**.
3. **Trailing Runner**: The remaining **25%** trails with a **15% trailing stop** from peak high-water mark.
4. **Adverse Rug Liquidation (NORMATIVE AGENTS.md §11)**: If a tracked insider or dev submits a sell transaction (`txType == "sell"`), the bot detects it on the WebSocket stream and **immediately dumps your position on their print** before the bonding curve collapses.

---

## 3. Quickstart & CLI Commands

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```powershell
uv sync
```

The CLI entry points `cabal` and `cabal_sniper` are registered in `pyproject.toml` (with `rug_cabal` preserved as an alias).

### Step 1: Ingest & Refresh Cabal Clusters
Reverse-engineers recent Pump.fun winners, extracts earliest unique buyers, clusters them by funder origin, and persists to `.state/cabal/cabal_clusters.sqlite3`:
```powershell
uv run cabal discover --min-mcap 50000 --limit-winners 25
```

### Step 2: Query Monitored Clusters (<1s query)
Displays all persisted clusters, typical buy sizes, historical winrates, and token counts:
```powershell
uv run cabal list
```

### Step 3: Run Empirical Historical Backtests (`cabal backtest`)
Backtest trading parameters against real, authentic 1-minute OHLC candlesticks from Pump.fun for discovered cabal tokens:
```powershell
# 1. Backtest top 10 discovered cabal tokens with default TP/trail parameters
uv run cabal backtest --limit 10

# 2. Backtest a specific token mint
uv run cabal backtest --mint F2reRYPQUPkagQy7t5VaZgtiHqXNFQ91ej15GRypump

# 3. Backtest and export performance sheet directly to Apache Parquet
uv run cabal backtest --limit 20 --parquet

# 4. Custom backtest: 10% trailing stop, 0.50 SOL trade size, 5.0 SOL starting balance
uv run cabal backtest --trail 10.0 --size-sol 0.50 --paper-balance 5.00
```

### Step 4: Launch Live Dry Run Monitoring (`cabal dryrun`)
Monitors tracked cabals in real-time via dual-engine ingestion (Helius RPC poller + PumpPortal WS), applies positive-EV confluence gating, dispatches Discord/Telegram alerts, and executes paper trades with live balance and PnL tracking:
```powershell
uv run cabal dryrun --profitable --seconds 0
```
*(Pass `--seconds 0` to run indefinitely, or e.g. `--seconds 300` for a 5-minute session).*

> [!TIP]
> **Zero Balance Required for Ingestion**: The bot uses your configured Helius RPC (`SOLANA_RPC_HTTP`) to poll on-chain transactions directly across both Pump.fun and DEXs every 3s. You do **not** need to deposit any SOL into PumpPortal. Terminal heartbeats tick every 15s with live paper balance, PnL, and open positions.

CLI Options for `cabal dryrun`:
* `--profitable`: Enforces +59% Net EV settings: multi-wallet confluence $\ge 2$, minimum buy 0.10 SOL, excludes dust sprayers (default: `True`).
* `--paper-balance`: Initial simulated cash balance in SOL (default: `2.00`).
* `--min-buy-sol`: Minimum buy size in SOL to trigger copytrade (default: `0.10`).
* `--min-cluster-buy`: Minimum cluster typical buy size to avoid dust sprayers (default: `0.10`).
* `--require-confluence`: Demands $\ge 2$ wallets from the cluster co-buy within 30s (default: `True`).
* `--tp2x`: Fraction of position to sell at 2.0x (default: `0.50`).
* `--tp5x`: Fraction of position to sell at 5.0x (default: `0.25`).
* `--trail`: Trailing stop loss percentage from peak (default: `15.0`).
* `--size-mode`: Position sizing algorithm: `fixed`, `proportional`, or `balance_pct` (default: `fixed`).
* `--size-sol`: Fixed order size in SOL when `--size-mode=fixed` (default: `0.25`).
* `--copy-ratio`: Fraction of insider buy when `--size-mode=proportional` (default: `0.50` = 50%).
* `--balance-pct`: Percentage of portfolio per snipe when `--size-mode=balance_pct` (default: `10.0` = 10%).
* `--max-size-sol`: Hard risk ceiling per trade across all modes (default: `1.00`).
* `--min-size-sol`: Minimum order size floor to clear fees (default: `0.05`).

### Step 5: Inspect Bot Trades & Performance (`cabal trades`)
Query your bot's executed trades, realized PnL, winrate, and full performance table with copyable mints:
```powershell
# 1. View bot paper executions, KPIs, Net PnL, and copyable mints
uv run cabal trades

# 2. Quick-copy a specific token mint directly to system clipboard (e.g. token #1)
uv run cabal trades --copy 1

# 3. Output raw copyable mint list (one per line, ideal for piping / DexScreener batching)
uv run cabal trades --mints

# 4. Export trade records directly to CSV or Apache Parquet
uv run cabal trades --csv .state/reports/trades.csv
uv run cabal trades --parquet .state/reports/bot_stats.parquet

# 5. View recent on-chain transactions executed by tracked cabal wallets
uv run cabal trades --cabal --limit 15

# 6. Inspect on-chain transaction history for a specific wallet address
uv run cabal trades --wallet <WALLET_ADDRESS>
```

---

## 4. Notifications & Manual Review

Configure your `.env` file:
```env
SOLANA_RPC_HTTP=https://api.mainnet-beta.solana.com
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

The bot runs 100% autonomously, but automatically alerts you on Discord and Telegram for events requiring attention:
* **`⚠️ MANUAL REVIEW REQUIRED` (Amber Embed)**: Dispatched when an insider buys with high conviction ($\ge 80\%$ baseline or $\ge 0.25$ SOL) on a fresh token, but a 2nd wallet from the cluster hasn't co-bought within 30s. The bot abstains automatically to protect capital, allowing you to manually inspect DexScreener if you want to enter.
* **`🟢 PROFIT EXIT` (Green Embed)**: Dispatched when 2.0x or 5.0x TP tiers execute, de-risking principal.
* **`🚨 ADVERSE EXIT` (Red Embed)**: Dispatched when an insider sell signature is detected, liquidating position immediately on their print.

---

## 5. Verification & Automated Tests

All core checks are automated as formal regression tests:

### Run Dedicated Verification Suite
```powershell
uv run pytest tests/test_cabal_verification.py
```
Validates:
1. `test_profitable_preset_net_ev_guarantee`: Mathematical assertion of positive EV ($>+40\%$ ROI), low breakeven ($<40\%$), and principal de-risking.
2. `test_persisted_cabal_clusters_integrity`: Data integrity check on SQLite store addresses, funder entities, and typical buys.
3. `test_manual_review_borderline_flagging`: Confirms high-conviction solo buys are tagged for manual review.
4. `test_discord_webhook_delivery_check`: Live delivery test to Discord webhook (fail-soft if not configured).

### Run Pipeline Integration Suite
```powershell
uv run pytest tests/test_cabal_pipeline.py
```
Validates:
* Address parsing, early buyer extraction, and transitive clustering.
* Signal filter gates: age, liquidity bounds, mayhem rejection, and confluence.
* Stealth `WalletPool` rotation policies (`ROUND_ROBIN`, `LRU`).
* `CabalExecutor` entry, tiered TP ladder, trailing stop, and insider dump liquidation.

### Run Full Test Suite (449 Tests Green)
```powershell
uv run pytest
```

---

## 6. Repeat-Rugger Backtest & Profiling Tools (Legacy)

The repository also includes point-in-time operator profiling and finalized replay backtesting for historical wallet investigations.

### Leakage-Safe Demo Backtest
```powershell
uv run python -m rugbot.backtest.cli --input fixtures/backtest/demo.json --pretty
```

### Inspect Single Wallet Intelligence
```powershell
uv run rug_watch --intelligence --wallet CREATOR_WALLET --pretty
```

### Terminal UI (TUI)
```powershell
uv run rug_wallet_tui --state-dir .state/watch
```

---

## 7. Safety & Policy Notice

* **Observe & Paper Only**: Per `AGENTS.md` §7, live trading remains strictly forbidden during development until out-of-sample paper verification is completed.
* **No Secret Storage**: Private keys must never be logged or committed.
* **RPC Protection**: Public RPC endpoints will rate-limit or disconnect under load. Use a dedicated private RPC for extended monitoring sessions.
