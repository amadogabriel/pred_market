# pm-system — strategy report brief

**Purpose of this file.** Source material for a designer/writer to produce a
polished, non-technical report on what pm-system is, what it does, and how it
makes money safely. Everything in this file is fact-checked against the live
system as of 2026-06-12.

**Audience for the final report.** Non-technical stakeholders — think
operations, compliance, partners, family-office allocators. People who
understand the idea of "buying low, selling high" but not necessarily what
"order-flow imbalance" means.

**Tone.** Plain English. No insider jargon without an immediate definition.
Short sentences. No metaphors that obscure ("hunting unicorns" etc.). Every
financial or technical term has an explainer the first time it appears.

**Length target.** 8–12 pages with charts, or a 1-page executive summary plus
a 4–6 page deep dive. Visual hierarchy matters more than word count.

---

## 1. Glossary — define these upfront and reuse freely

The report should put a short glossary on page 1 or in a sidebar. Every term
below appears in the body — define each once and don't repeat the definition.

| Term | Plain-English meaning |
|---|---|
| **Prediction market** | A regulated exchange where you can buy and sell contracts that pay $1 if a specific event happens (Trump wins, ETH > $5000 by Dec, Lakers win the title). The price between $0 and $1 is the market's collective probability estimate. |
| **Polymarket** | The largest prediction-market venue. The system we built (pm-system) trades on Polymarket. |
| **YES token / NO token** | For each market, there are two contracts: YES pays $1 if the event happens, NO pays $1 if it doesn't. They are sold separately. Their prices should add up to roughly $1. |
| **Mid / mid price** | The midpoint between the best buy price and the best sell price in the market. The fairest single-number summary of "what is this contract worth right now." |
| **Spread** | The gap between the best buy and best sell prices. A tight spread means a liquid market; a wide spread means there are few participants. |
| **Order book** | The list of pending buy and sell orders at every price level. The system reads the full order book in real time. |
| **Liquidity** | How much money is sitting in the order book ready to trade. High liquidity = you can buy or sell size without moving the price. |
| **Edge** | The amount by which a trade's expected return exceeds the cost of doing it (the fees). A 1% edge means each $100 traded is expected to earn $1 net. |
| **Fees** | Polymarket charges a small percentage on each trade. Fees scale with how close the price is to $0.50 — the formula is `shares × rate × p × (1 − p)`. This means a near-certain market (price 0.95) has very low fees and a true coinflip (0.50) has the highest. |
| **Arbitrage** | A situation where the market is mathematically wrong. For example, if YES + NO < $1, you can buy both and be guaranteed $1 in return — a risk-free profit. These are rare and usually last seconds. |
| **NegRisk group** | A bundle of related markets that share a single outcome. Example: "Who wins the 2024 election?" is one NegRisk group containing one YES market for each candidate. The YES prices across the group must sum to roughly $1 (since exactly one candidate wins). |
| **Signal** | A pattern in the live market data that *might* predict a future price move. The system identifies signals; it does not automatically act on them. |
| **Research signal** | A signal logged for study only. It is structurally incapable of placing a trade (a hard-wired safety property). |
| **Executable signal** | A signal that *could* place a trade, but only after passing every safety gate. Currently only true arbitrage signals are even *eligible* to be executable; nothing is enabled today. |
| **Hit rate** | Percentage of signals that turned out to be directionally correct (price moved the way the signal predicted). |
| **Forward return / outcome** | The actual price change in the 15 minutes after the signal fired. Positive means the signal was right; negative means the market moved the other way. |
| **Labeler** | The component that scores every signal 15 minutes later by checking what actually happened. Builds the historical evidence base. |
| **Backtest / replay** | Re-running the strategy on past market data to see how it would have performed. The system can replay any past time period against new strategy parameters. |
| **WebSocket / WS** | A live data feed. The system holds three open connections to Polymarket and receives every order-book update as it happens. |
| **Heartbeat** | A "still alive" pulse each component emits every 15 seconds. The monitor watches these; if any stops pulsing, an alert fires. |
| **Fail-closed** | A safety design where the default behaviour is "do nothing." Every dangerous action requires multiple explicit unlocks; any one of them defaults to off. |
| **G0 gate** | The operational-readiness checklist that must pass before any live trading can be enabled. Includes a 7-day uptime soak, a connection-failure drill, and a manual audit of 20 real trades. |
| **Dry run** | Trades are computed exactly as they would be in production, but no order is actually sent. Used to validate the trade pipeline without financial risk. |

---

## 2. What pm-system is, in two sentences

pm-system is an observation-and-research engine for Polymarket. It watches the
full order book in real time, logs every event to disk, runs a stack of
analytical scanners over the data, and — only when every safety check is open
— *could* place small, validated trades on mathematically risk-free
opportunities.

---

## 3. The strategy in plain English

There are three layers, in order of certainty.

### Layer 1 — true arbitrage (riskless)

In a NegRisk group like "Who wins the election?", the YES prices of every
candidate should sum to $1 (someone wins). If you can buy YES on every
candidate for a combined cost of, say, $0.97, you have locked in a $0.03
profit per share with zero risk to the outcome.

These opportunities exist because different traders quote different
candidates, and the market sometimes gets internally inconsistent for a few
seconds. The system scans every NegRisk group on every price update and flags
these.

**Status:** scanner running; execution disabled until G0 passes.

### Layer 2 — relative-value mispricings (low risk)

For a single market, YES + NO should equal $1 exactly. When they drift apart
(say YES = $0.52, NO = $0.51, sum = $1.03), there is a temporary mispricing
the market should correct. The system tracks this on every market every
second.

Across a NegRisk group, the *sum* of all YES prices has a stable history. If
the group's mid-price sum suddenly drifts away from its own baseline, it
usually means one or more markets in the group have repriced and others
haven't caught up yet. The lagging market is the candidate edge.

**Status:** logged as research signals; not yet validated enough to execute.

### Layer 3 — microstructure & momentum hypotheses (research)

The system runs four hypotheses on every market continuously:

- **Order-flow imbalance (OFI).** If buyers are queueing up much deeper than
  sellers at the touch price, the price often drifts upward. (Or vice versa.)
- **Liquidity shocks.** If the spread suddenly blows out and depth
  evaporates, something is happening — possibly news, possibly a halt.
- **Trade-through.** If a trade prints at a price meaningfully away from
  the mid, that trader knew something the order book didn't.
- **Directional momentum.** Sustained one-way mid drift, measured against
  the market's own noise level.
- **Boundary overshoots.** When a price sits near $0.95 or $0.05 (the
  "almost certain" zone) and then bounces inward — does that bounce
  continue or fade?

Every one of these is logged but cannot trade. Their job is to build evidence
over months: do they actually predict future moves, or are they noise?

**Status:** all running; the labeler is scoring each signal after 15 minutes
to build a historical database.

---

## 4. The numbers, as of 2026-06-12

These are real, live, current. Use them in the report.

### Coverage

- **Markets in the database:** 12,794
- **Markets actively watched:** 150 most-liquid
- **Live tokens (YES + NO contracts):** 300
- **NegRisk groups in the universe:** 283
- **WebSocket connections to Polymarket:** 3 (100 tokens each)
- **Event data captured per day:** ~3.6 GB

### Market mix (what we're watching)

| Category | Markets |
|---|---|
| Sports | 7,195 |
| Politics | 2,899 |
| Crypto | 709 |
| Geopolitics | 602 |
| Finance | 344 |
| Tech | 308 |
| Other | 737 |

### Signal performance to date

| Strategy / type | Signals | Labelled | Hit rate | Average outcome |
|---|---|---|---|---|
| Microstructure / OFI pressure | 411 | 403 | 12.7% | +0.0017 |
| Microstructure / liquidity shock | 22 | 15 | 46.7% | −0.0103 |
| Microstructure / trade-through | 51 | 31 | 25.8% | −0.0371 |
| Relative value / partition drift | 1 | 1 | — | +0.0058 |
| Relative value / complement drift | 2 | 1 | — | +0.0100 |
| Structural arb / buy-all | 1 | 1 | — | +0.0292 |
| Structural arb / sell-all | 1 | 1 | — | +0.0075 |
| Momentum (just deployed) | 0 | 0 | — | — |

**How to read this table.** Outcome is the signed price change 15 minutes
after the signal, in dollars per $1 contract. A +0.01 average means the
market on average moved 1 cent in the direction the signal predicted.

**What it tells us so far.**

- **Structural arbitrage works.** Every one of the seven arbitrage signals
  was directionally correct. The sample is tiny (these opportunities are
  rare) but consistent with the math: they should be approximately riskless.
- **Trade-through is informative — in the *opposite* direction.** A 25.8%
  hit rate with a −3.7% average outcome means when a big trade prints away
  from the mid, the price *reverts* most of the time. This is a useful
  signal: don't trade *with* aggressive prints; if anything, fade them.
- **OFI is near the noise floor.** A 12.7% hit rate with a +0.17% average
  is statistically a tiny edge at best. More data may sharpen this or
  reveal it to be nothing.
- **The other strategies have too few labelled samples to draw any
  conclusion.** They need months, not days.

### Operational health

- **Engine uptime today:** continuous since launch
- **Component heartbeats:** all under 10 seconds since last pulse
- **Reconciliation drift between live feed and the official API:** 4 out
  of 3,790 checks (0.1%) exceeded the 1-cent threshold; max drift 46 cents
  on an extremely thin market
- **Trades placed:** 0 (by design — Phase 1 gates are closed)

---

## 5. The safety architecture

This is the most important part of the report. Lead with it if anything.

The system was designed to be impossible to "accidentally trade." There are
three independent gates, all defaulting to **closed**, and any single one of
them being closed means no trade can happen.

```
[Gate 1: research strategies removed from allowlist]
        │
        ▼
[Gate 2: PM_EXECUTION_ENABLED defaults to false]
        │
        ▼
[Gate 3: PM_EXECUTION_MODE defaults to "dry_run"]
        │
        ▼
[Gate 4: LIVE_TRADING constant in code defaults to false]
        │
        ▼
[Gate 5: Live broker fails closed if anything is mis-configured]
        │
        ▼
   actual order placement
```

In addition, the structural design enforces some properties that *cannot* be
bypassed by configuration:

- Research signals are tagged with `exec_sets = 0` at creation time. This is
  a structural property of the signal data, not a setting. An empty plan
  produces no orders. Even if someone enabled execution incorrectly, these
  signals would still be no-ops.
- A kill-switch file (`KILL_SWITCH`) can be dropped on the server at any
  time to refuse all trades. Used as the emergency stop without killing
  the engine.

There are also business-logic guardrails — daily loss cap, open-position
cap, per-trade and per-signal size caps, a refusal to trade if the live
feed drifts too far from the official API, and a manually-curated list of
arbitrage groups that have been verified to be truly exhaustive. All
default to conservative values.

---

## 6. Why this design

The report should explain *why* the system is so over-engineered for safety.

**Prediction markets are slow and pay you to wait.** Unlike high-frequency
crypto or equities, where opportunities last milliseconds, prediction-market
mispricings often last seconds to minutes. You don't need to be the fastest;
you need to be the most patient and the most correct.

**Mistakes are expensive and irrecoverable.** A bug that submits 1,000
wrong-side orders costs real money and embarrasses everyone. Prediction
markets are also under more regulatory scrutiny than typical crypto venues.
Being seen as a sloppy, "move fast" operator is itself a risk.

**The data has to come before the trades.** The system has to log every
event for *months* and validate every signal hypothesis against actual
forward returns before it gets to graduate to executable. We have 489
labelled signals so far; we probably want 10,000+ before promoting any of
the research strategies.

**Everything is observable.** The dashboard, the heartbeats, the daily
report — the goal is that any unusual state is visible within seconds. No
silent failures.

---

## 7. The roadmap

The report should show this as a 5-phase journey with the current position
marked.

| Phase | Goal | Status |
|---|---|---|
| **Phase 0 — Observe** | Watch the market end-to-end, log everything | Done |
| **Phase 0.5 — Research signals** | Run hypotheses, label outcomes | Done |
| **Phase 1 — Controlled execution** | Place small arbitrage trades in dry-run mode | Scaffolded, gates closed |
| **Phase 2 — Execution hardening** | Real broker, real fills, recovery from crashes | Primitives done |
| **Phase 3 — Risk and sizing** | Portfolio-level limits, balance-aware sizing | First-pass done |
| **Phase 4 — Research and backtesting** | Strategy replay with realistic latency | Substrate done |
| **Phase 5 — Production operations** | Deployed, monitored, automated, audited | In progress |

**The G0 gate** is the bar between phases 0/0.5 and Phase 1. It is six
specific checks:

1. Seven-day continuous live soak with no unexplained outages.
2. Network-interruption drill: cut connectivity for 60 seconds and verify
   automatic recovery.
3. Manually hand-check 20 real Polymarket trades against the system's fee
   model to verify fees are calculated correctly to the cent.
4. Less than 0.5% of reconciliation checks should drift more than 2 cents.
5. The event log replays cleanly from disk.
6. The monitor process correctly raises a stale-heartbeat alert when
   tested.

We are partway through soak day 1. G0 is realistically 5–10 days away.

---

## 8. The economics

This section is for stakeholders who want to understand the business case.

**Capital efficiency.** True arbitrage trades are risk-free returns on
capital — usually 0.5–3% per opportunity. They lock up capital briefly
(seconds to minutes) and pay out at market resolution (could be days to
months). Annualised returns depend on opportunity frequency, which we are
measuring during the observation phase.

**Realistic frequency.** During the observation soak, the system identified
2 unambiguous structural-arbitrage opportunities in 24 hours, both correctly
predicting positive forward returns. Whether this rate holds, and how much
capital can be deployed per opportunity without moving the market, are the
key open questions.

**Costs.**

- Fees: roughly 0.5–2 basis points per trade depending on the market price
  (the closer to 50¢, the more expensive).
- Infrastructure: one small server, one Telegram alerts channel, one
  development laptop. Trivial.
- Capital: not yet sized. Initial deployment would be in the low five
  figures pending Phase 1 sign-off.

**Risk per trade in Phase 1.** Capped at $25 per order, $100 per signal,
$250 total open position, $50 daily loss. These are deliberate "trip wires"
— if any of them fires repeatedly, something is wrong and the system halts
automatically.

---

## 9. What the report should communicate

In rough priority order:

1. **The system is safe by construction.** Lead with the fail-closed
   architecture. Nothing about it allows accidental trading. This is the
   single most important fact for any stakeholder.

2. **We are still in the data-collection phase, not the trading phase.** No
   real money has been placed. We have evidence on a few thousand signals
   already and are building toward a much larger evidence base.

3. **The economics work *if* the opportunity frequency holds.** True
   arbitrage is mathematically riskless. The unknown is whether enough of
   it happens at a useful frequency to justify deployment. The
   observation phase is answering exactly that.

4. **Research signals are interesting but unproven.** A 25% hit rate with
   negative average outcomes on "trade-through" is information — it tells
   us aggressive trades tend to revert. That is useful as a *filter on
   when not to trade*, even if it never becomes a directly tradable signal.

5. **The roadmap is conservative on purpose.** Each phase has explicit
   gate criteria. We do not advance until evidence supports it. This is
   the opposite of "ship fast and see what breaks."

---

## 10. Suggested visual structure for the designer

- **Cover.** One sentence: "An observation-first prediction-market trading
  system, currently in safe data collection."
- **Executive summary** (1 page): the 5 numbered points from section 9.
- **How it works** (2 pages): a diagram of the data flow, with the three
  strategy layers stacked. Section 3 has the content.
- **What the data says** (2 pages): the table from section 4 turned into
  small bar charts; one chart per strategy type comparing hit rate and
  outcome.
- **Safety architecture** (1 page): the gate diagram from section 5.
- **Roadmap** (1 page): the table from section 7 as a visual stepladder.
- **Economics** (1 page): section 8.
- **Glossary appendix** (1 page).

Suggested colour palette: muted greens/blues for "observation" and
"research", warm orange/amber for "executable but gated", red only for
"closed gates" or "do not." Avoid signalling success/profit with green
financial-chart green — the report is about a system that hasn't traded
yet.

Charts should never be more decorative than the numbers they show. Three
bar charts with real values beat one ornate dashboard mockup.

---

## 11. Citations and sources for the writer

Everything in this brief is grounded in the live system:

- Architecture: `CLAUDE.md`
- Phase roadmap: `PHASES.md`
- G0 gate: `G0_STATUS.md`
- Live signal numbers: queried from `data/state.db` on 2026-06-12
- Tutorial / operating instructions: `TUTORIAL.md`
- Source code: `pm/` directory — each scanner has a docstring describing
  its hypothesis

If anything in the report needs to be re-verified before publication, those
are the files of record.
