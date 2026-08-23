---
name: textual-tui-ux
description: Design, build, and review polished Python TUIs with Textual. Use for dashboards, consoles, monitors, trading terminals, admin tools, data viewers, focus/navigation, responsive terminal layout, visual hierarchy, and screenshot-driven UX verification.
---

# Textual TUI UX

## 0. Normative semantics

`MUST`, `MUST NOT`, `ONLY`, `REQUIRED`, `INVALID`, and `ABORT` are normative.

No implicit exception exists.
An exception is valid ONLY when explicitly listed by this skill or by a higher-priority product contract.

When a rule cannot be satisfied, report the exact conflict instead of silently weakening it.

## 1. UX contract before implementation

Implementation MUST NOT begin until the screen has a coherent UX contract.

For every new or materially changed screen, define:

- `HUMAN`: who operates it.
- `PRIMARY_TASK`: the main verb.
- `PRIMARY_REGION`: the region that deserves first attention.
- `PRIMARY_ACTION`: the most likely next action.
- `INITIAL_FOCUS`: the widget focused on entry.
- `DENSITY`: sparse, balanced, or dense.
- `STATES`: loading, empty, populated, error, disabled, success where applicable.
- `80x24_BEHAVIOR`: what survives, collapses, hides, or moves.
- `120x36_BEHAVIOR`: how extra space is used.

Exactly one primary task and one primary region MUST exist per screen state.

If no primary task or primary region can be identified, the design is INVALID.

For a new screen, produce an `80x24` and `120x36` wireframe before implementation.

## 2. Decision-first information architecture

ASSERT: Every persistent region MUST support a current decision, action, or orientation need.

Before adding a persistent region, answer:

1. What question does it answer?
2. What decision does it support?
3. What action can follow?
4. Why must it remain visible now?

If none has a concrete answer, the region MUST NOT exist on the primary screen.

Rules:

- Overview MUST precede detail.
- Selection-dependent detail MUST appear only after a relevant selection exists.
- Repeated values MUST share alignment, units, precision, and formatting.
- Labels MUST describe domain meaning, not implementation structure.
- Raw telemetry MUST NOT outrank user risk, task state, or actionable information.
- Duplicate presentation of the same state is forbidden unless the second representation serves a distinct interaction purpose.

## 3. Terminal-native visual hierarchy

ASSERT: Hierarchy MUST be established with alignment, spacing, contrast, grouping, and typography before borders.

Rules:

- Exactly one region MUST be visually dominant per screen state.
- More than three simultaneous major regions is INVALID unless each region is required for the primary task at the same moment.
- Structured dense data MUST use terminal-native primitives such as tables, lists, trees, logs, or aligned text.
- A border MUST communicate grouping, focus, interaction scope, modal separation, or state.
- Decorative borders are forbidden.
- A header MUST communicate identity, scope, state, or navigation context.
- Decorative headers are forbidden.
- Empty space is valid and MUST NOT be filled with meaningless UI.
- Secondary metadata MUST have lower visual weight than actionable state.
- Status color MUST NOT be the only carrier of meaning.
- Color used only for decoration is forbidden.
- Focus MUST always be visually identifiable.

## 4. Layout contract

ASSERT: The primary task MUST remain usable at `80x24`.

Rules:

- Textual layout primitives MUST be used before manual positioning.
- `fr`, `auto`, min/max constraints, grid, horizontal/vertical containers, and `dock` MUST be preferred over arbitrary fixed dimensions when they can express the geometry.
- Fixed dimensions are valid ONLY for semantically fixed regions.
- Multiple vertical siblings MUST NOT each claim `height: 100%`.
- Flexible siblings MUST use fractional or content-aware sizing.
- Scrolling MUST belong to an explicit content region.
- Header, footer, and fixed navigation MUST NOT accidentally join the content scroll surface.
- Ordinary dashboard operation MUST NOT require horizontal scrolling.
- At `80x24`, secondary context MUST yield before the primary task.
- If all regions cannot coexist at `80x24`, the design MUST collapse, hide, drill down, switch screen, or move secondary actions to the command palette.
- Extra space at `120x36` MUST improve readability or useful context; it MUST NOT create decorative emptiness or redundant panels.

Read `references/textual-layout.md` before implementing unfamiliar Textual sizing behavior.

## 5. Interaction contract

ASSERT: Frequent operation MUST be keyboard-first.

Rules:

- Initial focus MUST be intentional.
- Focus order MUST follow task order.
- `Tab` and `Shift+Tab` MUST be coherent.
- Frequent actions MUST have short bindings.
- Rare/global actions MUST use the command palette or a secondary interaction surface instead of occupying permanent primary-screen space.
- Footer bindings MUST be contextual and limited to actions relevant to the current screen/state.
- `Escape` MUST cancel or close the current transient interaction when cancellation is safe.
- Destructive actions MUST require deliberate confirmation.
- A modal MUST be used ONLY for blocking decisions, confirmation, or short focused input.
- A modal MUST NOT become generic navigation.
- Selection changes MUST update related detail in place when navigation to another screen would interrupt the primary workflow.
- Mouse support MUST NOT be required for core operation.

## 6. Dashboard density rules

ASSERT: A dashboard MUST optimize time-to-decision, not widget count.

Rules:

- Dense does NOT mean boxed.
- A metric card MUST NOT exist when an aligned row, status strip, table column, or inspector field communicates the same information more efficiently.
- Repeated cards are forbidden for homogeneous tabular entities.
- Tables MUST expose only columns needed for scanning, comparison, or immediate action.
- Detail MUST move to an inspector, drill-down, or secondary screen when it harms scan speed.
- Global status MUST occupy a stable region.
- Rapidly changing event data MUST NOT visually destabilize unrelated layout.
- Live data MUST distinguish fresh, stale, disconnected, loading, and failed states when those differences affect decisions.

Read `references/dashboard-ux.md` for dashboard composition patterns.

## 7. Textual implementation boundaries

ASSERT: Use Textual's native state, layout, screen, event, and worker models.

Rules:

- Python MUST define structure, behavior, state, and data flow.
- TCSS MUST define presentation and layout unless runtime state strictly requires dynamic styling.
- Built-in widgets MUST be used when they satisfy the interaction contract.
- Custom widgets MUST exist ONLY when a native widget cannot express the required behavior or semantic boundary.
- `Screen` and `ModalScreen` MUST represent distinct interaction modes instead of giant conditional widget trees.
- Reactive state MUST be canonical for UI state that Textual should refresh.
- The same visible state MUST NOT be manually synchronized across multiple widgets when it can derive from one canonical value.
- Blocking IO MUST NOT run on the event loop.
- Long-running work MUST expose relevant loading, progress, cancellation, and error state.
- Row/entity selection MUST communicate through a typed message/event or canonical reactive state rather than arbitrary cross-widget mutation.

Read `references/textual-layout.md` and `references/verification.md` when implementing unfamiliar framework behavior.

## 8. Application organization

ASSERT: The `App` subclass MUST be a thin shell.

- The App owns construction, lifecycle, top-level navigation/composition, dispatch, worker entry points, and typed message wiring.
- Widgets own their local DOM, reactive UI state, and local event translation; emit typed messages upward (see §7).
- `Screen`/`ModalScreen` are ONLY for distinct or blocking modes (see §7).
- Stateful managers exist ONLY for real workflow state/behavior; one typed App reference is allowed when simpler than callback plumbing.
- Stateless rendering/update logic uses focused module-level functions; a class MUST NOT be a namespace.
- Styling lives in external TCSS (`CSS_PATH`) or justified component `DEFAULT_CSS`; inline App CSS blocks are INVALID.
- Repeated panel/command selection uses data-driven dispatch maps, not branching.
- No mixin-based god-App split; no ceremonial Protocols, result wrappers, callback bundles, or base managers.
- No speculative manager/plugin/tab/key infrastructure.
- Prune before splitting; verify behavior and visuals after each batch (see §11).

## 9. Visual system

ASSERT: The visual system MUST remain intentionally small.

Define one reusable semantic token for each applicable role:

- background
- surface
- foreground
- muted
- accent
- positive
- warning
- danger
- focus
- separator

Rules:

- Theme variables MUST be reused instead of scattering arbitrary colors.
- Per-widget arbitrary colors are forbidden unless they encode a documented domain distinction.
- Multiple border styles are forbidden unless each style has a distinct semantic meaning.
- Every container MUST NOT be styled as a card.
- Visual hierarchy MUST use weight, spacing, alignment, case, contrast, and separators before adding containers.
- Large ASCII branding MUST NOT consume space needed by the primary task.

## 10. Anti-AI-slop bans

The following are INVALID unless a product contract explicitly requires them:

- border around every widget
- header on every tiny region
- dashboard composed mainly of metric cards
- identical visual weight across all regions
- giant centered title
- permanent legend for rare shortcuts
- duplicated navigation controls
- centered operational data that scans better aligned
- arbitrary fixed layout for one terminal size
- decorative empty panes
- color used only to look richer
- clever labels that reduce precision
- copying web-dashboard card patterns without terminal justification

## 11. Mandatory visual verification

A UI change is NOT complete because it compiles or because event tests pass.

For every new or materially changed screen:

1. Run at `80x24`.
2. Capture the rendered screen.
3. Inspect hierarchy, alignment, clipping, overflow, scroll ownership, border noise, focus visibility, scan order, and action discoverability.
4. Correct defects.
5. Run at `120x36`.
6. Repeat visual inspection.
7. Exercise keyboard navigation through the primary task.
8. Verify relevant loading, empty, error, disabled, and populated states.
9. Verify destructive actions cannot execute accidentally.

Textual `run_test(size=...)` and `Pilot` MUST be used for deterministic interaction checks when available.
SVG screenshots or snapshot tooling MUST be used for visual inspection when available.

If visual rendering cannot be inspected, report `VISUAL VERIFICATION INCOMPLETE`.

Read `references/verification.md` before declaring UI work complete.

## 12. Output contract

For completed TUI work, report ONLY:

```text
UX CONTRACT
- human
- primary task
- primary region
- primary action

LAYOUT
- major regions
- 80x24 behavior
- 120x36 behavior

INTERACTION
- initial focus
- primary bindings

VISUAL VERIFICATION
- sizes inspected
- states exercised
- defects corrected

RESIDUAL RISK
- unverified behavior only
```

Do not produce a design essay unless requested.
