# Part 3: The Portfolio Advisor Agent — Screening, Analysis, and Actionable Recommendations From Your Phone

## What This Agent Does

The **Portfolio Advisor** is the most complex agent in my OpenCopilot setup. It chains together multiple sub-agents to deliver a full portfolio screening and analysis workflow:

1. **Screen** a list of stock tickers against their 7-day moving averages
2. **Flag** tickers with ±5% deviation (dips and surges)
3. **Run** the full `stock-analysis-pro` agent (10 analysis modules) on every flagged ticker
4. **Consolidate** everything into a single portfolio report with buy/sell/hold recommendations

The whole thing is triggered with a single Telegram message and runs autonomously in the cloud.

## The Input

From Telegram:

```
/portfolio-advisor AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA
```

Or with the longer syntax:

```
/agent portfolio-advisor AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA
```

That's it. A comma-separated list of tickers. The agent does the rest.

## What Happens Under the Hood

When this message hits the container, here's the chain of execution:

### Phase 1: Price Screening

The agent starts by fetching current prices and 7-day simple moving averages for every ticker via web search. This is the Copilot CLI's `web_search` and `web` tools at work — the agent is literally browsing the internet for stock data.

In Telegram, you see tool activity streaming in:

```
⚡ Web search: AAPL current stock price
⚡ Web search: AAPL 7 day moving average
⚡ Web search: MSFT current stock price
⚡ Web search: MSFT 7 day moving average
⚡ Web search: NVDA current stock price
...
```

After collecting all the data, the agent calculates the deviation from the 7-day SMA for each ticker and displays the screening table:

```
╔══════════════════════════════════════════════════════════════════════╗
║                    PORTFOLIO SCREENING RESULTS                      ║
║                  (7-Day Moving Average Basis)                      ║
╠══════════════════════════════════════════════════════════════════════╣

| Ticker | Current Price | 7-Day SMA | Deviation % | Alert            |
|--------|---------------|-----------|-------------|------------------|
| AAPL   | $198.50       | $201.30   | -1.4%       | ✅ Normal        |
| MSFT   | $441.20       | $438.75   | +0.6%       | ✅ Normal        |
| NVDA   | $118.40       | $131.60   | -10.0%      | ⬇️ DIP ALERT    |
| GOOGL  | $176.80       | $174.20   | +1.5%       | ✅ Normal        |
| AMZN   | $203.10       | $198.40   | +2.4%       | ✅ Normal        |
| META   | $628.90       | $589.50   | +6.7%       | ⬆️ SURGE ALERT  |
| TSLA   | $272.50       | $258.30   | +5.5%       | ⬆️ SURGE ALERT  |

Flagged for deep analysis: 3 of 7 tickers
```

### Phase 2: Deep Analysis on Flagged Tickers

Now the agent invokes `stock-analysis-pro` on NVDA, META, and TSLA. The `stock-analysis-pro` agent itself runs **10 independent analysis modules** (each a separate skill):

1. **Goldman Sachs Screener** — Quantitative scoring on valuation metrics (P/E, PEG, P/S, etc.)
2. **Morgan Stanley DCF** — Discounted cash flow with bull/base/bear scenarios
3. **Bridgewater Risk** — Macroeconomic risk parity analysis
4. **JPMorgan Earnings** — Earnings quality, growth trajectory, revision trends
5. **BlackRock Portfolio Fit** — Factor exposure, sector allocation, diversification
6. **Citadel Technical** — Chart patterns, support/resistance, momentum indicators
7. **Harvard Dividend** — Dividend sustainability, payout ratio, yield analysis
8. **Bain Competitive** — Moat assessment, competitive positioning, market share
9. **RenTech Patterns** — Statistical pattern recognition, mean reversion signals
10. **McKinsey Macro** — Industry trends, regulatory landscape, macro headwinds/tailwinds

Each module does its own web research, writes a detailed report to the `reports/` folder, and returns a structured score (1-5).

In Telegram, you see a flurry of tool activity:

```
⚡ Loading agent: stock-analysis-pro
⚡ Web search: NVDA latest earnings Q4 2025
⚡ Web search: NVDA revenue growth rate
⚡ Web search: NVDA analyst price targets
⚡ Writing NVDA-01-goldman-sachs-screener.md
⚡ Web search: NVDA discounted cash flow
⚡ Writing NVDA-02-morgan-stanley-dcf.md
⚡ Web search: NVDA geopolitical risk China export
⚡ Writing NVDA-03-bridgewater-risk.md
...
⚡ Writing NVDA-10-mckinsey-macro.md
⚡ Writing NVDA-final-report.md
⚡ Loading agent: stock-analysis-pro
⚡ Web search: META latest earnings
...
⚡ Writing META-final-report.md
⚡ Loading agent: stock-analysis-pro
⚡ Web search: TSLA latest earnings
...
⚡ Writing TSLA-final-report.md
```

This phase generates **33 report files** (10 modules + 1 final per flagged ticker).

### Phase 3: Consolidated Portfolio Report

Finally, the agent synthesizes everything into a single master report. Here's what gets displayed in Telegram:

```
═══════════════════════════════════════════════════════════════
  PORTFOLIO ADVISORY REPORT
  Date: April 5, 2026
  Screened: 7 tickers | Flagged: 3
═══════════════════════════════════════════════════════════════

CONSOLIDATED RECOMMENDATIONS:

| Ticker | Alert        | Deviation | Grade     | Score    | Action   |
|--------|-------------|-----------|-----------|----------|----------|
| NVDA   | ⬇️ DIP      | -10.0%    | BUY 🟢   | 3.85/5   | **BUY**  |
| META   | ⬆️ SURGE    | +6.7%     | HOLD 🟡  | 3.28/5   | **TRIM** |
| TSLA   | ⬆️ SURGE    | +5.5%     | HOLD 🟡  | 2.95/5   | **HOLD** |

KEY FINDINGS:
• NVDA's 10% dip driven by temporary export concern fears — fundamentals
  remain strong with 78% YoY data center revenue growth. Represents a
  buying opportunity.
• META surge reflects strong Reels monetization but elevated P/E of 28x
  suggests taking partial profits.
• TSLA surge is momentum-driven without corresponding earnings improvement.
  Hold current position, do not add.

PORTFOLIO ACTION PLAN:
  1. BUY NVDA — Add to position on the dip (Priority: HIGH)
  2. TRIM META — Take 15-20% off the table (Priority: MEDIUM)
  3. HOLD TSLA — Monitor but no action needed (Priority: LOW)

  OVERALL PORTFOLIO HEALTH: HEALTHY

✅ Normal: AAPL, MSFT, GOOGL, AMZN — trading within 5% of 7-day SMA.

📂 34 reports saved in reports/ folder — 3 full stock analyses
   (10 modules each) + 1 consolidated portfolio report.
```

## The Agent Definition

Here's the actual `portfolio-advisor.agent.md` that drives all of this (abbreviated — the full file is ~180 lines):

```yaml
---
name: portfolio-advisor
description: Monitors a portfolio of stock tickers for significant price moves
argument-hint: A list of stock tickers, e.g., "AAPL, MSFT, NVDA, GOOGL, AMZN"
tools: ["edit", "agent", "search", "web"]
agents: ["stock-analysis-pro"]
---
```

Key design decisions:

- **`tools: ["edit", "agent", "search", "web"]`** — the agent needs `agent` tool access to invoke `stock-analysis-pro` as a sub-agent. It also needs `edit` to write the consolidated report, `search` to look for MCP server data, and `web` to fetch live prices.

- **`agents: ["stock-analysis-pro"]`** — declares a dependency on the sub-agent. The Copilot CLI resolves this and makes the sub-agent available.

- **Three-phase workflow** — the markdown body defines explicit phases (Screening → Deep Analysis → Consolidation) with structured output templates. The AI follows these instructions to produce consistent, repeatable reports.

- **±5% threshold** — this is the trigger for deep analysis. Without it, the agent would run 10 modules on every ticker, which would take 30+ minutes for a 7-ticker portfolio. The screening step keeps it manageable.

## The Sub-Agent: stock-analysis-pro

The `stock-analysis-pro` agent is itself a complex piece — it orchestrates 10 skill files:

```yaml
---
name: stock-analysis-pro
description: Comprehensive 10-module institutional-grade stock analysis
tools: ["edit", "agent", "search", "web"]
skills: [goldman-sachs-screener, morgan-stanley-dcf, bridgewater-risk,
         jpmorgan-earnings, blackrock-portfolio, citadel-technical,
         harvard-dividend, bain-competitive, rentech-patterns, mckinsey-macro]
---
```

Each skill is a `.skill.md` file with detailed instructions for one analysis module. For example, the Goldman Sachs Screener skill tells the AI to:
1. Look up current valuation metrics (P/E, PEG, P/S, EV/EBITDA)
2. Compare against sector averages
3. Apply a quantitative scoring framework
4. Write the report to `reports/{TICKER}-01-goldman-sachs-screener.md`
5. Return a JSON score

The Morgan Stanley DCF skill instructs the AI to build a full discounted cash flow model with revenue projections, discount rates, and terminal values across bull/base/bear scenarios.

Every skill has a mandatory **"Step 0: Live Data Collection"** that forces the agent to do fresh web searches before analysis. This is critical — without it, the model would produce analysis based on stale training data.

## Running on a Schedule

The real power emerges when you combine this with the cron system:

```
/cron daily portfolio-advisor AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA --email me@company.com --time 08:00
```

Every morning at 8:00 UTC, the system automatically:
1. Screens all 7 tickers
2. If any deviate ≥5% from their 7-day average, runs full analysis
3. Emails the complete report with all generated files
4. Sends a short ✅ notification to Telegram

On quiet days (no tickers flagged), the email simply says:
> ✅ No Action Required — All portfolio holdings are trading within 5% of their 7-day moving average.

On volatile days, you get a full institutional-grade analysis in your inbox before your morning coffee.

## Generated File Structure

After a full run with 3 flagged tickers, the `reports/` folder contains:

```
reports/
├── NVDA-01-goldman-sachs-screener.md
├── NVDA-02-morgan-stanley-dcf.md
├── NVDA-03-bridgewater-risk.md
├── NVDA-04-jpmorgan-earnings.md
├── NVDA-05-blackrock-portfolio.md
├── NVDA-06-citadel-technical.md
├── NVDA-07-harvard-dividend.md
├── NVDA-08-bain-competitive.md
├── NVDA-09-rentech-patterns.md
├── NVDA-10-mckinsey-macro.md
├── NVDA-final-report.md
├── META-01-goldman-sachs-screener.md
├── META-02-morgan-stanley-dcf.md
├── ...
├── META-final-report.md
├── TSLA-01-goldman-sachs-screener.md
├── TSLA-02-morgan-stanley-dcf.md
├── ...
├── TSLA-final-report.md
└── portfolio_advisory_report.md    ← the master consolidated report
```

Each individual module report is 500-1,500 words of structured analysis. The consolidated report is typically 3,000-5,000 words. All accessible through the web dashboard's file explorer with full markdown rendering.

## What a Module Report Looks Like

Here's an abbreviated example of what the Goldman Sachs Screener module produces for NVDA:

```markdown
# NVDA — Goldman Sachs Multi-Factor Screener
**Date:** April 5, 2026
**Current Price:** $118.40

## Valuation Metrics

| Metric       | NVDA     | Sector Avg | vs Sector | Signal |
|-------------|----------|------------|-----------|--------|
| P/E (TTM)   | 52.3x    | 28.4x      | +84%      | 🔴     |
| Forward P/E | 31.2x    | 24.1x      | +29%      | 🟡     |
| PEG Ratio   | 0.84     | 1.45       | -42%      | 🟢     |
| P/S (TTM)   | 28.7x    | 6.2x       | +363%     | 🔴     |
| EV/EBITDA   | 45.1x    | 18.3x      | +146%     | 🔴     |

## Quantitative Score

Despite elevated traditional valuation multiples, NVDA's PEG ratio of
0.84 (below 1.0) indicates the growth rate justifies the premium.
Revenue grew 78% YoY in the most recent quarter...

## Score: 3.5/5 — 🟢 Bullish
```

## Lessons Learned Building This Agent

### The Prompt is the Product

The `portfolio-advisor.agent.md` file is ~180 lines of carefully structured markdown. Every section — the screening criteria, the report template, the recommendation categories (BUY/SELL/HOLD/TRIM/ADD) — is there because without it, the AI produces inconsistent output. The first version of this agent produced different report formats every time. Adding explicit templates with placeholder values (`{TICKER}`, `${PRICE}`, `{+/- X.X%}`) made the output repeatable.

### Sub-Agent Orchestration Is Fragile

When the portfolio advisor invokes `stock-analysis-pro`, it's the Copilot CLI invoking itself recursively via the `agent` tool. This mostly works, but:

- Sometimes a module skill gets skipped if the agent decides it already has "enough" information
- The "run in parallel" instruction is aspirational — the CLI processes skills sequentially
- If one module errors out, the agent needs explicit instructions to record it as "Neutral (3/5)" and continue

### ±5% Threshold Is Arbitrary But Practical

The 5% threshold from the 7-day moving average was chosen through trial and error. At 3%, too many tickers get flagged on normal market days. At 10%, you miss meaningful dips. 5% catches genuinely unusual moves without overwhelming the system.

### Cron + This Agent = Daily Portfolio Intelligence

The combination of the cron scheduler and this agent is the use case that makes OpenCopilot worth deploying. You get a daily email that either says "everything's fine, no action needed" or "NVDA dropped 10%, here's a full analysis of why and what to do." Zero daily effort required.

---

*This is Part 3 of a 4-part series on OpenCopilot. [← Part 2: Design Deep-Dive](part-2-design-deep-dive.md) | [Part 4: Real Estate Analysis Agent →](part-4-real-estate-agent.md)*
