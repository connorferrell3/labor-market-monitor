# The Post-Pandemic Labor Market: What the Data Actually Says

*A one-page read on U.S. labor conditions, 2021–2026. Built from four federal/public data sources. — Connor Ferrell, August 2026*

---

## The question

Everyone has a take on whether the job market is "good" or "bad." I wanted to answer it from the data directly: **Are workers actually getting ahead, and is the labor market cooling or holding?** I built a pipeline pulling monthly data from the BLS (JOLTS, CPS, CES, CPI), Adzuna, and the University of Michigan, and looked at what the numbers — not the headlines — support.

## What I found

**1. Workers spent most of this cycle losing ground, and only recently broke even.**
Nominal wage growth stayed positive the whole period, so paychecks kept rising in dollar terms. But once you subtract inflation, *real* wage growth was negative through the 2022 inflation spike — people earned more dollars that bought less. Real wages only clawed back toward break-even as inflation cooled in 2024–26. The takeaway a headline misses: "wages are up" and "workers are worse off" were both true at the same time.

**2. Labor demand has cooled sharply from its peak — but hasn't collapsed.**
Job openings across every sector are down substantially from their 2021 record highs. That's a real cooling. But unemployment has stayed in a historically moderate 4–4.5% range, not a recessionary one. The picture is *normalization*, not contraction — the post-COVID hiring frenzy ending, not a downturn beginning.

**3. Consumer sentiment does NOT reliably predict the job market — and that's a finding, not a failure.**
The popular narrative says how people *feel* leads what the economy *does*. I tested it directly with a lead-lag correlation analysis (first-differencing the series to avoid spurious trend correlation, then testing significance). Result: **no statistically significant lead-lag relationship** in the post-2021 window — the correlations were weak and their confidence intervals included zero. In plain terms: sentiment and the labor market moved largely independently month to month. A weaker analysis would have reported noise as a signal; the honest answer is that the relationship isn't there in this data.

## Why you can trust these numbers (and where you shouldn't)

I built specific safeguards against the ways time-series data fools people:

- **First-differencing before correlating** — because two trending series always *look* correlated even when unrelated.
- **Same-month year-over-year comparisons** — so seasonal patterns (retail spikes every December) don't masquerade as trends.
- **Significance testing with confidence intervals** — so a weak correlation gets reported as "not distinguishable from zero," not dressed up as a discovery.

**The honest limit:** the reliable overlapping window is ~55 months (post-2021, chosen deliberately to exclude the distorting COVID shock). That's enough for the relationship analysis above, but too little to *forecast* — so I don't. Where the data can't support a claim, I say so.

## How it was built

A four-stage pipeline — Collect → Store → Analyze → Display — pulling from four independent government/public sources into one unified schema, computing derived indicators (real wages, sector breakdowns) and a lead-lag analysis, served as an interactive dashboard. Python, SQLite, pandas, Chart.js.

**Live dashboard:** (https://connorferrell3.github.io/labor-market-monitor/)  ·  **Code + methodology:** connorferrell3

---

*The most useful thing an analyst can say is often "the data doesn't support that." This project is an exercise in doing exactly that — carefully, and out loud.*
