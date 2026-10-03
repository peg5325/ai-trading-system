# Project session contract

## Start every session

Before planning or changing this project, read in order:

1. `docs/PROJECT_CHARTER.md` — goal, measurement, risk and evaluation policy.
2. `docs/ROADMAP.md` — current stage and completion conditions.
3. `docs/PROJECT_STATUS.md` — verified evidence and next action.

Then inspect actual git/worktree state and any relevant current issue/PR. State briefly
which roadmap stage the requested task serves. Missing documents or stale claims must
be reported, not replaced with invented progress. These documents provide context;
the user's latest explicit instructions govern the active task.

## Project direction

Preserve the existing project and research evidence. The current objective (Charter
v2.0, 2026-10-02) is seed growth: grow the dedicated account from USD 2,000 to a
balance of USD 4,000 (deposits included, no deadline) within the Charter's risk policy,
using US-listed index ETFs (1x, leveraged, inverse) driven only by signals that can be
validated on long historical data. The former S&P 500 outperformance goal is on hold and
is not a decision criterion. A watchlist, one profitable day, a backtest or a framework's
marketing claim is not proof of a deployable profitable strategy.

Read-only shadow results do not authorize orders, blind-OOS access or promotion.
Policy documentation is not evidence that enforcement code exists. Do not silently
rewrite preregistrations, past verdicts, benchmarks or evaluation thresholds after
observing outcomes. Version changes prospectively and preserve evidence.

## Daily trading journal (user rule, 2026-10-04)

Exploratory Paper (Charter v2.3 §4.6) journals start with the 2026-10-05 US session.

Once day-trading runs (Paper or live), every trading day must end with a journal written after the
US close. It goes in `docs/journal/YYYY-MM-DD.md` (Korean, plain language) and must contain:

1. Each entry and exit: time, symbol, side, size, price, and **why**. Why means the market situation
   the system detected, the strategy it chose, the signal values that fired, and the exit reason.
2. Days or periods with no trade, and why the system stayed out.
3. Result after costs, compared with simply holding 1x for the day.
4. **What went well** (keep it) and **what went wrong**, each with a concrete idea for improving it.
5. Before writing a new entry, read the recent entries. Note which past ideas were tried and whether
   they helped.

Facts (trades, signals, P&L) come from the system's journal and ledgers, never from memory.
Improvement ideas are applied as versioned rule changes that take effect from the next session, and
the journal records the change and its reason. Rules are never edited in the middle of a session.
Session start: read the latest journal entries before proposing strategy changes.

## End meaningful work

Update `docs/PROJECT_STATUS.md` with verified work, evidence, remaining blockers and
the next concrete step. Update `docs/ROADMAP.md` when a stage changes. Change Charter
policy only within user-authorized scope, recording the reason and effective period.
Use one policy source of truth rather than copying thresholds across documents.

Preserve unrelated user edits and `.codex/`. Never store credentials or account
identifiers in these documents. Follow applicable parent/session instructions and
the repository's existing commit/review conventions.
