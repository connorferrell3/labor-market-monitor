"""
Lead-lag analysis: does one macro series move BEFORE another?

This is the analytical centerpiece. It answers questions like:
  "Do changes in consumer sentiment lead changes in unemployment?
   And if so, by how many months?"

WHY THIS IS THE RIGHT ANALYSIS FOR THIS DATA
--------------------------------------------
You have ~130 months of overlapping series. That's too little to forecast
reliably, but plenty for lead-lag (cross-correlation) analysis, which is a
standard, respected technique in macro/econ. It's honest about being
correlational, and it produces one clean, interpretable result.

THE ONE TRAP YOU MUST AVOID (and can explain in an interview)
-------------------------------------------------------------
Macro series TREND over years. If you correlate two trending series in their
raw "level" form, you get a big correlation that means almost nothing - both
are just drifting with time. (The famous example: cheese consumption vs. deaths
by bedsheet entanglement - both rose for years, so they "correlate" at ~0.95.)

The fix is to analyze CHANGES, not levels: take the month-to-month difference
of each series first. This is called "differencing." It removes the shared
trend, so any correlation left over is a real relationship between the two
series' movements - not just both going up over time.

Everything below works on differenced series for exactly this reason.

Setup:
    pip install pandas numpy
Usage:
    python leadlag.py
"""

import sqlite3
import pandas as pd
import numpy as np

DB_PATH = "jobs.db"

# How many months to slide, in each direction, when hunting for the best lag.
MAX_LAG = 12


def load_series(conn, source, metric):
    """
    Pull one metric into a pandas Series indexed by month.

    pandas is the core data-analysis library - a Series here is basically a
    labeled column: the label (index) is the date, the value is the number.
    Indexing by date is what lets us align two different series by month later.
    """
    rows = conn.execute(
        "SELECT date, value FROM series "
        "WHERE source = ? AND metric = ? ORDER BY date ASC",
        (source, metric),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    dates = pd.to_datetime([r[0] for r in rows])
    values = [r[1] for r in rows]
    return pd.Series(values, index=dates)


def lead_lag(series_a, series_b, max_lag=MAX_LAG):
    """
    Measure how series A and series B move together at different time shifts.

    Returns a DataFrame: for each lag from -max_lag to +max_lag, the
    correlation between A's changes and B's changes at that shift.

    Interpretation of the lag sign:
      lag > 0  -> A LEADS B  (A's move today lines up with B's move `lag`
                  months LATER). This is the interesting case: A predicts B.
      lag = 0  -> they move in the same month.
      lag < 0  -> B leads A.
    """
    # STEP 1: align the two series on their shared months. If one starts later
    # or has gaps, we only keep months present in BOTH. Analyzing mismatched
    # dates is a classic silent bug - alignment prevents it.
    df = pd.DataFrame({"a": series_a, "b": series_b}).dropna()
    if len(df) < max_lag + 5:
        return None   # not enough overlap to say anything trustworthy

    # STEP 2: DIFFERENCE both series (changes, not levels). .diff() computes
    # each month minus the previous month. This is the anti-spurious-correlation
    # step described at the top. The first value becomes NaN (nothing before it),
    # so we drop it.
    changes = df.diff().dropna()

    # STEP 3: for each lag, shift B and correlate with A.
    results = []
    for lag in range(-max_lag, max_lag + 1):
        # shifting b by -lag lines up "A now" with "B lag-months-later"
        shifted_b = changes["b"].shift(-lag)
        pair = pd.DataFrame({"a": changes["a"], "b": shifted_b}).dropna()
        if len(pair) < 5:
            continue
        # Pearson correlation of the two columns of changes.
        r = pair["a"].corr(pair["b"])
        results.append({"lag_months": lag, "correlation": r, "n": len(pair)})

    return pd.DataFrame(results)


def describe_strength(r):
    """Plain-English read of a correlation's size (absolute value)."""
    a = abs(r)
    if a >= 0.6:
        return "strong"
    if a >= 0.4:
        return "moderate"
    if a >= 0.2:
        return "weak"
    return "negligible"


def significance(r, n):
    """
    Compute a p-value and 95% confidence interval for a correlation r from n
    paired observations.

    P-VALUE - "if the true correlation were zero, how likely is a sample
    correlation at least this extreme?" We use the t-statistic for a Pearson r:
        t = r * sqrt((n - 2) / (1 - r^2))
    with n-2 degrees of freedom, two-tailed. Small p (< 0.05) => unlikely to be
    a fluke.

    CONFIDENCE INTERVAL via Fisher z-transform - you can't put a normal interval
    directly on r because r is bounded at +/-1 and its sampling distribution is
    skewed near the edges. Fisher's transform z = arctanh(r) is approximately
    normal with standard error 1/sqrt(n-3); we build the interval there and map
    back with tanh. This is the textbook-correct method.

    Returns (p_value, ci_low, ci_high). Any may be None if n is too small.
    """
    from math import sqrt, atanh, tanh

    if n is None or n < 4 or r is None:
        return None, None, None
    # guard against r = +/-1 exactly (transform blows up)
    r = max(min(r, 0.999999), -0.999999)

    # --- p-value from the t-distribution (exact, via the regularized
    # incomplete beta function - this is what a stats library uses under the
    # hood, implemented here to avoid a heavy dependency) ---
    df = n - 2
    t = r * sqrt(df / (1 - r**2))

    def _betacf(a, b, x):
        # Lentz's continued-fraction for the incomplete beta. Standard numerical
        # recipe; converges fast for our range.
        MAXIT, EPS, FPMIN = 200, 3e-16, 1e-300
        qab, qap, qam = a + b, a + 1, a - 1
        c = 1.0
        d = 1 - qab * x / qap
        if abs(d) < FPMIN:
            d = FPMIN
        d = 1 / d
        h = d
        for m in range(1, MAXIT + 1):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1 + aa * d
            if abs(d) < FPMIN:
                d = FPMIN
            c = 1 + aa / c
            if abs(c) < FPMIN:
                c = FPMIN
            d = 1 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1 + aa * d
            if abs(d) < FPMIN:
                d = FPMIN
            c = 1 + aa / c
            if abs(c) < FPMIN:
                c = FPMIN
            d = 1 / d
            delta = d * c
            h *= delta
            if abs(delta - 1) < EPS:
                break
        return h

    def _betai(a, b, x):
        from math import lgamma, exp, log
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        bt = exp(lgamma(a + b) - lgamma(a) - lgamma(b)
                 + a * log(x) + b * log(1 - x))
        if x < (a + 1) / (a + b + 2):
            return bt * _betacf(a, b, x) / a
        return 1 - bt * _betacf(b, a, 1 - x) / b

    # two-tailed p-value for Student's t
    p = _betai(df / 2, 0.5, df / (df + t * t))

    # --- 95% CI via Fisher z ---
    if n > 3:
        z = atanh(r)
        se = 1 / sqrt(n - 3)
        z_lo, z_hi = z - 1.96 * se, z + 1.96 * se
        ci_low, ci_high = tanh(z_lo), tanh(z_hi)
    else:
        ci_low = ci_high = None

    return p, ci_low, ci_high


def analyze(conn, name_a, source_a, metric_a, name_b, source_b, metric_b):
    """Run one lead-lag comparison and print an honest, readable summary."""
    a = load_series(conn, source_a, metric_a)
    b = load_series(conn, source_b, metric_b)
    table = lead_lag(a, b)

    print(f"\n{'='*64}")
    print(f"{name_a}  vs  {name_b}")
    print(f"{'='*64}")

    if table is None or table.empty:
        print("Not enough overlapping data yet to analyze this pair.")
        return

    # Find the lag with the strongest relationship (largest |correlation|).
    best = table.loc[table["correlation"].abs().idxmax()]
    lag = int(best["lag_months"])
    r = best["correlation"]
    strength = describe_strength(r)
    direction = "same-direction" if r > 0 else "opposite-direction"

    # Translate the lag into a sentence.
    if lag > 0:
        lead = f"{name_a} changes lead {name_b} changes by {lag} month(s)"
    elif lag < 0:
        lead = f"{name_b} changes lead {name_a} changes by {abs(lag)} month(s)"
    else:
        lead = f"{name_a} and {name_b} changes move in the same month"

    print(f"Best fit: {lead}")
    print(f"Correlation at that lag: {r:+.2f}  ({strength}, {direction})")
    print(f"Months compared: {int(best['n'])}")

    # Significance: p-value + 95% CI on the peak correlation.
    p, ci_low, ci_high = significance(r, int(best["n"]))
    if p is not None:
        print(f"Statistical test: p = {p:.3f}", end="")
        if ci_low is not None:
            print(f",  95% CI [{ci_low:+.2f}, {ci_high:+.2f}]")
        else:
            print()

    # Show the lag=0 (contemporaneous) value for contrast.
    zero = table.loc[table["lag_months"] == 0, "correlation"]
    if not zero.empty:
        print(f"For reference, same-month correlation: {zero.iloc[0]:+.2f}")

    # VERDICT - combine significance and CI into one honest sentence. This is
    # the payoff of the stats: it distinguishes "real but modest" from "could
    # easily be zero".
    print("\nRead with care:")
    ci_straddles_zero = (ci_low is not None and ci_low < 0 < ci_high)
    if p is not None and p >= 0.05:
        print(f"  NOT statistically significant (p = {p:.2f}). A correlation this")
        print("  size could easily arise by chance in a sample this small.")
        if ci_straddles_zero:
            print("  The 95% CI includes zero, so we can't even be confident of the")
            print("  direction. Treat this as 'no detectable relationship'.")
    elif p is not None:
        print(f"  Statistically significant (p = {p:.2f}) and the 95% CI excludes")
        print("  zero, so the relationship is unlikely to be pure chance. But")
        print("  significance is not causation - co-movement can be driven by")
        print("  common factors (the broader economy) rather than one causing the other.")
    else:
        print("  Sample too small to test reliably.")


def main():
    conn = sqlite3.connect(DB_PATH)

    # The headline question you set out to answer, plus two natural follow-ups.
    analyze(conn,
            "Consumer sentiment", "SENTIMENT", "umich_ics",
            "Unemployment",       "UNEMPLOYMENT", "u3_rate_pct")

    analyze(conn,
            "Consumer sentiment", "SENTIMENT", "umich_ics",
            "Job openings",       "BLS_JOLTS", "total_openings")

    analyze(conn,
            "Consumer sentiment", "SENTIMENT", "umich_ics",
            "Inflation",          "CPI", "inflation_yoy_pct")

    conn.close()
    print(f"\n{'='*64}")
    print("Note: 'lead' here means one series' month-to-month CHANGES tend to")
    print("precede the other's. It's a correlational signal, not causation.")


if __name__ == "__main__":
    main()