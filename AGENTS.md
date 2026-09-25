# AGENTS.md — Project Rules

The shared normative framework lives in `~/.config/opencode/AGENTS.md` (pipeline, A1–A4, D1–D3, R1–R5, C1–C4, I1–I4, X1–X2, V1–V6, H1–H3, review priority, hard bans, default decision rule) and applies here. This file adds **project rules and overrides only** — no duplication.

## 1 · Dependencies
- Add deps with `uv add <pkg>` (updates `uv.lock`).
- Restart running bots after dependency changes.
- Target Python 3.11+.

## 2 · Conventions
- Ruff per `pyproject.toml`; 88 cols; double quotes.
- Google-style docstrings; type hints on all public functions.
- Logger: `from rugbot.utils.logger import get_logger`.

## 3 · Quality gates (run before completing)
| cmd | purpose |
| --- | --- |
| `ruff format` | format to project standard |
| `ruff check` | lint |
| `ruff check --fix` | autofix where possible |

## 4 · Platform
- Check Pump.fun protocol compatibility for core-logic changes.
- Update `src/platforms/pumpfun/` when needed; verify IDL matches on-chain programs.

## 5 · Safety
- Never expose private keys (code, logs, commits).
- Test with minimal amounts; verify tx on explorer before scaling.
- Respect RPC rate limits; keep logs for audit/debug.

## 6 · Commands
| cmd | purpose |
| --- | --- |
| `uv sync` | install/update deps |
| `source .venv/bin/activate` / `uv pip install -e .` | venv / editable install |
| `uv run rug_watch --once` | one finalized read-only pass (DB config in `state.sqlite3`) |
| `uv run python -m rugbot.backtest.cli --input fixtures/backtest/demo.json --pretty` | canonical demo backtest |

## 7 · Core-Only Delivery Policy
Private solo project. Optimize for the smallest reliable system proving: can it identify a known operator pattern and demonstrate a fully-executable, net-profitable exit in paper/backtest?
1. No backward-compat layers, migrations, deprecation shims, versioning; do not preserve old interfaces unless the user asks.
2. No ad-hoc scripts, one-off JSON contracts, improvised decoders, fake readiness, or parallel implementations. Use canonical typed contracts/fixtures.
3. Core only: read-only ingestion, finalized replay, point-in-time wallet/entity tracking, operator features, adverse-event attribution, executable quote simulation, paper entry/exit, leakage-safe backtesting, fail-closed gates.
4. Defer dashboards, UI, deploy automation, multi-provider abstractions, plugin systems, broad refactors.
5. Each change: state the core behavior enabled, keep a disjoint scope, add focused tests, stop when the acceptance behavior is proven.
6. Strict identifiers already in code are fixed validation constants — do not extend them into version management.
7. NEVER run live trading while developing. Observe/paper/simulation only until separate explicit authorization after out-of-sample evidence.
8. No scratch artifacts at repo root. One-off output → `C:\Users\got\AppData\Local\Temp\opencode\` or `.state/`. Only `fixtures/` is committed in-tree.

## 8 · Verification (hardened)
- Unit tests alone NEVER prove usability/readiness; a passing suite must never be used to claim the bot is operational.
- Validate every UI action, tracker update, strategy transition, and execution flow with realistic integration tests, live RPC/fixture replays, and end-to-end pipelines.
- No mock-only verification: prove ingestion → state mutation → UI update → execution dispatch.
- Run end-to-end CLI (`rug_watch --once`, backtest replay on golden data, live transport) before declaring completion.

## 9 · Operator Target Mandate
When given a wallet or mint, the ONLY question: does this operator show a predictable, repeatable, profitably-snipable launch pattern?

### 9.1 Entity ≠ wallet (NORMATIVE)
`ASSERT: an entity is the SET of wallets one operator controls; a wallet is NEVER an entity.`
- Spans funders, relays, burners/deployers, bundled buyers, treasury.
- Per-wallet rows MUST be reconciled into ONE entity before counting/ranking/reporting: `discover_rugger_cache` is 1 row per *seed wallet*; `tracker_entity_nodes` is 1 row per *wallet*.
- Merge wallets sharing a funder, entity-wallet membership, or the same distinctive symbol in the same launch window; merge transitively.
- Before any count: group → merge transitively → report `entities = <n>` WITH each wallet set. Reporting a wallet/seed count as entities is FORBIDDEN.

### 9.2 Archetypes
1. **Type 1 — serial same-wallet deployer (primary)**: many tokens from one wallet. Score winrate/ATH/dump-speed/EV from that wallet; re-arm a listener on it.
2. **Type 2 — burner-per-launch**: fresh burner per token; the history lives on the funder. Long-run method = watch upstream funding nodes for the staging transfer, then arm the fresh burner.

### 9.3 Scope S1 (binding)
`ASSERT: S1 = Type 1.`
- `TargetKind` is exactly `{WALLET, TOKEN}` (`runtime/config.py`); `runtime/matcher.py` fail-closes on any other kind.
- **Type 2 auto-arm: AUTHORIZED (explicit user authorization, 2026-09-11)** — build the exchange-hot-wallet staging watcher + arming as **OBSERVE/PAPER ONLY**. It MUST NOT place live orders; live trading remains forbidden until separate authorization after out-of-sample evidence (§7 item 7).
- Until that watcher ships, `discover/ruggers.py::_next_action` returning the honest manual instruction for Type 2 is correct behavior.
- Do NOT add speculative Type 2 arming beyond the authorized observe/paper watcher (R1/YAGNI).

### 9.4 Reporting contract (measurements, not verdicts)
The tool MEASURES; the user JUDGES. It MUST NOT emit a "worth tracking / qualified" verdict; thresholds are reference lines only. Report:
1) archetype (Type 1 / Type 2);
2) entity activity — N launches, cadence, first/last, active? (N≥10 = **activity/sample-size indicator**, not a winrate gate);
3) bundler winrate — operator-behavior **indicator** (distinct from backtest winrate);
4) backtest output — **net EV is the primary metric**; TP/SL grid, winrate, fee breakdown, per-sample ATH are context;
5) next action (Type 1: re-arm dev wallet; Type 2: manual funder instruction);
6) entity count = reconciled wallet sets.

## 10 · Long-Operation Durability (NORMATIVE)
`ASSERT: any operation that can exceed ~10s MUST persist its results so an interruption or re-run never repeats the work.`
- Saving is a property of the operation, NOT the caller's shell — never rely on ad-hoc scripts or stdout redirection to "save" a scan.
- External reads >10s (RPC, pump.fun REST, creator index, candles) MUST flow through the shared durable cache (`rugbot.integrations.rpc_cache.RpcResponseCache`, `rpc_cache.sqlite3`), keyed by (endpoint/method, canonical params), explicit TTL, per-response commit.
- Immutable reads (finalized tx, `before`-cursor signature pages, finalized trade pages) → cached indefinitely. Mutable reads (newest signature page, creator-index counts, live candle windows) → bounded TTL.
- Multi-step scans persist checkpoints as they go. A long-running command is NOT "working" until this holds.
- **Fast by default — no slow commands (NORMATIVE)**: a user-facing command MUST return in **under 30 seconds**. Network work that cannot fit in that budget belongs to a **background collector that persists into the local store**; the command then reads the store. A command that blocks for minutes of synchronous network I/O is a **defect**, not a scan — fix it by adding persistence, not by telling the user to wait.

## 11 · Overrides to the shared framework
- **Unit-test limit**: unit tests alone NEVER satisfy feature verification OR proof of bot usability; local use for pure helpers only, never as evidence a user workflow works.
- **Output contract**: concise, human-readable. Include only useful sections — what changed (1–3 sentences), `Changed:` paths+reasons, `Verification:` actual checks+results, `Notes:` decisions/risks. No task/diff restatement, no empty sections, only executed checks, state unverified boundaries explicitly. Blocked → `BLOCKED / Reason / Needed`. No change → `NO CHANGE / Reason`.
- **Default decision rule** (priority order): 1 contract correctness, 2 architectural locality, 3 explicitness, 4 local discoverability, 5 real-path verifiability, 6 minimal abstraction, 7 minimal total comprehension token cost, 8 ease of deletion/replacement. Lower MUST NOT override higher; if no option works without violating a normative rule → ABORT.
- **Real-world execution & data honesty**: 1) sim/paper MUST model slippage, liquidity floor, adverse impact — not ideal unexecutable exits; 2) live feeds display only real-time events, never pre-populate with historical/synthetic/mock data; 3) backtest UI MUST render winrate, sample size, fee breakdown, net ROI; 4) prefer fast unified integration tests over sprawling fragile mock unit tests.
- **Exit modeling — no fixed stop on a rug (NORMATIVE)**: a fixed stop-loss MUST NOT be treated as an executable exit. On a rug there is no fill at −10/−20/−30%; the real exit is whatever prints at the **dev/bundle sell leg** (bundle wallets often sell in the same candle). Therefore: 1) the primary backtest EV MUST come from a **no-fixed-stop model** (TP hit, else exit at the observed adverse print); 2) fixed-SL grid rows are **scenario/sensitivity only** and MUST be labelled as such in output — never presented as the executable result.
