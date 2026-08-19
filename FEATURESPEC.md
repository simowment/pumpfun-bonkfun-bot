P0 — Core sniper

Target management

Add dev wallet by address.
Label it.
LIVE / DRY RUN / PAUSED.
Buy amount.
TP / SL.
Slippage cap.
priority fee / Jito tip cap.
One-click preset application.
Duplicate-wallet prevention.
Hard validation before arming: no LIVE target without buy amount, funded execution wallet, valid RPC and required execution settings. F Project explicitly warns about the “tracker with no buy amount” silent failure case, so yours should make that impossible.

Setup should basically be:

ADD DEV
FaBGr...HikQ
      ↓
Preset       Dev Sniper
Buy          0.010 SOL
TP           ...
SL           ...
Mode         DRY RUN
      ↓
[START WATCHING]

Then after validation:

[GO LIVE]

Not a giant settings form.

Creation watcher

Subscribe to every active target.
Detect a token creation immediately.
Record detection timestamp + slot.
Deduplicate launches.
Associate mint → target.
Feed event:
LAUNCH FaBGr… → DOGE69.
Immediately hand it to execution. No historical backtest/graph/funding analysis in this hot path.

Buy execution

DRY RUN: simulate exactly what would have been submitted.
LIVE: sign/send immediately.
States:
DETECTED → SIGNING → SENT → CONFIRMED / FAILED.
Measure:
detect→send, send→confirm, slot delta.
Record actual fill, tokens received, SOL spent and execution costs.
P0 — Rug monitoring

This should be equally important as creation detection.

For each position, continue watching the dev, not just token price.

Core events:

DEV BUY
DEV TRANSFER
DEV SELL
DEV SOLD 25%
DEV SOLD 100%
DEV WALLET EMPTY

For the simple MVP, only actions by the known dev wallet count. Don't try to infer his 17 hidden wallets yet.

The most important event is:

RUG SIGNAL
Dev started selling

Then configurable behavior:

On dev sell:
[ ] notify only
[ ] sell same percentage
[x] sell 100%

F Project has the same fundamental idea in its “Track Sells” mode: monitor the watched wallet’s sells and automatically sell when it sells something the user holds.

That means the live position view becomes useful:

DOGE69                         LIVE


Entry          0.010 SOL
Current        0.014 SOL
Net PnL        +0.0032 SOL


DEV
Holding        8.4%
Last action    BUY
Last activity  4.2s ago


EXIT
TP             +...
SL             -...
Dev sell       SELL 100%


[E] Exit     [H] Sell 50%

That DEV section is far more relevant to your use case than generic charts and dashboard metrics.

P0 — Manual controls

You need very fast overrides:

Target:
Pause / Resume
Go Live / Dry Run
Edit size
Edit TP / SL


Position:
Sell 25%
Sell 50%
Sell 100%
Cancel automation


Global:
PAUSE ALL
KILL SWITCH
RESUME WATCHERS

I would make SELL 100% available from anywhere when a position is selected.

And there should be a distinction between:

PAUSE TARGET
→ stop future entries for this dev

and:

EXIT POSITION
→ sell an existing position

Pausing a target must never silently leave the user unsure what happens to an existing position.

P0 — Shutdown/restart safety

This is one area I would explicitly design now, because a sniper that behaves incorrectly after a crash is dangerous.

On normal shutdown:

1. stop accepting new launches
2. persist target states
3. persist open positions
4. persist last observed slot/signatures
5. stop watchers
6. close DB/RPC

On startup:

START
 ↓
load targets
 ↓
load open/pending positions
 ↓
query actual wallet balances
 ↓
reconcile DB ↔ blockchain
 ↓
check pending tx signatures
 ↓
restore subscriptions
 ↓
READY

Critical rule:

ASSERT: Restart MUST NOT blindly replay missed BUY intents.

Imagine the bot was offline for 20 seconds and sees an old creation after reconnecting. It must not suddenly “snipe” it as though it were fresh.

Instead:

MISSED LAUNCH
FaBGr… launched DOGE69 while bot offline
Age 18.4s
No automatic buy performed

For positions, however, it should recover management after restart:

RECOVERED POSITION
DOGE69
Wallet holds 1,293,402 tokens
Resuming TP/SL + dev-sell monitoring

That feature is far more valuable than another dashboard panel.

P0 — Operational dashboard

No backtest information here.

I’d make the main screen roughly:

TARGETS        LIVE ACTIVITY                 EXECUTION
─────────      ───────────────────────        ─────────────────
FaB… LIVE      06:21 LAUNCH DOGE69           FaBGr…HikQ
8Ks… DRY       06:21 BUY    SENT             LIVE
7Bc… PAUSED    06:21 BUY    CONFIRMED        Buy 0.010
               06:22 DEV    SELL 40%          Dev-sell: EXIT
               06:22 SELL   CONFIRMED


               CURRENT POSITION
               DOGE69 +32% net
               Dev holding 4.8%

The main feed should include:

LAUNCH
SIM
BUY
BUY FAIL
DEV BUY
DEV TRANSFER
DEV SELL
TP
SL
MANUAL SELL
AUTO SELL
RPC LOST
RPC RESTORED
RECOVERED

This gives the user an audit trail of what the bot is actually doing.

P1 — Positions

A dedicated positions screen:

TOKEN     DEV       MODE   SIZE    NET PNL   DEV HOLD   STATE
DOGE69    FaB…      LIVE   .010    +32%      4.8%       OPEN
PEPE2     8Ks…      DRY    .015    +12%      7.1%       SIM

Inspecting one shows:

entry/fill;
current price/MC;
net PnL;
fees;
TP/SL;
dev holdings;
dev's recent actions;
execution latency;
transaction signatures;
manual sell controls.

I wouldn't build a sophisticated portfolio system.

P1 — Launch history

Yes, absolutely keep launch history.

Not because the dashboard needs it, but because a user will want to investigate:

“What has this guy been doing since I started watching him?”

Per target:

LAUNCH HISTORY · FaBGr…HikQ


TIME      TOKEN    ENTRY     PEAK     DEV SELL    OUR RESULT
06:21     DOGE69   ...       ...      +41s        +...
04:52     CAT2     ...       ...      +18s        -...
01:13     AI69     ...       ...      +63s        +...

Then inspect a launch:

CREATED
  ↓  +12ms
DETECTED
  ↓  +21ms
OUR BUY
  ↓
DEV BUY/TRANSFER ACTIVITY
  ↓
DEV STARTS SELLING
  ↓
OUR SELL

This is extremely useful to study whether your execution is actually getting ahead of the rug.

It also feeds the Backtester later without mixing the Backtester into the main UI.

P1 — Presets

F Project exposes presets for rugger configuration, including protection/sell configuration and gas settings. I’d replicate the concept, but keep it tiny:

DEV SNIPER FAST
Buy       .010
TP        ...
SL        ...
Slippage  ...
Priority  ...
Jito      ...
Dev sell  Exit 100%


DEV SNIPER SAFE
...


DRY TEST
...

Then new-target setup is almost instant:

wallet → preset → buy size → DRY/LIVE

That's good UX.

P1 — Protection

Keep protections directly actionable:

max SOL/trade;
max total exposure;
max slippage;
max priority/Jito cost;
max consecutive losses;
daily loss stop;
insufficient balance protection;
RPC unhealthy → no new buys;
duplicate launch protection;
stale launch protection;
global kill switch.

F Project's documented presets include an auto-buy protection based on consecutive losses, so this general class of circuit breaker is aligned with the product you're referencing.

Avoid a generic “risk engine.” A few explicit guards are enough.

P1 — Notifications

Notify only on meaningful state changes:

TARGET LIVE
TOKEN CREATED
BUY CONFIRMED
BUY FAILED
DEV SELL DETECTED
AUTO EXIT
TP / SL
RPC OFFLINE
RPC RECOVERED
BOT RESTARTED
POSITION RECOVERED
KILL SWITCH

Not every RPC event.

F Project also exposes Notifications as a first-class tool area.

P2 — Backtester

Separate screen/module, as discussed:

known dev
→ historical launches
→ realistic entries
→ price-path replay
→ exact TP breakpoints
→ TP hit rate
→ net EV
→ historical optimal TP
→ robust region

Then:

[APPLY TO TARGET]

copies the chosen parameters into target configuration.

Backtester informs configuration. It never enters the live snipe path.

P3 — Tracker / Rugger Intelligence

This is where the complex stuff from your .md belongs. The source material explicitly describes tracing funding sources, old launches and connected wallet clusters, while the secondary method looks for pump wallets via repeated signatures, overlapping purchases, bundles and activity around pump/dump blocks.

Future features:

Known wallet
   ↓
Funding parent
   ↓
Children / sibling wallets
   ↓
Shared funding
   ↓
Historical launches
   ↓
Bundle / same-block behavior
   ↓
Dev / pump wallet cluster
   ↓
Graph

Then detection:

Known rugger funded a NEW wallet


Parent   FaBGr…
Child    7Ks2…
Amount   1.42 SOL
Age      8 sec
Confidence HIGH


[F] Follow
[B] Backtest
[A] Add Target

And eventually:

AUTO DISCOVERY


Known parent
 ├─ creator A
 ├─ creator B
 ├─ fresh wallet C  ← NEW
 └─ pump wallet D

But this must remain completely independent of the sniper.

The source's methodology makes this distinction useful: funding/history analysis is used to identify and qualify the actor, while the subsequent execution phase watches the relevant address for the next creation.

So I'd define the product in four verbs:

WATCH
→ known wallets and their creations/sells


SNIPE
→ execute immediately


PROTECT
→ TP/SL/dev-sell/circuit breakers/recovery


STUDY
→ launch history, backtest, later wallet intelligence

And the UX navigation can be almost embarrassingly simple:

F1 DASHBOARD
F2 TARGETS
F3 POSITIONS
F4 BACKTEST
F5 TRACKER        # later
F6 SETTINGS

The key design change from what we were doing earlier is that the most valuable live information isn't generic PnL telemetry; it's the chronology of the rugger's behavior relative to your own execution:

DEV CREATED → WE BOUGHT → DEV MOVED FUNDS → DEV SOLD → WE EXITED

That should become the central mental model of the bot.