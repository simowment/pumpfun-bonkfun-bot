# Enemy Operator Tracing — Goal & Current State

## 1. The goal

**Identify meme-coin launch operators whose behaviour is predictable enough to be
profitably and safely sniped, and reconstruct each operator as a complete,
classified entity.**

The operational question is not "is this wallet a scammer" but:

> Does this operator run a *repeatable, predictable launch pattern* — and can we
> know it well enough to trade its next launch?

Concretely, for a given operator we need three things:

1. **Entity resolution** — the full set of wallets the operator controls
   (funders, relays, burners, bundle/satellite buyers, treasury), not just the
   one wallet we started from.
2. **Launch history** — every *token creation* the entity has produced, with the
   time, creator, staging amount, and (where still recoverable) outcome stats.
3. **Predictability gate** — enough history to compute win rate, ATH
   distribution, optimal take-profit, and net expected value after fees.

A target qualifies only when that history shows a real edge. Anything else is
rejected.

## 2. The two operator archetypes

Per the cluster playbook (*Memecoin Bible* Acte V):

| | Type 1 — Serial same-wallet deployer | Type 2 — Burner-per-launch |
|---|---|---|
| Wallet | Reuses one creator wallet for many tokens | Fresh burner per token |
| History location | On the wallet itself | Spread across burners; only the **funder** persists |
| Detectability | Easy (watch the wallet) | Hard (per-wallet views show nothing) |
| Current focus | supported | **primary** |

**Type 2 is the hard case and the one that matters.** Its burners create exactly
one token each and never appear again, so any per-wallet launch view is
structurally blind. The entity's history lives in the wallets its *funder*
disbursed staging capital to.

## 3. What "solved" looks like

- [ ] Given a seed (mint or wallet), resolve the entity's wallet set with roles.
- [ ] Reconstruct the entity's full token-creation timeline from its funder(s).
- [ ] Carry per-token outcome stats (mcap, ATH, graduated) where recoverable.
- [ ] Flag which tokens are *not* backtestable rather than silently zeroing them.
- [ ] Compute win rate / optimal TP / net EV over the entity's launches.
- [ ] Arm a watcher on the funder so the *next* launch is caught live.

## 4. What exists today

| capability | command | status |
|---|---|---|
| Wallet dossier + lite TP profile | `rug_wallet <wallet> --trace-funding --lite-profile` | pre-existing |
| Upstream funding spine + hub payouts | `rug_chain <wallet>` | built & live-verified |
| Classified entity graph + funding batches | `rug_graph <seed>` | built & live-verified |
| Entity token-creation timeline | `rug_entity_history <funder>` | built & live-verified |
| Entity launch alerts (Discord) | `rug_entity_watch` | pre-existing |
| Replay / TP×SL optimisation | `rug_backtest <wallet\|mint> --optimize` | pre-existing |

### Evidence that the approach works

From a single seed token (COTE), the funder `BmFdpraQ…` was reached and its
launch-window dispersals reconstructed into a token timeline:

```
2026-09-10 20:04  COTE      creator ArDMYcz6…   (via relay chain)
2026-09-10 20:08  WOBBLE    creator BwHgy3tS…   funded 3.3444 SOL
2026-09-10 20:33  Bigduck   creator DoMBRKzx…   funded 3.3444 SOL
2026-09-10 20:35  MINIJUG   creator 4oZc1d2N…   funded 7.6351 SOL
```

Four creations in 31 minutes, four distinct burners, one funder.

## 5. Hard constraints discovered

These are measured, not assumed:

1. **Candle retention is the wall.** The candle endpoint only retains a recent
   window. Tokens days old lose most candles; tokens months old return *no*
   data at all (`mcap=None`, `ath=None`, zero candles). **Retroactive
   backtesting is impossible for anything but very recent launches** — outcome
   data must be captured forward, at launch time.
2. **The creator index never reports a funder.** It only counts tokens where the
   wallet *is the creator*. A funder's launch count is therefore always `0` and
   carries no information.
3. **Depth is bounded, not exhaustive.** Enumeration walks a `before` cursor for
   `--pages × 1000` signatures. One funder measured 6000+ signatures over ~36
   hours; the default 3 pages covers only ~2 hours.
4. **Free-RPC burst limits.** Signature paging and deep hydration hit HTTP 429
   under load; every scan needs pacing and cannot run unbounded.
5. **Deliberate obfuscation.** Some operators cycle value through chains of
   single-use relay wallets and circular transfers, so a walk can terminate at
   infrastructure rather than a stable root. Entity attribution is therefore
   always **circumstantial** — the standard is how many independent signals
   agree, not proof.

## 6. Known gaps

- **No funder watcher.** Nothing arms on the funder to catch the next launch
  live. This is the last piece before sniping is possible.
- **No `--all-pages`.** Depth is chosen, not exhausted.
- **No per-token stats in the history command.** `--stats` would attach mcap /
  ATH / graduated / candle-availability to each event.
- **No cross-funder merge.** Multiple funders for one entity are traced
  separately.
- **Classification limits.** `REPEAT_BUNDLER` requires cross-launch evidence a
  single graph cannot establish; relay-vs-deployer needs a time dimension the
  current heuristics lack.

## 7. How we verify

- **Live paths, not mocks** — every command must be run against real mainnet
  data and its output inspected.
- **Cross-check inferred stats** — ATH figures come from `ath_market_cap`, whose
  units are flagged ambiguous; ratios must be validated against candles before
  being trusted in a backtest.
- **Independent signal agreement** — an entity link is only as strong as the
  number of independent corroborating signals (shared funder, shared bundle
  wallets, timing, co-participation). A shared funder alone is suggestive, not
  conclusive.
- **Reject honestly** — a target that cannot clear the sample-size / win-rate /
  positive-EV gate is reported as rejected, with the reason.
