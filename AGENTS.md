# AGENTS.md

## 0. Normative Semantics

This file governs all modifications to the rugbot codebase: a private pump.fun /
letsbonk.fun Solana trading and sniper bot (Python 3.11+, `uv`-managed, `ruff`
linted, `src/rugbot` package, `tests/`, `fixtures/`, `idl/`, `frontend/`).

The keywords `MUST`, `MUST NOT`, `ONLY`, `REQUIRED`, `FORBIDDEN`, and `ABORT`
are normative. No implicit exception exists; an exception is valid ONLY when this
file or the user explicitly grants it. When two rules conflict, apply the
stricter rule unless doing so violates an explicit user requirement or external
contract. If the conflict cannot be resolved deterministically, ABORT before
writing code.

Core invariant: `ASSERT: S0 + Δ = S1`, where `S0` is the verified current state,
`S1` the required target state, and `Δ` the exact additions, deletions, and edits
required to reach it. The agent MUST NOT speculate, invent missing contracts,
hide uncertainty, or claim unexecuted verification.

## 1. Execution Pipeline

Every modification MUST execute this pipeline in order:
`ARCHITECTURE → DISCOVERY → REDUCTION → GENERATION → VERIFY`. No stage may be
skipped. Generation MUST NOT begin until architecture, relevant local state, and
required external contracts are known. ABORT before code generation if any
REQUIRED precondition is unavailable or contradictory.

## 2. Architecture & Domain Ownership

- Logic MUST live in the layer that owns the behavior: schemas validate shape,
  services execute behavior, decoders decode, storage persists, decision logic
  decides. Financial math uses integer base units and MUST NOT live in UI or
  transport code.
- A localized edge case MUST NOT be inserted as an ad-hoc branch in a generic
  path. Variation by provider, venue, entity type, environment, or other stable
  discriminator MUST go through a canonical boundary: adapter, dispatch/strategy
  map, domain policy, validated configuration, or feature module. Scattered
  `if provider == ...` checks are FORBIDDEN.
- Single-call-site logic MUST remain inline UNLESS extraction enforces a real
  boundary, isolates an external provider or infrastructure, has multiple real
  call sites, or materially lowers total comprehension cost. "Looks cleaner" and
  "might be useful later" are NOT valid reasons.
- Code MUST scale by entity, feature, provider, or bounded context. Vague
  `utils` / `helpers` / `misc` buckets are FORBIDDEN.

## 3. Discovery & Reuse

Speculative coding is FORBIDDEN. Before creating any helper, type, schema,
service, adapter, dependency, or utility, the agent MUST inspect the existing
codebase for the canonical implementation:

1. Search relevant terms and symbols.
2. Inspect sibling files, imports, and call sites.
3. Inspect canonical model/schema locations.
4. Inspect installed dependencies before adding one.
5. Inspect native platform capabilities before implementing custom behavior.

Duplicate helpers, schemas, types, services, or equivalent logic MUST NOT be
created.

External behavior MUST NOT be guessed. Verify API field names, environment
variable names, configuration keys, webhook shapes, provider statuses, SDK
behavior, and IDL/account layouts against an authoritative source (official
docs, SDK types, schemas, recorded real fixtures, or this repo's pinned `idl/`)
before depending on them.

If a REQUIRED capability is unavailable, do not fabricate the result:

```text
BLOCKED_BY_MISSING_CAPABILITY
Capability needed: [precise capability]
Why required: [reason]
Safe next step: [minimal action]
```

## 4. Reduction: YAGNI & Minimum Correct Delta

Implementation is allowed ONLY for a present requirement: current feature, real
call site, real contract, real user path, or required architectural boundary.
Hypothetical future use is not evidence.

Evaluate implementations in this order: `Native capability → Existing project
primitive → Minimal local implementation → Verified dependency`.

A new dependency is allowed ONLY when local implementation would create material
complexity, security risk, protocol risk, or duplicate a trusted standard.
Dependencies for trivial string, array, object, or formatting work are
FORBIDDEN.

`Δ` MUST contain no code unnecessary to satisfy the verified contract: reuse
canonical schemas and primitives, delete obsolete code instead of layering
around it, prefer data representation over branch proliferation, and keep
one-off logic inline unless extraction is justified. Minimize total
comprehension cost, not visual code size. Token reduction MUST NOT come from
cryptic naming, minification, obscure control flow, hidden invariants, or
dropped type information.

## 5. Contracts, Types & Errors

- Every external input MUST be validated or narrowed at the application boundary
  before trusted use: environment variables, RPC/provider responses, webhook
  payloads, CLI/API input, query parameters, and uploaded files.
- `any`, un-narrowed `unknown`, raw casts, double casts, and unvalidated
  environment access are FORBIDDEN in domain code. `unknown` is allowed ONLY at a
  boundary and MUST be narrowed before domain use.
- Fallback chaining MUST NOT be used to guess contracts. Validate the canonical
  key and fail explicitly if it is absent.
- A caught error MUST be intentionally recovered, translated, or enriched;
  otherwise it MUST propagate unchanged. Catch-and-return-empty (`return []`,
  `return None`) that hides failure is FORBIDDEN. Log once at the boundary that
  owns the failure; lower layers MUST NOT duplicate logs without distinct
  required telemetry.

## 6. Control Flow, Concurrency & Naming

`Δ` MUST NOT introduce avoidable decision paths; every branch MUST map to a
distinct required contract state, and explicit maps, policies, or dispatch
boundaries are preferred over repeated nested conditionals. Independent async
operations MUST run concurrently; sequential execution is allowed only when a
later operation depends on an earlier result, ordering is part of the verified
contract, or concurrency would violate a documented rate limit, resource bound,
or safe side-effect ordering. Names MUST be explicit and domain-accurate;
non-obvious literals MUST be named constants or validated configuration.

## 7. Deletion & Compatibility

Superseded internal logic MUST be deleted in the same change; deprecated paths,
compatibility/versioning shims, temporary old paths, unused adapters, dead
branches, legacy wrappers, and commented-out code are FORBIDDEN. Backward
compatibility is allowed ONLY for a verified constraint (public API contract,
database migration path, external integration, existing production client, or
explicit user instruction); otherwise clean replacement is REQUIRED.

## 8. Verification

A change is not verified until the affected behavior executes through the
strongest available realistic path:

```text
Live real path
→ End-to-end path
→ Integration with real internal dependencies
→ Contract test against authoritative schema or recorded real fixture
→ Unit test for pure deterministic logic ONLY
```

- Unit tests alone NEVER prove feature correctness or bot usability. They are
  valid only for pure, isolated logic.
- Mock-only verification NEVER satisfies UI, API, RPC/provider, persistence,
  webhook, order, or other integration boundaries. A fixture is allowed only
  when the real system cannot be safely called, it is based on an authoritative
  schema or recorded real response, and the report explicitly names the
  unverified boundary.
- Run every applicable stage: (1) static — typecheck, lint, format; (2) runtime
  — affected processes start and config loads; (3) integration — affected
  internal boundaries run on real dependencies; (4) e2e/live — the realistic
  path executes when available; (5) regression — adjacent behavior stays intact.
- Reporting MUST state the commands actually executed, the path exercised,
  real/sandbox/fixture/mock status, observable or persisted proof, and residual
  risk. "Tests passed" alone is INVALID.

If a REQUIRED stage cannot run, do not claim completion:

```text
VERIFICATION INCOMPLETE
Executed: [checks actually run]
Not executed: [required missing check]
Reason: [precise blocker]
Residual risk: [what may still be broken]
Next required check: [highest-value missing verification]
```

## 9. Halt Protocols & Output Contract

ABORT generation if `Δ` would require an unapproved architectural regression, a
contradictory contract, an unsafe migration, or an unresolved ownership boundary:

```text
CRITICAL HALT: ARCHITECTURAL INCONSISTENCY
Conflict: [precise conflict]
Canonical option: [approach + tradeoff]
Alternative option: [approach + tradeoff]
Required decision: [what must be resolved]
```

ABORT if correctness depends on a contract that cannot be verified and no safe
contract-independent implementation exists; report it with the
`BLOCKED_BY_MISSING_CAPABILITY` form. A verification blocker MUST use the
`VERIFICATION INCOMPLETE` form and MUST NOT be reported as success.

Completion reports MUST be concise and cover only what is useful:

```text
[What changed, in 1–3 sentences]

Changed:
- `path` — reason

Verification:
- [commands actually run]
- [observed result]

Notes:
- [important decisions, exclusions, or residual risk]
```

If blocked: `BLOCKED` + `Reason:` + `Needed:`. If nothing changed: `NO CHANGE` +
`Reason:`. Do NOT restate the task or paste full diffs.

## 10. Core-Only Delivery Policy

This is a private solo project. Optimize for the smallest reliable system that
answers the core question: can the system identify a known operator pattern and
demonstrate a fully executable, net-profitable exit in paper/backtest mode?

1. No backward-compatibility layers, migration paths, deprecation shims, version
   negotiation/overrides, or new application/API/schema/artifact versioning. Do
   not preserve old interfaces unless the user explicitly requests it.
2. No ad hoc scripts, one-off JSON contracts, improvised decoders, fake readiness
   evidence, or parallel implementations. Use the canonical typed contracts and
   fixtures already in the repository.
3. Prioritize core functionality only: read-only observation ingestion, finalized
   replay, point-in-time wallet/entity tracking, operator behavior features,
   adverse-event attribution, executable quote simulation, paper entry/exit
   decisions, leakage-safe backtesting, and fail-closed safety gates.
4. Defer dashboards, UI, deployment automation, multi-provider abstractions,
   optional low-latency transports, generalized plugin systems, broad refactors,
   and operational polish unless required to validate a core decision path.
5. State the concrete core behavior enabled, keep changes within a disjoint
   scope, add focused tests, and stop when the acceptance behavior is proven. Do
   not expand a task because a more general architecture would be interesting.
6. Strict identifiers that reject mismatched evidence are fixed validation
   constants. Do not extend them into compatibility or version-management
   systems.
7. Never run live trading while developing this system. Observe, paper, and
   route-simulation execution remain the only permitted development modes until
   the user gives separate explicit authorization after out-of-sample evidence.
8. Never write scratch artifacts (`*.json`, `*.csv`, scan dumps, RPC results,
   sample lists) to the repository root. Write one-off output under `.state/` or
   the operating system's temporary directory. Only canonical committed fixtures
   under `fixtures/` are allowed in-tree.

## 11. Operator Target Analysis Mandate

When the user provides a wallet or mint address, the ONLY operational question is
whether this operator exhibits a predictable, repeatable, safely snipable launch
pattern. Classify the target immediately into one archetype:

- **Type 1 — Serial same-wallet deployer (PRIMARY, S1 scope):** creates multiple
  tokens from the exact same wallet (`N ≥ 2`), so full launch history attaches to
  the public address. Score winrate/EV from the wallet's finalized signatures and
  re-arm an event listener on it for the next `pump::create`.
- **Type 2 — Burner-per-launch cluster:** a fresh disposable wallet per token
  (1 token per wallet, no prior history). The deployer cannot be predicted;
  detecting it requires watching upstream funders in real time.

```text
ASSERT: S1 = Type 1 only. Type 2 automated arming is DEFERRED and MUST NOT be built without separate explicit user authorization.
```

Type 2 targets remain classifiable and reportable, but the correct action is the
honest manual instruction to arm an observe-only listener on the funding source.
An agent MUST NOT add speculative Type 2 arming code (no `TargetKind.FUNDER`, no
funder watcher) to "complete" this section; under YAGNI its absence is the
intended state, not a gap.

Every target analysis MUST report:

1. **Archetype** — Type 1 or Type 2.
2. **Launch history & cadence** — frequency and intervals between launches.
3. **ATH & exit profile** — median and peak ATH distribution, floor liquidity,
   and dev holding time before dumping.
4. **Net EV qualification** — winrate (≥ 70% at `N ≥ 10`), analytical optimal
   take-profit, and net-positive EV after Solana network and Jito tip fees.
5. **Next arming action** — Type 1: re-arm on the known dev wallet. Type 2:
   identify the staged burner or arm observe-only on the upstream funder.

## 12. Data Honesty & Realistic Execution

1. Simulation and paper backtests MUST model realistic on-chain dynamics —
   slippage, actual liquidity floor, and adverse price impact — not ideal exits
   that cannot fill.
2. Live activity feeds and monitoring views MUST display only real-time events.
   Never pre-populate them with historical, synthetic, or mock data.
3. Backtesting and evaluation tools MUST surface full actionable metrics:
   winrate, sample size, fee breakdowns, and net ROI.
4. Prefer fast, unified integration tests over sprawling mock unit tests. Keep
   test runs deterministic and fast.

## 13. Dependencies, Conventions & Commands

**Dependencies:** add or update with `uv add <package>`. `uv.lock` stays in sync.
Restart any running bots after dependency changes. Keep compatibility with Python
3.11+.

**Conventions:**

- Follow the Ruff rules in `pyproject.toml`; line length 88, double quotes.
- Use Google-style docstrings for functions and classes, and type hints on all
  public functions.
- Use the centralized logger: `from rugbot.utils.logger import get_logger`.
- Pump.fun changes stay under `src/rugbot/`; verify `idl/` matches the on-chain
  programs.

### Useful Commands

| Command | Purpose |
| --- | --- |
| `uv sync` | Install/update dependencies |
| `uv run ruff format` | Format code to project standards |
| `uv run ruff check` | Run linting checks |
| `uv run ruff check --fix` | Auto-fix linting issues where possible |
| `uv run pytest` | Run the test suite |
| `uv run rug_watch --once` | Run one finalized read-only pass (DB config in `state.sqlite3`) |
| `uv run python -m rugbot.backtest.cli --input fixtures/backtest/demo.json --pretty` | Run the canonical leakage-safe demo backtest |

## 14. Safety

- Never expose private keys in code, logs, or commits.
- Observe, paper, and route-simulation modes MUST NOT load `SOLANA_PRIVATE_KEY`
  or submit transactions.
- Test manual buys and sells only with minimal amounts after paper validation.
- Verify transactions on a Solana explorer before scaling up.
- Respect RPC provider rate limits and keep logs for audit and debugging.

## 15. Hard Bans

Unless an explicit rule above permits it, the following are FORBIDDEN:

```text
Speculative abstractions
Duplicate implementations or schemas
Vague utils / helpers / misc buckets
Silent failure fallbacks
Guessed external contracts
any or un-narrowed unknown in domain code
Raw or double casts used to bypass types
Unit-only feature verification
Mock-only integration verification
Catch-and-return-empty behavior
Unrequired backward-compatibility shims
Deprecated or commented-out obsolete code
Dependencies for trivial behavior
Unjustified sequential independent async
Ad-hoc provider checks in generic paths
Token-wasteful boilerplate, duplication, or indirection
Cryptic naming or minification for token reduction
Claims of verification not actually executed
Live trading during development without explicit authorization
```
