---
name: trading-tui-product
description: Product and UX contract for a high-density Textual/Python crypto trading execution terminal. Use with textual-tui-ux for wallet/target monitoring, launch/sniper workflows, paper/live execution, positions, risk, fees, and fast keyboard actions.
---

# Trading TUI Product Contract

This skill defines product semantics. It does NOT redefine generic Textual layout rules.

When active, `textual-tui-ux` MUST also govern implementation and visual verification.

## 0. Normative semantics

`MUST`, `MUST NOT`, `ONLY`, `REQUIRED`, `INVALID`, and `ABORT` are strict.
No implicit exceptions exist.

## 1. Operator questions

ASSERT: The primary execution screen MUST answer all of these without screen navigation:

1. What is happening now?
2. Which target/token caused it?
3. Is real execution armed?
4. Is real money currently at risk?
5. What position or pending execution exists?
6. What is the immediate net PnL / downside / exposure?
7. What action can the operator take now?

If any answer requires opening configuration or navigating away, the primary layout is INVALID.

## 2. Entity-local execution state

ASSERT: Execution state belongs to the individual target/strategy unless the backend contract explicitly defines a global execution boundary.

Allowed target execution states:

### `LIVE`
- monitoring enabled
- real transaction submission enabled

### `DRY_RUN`
- monitoring enabled
- execution simulated
- simulated costs MUST include configured fees and execution assumptions

### `PAUSED`
- monitoring disabled
- transaction submission disabled

A persistent ambiguous global toggle that hides per-target execution state is forbidden.
`BACKTEST` is an action, not a persistent execution state.

## 3. Real vs simulated execution

ASSERT: Real and simulated execution MUST be distinguishable immediately.

The distinction MUST use at least two independent cues chosen from explicit text, semantic color, symbol, action wording, or region treatment.
Color alone is insufficient.

Any control capable of submitting a real transaction MUST expose `LIVE` state in its immediate visual context.
A real-money action MUST NOT look identical to its dry-run equivalent.

## 4. Primary-screen information hierarchy

Operational priority MUST be:

1. execution state / critical risk
2. current position or pending transaction
3. currently actionable event
4. selected target/token strategy context
5. live feed / recent events
6. historical performance
7. infrastructure telemetry

RPC latency, block timing, websocket status, and other telemetry MUST NOT visually outrank current financial exposure unless infrastructure failure is actively blocking execution.

## 5. Target scanning

Targets/strategies MUST be scannable as a homogeneous list or table.
Each row MUST expose only fields needed for comparison or immediate state recognition.

Full configuration MUST NOT be duplicated into every row.
Selecting a target MUST update its inspector in place when navigation would interrupt monitoring.

## 6. Selected target inspector

The target inspector MUST group information by operator decision, not backend object structure.

Use these semantic groups when applicable:

1. identity / track record
2. execution state
3. entry qualification
4. execution economics
5. sizing / exits / risk

A group MUST NOT exist if it has no values relevant to the selected target.

## 7. Live event feed

The live feed MUST optimize chronological scanning.
Each event MUST expose time, event type, entity identity, and concise decision-relevant detail.

Verbose raw payloads MUST NOT appear inline in the primary feed.
Raw payload/detail MUST live behind selection, expansion, inspector, or secondary screen.
Events requiring operator action MUST visually outrank informational events.
Live updates MUST NOT steal keyboard focus.

## 8. Position and action cockpit

When a position, pending transaction, or actionable signal exists, its region MUST expose:

- real/simulated state
- token/entity
- triggering target/strategy
- size/exposure
- gross result when relevant
- estimated/realized execution costs
- net result
- exit/risk thresholds
- immediately valid actions

Net result MUST NOT be hidden behind gross result.
A destructive exit/action MUST be clearly distinguishable from non-destructive actions.

## 9. Fees and execution economics

The UI MUST distinguish configuration, estimate, and realized cost.

Applicable categories MAY include priority fee, validator/builder/MEV tip, protocol/bot fee, swap/DEX fee, slippage impact, account creation/rent effects, and network base fee.

Do not hard-code a fee category unless it exists in the actual execution contract.

When paper trading, `SIMULATED_NET_RESULT` MUST deduct every configured cost that the simulator claims to model.
If a cost cannot be modeled reliably, the UI MUST label the result as estimated/incomplete instead of presenting false precision.

## 10. Risk surface

Global risk MUST remain visible while real execution is armed.

Relevant values MAY include total exposure, max position size, realized daily PnL, daily loss limit, open position count, remaining risk budget, and kill-switch state.

Only active/configured risk controls MUST consume persistent screen space.
A breached risk control MUST outrank ordinary feed activity.

## 11. Action design

Frequent actions MUST have short bindings.
Context-invalid actions MUST be hidden or disabled rather than allowed to fail after activation.

Examples:
- no `SELL` without sellable exposure
- no `GO LIVE` if mandatory execution configuration is invalid
- no duplicate `PAUSE` action when already paused
- no `EXIT` action when no active position exists

Rare configuration actions MUST move to command palette, inspector action, or dedicated configuration screen.
The footer MUST NOT become a permanent list of every command.

## 12. Live action safety

Any action that can submit a real transaction MUST:

1. expose the active execution state;
2. expose the amount or maximum exposure before submission;
3. reject execution when required risk/config state is invalid;
4. produce immediate pending/submitted/failed/confirmed feedback.

A pending transaction MUST NOT be presented as confirmed.
A transaction failure MUST NOT silently revert to an apparently idle state.

## 13. Market-data freshness

When freshness changes the decision, market/execution data MUST distinguish live/fresh, stale, disconnected, loading, and failed.

Stale data MUST NOT retain the same visual treatment as confirmed fresh data.
A disconnected feed MUST NOT imply that monitoring remains healthy.

## 14. Product-specific anti-patterns

The following are forbidden:

- giant top-level `OBSERVE`/`ARMED` control that obscures target state
- identical styling for `LIVE` and `DRY_RUN`
- gross PnL displayed without net execution cost context
- fee settings scattered across unrelated panels
- one metric card per fee/risk value
- live logs outranking an open real-money position
- raw wallet addresses repeated at full length when a short unambiguous form works
- confirmation modal for every harmless action
- no deliberate friction for destructive real-money actions
- infrastructure metrics dominating the main dashboard while execution is healthy

## 15. Verification scenarios

In addition to `textual-tui-ux` verification, exercise:

1. no target selected
2. `PAUSED` target selected
3. `DRY_RUN` target selected
4. `LIVE` target selected
5. actionable launch/signal
6. simulated open position
7. real open position
8. failed transaction
9. pending transaction
10. stale/disconnected market data
11. breached risk limit

At `80x24`, the operator MUST still identify execution state, current exposure, immediate event, and valid action.

Read `references/example-layouts.md` for compositions, not fixed mockups.

## 16. Keyboard & Footer Bar Invariants

1. Core operator actions and tab navigation shortcuts MUST remain persistently visible in the footer bar.
2. Textual `Binding` declarations MUST include explicit `key_display` labels to ensure readable formatting across all terminal sizes.
