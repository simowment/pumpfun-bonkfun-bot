# Memecoin Bible — Sniper Manual

*Restructured for token efficiency and algorithmic use. Content preserved from the French source.*

Two layers, always separate:
- **LAYER 1 · DISCOVERY** — find candidate mints (human, Axiom/Photon).
- **LAYER 2 · TRACKING** — follow a funding source (bot, Solscan/RPC).

---

## 0 · Layer contract — READ FIRST

| | LAYER 1 · DISCOVERY | LAYER 2 · TRACKING |
|---|---|---|
| Run by | user, manual | bot (Solscan / RPC) |
| Input | market screener | one candidate mint |
| Mechanism | UI filters (Axiom/Photon) | funding-source walk, creator index |
| Output | candidate mint list | data sheet (activity, winrates, EV) |

`RULE 1: discovery filters screen TOKENS; tracking rules follow WALLETS. Never use one as the other.`

Screener settings (dev-creation count, platform, quote, volume, mcap) are **discovery only** — they are NOT wallet-graph rules.

`RULE 2: the tracking tool MEASURES; the user JUDGES. It never emits "worth tracking / qualified".`

---

## 1 · LAYER 1 — DISCOVERY (user, Axiom/Photon)

Goal: find candidate mints whose operator is likely a repeatable, snipable rugger.

```text
D1  apply screener (1.1)                      -> candidate list
D2  sort oldest->newest; isolate the launch
    and the block-0 bundle buys
D3  read the creation 1s candle;
    REJECT if its mcap > $15k                 (entry sanity, §3)
D4  hand each survivor mint to LAYER 2
```

### 1.1 Screener filters (discovery only)
| filter | value | why |
|---|---|---|
| dev creations | <= 5-10 | drop mass spammers |
| platform | Pump.fun only | only source of the pattern |
| quote | SOL only | reason in SOL |
| volume | ~ $20-30k | real pump activity |
| maturity | mint ~1h old, already dumped | full pump->ATH->floor cycle |
| window | 00:00-06:00 UTC+1 | operator activity peak |
| columns | MC, volume, fees, dev-creation count, funding time | signal > noise |

---

## 2 · LAYER 2 — TRACKING (bot, Solscan/RPC)

Goal: from one candidate, resolve the operator's funding source and enumerate the fleet's other launches.

```text
T1  creator C  = mint.creator
T2  source  S  = wallet that paid C's first SOL
T3  classify funding shape (2.1)
T4  enumerate mints created by S's fleet (last 7-10 days)
T5  measure: activity + bundler winrate + backtest EV (§3-5)
T6  hand the data sheet to the user (no verdict)
```

### 2.1 Funding shapes — pick the simplest that applies
| # | shape | S = | difficulty |
|---|---|---|---|
| 1 | direct `A -> B -> C` | previous creator wallet | easy |
| 2 | `CEX -> fresh wallet` | CEX hot wallet + tight amount band | most common & profitable |
| 3 | `mother -> sub-mother (rotated 3-4h) -> bundle wallets` | walk the intermediate address | hard / obfuscated |

**Build on shape 1 or 2. Do NOT build on shape 3.**
- shape 2 rule: tight amount interval (e.g. `2.40-2.60 SOL`) + `fresh-wallet-only`. Track the AMOUNT, not the identity.
- shape 3 present (relays / randomized amounts / mixers) -> go to 2.2.

### 2.2 Shape 3 -> satellite copytrade (stop tracing funding)
Detect the bundle wallets directly:
1. red-candle: who dumped large at the top
2. same-block bundle: co-timed buys, similar amounts
3. cross-basket: same Token A/B/C as the operator
4. dev-buy: small public dev buy; bundle wallets take 40-60% of supply

---

## 3 · Reference thresholds (NOT a verdict)

The tool reports measurements; the user judges. These are reference lines, not pass/fail gates.

| measurement | reference | note |
|---|---|---|
| entity activity | N >= 10 launches | **activity / sample-size indicator**, NOT a winrate gate |
| post-bundle amplitude to ATH | >= +100% | target profile |
| creation 1s-candle mcap | <= $15k | entry-price sanity |
| max adverse excursion | <= 30% | risk profile (TP:SL >= 1:3) |
| winrate @ TP +100% | >= 33% | reference floor only |
| **backtest net EV** | **> 0** | **the decision metric** |

---

## 4 · Entry & block-0

`Entry = close of the first 1s candle AFTER the creation/bundle candle.`

The creation 1s candle contains the dev + bundle fills (block-0). An outside tx lands in the NEXT second — the creation candle's price is NOT an achievable fill.

| mode | entry block | entry price | TP margin | floor risk |
|---|---|---|---|---|
| dedicated bot (Jito/B0) | 0 | floor ~ $3k MC | +150..300% | -10..-20% |
| public bot (Trojan/Bloom) | +1..+2 | $8-12k MC | +20..40% | -60..-80% |
| manual | +4..+5 | >= $15k MC | negative | -80..-90% |

Latency cost: being 2nd-3rd after the bundle ~ **0.115 SOL/trade** (~11.5 SOL over 100 snipes).

---

## 5 · Backtest output — EV is the metric

```text
net_ev   = mean over samples of [ (exit - entry) - fees ]   # per-trade expectancy
net_pnl  = sum of net PnL over samples
winrate  = wins / N                                          # context, NOT the goal
fees     = base network + priority + Jito tip
```

Report per TP/SL cell: **net EV (primary)**, net PnL, winrate, N, fees, max drawdown.
A backtest is "good" when net EV > 0 — not when winrate is high. Low winrate with +EV wins; high winrate with -EV loses.

### 5.1 Two winrates — never conflate
| winrate | measures | role |
|---|---|---|
| **bundler winrate** | did the OPERATOR profit (bundle sold into strength) | operator-behavior **indicator** |
| **backtest winrate** | did YOUR sniper replay profit | context for the EV |

An operator can be ~90% profitable while your replay is ~30% (bundle dumps before your TP). Report both; never merge them.

The engine measures; it does not label a target "worth tracking".

### 5.2 No fixed stop on a rug (NORMATIVE)

A fixed stop-loss is NOT an executable exit. On a rug there is no fill at −10/−20/−30% — the real exit is whatever prints at the **dev/bundle sell leg** (bundle wallets often sell in the same candle, so the dump is one print, not a staircase).

- **Primary EV = no-fixed-stop model**: TP hit, else exit at the observed adverse print (the dev/bundle dump / floor).
- **Fixed-SL grid rows are scenario/sensitivity only** — label them and never present them as the executable result.
- Estimate the loss from the big dev-sell candle (or the bundle sell landing with it), not from a stop level.

---

## 6 · Execution & risk
- size: 2-5% of bankroll per trade
- anti-detection: keep orders small; scale via multiple wallets, not size
- dev dumps right after your buy -> you were spotted: rotate the target
- active targets: <= 2-3 operators
- operator lifetime: ~1-2 weeks before the scheme changes -> re-audit continuously
- circuit breakers: 5 consecutive losses -> stop the day; -35% week -> pause + full re-audit
- TP discipline: fix TP +100%; never turn a mechanical exit into a hope trade

---

## 7 · Key metrics
| param | value | why |
|---|---|---|
| **backtest net EV** | **> 0** | **the decision metric** |
| entity activity | >= 10 launches | sample-size/activity indicator (not a gate) |
| bundler winrate | measure | operator-behavior indicator (separate from backtest winrate) |
| winrate @ TP +100% | >= 33% | reference floor (1 win covers 2 losses @ -40%) |
| creation-candle mcap | <= $15k | floor risk down to ~$2.5k |
| size | 2-5% | survive 5-7 loss streaks |
| TP | +100% | exit before the operator's bundle dump |
| SL | dev-sell -> exit | leave when creator/bundle start selling |
