
## 2. Keep Dependencies in Sync

If you add or update dependencies:

1. Use `uv add <package>` to add new dependencies.
2. The `uv.lock` file will be automatically updated.
3. Restart any running bots after dependency changes.
4. Verify compatibility with Python 3.11+ as specified in the project.

## 3. Coding Conventions

* Follow Ruff linting rules defined in `pyproject.toml`.
* Use Google-style docstrings for functions and classes.
* Include type hints for all public functions.
* Use the centralized logger: `from src.utils.logger import get_logger`.
* Keep line length to 88 characters (auto-formatted).
* Use double quotes for strings.

## 4. Code Quality Checks

Before completing any task, run these quality checks:

| Command                 | Purpose                                    |
| ----------------------- | ------------------------------------------ |
| `ruff format`           | Format code to project standards          |
| `ruff check`            | Run linting checks                        |
| `ruff check --fix`      | Auto-fix linting issues where possible    |



## 8. Platform-Specific Development

When adding features:

* Check Pump.fun protocol compatibility when changes affect core logic.
* Update the Pump.fun implementation in `src/platforms/pumpfun/` when needed.
* Verify IDL files match the on-chain programs.

## 9. Safety Reminders

* **Never expose private keys** in code, logs, or commits.
* **Test with minimal amounts** first.
* **Verify transactions** on Solana explorer before scaling up.
* **Monitor rate limits** of your RPC provider.
* **Keep logs** for audit and debugging purposes.

## 10. Useful Commands Recap

| Command                                            | Purpose                           |
| -------------------------------------------------- | --------------------------------- |
| `uv sync`                                          | Install/update dependencies       |
| `source .venv/bin/activate`                       | Activate virtual environment      |
| `uv pip install -e .`                              | Install package as editable       |
| `uv run rug_watch --once`     | Run one finalized read-only pass (DB config in state.sqlite3)  |
| `uv run python -m rugbot.backtest.cli --input fixtures/backtest/demo.json --pretty` | Run the canonical demo backtest |

---

## 12. Core-Only Delivery Policy

This is a private solo project. Optimize for the smallest reliable system that
answers the core question: can the system identify a known operator pattern and
demonstrate a fully executable, net-profitable exit in paper/backtest mode?

1. Do not add backward-compatibility layers, migration paths, deprecation
   shims, version negotiation, version overrides, or new application/API/
   schema/artifact versioning. Do not preserve old interfaces unless the user
   explicitly requests it.
2. Do not add ad hoc scripts, one-off JSON contracts, improvised decoders,
   fake readiness evidence, or parallel implementations. Use the canonical
   typed contracts and fixtures already in the repository.
3. Prioritize only core functionality: read-only observation ingestion,
   finalized replay, point-in-time wallet/entity tracking, operator behavior
   features, adverse-event attribution, executable quote simulation, paper
   entry/exit decisions, leakage-safe backtesting, and fail-closed safety
   gates.
4. Defer dashboards, UI, deployment automation, multi-provider abstractions,
   optional low-latency transports, generalized plugin systems, broad
   refactors, and operational polish unless they are required to validate a
   core decision path.
5. Every agent must state the concrete core behavior it enables, keep changes
   within a disjoint scope, add focused tests, and stop when the acceptance
   behavior is proven. Do not expand a task merely because a more general
   architecture would be interesting.
6. Existing code may contain strict identifiers needed to reject mismatched
   evidence. Treat them as fixed validation constants; do not extend them into
   compatibility or version-management systems.
7. Never run live trading while developing this system. Observe, paper, and
   route-simulation execution remain the permitted development modes until the
   user gives separate explicit authorization after out-of-sample evidence.
8. **Never write temporary/scratch JSON files to the repository root.** Any
   one-off output produced during analysis (sample mint lists, scan dumps,
   RPC results, temporary data) MUST be written to
   `C:\Users\got\AppData\Local\Temp\opencode\` (the pre-approved scratch dir)
   or under `.state/`. Do NOT drop `*.json`/`*.csv`/scratch artifacts at the
   repo root. Only canonical committed fixtures under `fixtures/` are allowed
   in-tree.

---

## 13. Verification Policy: Integration & Live Replay Tests Required

**Unit tests alone are strictly NOT accepted as verification that the bot is usable, functional, or ready.**

1. **Unit Tests Do NOT Prove Usability**: Running isolated unit tests or isolated mocks does not verify that the bot is actually usable in production or practice. A passing test suite must never be used to claim that the bot is operational.
2. **Mandatory Integration & Usability Verification**: Every UI action, tracker update, strategy transition, and execution flow must be validated using **realistic integration tests**, live RPC/fixture replays, and full end-to-end component pipelines.
3. **No Mock-Only Verification**: Do not rely on isolated mock units to claim working state. Tests must verify the actual end-to-end flow: from event ingestion -> state mutation -> UI table & card updates -> execution dispatch.
4. **Live Replay & CLI Tests**: Run end-to-end CLI runs (`rug_watch --once`, backtest replay against golden datasets, live WebSocket/HTTP transport integration) before declaring usability or completion.

---

## 14. Operator Target Analysis Mandate: Predictable Snipable Launch & Archetype Classification

**When the user provides a wallet or mint address, the ONLY operational question is whether this operator exhibits a predictable, repeatable launch pattern that can be profitably and safely sniped.**

### 🏛️ The Two Rugger Archetypes (*Memecoin Bible* Acte V)
Every target MUST be classified immediately into one of two distinct operational archetypes:

1. **Type 1: Serial Same-Wallet Deployers (PRIMARY FOCUS)**:
   - **Definition**: The operator creates multiple tokens from the **exact same wallet address** ($N \ge 2$, often dozens).
   - **Why it is prioritized**: Full launch history (Winrate, ATH distribution, dev dump speed, launch cadence) is directly attached to the public address. It is the most reliable and actionable target for manual users and automated snipers.
   - **Operational Strategy**: Score historical winrate and EV directly from the wallet's signatures. Arm an event listener directly on this known wallet address awaiting the next `pump::create`.

2. **Type 2: Disposable Burner-per-Launch Operators (Cluster Tracking)**:
   - **Definition**: The operator creates a brand new, clean disposable burner wallet for each individual token (1 token per wallet, 0 prior history on the deployer itself).
   - **Operational Strategy**: The deployer address cannot be predicted beforehand; the system MUST watch upstream funding nodes (mother / relay / CEX) in real-time to detect the staging transfer (0.2 - 3.0 SOL) and dynamically arm the listener on the newly funded burner before `pump::create`.

### 📋 Systematic Target Reporting Contract
Every target analysis MUST systematically extract and report:
1. **Archetype Sorting**: Explicitly declare whether the target is **Type 1 (Same-Wallet Serial Dev)** or **Type 2 (Burner-per-Launch Cluster)**.
2. **Launch History & Cadence**: Exact frequency and intervals between launches (e.g. 1 launch every 3.5 hours, burst cadence).
3. **ATH & Exit Profile**: Distribution of historical peak multipliers (median ATH, peak ATH, floor liquidity), and dev holding time before rugging/selling.
4. **Qualification & Mathematical Net EV (*Memecoin Bible* Acte V)**: Winrate ($\ge 70\%$) on sample size $N \ge 10$, analytical optimal Take-Profit, and net positive EV after all Solana network and Jito tip fees.
5. **Next Target / Arming Action**:
   - For Type 1: Re-arm listener directly on the known dev wallet address.
   - For Type 2: Identify the staged burner wallet or arm on upstream funder.

---

Following these practices ensures safe development, prevents accidental trades, and maintains code quality. Always prioritize testing and security when working with trading bots.


# AGENTS.md

## 0. Normative Semantics

This file governs modifications to a real production-oriented codebase.

The keywords `MUST`, `MUST NOT`, `ONLY`, `REQUIRED`, `FORBIDDEN`, and `ABORT` are normative.

No implicit exception exists. An exception is valid ONLY when this file explicitly lists it or the user explicitly overrides the rule.

When two rules conflict, apply the stricter rule unless doing so violates an explicit user requirement or external contract. If the conflict cannot be resolved deterministically, ABORT before writing code.

Core invariant:

```text
ASSERT: S0 + Δ = S1
```

Where:

- `S0` = verified current codebase state.
- `S1` = required target state.
- `Δ` = exact additions, deletions, and edits required to reach `S1`.

The agent MUST NOT speculate, invent missing contracts, hide uncertainty, or claim unexecuted verification.

---

## 1. Execution Pipeline

Every modification MUST execute this pipeline in order:

```text
ARCHITECTURE → DISCOVERY → REDUCTION → GENERATION → VERIFY
```

No stage may be skipped.

Generation MUST NOT begin until architecture, relevant local state, and required external contracts are known.

ABORT before code generation if any REQUIRED precondition is unavailable or contradictory.

---

## 2. Architecture

### A1. Canonical Placement

ASSERT: Logic MUST live in the layer that owns the behavior.

Examples:

- UI components MUST NOT contain payment-domain logic.
- API routes MUST NOT contain provider-specific parsing.
- Provider adapters MUST NOT contain storefront display logic.
- Database models MUST NOT perform external API calls.
- Schemas validate shape; services execute behavior.

### A2. No Localized Fix in Generic Flow

ASSERT: A localized edge case MUST NOT be inserted as an ad-hoc branch in a generic path.

If behavior varies by provider, channel, entity type, environment, or other stable discriminator, the variation MUST be represented through a canonical boundary such as:

- provider adapter,
- strategy/dispatch map,
- domain policy,
- validated configuration,
- schema-level normalization,
- feature-specific module.

Scattered checks such as `if provider === ...` across unrelated code are FORBIDDEN.

### A3. Extraction Requires Proven Value

ASSERT: Single-call-site logic MUST remain inline UNLESS extraction satisfies at least one condition below:

- multiple real call sites exist,
- a clear domain boundary is enforced,
- an external provider boundary is enforced,
- infrastructure is separated from domain logic,
- a real integration boundary requires isolation,
- extraction materially reduces total comprehension context for the bounded concern.

The following are NOT valid reasons:

- "looks cleaner",
- "might be useful later",
- "could scale later",
- agent preference for smaller functions,
- stylistic symmetry.

### A4. Domain Scaling

ASSERT: Code MUST scale by entity, feature, provider, or bounded context; vague technical buckets are FORBIDDEN.

Valid:

```text
models/product_models.py
models/order_models.py
services/stripe_service.py
routes/storefront_checkout.py
```

Invalid:

```text
models.py
services.py
utils.py
helpers.py
misc.py
stuff.py
```

A split is INVALID if it increases navigation and indirection without establishing a real boundary or lowering total comprehension context.

---

## 3. Discovery

Speculative coding is FORBIDDEN.

### D1. Local Discovery

Before creating any helper, type, schema, service, adapter, dependency, or utility, the agent MUST inspect the existing codebase for an existing canonical implementation.

REQUIRED checks:

1. Search relevant terms and symbols.
2. Inspect sibling files, imports, and call sites.
3. Inspect canonical model/schema locations.
4. Inspect installed dependencies before adding one.
5. Inspect native platform capabilities before implementing custom behavior.

ASSERT: Duplicate helpers, schemas, types, services, or equivalent logic MUST NOT be created.

### D2. External Contract Discovery

ASSERT: External behavior MUST NOT be guessed.

The agent MUST verify relevant external contracts from an authoritative source before depending on them, including as applicable:

- official API documentation,
- official SDK types,
- official schemas,
- official webhook examples,
- provider sandbox responses,
- recorded real response fixtures.

FORBIDDEN guesses include:

- API field names,
- environment variable names,
- configuration keys,
- webhook shapes,
- provider statuses,
- SDK behavior,
- undocumented fallback keys.

Invalid:

```ts
const userId = payload.userId || payload.user_id || "anon";
```

Valid:

```ts
const PayloadSchema = z.object({ user_id: z.string() });
const payload = PayloadSchema.parse(rawPayload);
```

### D3. Missing Capability

If a REQUIRED capability is unavailable, the agent MUST NOT fabricate the result.

Return:

```text
BLOCKED_BY_MISSING_CAPABILITY
Capability needed: [precise capability]
Why required: [reason]
Safe next step: [minimal action]
```

---

## 4. Reduction & Token Efficiency

### R1. YAGNI

ASSERT: Speculative implementation MUST NOT be added.

Implementation is allowed ONLY when supported by at least one present requirement:

- current feature requirement,
- real call site,
- real contract,
- real user path,
- required architectural boundary.

Hypothetical future use is not evidence.

### R2. Implementation Order

The agent MUST evaluate implementations in this order:

```text
Native capability
→ Existing project primitive
→ Minimal local implementation
→ Verified dependency
```

A new dependency is allowed ONLY when local implementation would create material complexity, security risk, protocol risk, or duplicate a trusted standard implementation.

Dependencies for trivial string, array, object, formatting, or equivalent basic operations are FORBIDDEN.

### R3. Minimum Correct Delta

ASSERT: `Δ` MUST contain no code unnecessary to satisfy the verified contract.

The agent MUST:

- reuse existing canonical schemas and primitives,
- delete obsolete code instead of layering around it,
- use data representation instead of branch proliferation when equivalent and clearer,
- keep one-off logic inline unless A3 permits extraction,
- avoid transformations unrelated to the requested behavior.

### R4. Total Comprehension Token Cost

ASSERT: Minimize total comprehension tokens, not visual code size.

Total comprehension cost includes:

- implementation code,
- types and interfaces,
- abstraction layers,
- indirection,
- boilerplate,
- navigation across modules,
- duplicated concepts,
- duplicated instructions needed to understand behavior.

When two implementations satisfy the same contract with equivalent correctness and readability, the lower total comprehension cost MUST be selected.

A transformation is INVALID if it reduces local code size while increasing the context required to understand or safely modify the bounded behavior.

Token reduction MUST NOT be achieved through:

- cryptic naming,
- minification,
- compressed or obscure control flow,
- hidden invariants,
- semantic overloading,
- removal of required type information,
- abstraction that merely moves complexity elsewhere.

### R5. Token-Waste Ban

ASSERT: Redundant semantic material MUST be removed when doing so preserves behavior and discoverability.

FORBIDDEN without proven value:

- duplicate implementations,
- duplicate schemas,
- repeated wrappers,
- dead code,
- ceremonial interfaces,
- comments that only restate code,
- forwarding methods with no boundary semantics,
- repeated normalization of the same contract,
- parallel abstractions representing the same concept.

---

## 5. Control Flow & Concurrency

### C1. Path Complexity

ASSERT: `Δ` MUST NOT introduce avoidable decision paths.

Every added branch MUST correspond to a distinct required contract state.

Branches introduced only for implementation convenience, speculative behavior, or provider quirks outside the provider boundary are FORBIDDEN.

Prefer explicit maps, schemas, policies, and canonical dispatch boundaries over repeated nested conditionals when they represent the same contract more directly.

### C2. Independent Async

ASSERT: Independent asynchronous operations MUST execute concurrently.

Sequential execution is allowed ONLY when at least one condition is true:

- a later operation depends on an earlier result,
- ordering is part of the verified contract,
- concurrency violates a documented rate limit,
- concurrency violates a documented resource bound,
- concurrent side effects are unsafe.

No other exception is permitted.

### C3. Naming

ASSERT: Names MUST be explicit, domain-accurate, and locally understandable without external decoding.

Valid:

```text
calculate_signal
process_payment
create_shipping_quote
provider_order_service.py
```

Invalid:

```text
calc_sig
my_calculate_signal
misc.py
stuff.py
new_utils.py
```

Shorter names are INVALID when they reduce semantic clarity merely to save tokens.

### C4. Non-Obvious Literals

ASSERT: Non-obvious behavioral literals MUST be named constants or validated configuration.

Inline literals are allowed ONLY when their meaning is self-evident from the local contract, such as booleans, obvious indexes, schema keys, UI labels, or standard protocol values.

---

## 6. Contracts, Types & Errors

### I1. External Input Validation

ASSERT: Every external input MUST be validated or narrowed at the application boundary before trusted use.

This includes as applicable:

- environment variables,
- webhook payloads,
- provider responses,
- API input,
- query parameters,
- uploaded files,
- payment metadata,
- user-submitted forms.

### I2. Strict Types

ASSERT: Internal behavior MUST operate on explicit, narrowed contracts.

FORBIDDEN:

```text
any
unknown without narrowing
raw casts
double casts
untyped provider payloads
untyped webhook payloads
unvalidated environment access
```

`unknown` is allowed ONLY at an external boundary and MUST be narrowed before domain use.

Generated official SDK types and schema-validated contracts are allowed.

### I3. No Guessed Fallbacks

ASSERT: Fallback chaining MUST NOT be used to guess contracts.

Invalid:

```ts
const key = process.env.STRIPE_SECRET_KEY || process.env.stripeKey || "mock-key";
```

Valid behavior is to validate the canonical key and fail explicitly if absent.

### I4. Error Handling

ASSERT: A caught error MUST be intentionally recovered, translated, or enriched; otherwise it MUST propagate unchanged.

Catch-and-return-empty behavior that hides failure is FORBIDDEN.

Fallback values such as `[]`, `{}`, `null`, `"unknown"`, or `"mock"` MUST NOT conceal runtime failure.

Logging MUST occur once at the boundary that owns operational reporting. Lower layers MUST NOT duplicate logs unless they add distinct required telemetry that cannot be emitted by the owning boundary.

---

## 7. Deletion & Compatibility

### X1. Active Deletion

ASSERT: Superseded internal logic MUST be deleted in the same change.

FORBIDDEN unless required by X2:

- deprecated paths,
- compatibility shims,
- temporary old paths,
- unused adapters,
- dead branches,
- commented-out obsolete code,
- legacy wrappers.

### X2. Backward Compatibility

Backward compatibility is allowed ONLY when required by at least one verified constraint:

- public API contract,
- database migration path,
- external integration,
- existing production client,
- explicit user instruction.

If none applies, clean replacement is REQUIRED.

---

## 8. Verification

### V1. Reality Requirement

ASSERT: A change is not verified until the affected behavior is exercised through the strongest available realistic execution path.

Required preference order:

```text
Live real path
→ End-to-end path
→ Integration path with real internal dependencies
→ Contract test against authoritative schema or recorded real fixture
→ Unit test for pure deterministic logic ONLY
```

### V2. Unit-Test Limit

ASSERT: Unit tests alone NEVER satisfy feature verification or proof of bot usability.

Unit tests do not verify that the trading bot or system is actually usable, functional, or safe in an operational environment. Unit tests may be used only for local verification of pure, isolated helper functions, but NEVER as evidence that the bot or a user workflow is usable.

### V3. Mock Limit

ASSERT: Mock-only verification NEVER satisfies verification for behavior involving UI, API flows, payments, checkout, providers, inventory, authentication, admin actions, persistence, orders, webhooks, or other integration boundaries.

A mock/fixture is allowed ONLY when:

- the real external system cannot safely be called in the current environment,
- the fixture is based on an authoritative schema or recorded real response,
- the report explicitly states the unverified real boundary.

Mocks MUST NOT be described as proof that the real external integration works.

### V4. Required Verification Sequence

The agent MUST run every applicable stage:

```text
1. Static: typecheck, lint, formatting
2. Runtime: affected processes start and required migrations/config load
3. Integration: affected internal boundaries execute with real internal dependencies
4. E2E/live: affected realistic user/API/admin/provider path executes when available
5. Regression: relevant unaffected behavior remains intact
```

A stage is skippable ONLY when it is not applicable to `Δ` or the capability is unavailable.

### V5. Proof Report

Verification reporting MUST state:

- commands/checks actually executed,
- feature path exercised,
- real/sandbox/fixture/mock status,
- observable or persisted proof,
- remaining unverified risk.

`Tests passed` alone is INVALID reporting.

### V6. Incomplete Verification

If a REQUIRED verification stage cannot run, the agent MUST NOT claim completion.

Return:

```text
VERIFICATION INCOMPLETE
Executed: [checks actually run]
Not executed: [required missing check]
Reason: [precise blocker]
Residual risk: [what may still be broken]
Next required check: [highest-value missing verification]
```

---

## 9. Halt Protocols

### H1. Architectural Conflict

ABORT generation if the requested `Δ` would require an unapproved architectural regression, contradictory contract, unsafe migration, or unresolved ownership boundary.

Return:

```text
CRITICAL HALT: ARCHITECTURAL INCONSISTENCY
Conflict: [precise conflict]
Canonical option: [approach + tradeoff]
Alternative option: [approach + tradeoff]
Required decision: [what must be resolved]
```

### H2. Contract Uncertainty

ABORT if correctness depends on a contract that cannot be verified and no safe contract-independent implementation exists.

Do not guess to continue.

### H3. Verification Blocker

A verification blocker MUST use V6. It MUST NOT be converted into a success claim.

---

## 10. Review Priority

Findings MUST be sorted by descending severity:

```text
1. Contract, data, security, or architectural invalidation
2. Wrong domain ownership or control-flow pollution
3. Type/integrity violations or guessed external behavior
4. Verification insufficiency
5. Avoidable complexity, duplication, or context cost
6. Style issues with no behavioral impact
```

Higher-severity findings MUST NOT be buried below lower-severity cleanup.

---

## 11. Output Contract

Completed modification reports MUST be concise and MUST NOT restate the diff.

Return only applicable fields:

```text
SUMMARY
- [behavior changed]

FILES CHANGED
- [path]: [reason]

ARCHITECTURE
- [canonical fit]

DISCOVERY
- [reused/verified contracts]

REDUCTION
- [unnecessary work intentionally excluded]

VERIFICATION
- [executed proof and environment status]

RESIDUAL RISK
- [only actual remaining risk]
```

Empty sections are FORBIDDEN.

If no code changed:

```text
NO CHANGE MADE
Reason: [precise reason]
Required next input/capability: [only when necessary]
```

---

## 12. Hard Bans

Unless an explicit rule above permits the behavior, the following are FORBIDDEN:

```text
Speculative abstractions
Duplicate implementations or schemas
Vague utils/helpers/misc buckets
Silent failure fallbacks
Guessed external contracts
any or un-narrowed unknown in domain code
Raw or double casts used to bypass types
Unit-only feature verification
Mock-only integration verification
Catch-and-return-empty behavior
Unrequired backward-compatibility shims
Deprecated or commented-out obsolete paths
Dependencies for trivial behavior
Unjustified sequential independent async
One-off wrappers without A3 justification
Ad-hoc provider checks in generic paths
Token-wasteful boilerplate, duplication, or indirection
Minification or cryptic naming for token reduction
Claims of verification not actually executed
```

---

## 13. Default Decision Rule

When multiple valid implementations remain, the agent MUST choose the one that maximizes this ordered objective:

```text
1. Contract correctness
2. Architectural locality
3. Explicitness
4. Local discoverability
5. Real-path verifiability
6. Minimal abstraction
7. Minimal total comprehension token cost
8. Ease of deletion or replacement
```

A lower-priority objective MUST NOT override a higher-priority objective.

If no option can be selected without violating a normative rule, ABORT before coding.

---

## 14. Real-World Execution Mechanics & Data Honesty

1. **Realistic Market Dynamics**: Simulation and paper backtesting MUST model realistic on-chain market dynamics (slippage, actual liquidity floor, and adverse price impact) rather than theoretical ideal exits that cannot execute in practice.
2. **Zero Synthetic Feed Pollution**: Live activity feeds and monitoring views MUST display only real-time events. Never pre-populate live feeds with historical, synthetic, or mock data.
3. **High-Density Decision Metrics**: Backtesting and evaluation tools MUST visually render full actionable metrics (winrate, sample size, fee breakdowns, and net ROI) directly in the UI.
4. **Lean & Deterministic Test Suites**: Prefer fast, unified integration tests over sprawling, fragile mock unit tests. Keep test runs deterministic and fast.
