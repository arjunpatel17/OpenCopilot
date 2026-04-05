# Part 4: The Real Estate Analysis Agent — Institutional-Grade Property Intelligence From a Telegram Message

## What This Agent Does

The **Real Estate Analysis Agent** evaluates a property investment through 6 specialized modules — property valuation, neighborhood intelligence, rental cash flow, market trends, mortgage financing, and risk assessment — then synthesizes everything into an investor-ready verdict with a weighted score and specific action recommendation.

Unlike the Portfolio Advisor (which screens a set of tickers), this agent performs a deep dive on a single property or market. You give it an address, and it comes back with a comprehensive investment analysis.

## The Input

From Telegram:

```
/real-estate-analysis 505 Regency Trl, Acworth GA 30102, 4-bed 3-bath SFH listed at $415,000
```

Or with more context:

```
/agent real-estate-analysis 3-bed 2-bath townhouse at 1420 Maple Dr, Nashville TN, listed at $389,000. Planning to buy-and-hold as a rental. 20% down payment. Looking at a 30-year fixed at 6.75%.
```

The more context you provide upfront, the better the analysis. If you leave things out (like interest rate or investment strategy), the agent uses current market defaults and notes its assumptions.

## What Happens Under the Hood

### Step 1: Context Parsing

The agent extracts (or infers) the key variables from your message:

```
PROPERTY:             505 Regency Trl, Acworth GA 30102
LIST_PRICE:           $415,000
PROPERTY_TYPE:        Single-Family Home (SFH)
MARKET:               Acworth / NW Atlanta Metro
STATE:                Georgia
INVESTMENT_STRATEGY:  Buy-and-hold rental (default)
DOWN_PAYMENT:         20% ($83,000)
INTEREST_RATE:        Current market rate (~6.75%)
```

### Step 2: Six Skills Run in Parallel

The agent invokes 6 specialized skills, each performing independent web research and writing its own report:

```
⚡ Web search: 505 Regency Trl Acworth GA property details Zillow Redfin
⚡ Web search: Acworth GA comparable home sales 2025 2026
⚡ Web search: Acworth GA rent prices 4 bedroom
⚡ Web search: Acworth GA neighborhood crime rate schools walkability
⚡ Web search: Atlanta metro real estate market trends 2026
⚡ Web search: current 30 year fixed mortgage rates April 2026
⚡ Web search: Acworth GA flood zone natural disaster risk
⚡ Writing 505-regency-trl-acworth-ga-01-property-valuation.md
⚡ Writing 505-regency-trl-acworth-ga-02-neighborhood-intelligence.md
⚡ Writing 505-regency-trl-acworth-ga-03-rental-cashflow.md
⚡ Writing 505-regency-trl-acworth-ga-04-market-trends.md
⚡ Writing 505-regency-trl-acworth-ga-05-mortgage-financing.md
⚡ Writing 505-regency-trl-acworth-ga-06-property-risk.md
```

Let's look at what each module does:

#### Module 1: Property Valuation (CMA) — Weight: 20%

The skill performs a Comparative Market Analysis:
- Pulls 5-7 recent comparable sales from the area
- Identifies 3-5 active listings for market competition context
- Applies three valuation methods (Sales Comparison, Income Approach, Cost Approach)
- Calculates price per square foot vs. neighborhood and city medians
- Determines if the property is undervalued, fairly priced, or overvalued

#### Module 2: Neighborhood Intelligence — Weight: 15%

Researches the surrounding area:
- School ratings and districts
- Crime statistics and trends
- Walkability and transit scores
- Employer proximity and job market
- Demographic trends (population growth, income levels)
- Planned developments and infrastructure projects

#### Module 3: Rental & Cash Flow — Weight: 25%

The highest-weighted module — builds a complete cash flow model:
- Rent comps from the area (what similar homes rent for)
- Monthly income projections
- Full expense breakdown (mortgage, taxes, insurance, maintenance, vacancy, property management)
- Net Operating Income (NOI)
- Cap rate, cash-on-cash return, and gross rent multiplier (GRM)
- 5-year and 10-year projected returns with appreciation

#### Module 4: Market Trends & Cycle — Weight: 15%

Analyzes where the local market sits in the real estate cycle:
- Median home price trends (1-year, 3-year, 5-year)
- Days on market trends
- Inventory levels (months of supply)
- Price-to-rent ratio vs. historical norms
- Population and job growth rate
- New construction permits and pipeline

#### Module 5: Mortgage & Financing — Weight: 10%

Models the financing structure:
- Monthly PITI payment (principal, interest, taxes, insurance)
- Amortization schedule highlights (equity at 5, 10, 15 years)
- Comparison of financing options (30-year fixed, 15-year, ARM)
- Break-even analysis vs. renting
- Refinancing scenarios if rates drop

#### Module 6: Property Risk Assessment — Weight: 15%

Evaluates investment risks:
- Natural disaster risk (flood zone, earthquake, wildfire, hurricane)
- Environmental factors (proximity to industrial sites, Superfund)
- Market risk (oversupply, economic dependence on single employer)
- Property-specific risk (age, maintenance concerns, HOA)
- Insurance cost estimates
- Worst-case scenario analysis

### Step 3: Synthesis and Verdict

After all 6 skills complete, the agent collects their scores and synthesizes the final report.

## The Output

Here's what appears in Telegram:

```
═══════════════════════════════════════════════════════════════
  PROPERTY:           505 Regency Trl, Acworth GA 30102
  LISTING PRICE:      $415,000
  PROPERTY TYPE:      Single-Family Home
  MARKET:             Acworth, GA (NW Atlanta Metro)
  STRATEGY:           Buy-and-hold rental

  WEIGHTED SCORE:     3.64 / 5.00

  ██████████████████████████████████████████████████████████

  INVESTMENT GRADE:   BUY 🟢

  ESTIMATED VALUE:        $428,000 (+3.1% vs listing)
  MAX OFFER PRICE:        $420,000
  MONTHLY CASH FLOW:      $287/mo
  CAP RATE:               5.8%
  CASH-ON-CASH RETURN:    7.2%
  TOTAL ROI (5-YEAR):     48.3%
  RISK LEVEL:             4/10

  ██████████████████████████████████████████████████████████
═══════════════════════════════════════════════════════════════

CONSOLIDATED SCORECARD:

| # | Module                    | Finding                    | Signal     | Confidence |
|---|---------------------------|----------------------------|------------|------------|
| 1 | Property Valuation (CMA)  | ~3% below estimated value  | 🟢 Under   | High       |
| 2 | Neighborhood Intelligence | Strong schools, low crime   | 🟢 Strong  | High       |
| 3 | Rental & Cash Flow        | $287/mo positive cash flow | 🟢 Strong  | Medium     |
| 4 | Market Trends & Cycle     | Steady growth, low supply  | 🟢 Strong  | Medium     |
| 5 | Mortgage & Financing      | 6.75% rate, manageable DTI | 🟡 Average | High       |
| 6 | Property Risk Assessment  | No flood zone, low risk    | 🟢 Low     | High       |

KEY TAKEAWAYS:
1. Valuation: Property is slightly undervalued — listing is ~3% below
   estimated market value based on recent comps.
2. Cash Flow: Positive cash flow of $287/month after all expenses.
   Rent comps at $2,450-$2,600/mo for similar 4-bed homes.
3. Location: Acworth schools rated 7-8/10, crime below state average,
   strong employment from Lockheed Martin and KSU proximity.
4. Market Timing: NW Atlanta metro in growth phase — population up
   2.3% YoY, housing inventory at 2.1 months (seller's market).
5. Biggest Risk: Interest rate risk — if rates stay above 7%, refinancing
   upside is limited and appreciation may moderate.
6. Max Offer: $420,000 — deal still works up to this price.
7. Action: BUY at or below asking price. Strong cash flow property in a
   growing market with low risk profile.

📂 Detailed reports saved in reports/ folder — 6 individual analysis
   reports + final real estate investment synthesis.
```

After this summary, the bot sends a follow-up message with clickable links to each report:

```
📄 Generated Files:
• 505-regency-trl-acworth-ga-01-property-valuation.md
• 505-regency-trl-acworth-ga-02-neighborhood-intelligence.md
• 505-regency-trl-acworth-ga-03-rental-cashflow.md
• 505-regency-trl-acworth-ga-04-market-trends.md
• 505-regency-trl-acworth-ga-05-mortgage-financing.md
• 505-regency-trl-acworth-ga-06-property-risk.md
• 505-regency-trl-acworth-ga-final-real-estate-analysis.md

🔗 Open File Explorer
```

Each file name is a clickable link that opens the full rendered report in your browser.

## The Agent Definition

```yaml
---
name: real-estate-analysis
description: Comprehensive 6-module institutional-grade real estate investment analysis
argument-hint: "A property address or market, e.g., '3-bed SFH at 123 Oak St, Austin TX listed at $485,000'"
tools: ["edit", "agent", "search", "web"]
skills: [property-valuation, neighborhood-intelligence, rental-cashflow,
         realestate-market-trends, mortgage-financing, property-risk]
---
```

The body of the agent (~200 lines) defines:
- The input variables to extract from the user's message
- The workflow: parse → run skills → collect → synthesize → report
- The weighted scoring formula (25% for cash flow, 20% for valuation, 15% each for neighborhood/market/risk, 10% for financing)
- The exact report structure with template tables
- The grade scale (4.5-5.0 = STRONG BUY, 3.5-4.49 = BUY, etc.)
- What to show in chat vs. what to save to files

## What a Skill Report Looks Like

Here's an abbreviated example of the **Rental & Cash Flow** module output:

```markdown
# 505 Regency Trl — Rental & Cash Flow Analysis
**Property:** 4-bed 3-bath SFH, ~2,400 sqft
**Listing Price:** $415,000

## Rent Comparables

| # | Address              | Beds/Baths | SqFt  | Rent    | $/SqFt | Distance |
|---|----------------------|------------|-------|---------|--------|----------|
| 1 | 612 Bentwater Dr     | 4/3        | 2,350 | $2,500  | $1.06  | 0.8 mi   |
| 2 | 143 Brookwood Ct     | 4/2.5      | 2,200 | $2,400  | $1.09  | 1.2 mi   |
| 3 | 890 Lake Acworth Dr  | 4/3        | 2,600 | $2,600  | $1.00  | 1.5 mi   |
| 4 | 221 Baker Grove      | 3/2        | 1,900 | $2,150  | $1.13  | 0.5 mi   |
| 5 | 77 Chestnut Hill Rd  | 4/3        | 2,500 | $2,550  | $1.02  | 2.0 mi   |

**Estimated Monthly Rent:** $2,500 (conservative based on 4/3 comps)

## Monthly Cash Flow Projection

| Item                     | Amount     |
|--------------------------|------------|
| **Gross Rental Income**  | $2,500     |
| – Vacancy (5%)           | -$125      |
| **Effective Income**     | $2,375     |
|                          |            |
| – Mortgage (P&I)         | -$1,562    |
| – Property Tax           | -$260      |
| – Insurance              | -$145      |
| – Maintenance (5%)       | -$125      |
| – Property Mgmt (8%)     | -$190      |
| – HOA                    | $0         |
|                          |            |
| **Net Cash Flow**        | **$93/mo** |

## Key Metrics

| Metric              | Value    | Benchmark   | Rating    |
|---------------------|----------|-------------|-----------|
| Cap Rate            | 5.8%     | >5% good    | 🟢        |
| Cash-on-Cash Return | 7.2%     | >6% good    | 🟢        |
| Gross Rent Mult.    | 13.8x    | <15x good   | 🟢        |
| Debt Service Ratio  | 1.19     | >1.1 safe   | 🟢        |
| 1% Rule             | 0.60%    | 1% ideal    | 🟡        |

## 5-Year Projection (3% annual appreciation, 3% rent growth)

| Year | Property Value | Annual Rent | Annual Cash Flow | Total Equity |
|------|----------------|-------------|------------------|--------------|
| 1    | $427,450       | $30,000     | $1,116           | $98,130      |
| 2    | $440,273       | $30,900     | $2,196           | $114,212     |
| 3    | $453,481       | $31,827     | $3,345           | $131,287     |
| 4    | $467,086       | $32,782     | $4,568           | $149,404     |
| 5    | $481,098       | $33,766     | $5,867           | $168,614     |

**5-Year Total ROI: 48.3%** (appreciation + cash flow + principal paydown)

## Score: 4/5 — 🟢 Strong Cash Flow Expected
```

## Comparing the Two Agents

| Aspect | Portfolio Advisor | Real Estate Analysis |
|--------|------------------|---------------------|
| **Input** | List of stock tickers | Property address + details |
| **Sub-agents** | Uses `stock-analysis-pro` (10 modules) | Uses 6 skills directly |
| **Screening** | ±5% deviation filter before deep dive | No filter — always full analysis |
| **Output files** | 10-33+ reports depending on flags | 7 reports (6 modules + synthesis) |
| **Typical runtime** | 3-8 minutes depending on flagged tickers | 2-4 minutes |
| **Cron use case** | Daily portfolio screening | On-demand when evaluating a property |
| **Scoring** | 10 modules, 1-5 each, weighted → grade | 6 modules, 1-5 each, weighted → grade |

## Practical Workflow: Evaluating a Property Before Touring

Here's how I actually use this:

1. **Browse listings** on Zillow/Redfin during downtime
2. **Find an interesting property** — looks like it might cash flow
3. **Send to Telegram:** `/real-estate-analysis [address and details]`
4. **Wait 2-4 minutes** — work on something else
5. **Review the verdict** on my phone — is it a BUY, HOLD, or PASS?
6. **Deep dive** — if the verdict is promising, open the file explorer on my laptop and read the individual module reports
7. **Make a decision** — schedule a tour, make an offer, or move on

For comparison, hiring a professional analyst for this level of multi-factor property analysis would take days and cost hundreds of dollars. The agent produces comparable structured output in minutes, for essentially free.

## Tips for Building Your Own Agents

After building 15 agents for OpenCopilot, here's what I've learned:

### 1. Define the Output Template First

Start with what you want the final output to look like. Work backwards from there. If you want a table with specific columns, put that table in the agent's instructions with placeholder values. The AI follows templates far more reliably than it follows abstract descriptions.

### 2. Force Live Web Research

Every skill should have a mandatory web research step at the beginning. Without it, the model generates plausible-sounding but potentially outdated analysis. The `web` and `search` tools make a dramatic difference in report quality.

### 3. Modularize with Skills

Break complex analyses into independent skill files. Each skill should:
- Have a single responsibility (one module, one report)
- Write its output to a predictable file path
- Return a structured score/signal
- Be reusable across agents

### 4. Be Explicit About What NOT to Do

"Do NOT paste full module reports into chat" is in both agent definitions for a reason. Without that instruction, the agent dumps thousands of words into Telegram, making the summary unreadable. Explicit negative instructions are as important as positive ones.

### 5. Handle Failures Gracefully

"If any skill fails or is unavailable, record its score as 3 (Neutral) and note 'Analysis unavailable' in the final report." Without this, one failed module crashes the entire analysis.

## Wrapping Up

OpenCopilot started as a weekend project to use the Copilot CLI from my phone. It turned into a full platform for running AI agents in the cloud — with scheduling, email delivery, file management, and real-time streaming.

The agents themselves — from portfolio screening to property analysis — demonstrate what you can build when you combine:
- A powerful AI model with tool access (shell, web, file I/O)
- A structured agent definition format (markdown + YAML)
- A cloud runtime with persistent storage
- A mobile-friendly interface (Telegram)

The code is straightforward Python and vanilla JS. No frameworks, no complexity for its own sake. The intelligence lives in the agent and skill definitions — the infrastructure just makes it accessible from anywhere.

If you want to build your own agents on this platform, the pattern is simple:
1. Write a `.agent.md` file with frontmatter + instructions
2. Write `.skill.md` files for modular sub-tasks
3. Drop them in `workspace/.github/agents/` and `workspace/.github/skills/`
4. Deploy with `./deploy.sh`
5. Chat with your agents from Telegram

---

*This is Part 4 of a 4-part series on OpenCopilot. [← Part 3: Portfolio Advisor Agent](part-3-portfolio-advisor-agent.md) | [Part 1: Architecture & Vision](part-1-architecture-and-vision.md)*
