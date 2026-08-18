"""Check the quality rules against the data's actual distribution.

A hand-picked threshold ("distance over 100 miles is suspicious") is a guess
until something measures it.  This module measures it, and the headline finding
is that the obvious statistical tool does not work here.

Why mean and standard deviation fail on this data
-------------------------------------------------
Over the 747,566 positive distances, trip_distance has mean 20.02 and standard
deviation 1,067.11, so the textbook "mean + 3 sigma" threshold lands at 3,221
miles.  The median trip is 2.02 miles.

The failure is not that the row count comes out wrong -- 3,221 miles flags 507
rows, which looks superficially reasonable.  The failure is that the *number is
absurd*: it says a 3,000-mile green-taxi trip is statistically unremarkable.
implied_mph is the same story without the ambiguity, where mean + 3 sigma gives
16,941 mph -- a threshold that declares anything below Mach 22 acceptable.  Both
are inflated by the very readings they were meant to catch: sigma is computed
from the outliers, so the outliers set their own tolerance.  Mean and standard
deviation have a breakdown point of zero, meaning a single bad value can move
them arbitrarily far.

A threshold that is numerically absurd cannot be defended to anyone, even when
its hit count happens to look plausible -- and on trip_distance it also misses 66
of the 573 corrupt rows that the hand-picked 100-mile rule catches.

So this module computes four candidate thresholds per column and reports what
each would actually flag:

    mean + 3s          the naive version, included to show it failing
    P75 + 1.5*IQR      Tukey's fence. Robust, but calibrated for roughly
                       symmetric data; trip distances are heavily right-skewed,
                       so it over-flags badly
    median + 3*MADn    MAD (median absolute deviation, scaled by 1.4826 so it
                       estimates sigma for a normal distribution) is the robust
                       analogue of the standard deviation -- the direct answer to
                       "use mean and sd", with the non-robust parts replaced
    log-space mean+3s  take log10 first, then mean + 3s. Right-skewed positive
                       data is roughly log-normal, so this is the statistically
                       appropriate form and it is the one that gives sensible
                       answers here
    P99.9              a pure definition rather than an estimate: "the top 0.1%"

Everything is computed inside IRIS.  Two notes on that:

*   This build has no PERCENTILE_CONT, PERCENTILE_DISC or MEDIAN -- all three
    raise SQLCODE -359.  Percentiles are therefore computed with ROW_NUMBER()
    window functions, which do work.  One row comes back per percentile asked
    for.
*   IRIS SQL has no LOG function either, so this module creates one as an
    embedded-Python UDF (``Taxi.Log10``) and pushes the log-space aggregation
    down to the server.  That is the whole point of the earlier finding that
    CREATE FUNCTION ... LANGUAGE PYTHON works: a missing SQL function can be
    supplied in Python without writing any ObjectScript.

Statistics are computed over *positive* values only.  Zeros and negatives have
their own rules already, and folding them into a spread estimate would corrupt
the estimate of what a normal trip looks like.

Two populations
---------------
``--clean`` re-runs everything over only the rows no rule flagged, which answers
a fair objection to the above: of course the statistics look bad, they were
computed over data containing known-corrupt values.  Do the naive estimators
become usable once the outliers are gone?

Read that comparison knowing it is circular.  The clean population is defined by
the rules being calibrated, so clean-data statistics cannot validate the
thresholds that produced the population -- a threshold always looks well-placed
when measured against data it has already filtered.  What the comparison IS good
for is quantifying how much damage the outliers did to each estimator, and
establishing what an ordinary trip looks like once the corruption is out.  The
thresholds themselves have to be justified against the full population, which is
what the default mode reports.

Usage:
    python src/calibrate.py            # all rows with a positive value
    python src/calibrate.py --clean    # only rows no rule flagged
"""

import math
import sys
from collections import namedtuple

import rules
from iris_conn import execute, query

# One entry per quantity to calibrate.
#
# ``expr`` is interpolated into SQL, so it must stay a literal written here --
# nothing on this list comes from input.  ``depends_on`` names the stored columns
# the expression reads, which is what --clean uses to build the per-column
# exclusion: the clean population for fare_per_mile is not "rows with no flags"
# but "rows whose fare and distance are both trustworthy".  ``rule``/``threshold``
# repeat the rule rather than parsing it back out of the predicate, and main()
# asserts the code still exists, so renaming a rule in rules.py breaks this
# module loudly instead of silently ending the check.
Target = namedtuple("Target", "label expr extra depends_on rule threshold")

TARGETS = [
    Target("trip_distance", "trip_distance", None,
           ("trip_distance",), "EXTREME_DISTANCE", 100),
    Target("fare_amount", "fare_amount", None,
           ("fare_amount",), "EXTREME_FARE", 500),
    Target("duration_min", "duration_min", None,
           ("duration_min",), "DURATION_OVER_5_HOURS", 300),
    Target("implied_mph", "implied_mph", None,
           ("implied_mph",), "IMPLAUSIBLE_SPEED", 80),
    Target("total_amount", "total_amount", None, ("total_amount",), None, None),
    Target("tip_amount", "tip_amount", None, ("tip_amount",), None, None),
    # Derived: the relationship between two columns, which no single-column
    # threshold can see.  A $3 fare and a 3-mile trip are both ordinary; $3 for
    # 300 miles is not.
    #
    # The denominator is wrapped in NULLIF because IRIS does not guarantee that
    # a guard in the WHERE clause is evaluated before the division in the SELECT
    # list -- writing "WHERE trip_distance >= 0.1 AND fare/trip_distance > 0"
    # raises <DIVIDE> on this build.  NULLIF makes the expression safe on its
    # own, whatever order the optimiser picks; x / NULL is NULL, not an error.
    # The >= 0.1 filter is then only about population, keeping near-zero
    # denominators from dominating the ratio's tail.
    Target(
        "fare_per_mile",
        "fare_amount / NULLIF(trip_distance, 0)",
        "trip_distance >= 0.1 AND fare_amount > 0 AND duration_min > 0",
        ("fare_amount", "trip_distance"),
        None,
        None,
    ),
    Target(
        "minutes_per_mile",
        "duration_min / NULLIF(trip_distance, 0)",
        "trip_distance >= 0.1 AND duration_min > 0",
        ("duration_min", "trip_distance"),
        None,
        None,
    ),
]

PERCENTILES = [0.01, 0.25, 0.50, 0.75, 0.95, 0.99, 0.999, 0.9999]

LOG10_UDF = """
CREATE FUNCTION Taxi.Log10(x DOUBLE) RETURNS DOUBLE LANGUAGE PYTHON
{
    import math
    if x is None or x <= 0:
        return None
    return math.log10(x)
}
"""

# Scaling constant that makes MAD a consistent estimator of sigma for normal
# data, i.e. 1 / qnorm(0.75).
MAD_TO_SIGMA = 1.4826


class RowCounter:
    """Track how many rows cross the wire, for the push-down story."""

    def __init__(self):
        self.rows = 0
        self.queries = 0

    def run(self, sql, params=None):
        _, rows = query(sql, params)
        self.rows += len(rows)
        self.queries += 1
        return rows


def ensure_log10_udf():
    """Create the Python UDF that IRIS SQL is missing, if it is not there."""
    try:
        query("SELECT Taxi.Log10(10.0)")
        return "already present"
    except Exception:
        execute(LOG10_UDF)
        return "created"


def where(expr, extra=None):
    """Restrict to positive values, plus any per-target filter.

    Zeros and negatives are excluded on purpose.  They already have their own
    rules, and 39,494 zero distances folded into a spread estimate would drag
    the centre of the distribution away from what a real trip looks like --
    which is the thing being estimated.
    """
    clause = f"{expr} > 0"
    return f"{clause} AND {extra}" if extra else clause


def basic_stats(counter, expr, extra):
    """Non-robust moments, pushed down. One row back."""
    rows = counter.run(
        f"SELECT COUNT({expr}), AVG({expr}), STDDEV({expr}), "
        f"MIN({expr}), MAX({expr}) "
        f"FROM Taxi.Trip t WHERE {where(expr, extra)}"
    )
    count, mean, sd, low, high = rows[0]
    return {
        "n": count,
        "mean": float(mean) if mean is not None else None,
        "sd": float(sd) if sd is not None else None,
        "min": float(low) if low is not None else None,
        "max": float(high) if high is not None else None,
    }


def percentiles(counter, expr, extra, n):
    """Percentiles via ROW_NUMBER, since PERCENTILE_CONT is absent."""
    ranks = {}
    for p in PERCENTILES:
        ranks[max(1, math.ceil(p * n))] = p
    rank_list = ", ".join(str(r) for r in sorted(ranks))

    rows = counter.run(
        f"SELECT rn, value FROM ("
        f"  SELECT {expr} AS value, ROW_NUMBER() OVER (ORDER BY {expr}) AS rn"
        f"  FROM Taxi.Trip t WHERE {where(expr, extra)}"
        f") x WHERE rn IN ({rank_list})"
    )
    return {ranks[rank]: float(value) for rank, value in rows if rank in ranks}


def median_absolute_deviation(counter, expr, extra, median, n):
    """Median of |x - median|, computed server-side in a second pass."""
    middle = max(1, math.ceil(0.5 * n))
    rows = counter.run(
        f"SELECT value FROM ("
        f"  SELECT ABS({expr} - {median}) AS value,"
        f"         ROW_NUMBER() OVER (ORDER BY ABS({expr} - {median})) AS rn"
        f"  FROM Taxi.Trip t WHERE {where(expr, extra)}"
        f") x WHERE rn = {middle}"
    )
    return float(rows[0][0]) if rows else None


def log_space_stats(counter, expr, extra):
    """Mean and sd of log10(x), aggregated inside IRIS via the Python UDF."""
    rows = counter.run(
        f"SELECT AVG(Taxi.Log10({expr})), STDDEV(Taxi.Log10({expr})) "
        f"FROM Taxi.Trip t WHERE {where(expr, extra)}"
    )
    mean, sd = rows[0]
    if mean is None or sd is None:
        return None, None
    return float(mean), float(sd)


def count_above(counter, expr, extra, threshold):
    """How many rows a candidate threshold would flag.

    Counted over the same population the statistics were estimated from, so the
    percentages in the report are comparable to each other.
    """
    rows = counter.run(
        f"SELECT COUNT(*) FROM Taxi.Trip t "
        f"WHERE {where(expr, extra)} AND {expr} > {threshold}"
    )
    return rows[0][0]


def analyse(counter, expr, extra):
    stats = basic_stats(counter, expr, extra)
    if not stats["n"]:
        return None

    pct = percentiles(counter, expr, extra, stats["n"])
    stats["percentiles"] = pct
    stats["mad"] = median_absolute_deviation(
        counter, expr, extra, pct[0.50], stats["n"]
    )
    stats["log_mean"], stats["log_sd"] = log_space_stats(counter, expr, extra)

    iqr = pct[0.75] - pct[0.25]
    candidates = {
        "mean + 3s": stats["mean"] + 3 * stats["sd"],
        "P75 + 1.5*IQR": pct[0.75] + 1.5 * iqr,
        "median + 3*MADn": (
            pct[0.50] + 3 * MAD_TO_SIGMA * stats["mad"] if stats["mad"] else None
        ),
        "log mean + 3s": (
            10 ** (stats["log_mean"] + 3 * stats["log_sd"])
            if stats["log_sd"] is not None
            else None
        ),
        "P99.9": pct[0.999],
    }

    stats["candidates"] = {}
    for label, threshold in candidates.items():
        if threshold is None:
            continue
        stats["candidates"][label] = {
            "threshold": threshold,
            "flagged": count_above(counter, expr, extra, threshold),
        }
    return stats


def report(counter, label, expr, extra, rule_code, rule_threshold, stats):
    total_rows = stats["n"]
    print(f"\n{'=' * 78}\n{label}"
          + (f"   [{expr}]" if expr != label else "")
          + f"\n{'=' * 78}")
    pct = stats["percentiles"]
    print(
        f"  n (positive) {stats['n']:>12,}   "
        f"min {stats['min']:>12,.2f}   max {stats['max']:>14,.2f}"
    )
    print(f"  mean         {stats['mean']:>12,.2f}   sd  {stats['sd']:>12,.2f}")
    print(
        f"  median       {pct[0.50]:>12,.2f}   MAD {stats['mad']:>12,.2f}"
        f"   (robust analogue of mean/sd)"
    )
    if stats["log_sd"] is not None:
        print(
            f"  log10 mean   {stats['log_mean']:>12,.4f}   sd  {stats['log_sd']:>12,.4f}"
        )
    # Reference value: for normally distributed data sd == 1.4826 * MAD, so this
    # ratio is ~1.48 and anything much above it means heavier tails than normal.
    # Saying "sd and MAD broadly agree" at a ratio of 3 would be wrong -- that is
    # already twice what normality predicts.
    spread_ratio = stats["sd"] / stats["mad"] if stats["mad"] else float("inf")
    excess = spread_ratio / MAD_TO_SIGMA
    if spread_ratio > 10:
        reading = "<-- sd is meaningless here, dominated by outliers"
    elif spread_ratio > 2.5:
        reading = f"<-- tails {excess:.1f}x heavier than normal; sd overstates spread"
    else:
        reading = f"~normal would be {MAD_TO_SIGMA:.2f}; sd is usable"
    print(f"  sd / MAD     {spread_ratio:>12,.1f}   {reading}")

    print("\n  percentiles:")
    print("   " + "".join(f"{f'P{p * 100:g}':>12s}" for p in PERCENTILES))
    print("   " + "".join(f"{pct[p]:>12,.2f}" for p in PERCENTILES))

    print("\n  candidate upper thresholds:")
    print(f"   {'method':18s} {'threshold':>14s} {'would flag':>12s} {'% of pop.':>11s}")
    for method, result in stats["candidates"].items():
        print(
            f"   {method:18s} {result['threshold']:>14,.2f} "
            f"{result['flagged']:>12,} {100 * result['flagged'] / total_rows:>10.3f}%"
        )

    if rule_code:
        current = count_above(counter, expr, extra, rule_threshold)
        print(f"\n   {'IN USE: ' + rule_code:18s} {rule_threshold:>14,.2f} "
              f"{current:>12,} {100 * current / total_rows:>10.3f}%")
        if current == 0:
            print("     (0 by construction -- this population already excludes the "
                  "rows this rule\n      flags, so the figure says nothing about "
                  "whether the threshold is well placed)")
        print(f"\n  verdict: {assess(rule_threshold, stats)}")
    else:
        print("\n  no rule currently thresholds this.")


def assess(threshold, stats):
    """Compare a rule's threshold against the robust candidates."""
    log_threshold = stats["candidates"].get("log mean + 3s", {}).get("threshold")
    p999 = stats["candidates"]["P99.9"]["threshold"]
    naive = stats["candidates"]["mean + 3s"]["threshold"]

    parts = []
    if naive > stats["max"]:
        parts.append(
            f"mean+3s ({naive:,.0f}) exceeds the maximum value in the column "
            f"({stats['max']:,.0f}), so it would flag nothing at all"
        )
    if log_threshold:
        ratio = threshold / log_threshold
        if ratio > 2:
            parts.append(
                f"the threshold is {ratio:.1f}x the log-space estimate "
                f"({log_threshold:,.1f}), so it is conservative -- it flags only "
                "the clearest cases"
            )
        elif ratio < 0.5:
            parts.append(
                f"the threshold is well below the log-space estimate "
                f"({log_threshold:,.1f}), so it is aggressive and will flag "
                "ordinary trips"
            )
        else:
            parts.append(
                f"the threshold sits within a factor of two of the log-space "
                f"estimate ({log_threshold:,.1f}), which is good agreement"
            )
    parts.append(f"P99.9 is {p999:,.2f}")
    return "; ".join(parts) + "."


def main():
    print(__doc__.split("Usage:")[0].strip())
    print(f"\n{'-' * 78}")

    unknown = [t.rule for t in TARGETS if t.rule and t.rule not in rules.BY_CODE]
    if unknown:
        raise SystemExit(f"TARGETS names rules that no longer exist: {unknown}")

    clean = "--clean" in sys.argv
    status = ensure_log10_udf()
    print(f"Taxi.Log10 embedded-Python UDF: {status}")

    total_rows = query("SELECT COUNT(*) FROM Taxi.Trip")[1][0][0]
    print(f"Taxi.Trip: {total_rows:,} rows")
    if clean:
        print(
            "\nPOPULATION: --clean. Per target, INVALID rows are dropped and so are\n"
            "rows carrying a flag that invalidates any column the target reads. The\n"
            "exclusion is built from each rule's own `invalidates` metadata via\n"
            "rules.exclusion_clause, so this is the same filter the analytics queries\n"
            "use -- not a separate definition of 'clean' that could drift from it.\n"
            "Note that the population therefore differs per target: fare_per_mile\n"
            "requires fare AND distance to be trustworthy, tip_amount only the tip."
        )
    else:
        print("\nPOPULATION: all rows with a positive value. Use --clean to exclude "
              "flagged rows.")

    counter = RowCounter()
    results = {}
    for target in TARGETS:
        extra = target.extra
        if clean:
            # quality_status is the denormalised copy of "no INVALID flag"; the
            # anti-join handles the per-column SUSPECT flags.
            population = (
                "t.quality_status <> 'INVALID' AND "
                + rules.exclusion_clause("t", *target.depends_on)
            )
            extra = f"{extra} AND {population}" if extra else population

        stats = analyse(counter, target.expr, extra)
        if stats is None:
            print(f"\n{target.label}: no rows in population, skipped")
            continue
        results[target.label] = stats
        report(counter, target.label, target.expr, extra,
               target.rule, target.threshold, stats)

    print(f"\n{'=' * 78}\npush-down cost\n{'=' * 78}")
    print(
        f"  {counter.queries} queries, {counter.rows} rows returned in total.\n"
        f"  Computing the same statistics client-side would have moved "
        f"{total_rows * len(results):,} values across the wire.\n"
        f"  Ratio: {total_rows * len(results) / max(counter.rows, 1):,.0f}:1."
    )

    print(f"\n{'=' * 78}\nwhat this says about the rules\n{'=' * 78}")
    print(
        "  1. mean + 3sd produces absurd thresholds on every skewed column: 3,221\n"
        "     miles for a taxi trip, 16,941 mph for its speed. The hit counts can\n"
        "     look plausible (507 and 442 rows) while the thresholds are\n"
        "     indefensible, so judging these by row count alone hides the problem.\n"
        "     sigma is computed from the outliers, so the outliers set their own\n"
        "     tolerance.\n"
        "  2. sd/MAD is the one-number diagnostic for whether sd can be trusted at\n"
        "     all. It is 3.0 on tip_amount, where sd is fine, and 2,242 on\n"
        "     implied_mph, where it is meaningless.\n"
        "  3. Tukey's fence (P75 + 1.5*IQR) is robust but assumes rough symmetry, so\n"
        "     on this right-skewed data it flags 4-11% of rows -- too many for a\n"
        "     review queue, and mostly ordinary trips.\n"
        "  4. log-space mean + 3sd is the appropriate parametric form here, and it\n"
        "     independently corroborates two of the four hand-picked thresholds:\n"
        "     243 min vs. the 300 in DURATION_OVER_5_HOURS, and 68 mph vs. the 80 in\n"
        "     IMPLAUSIBLE_SPEED. It does NOT corroborate EXTREME_FARE, where the\n"
        "     estimate is $98 against a threshold of $500.\n"
        "  5. EXTREME_FARE is therefore the one rule this analysis leaves open, and\n"
        "     it is left deliberately unchanged. $500 encodes domain impossibility,\n"
        "     not rarity, and the 749 rows between $190 (P99.9) and $500 include\n"
        "     genuinely long trips rather than obvious corruption. Retuning it to a\n"
        "     percentile would flag real fares as defects, which is the failure mode\n"
        "     this whole design exists to avoid. Recorded as a known gap instead.\n"
        "  6. Not every rule needs statistics. EXTREME_DISTANCE works because the\n"
        "     data is bimodal, not because 100 is a good percentile: 746,901 trips\n"
        "     under 50 miles, 92 between 50 and 100, then 573 above -- of which 554\n"
        "     carry a thousands separator in the source text. The threshold sits in\n"
        "     an empty gap between two populations, which is stronger evidence than\n"
        "     any percentile.\n"
        "  7. Thresholds stay as literal constants in rules.py rather than being\n"
        "     recomputed at load time. A rule whose threshold moves with the data is\n"
        "     not reproducible: the same row would be flagged or not depending on\n"
        "     what else was loaded. Re-run this module to check for drift instead."
    )


if __name__ == "__main__":
    main()
