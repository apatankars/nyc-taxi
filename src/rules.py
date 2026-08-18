"""The trip-quality rule registry.

This module is the single definition of what "questionable data" means in this
project.  Everything downstream is generated from the ``RULES`` list below: the
flagging pass in load_trips.py, the Taxi.SuspectTrip view, the hit-count report,
and the per-question exclusion lists used by the analytics workflows.  Adding a
rule means adding one ``Rule(...)`` here and nothing else.

Severity, and why it is three tiers instead of a boolean
-------------------------------------------------------
Profiling the raw file found that 23.8% of rows (187,226) trip at least one
reasonable quality rule, while only 0.13% (1,040) have a problem that actually
prevents analysis.  Excluding everything flagged would therefore discard about
187,000 rows to protect against about 1,000.  So flags do not exclude rows by
themselves -- severity decides:

INVALID   A field that *every* workflow depends on is unusable: the pickup or
          dropoff time, or the pickup location.  These rows stay in the table
          but are excluded from Taxi.TripAnalytics.
SUSPECT   The value is present and parseable but implausible.  Included in
          analytics, visible in Taxi.SuspectTrip, and excluded only by the
          specific workflows whose columns it affects.
NOTE      A fact about how the record was produced, not a defect.  Never
          excludes anything.  Two of these matter a lot here -- see SPARSE_FEED
          and PU_ZONE_UNKNOWN below -- because without the tier they would
          inflate the apparent defect rate by 7%.

The ``invalidates`` field is what makes per-question exclusion work.  A query
that needs trip_distance and fare_amount to be sound calls
``codes_invalidating("trip_distance", "fare_amount")`` and gets exactly the flag
codes it should exclude, rather than a hand-maintained list that someone
forgets to update when a rule is added.

Why two predicates per rule
---------------------------
``py_predicate`` runs at load time, row by row, in Python.  ``sql_predicate``
expresses the same test as a SQL fragment, which generates the Taxi.SuspectTrip
view and the set-based version used for the push-down comparison.  Holding the
rule in both forms *is* the stretch-goal experiment, but it can drift, so
quality.py cross-checks every rule's Python count against its SQL count and
reports any disagreement.

The two forms line up more closely than they look.  A Python predicate guards
with ``is not None`` and SQL comparisons against NULL yield UNKNOWN rather than
true, so both skip missing values without extra effort.

Two flags cannot be expressed in SQL at all: they record what happened while
*parsing* the text, and the text is gone by the time the row is in a table.
They carry ``sql_predicate=None``, and that is a finding rather than an
oversight -- a pure server-side LOAD DATA would lose this information silently.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

# Severity tiers.  Constants rather than bare strings so a typo is an
# ImportError at startup instead of a row that never matches any filter.
INVALID = "INVALID"
SUSPECT = "SUSPECT"
NOTE = "NOTE"

# Valid taxi zone LocationIDs, per taxi_zone_lookup: 1..265 with no gaps.
MIN_LOCATION_ID = 1
MAX_LOCATION_ID = 265

# 264 = "Unknown", 265 = "Outside of NYC".  Legitimate codes, not errors.
UNKNOWN_ZONE_IDS = (264, 265)


@dataclass(frozen=True)
class Rule:
    """One quality rule.

    code           Short stable identifier, stored in Taxi.TripFlag.flag_code.
    severity       INVALID | SUSPECT | NOTE, as described in the module docstring.
    invalidates    Column names this rule makes untrustworthy.  Empty for rules
                   that describe the record as a whole rather than a field.
    py_predicate   row_dict -> bool.  None for parse-time-only flags, which the
                   loader raises directly.
    sql_predicate  The same test as a SQL boolean expression over Taxi.Trip.
                   None where the test cannot be recomputed from stored columns.
    enabled        False parks a rule without deleting it; see TOTAL_MISMATCH.
    expected       Row count observed when profiling the raw CSV, purely as
                   documentation -- a rule whose real count is far from this is
                   either miscalibrated or the input changed.
    note           Why the rule exists, or why it is parked.
    """

    code: str
    severity: str
    invalidates: Tuple[str, ...] = ()
    py_predicate: Optional[Callable[[dict], bool]] = None
    sql_predicate: Optional[str] = None
    enabled: bool = True
    expected: Optional[int] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Predicate builders.
#
# These exist so that each rule's Python and SQL forms sit on adjacent lines and
# can be read as the same statement.  Every one guards against None, which is
# not decoration: 55,613 rows have NULLs in five columns.
# ---------------------------------------------------------------------------


def _eq(col, value):
    return lambda r: r[col] is not None and r[col] == value


def _lt(col, limit):
    return lambda r: r[col] is not None and r[col] < limit


def _gt(col, limit):
    return lambda r: r[col] is not None and r[col] > limit


def _is_null(col):
    return lambda r: r[col] is None


def _between_exclusive(col, low, high):
    return lambda r: r[col] is not None and low < r[col] < high


def _in(col, values):
    return lambda r: r[col] is not None and r[col] in values


def _location_out_of_range(col):
    return lambda r: (
        r[col] is None or not (MIN_LOCATION_ID <= r[col] <= MAX_LOCATION_ID)
    )


_LOC_RANGE_SQL = "{col} IS NULL OR {col} NOT BETWEEN 1 AND 265"


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------

RULES = [
    # -- INVALID: a field every workflow needs is unusable --------------------
    Rule(
        code="PICKUP_TS_MISSING",
        severity=INVALID,
        invalidates=("pickup_ts", "duration_min", "implied_mph"),
        py_predicate=_is_null("pickup_ts"),
        sql_predicate="pickup_ts IS NULL",
        expected=0,
        note="Timestamp did not parse as MM/DD/YYYY hh:mm:ss AM/PM.",
    ),
    Rule(
        code="DROPOFF_TS_MISSING",
        severity=INVALID,
        invalidates=("dropoff_ts", "duration_min", "implied_mph"),
        py_predicate=_is_null("dropoff_ts"),
        sql_predicate="dropoff_ts IS NULL",
        expected=0,
    ),
    Rule(
        code="DROPOFF_BEFORE_PICKUP",
        severity=INVALID,
        invalidates=("pickup_ts", "dropoff_ts", "duration_min", "implied_mph"),
        py_predicate=lambda r: r["duration_min"] is not None and r["duration_min"] <= 0,
        sql_predicate="duration_min <= 0",
        expected=1033,
        note="Includes zero-second trips: no usable duration either way.",
    ),
    Rule(
        code="PICKUP_OUTSIDE_2023",
        severity=INVALID,
        invalidates=("pickup_ts",),
        py_predicate=lambda r: (
            r["pickup_ts"] is not None and r["pickup_ts"].year != 2023
        ),
        sql_predicate=(
            "pickup_ts IS NOT NULL AND "
            "(pickup_ts < '2023-01-01 00:00:00' OR pickup_ts >= '2024-01-01 00:00:00')"
        ),
        expected=7,
        note="Dataset is the 2023 extract; found pickups in 2008, 2009 and 2022.",
    ),
    Rule(
        code="PU_LOCATION_INVALID",
        severity=INVALID,
        invalidates=("pu_location",),
        py_predicate=_location_out_of_range("pu_location"),
        sql_predicate=_LOC_RANGE_SQL.format(col="pu_location"),
        expected=0,
        note="Pickup zone is the join key for most workflows, so this is fatal.",
    ),
    # -- SUSPECT: implausible but usable -------------------------------------
    Rule(
        code="DO_LOCATION_INVALID",
        severity=SUSPECT,
        invalidates=("do_location",),
        py_predicate=_location_out_of_range("do_location"),
        sql_predicate=_LOC_RANGE_SQL.format(col="do_location"),
        expected=0,
        note="Only SUSPECT: a bad dropoff zone does not spoil pickup analysis.",
    ),
    Rule(
        code="ZERO_DISTANCE",
        severity=SUSPECT,
        invalidates=("trip_distance", "implied_mph"),
        py_predicate=_eq("trip_distance", 0),
        sql_predicate="trip_distance = 0",
        expected=39494,
        note="5% of rows. Far too many to exclude; the single best argument for "
        "flagging rather than filtering.",
    ),
    Rule(
        code="NEGATIVE_DISTANCE",
        severity=SUSPECT,
        invalidates=("trip_distance", "implied_mph"),
        py_predicate=_lt("trip_distance", 0),
        sql_predicate="trip_distance < 0",
        expected=0,
    ),
    Rule(
        code="EXTREME_DISTANCE",
        severity=SUSPECT,
        invalidates=("trip_distance", "implied_mph"),
        py_predicate=_gt("trip_distance", 100),
        sql_predicate="trip_distance > 100",
        expected=573,
        note="Max in file is 278,990 miles. The overlap with "
        "SEPARATOR_IN_DISTANCE turns out to be exact, not partial: all 554 "
        "comma-formatted distances are above 1,000 miles, and all 554 rows above "
        "1,000 miles are comma-formatted. A thousands separator in a taxi "
        "distance field is a perfect corruption signal, because no real trip "
        "needs one. Calibration (src/calibrate.py) also shows the tail is not a "
        "tail at all but a second cluster: 746,901 trips under 50 miles, 92 "
        "between 50 and 100, then 573 above. The 100-mile threshold lands inside "
        "that gap, which is why it is 2.8x the log-space +3sd estimate of 35.2 "
        "and still misses nothing -- it separates two populations rather than "
        "cutting a percentile off one.",
    ),
    Rule(
        code="ZERO_FARE",
        severity=SUSPECT,
        invalidates=("fare_amount",),
        py_predicate=_eq("fare_amount", 0),
        sql_predicate="fare_amount = 0",
        expected=954,
    ),
    Rule(
        code="NEGATIVE_FARE",
        severity=SUSPECT,
        invalidates=("fare_amount", "total_amount"),
        py_predicate=_lt("fare_amount", 0),
        sql_predicate="fare_amount < 0",
        expected=2217,
        note="Plausibly refunds or corrections rather than corruption, which is "
        "exactly why these are flagged for review instead of deleted.",
    ),
    Rule(
        code="EXTREME_FARE",
        severity=SUSPECT,
        invalidates=("fare_amount", "total_amount"),
        py_predicate=_gt("fare_amount", 500),
        sql_predicate="fare_amount > 500",
        expected=30,
    ),
    Rule(
        code="NEGATIVE_TOTAL",
        severity=SUSPECT,
        invalidates=("total_amount",),
        py_predicate=_lt("total_amount", 0),
        sql_predicate="total_amount < 0",
        expected=2239,
    ),
    Rule(
        code="NEGATIVE_TIP",
        severity=SUSPECT,
        invalidates=("tip_amount",),
        py_predicate=_lt("tip_amount", 0),
        sql_predicate="tip_amount < 0",
        expected=63,
    ),
    Rule(
        code="TIP_EXCEEDS_2X_FARE",
        severity=SUSPECT,
        invalidates=("tip_amount",),
        py_predicate=lambda r: (
            r["tip_amount"] is not None
            and r["fare_amount"] is not None
            and r["fare_amount"] > 0
            and r["tip_amount"] > r["fare_amount"] * 2
        ),
        sql_predicate="fare_amount > 0 AND tip_amount > fare_amount * 2",
        expected=830,
        note="A relationship rule rather than a range rule: no single column "
        "looks wrong on its own.",
    ),
    Rule(
        code="FARE_PER_MILE_OUTLIER",
        severity=SUSPECT,
        invalidates=("fare_amount", "total_amount", "trip_distance"),
        py_predicate=lambda r: (
            r["trip_distance"] is not None
            and r["trip_distance"] >= 1
            and r["fare_amount"] is not None
            and r["fare_amount"] > 0
            and r["fare_amount"] > 20 * r["trip_distance"]
        ),
        sql_predicate=(
            "trip_distance >= 1 AND fare_amount > 0 "
            "AND fare_amount > 20 * trip_distance"
        ),
        expected=840,
        note="Derived in src/calibrate.py, and the threshold is the interesting "
        "part. Over the whole table fare-per-mile has median $6.64 and P99 $22, "
        "but that headline number is contaminated: below half a mile the median "
        "is $15/mile and P99.9 is $1,050/mile, because a $3 minimum fare over "
        "0.15 miles arithmetically IS $20/mile. Those rows are not errors, so a "
        "flat threshold on the ratio would flag 19,774 perfectly good short "
        "trips. Restricting to trips of a mile or more removes the base-fare "
        "artifact -- median drops to $6.94 and P99.9 to $25.59 -- and $20/mile "
        "then sits just under P99.9 of the population it actually applies to. "
        "Written as a multiplication rather than a division on purpose: "
        "fare_amount / trip_distance raises <DIVIDE> in IRIS because the "
        "trip_distance >= 1 guard is not guaranteed to be evaluated first.",
    ),
    Rule(
        code="DURATION_UNDER_1_MIN",
        severity=SUSPECT,
        invalidates=("duration_min", "implied_mph"),
        py_predicate=_between_exclusive("duration_min", 0, 1),
        sql_predicate="duration_min > 0 AND duration_min < 1",
        expected=20563,
        note="Lower bound is exclusive so this does not double-count the 1,033 "
        "DROPOFF_BEFORE_PICKUP rows. Non-overlapping rules keep hit counts readable.",
    ),
    Rule(
        code="DURATION_OVER_5_HOURS",
        severity=SUSPECT,
        invalidates=("duration_min", "implied_mph"),
        py_predicate=_gt("duration_min", 300),
        sql_predicate="duration_min > 300",
        expected=3041,
    ),
    Rule(
        code="DROPOFF_DATE_OFF_BY_ONE_DAY",
        severity=NOTE,
        # Nothing extra to invalidate: every row this fires on is already caught
        # by DURATION_OVER_5_HOURS, which invalidates duration_min and
        # implied_mph. This rule exists to say *why*, not to exclude more.
        invalidates=(),
        py_predicate=lambda r: (
            r["duration_min"] is not None
            and 1380 <= r["duration_min"] <= 1440
            and r["fare_amount"] is not None
            and r["fare_amount"] < 200
        ),
        sql_predicate=(
            "duration_min >= 1380 AND duration_min <= 1440 AND fare_amount < 200"
        ),
        expected=1851,
        note="A cluster found by src/calibrate.py rather than assumed. Durations "
        "between 23 and 24 hours hold 1,851 trips at roughly 46 rows per minute "
        "of duration, against roughly 1 row per minute in the 12-23 hour band -- "
        "a 40x density spike that a smooth long-trip tail cannot produce. 1,835 "
        "of the 1,851 have a dropoff exactly one calendar day after pickup at an "
        "EARLIER time of day. Trip 121 is the pattern: 6.53 miles for $31.00, "
        "stamped 01:25:46 to 00:29:15 the next day.\n"
        "\n"
        "The fare test is what makes this a date-error rule rather than a "
        "long-trip rule, and it matters because a duration of 23.5 hours is the "
        "signature of BOTH a stamping error and a genuine 23.5-hour trip -- in "
        "both cases the dropoff time-of-day falls earlier than the pickup's, so "
        "the timestamps alone cannot tell them apart. The other columns can. A "
        "real metered 23-hour trip accrues $0.50 per 60 seconds of slow or "
        "stopped time, so meter time alone would exceed $690 before any distance "
        "charge. The highest fare anywhere in this band is $161.90, and 0 rows "
        "reach even $200, so every row is contradicted by its own fare. The "
        "guard therefore excludes nothing today; it is here so that a genuine "
        "long trip in a future file is not silently mislabelled as a date bug.\n"
        "\n"
        "Two further checks argue against these being real overnight trips. "
        "Pickup hour is spread across the clock with midday most common (683 "
        "between 12:00 and 17:00, only 128 between 22:00 and 23:00); real "
        "overnight trips would cluster in the late evening. And the genuinely "
        "long 5-12 hour band looks entirely different -- mean fare $113.20 with "
        "30% of rows over $100, against mean $22.93 and 0.6% over $100 here.\n"
        "\n"
        "Kept as a NOTE because it explains rows that DURATION_OVER_5_HOURS "
        "already handles, and because these rows stay fully usable for fare and "
        "distance work -- the whole argument for flagging per column instead of "
        "dropping the row.",
    ),
    Rule(
        code="IMPLAUSIBLE_SPEED",
        severity=SUSPECT,
        invalidates=("trip_distance", "duration_min", "implied_mph"),
        py_predicate=_gt("implied_mph", 80),
        sql_predicate="implied_mph > 80",
        expected=2697,
        note="Distance and duration are each individually plausible; only their "
        "ratio is not. Profiling the CSV in pandas said 2,835, and the 138-row "
        "difference is instructive: pandas computed distance/(0/60) as inf for "
        "the 138 rows with a zero duration, and inf > 80 is True. There is no "
        "speed to compute when the duration is zero, so implied_mph is left NULL "
        "and this rule does not fire. All 138 are already INVALID via "
        "DROPOFF_BEFORE_PICKUP, so nothing goes unnoticed.",
    ),
    Rule(
        code="ZERO_PASSENGERS",
        severity=SUSPECT,
        invalidates=("passenger_count",),
        py_predicate=_eq("passenger_count", 0),
        sql_predicate="passenger_count = 0",
        expected=6143,
    ),
    Rule(
        code="EXTREME_PASSENGERS",
        severity=SUSPECT,
        invalidates=("passenger_count",),
        py_predicate=_gt("passenger_count", 6),
        sql_predicate="passenger_count > 6",
        expected=84,
        note="Green taxis seat 6; the file goes up to 9.",
    ),
    # -- NOTE: provenance, not defect ---------------------------------------
    Rule(
        code="SPARSE_FEED",
        severity=NOTE,
        invalidates=(
            "passenger_count",
            "store_and_fwd_flag",
            "ratecode_id",
            "payment_type",
            "congestion_surcharge",
        ),
        py_predicate=_is_null("passenger_count"),
        sql_predicate="passenger_count IS NULL",
        expected=55613,
        note="These five columns are NULL on exactly the same 55,613 rows -- the "
        "null masks are identical, verified. That is one upstream feed "
        "difference, not five defects across 7% of the data. Rolling it into a "
        "single NOTE is what keeps the defect rate honest.",
    ),
    Rule(
        code="TRIP_TYPE_MISSING",
        severity=NOTE,
        invalidates=("trip_type",),
        py_predicate=lambda r: (
            r["trip_type"] is None and r["passenger_count"] is not None
        ),
        sql_predicate="trip_type IS NULL AND passenger_count IS NOT NULL",
        expected=45,
        note="trip_type has 55,658 NULLs, 45 more than the SPARSE_FEED block. "
        "This isolates those 45 so they are not hidden inside it.",
    ),
    Rule(
        code="PU_ZONE_UNKNOWN",
        severity=NOTE,
        invalidates=(),
        py_predicate=_in("pu_location", UNKNOWN_ZONE_IDS),
        sql_predicate="pu_location IN (264, 265)",
        expected=2260,
        note="264 and 265 are the lookup's own codes for 'Unknown' and 'Outside "
        "of NYC'. Legitimate values that a naive range check would call errors.",
    ),
    Rule(
        code="DO_ZONE_UNKNOWN",
        severity=NOTE,
        invalidates=(),
        py_predicate=_in("do_location", UNKNOWN_ZONE_IDS),
        sql_predicate="do_location IN (264, 265)",
        expected=9040,
    ),
    # -- Parse-time only: no SQL form exists --------------------------------
    # The two separator rules below were one rule, THOUSANDS_SEPARATOR_REPAIRED,
    # a NOTE that invalidated nothing.  It was split because a single rule cannot
    # carry the right `invalidates` for both cases: 554 rows needed only
    # trip_distance repaired and 12 needed fare_amount and total_amount, and
    # `invalidates` is fixed per rule while the affected column varies per row.
    # The old single rule therefore had to invalidate everything or nothing, and
    # it chose nothing -- which left those rows excluded from distance and fare
    # analytics only because EXTREME_DISTANCE and EXTREME_FARE happened to catch
    # all of them.  That was verified true (0 rows slipped through) but it is
    # coverage by coincidence: it depends on thresholds in unrelated rules, and
    # retuning one of those would silently open a hole.  Splitting makes each
    # rule state its own consequences.
    #
    # Both are SUSPECT rather than NOTE, and that is the substantive finding
    # here.  Stripping the comma is an unambiguous *parse* -- every digit is
    # preserved, nothing is invented -- but the resulting number is not usable,
    # and the evidence is that the correspondence is exact in both directions:
    # all 554 comma-formatted distances exceed 1,000 miles, and all 554 rows
    # above 1,000 miles are comma-formatted.  No green-taxi trip needs a
    # thousands separator in its distance, so the separator is not incidental
    # formatting, it is the corruption signal itself, at 100% precision.
    Rule(
        code="SEPARATOR_IN_DISTANCE",
        severity=SUSPECT,
        invalidates=("trip_distance", "implied_mph"),
        py_predicate=None,
        sql_predicate=None,
        expected=554,
        note="trip_distance written with a thousands separator inside a quoted "
        "field ('278,990.28'). Range after stripping: 1,069.67 to 278,990.28 "
        "miles, against a median trip of 2.02. The rest of each row is normal -- "
        "mean fare $26.41 over a mean 31.7 minutes -- so only the distance is "
        "wrong, which is exactly the case for invalidating one column instead of "
        "dropping the row. The stripped value is stored rather than NULLed: it "
        "records what the source actually claimed, which is the evidence that the "
        "upstream feed has a formatting or unit bug, and trip_id is the source "
        "line number so the raw text is always recoverable. No attempt is made to "
        "infer the true distance from the fare -- that would be inventing data.",
    ),
    Rule(
        code="SEPARATOR_IN_AMOUNT",
        severity=SUSPECT,
        invalidates=("fare_amount", "total_amount"),
        py_predicate=None,
        sql_predicate=None,
        expected=12,
        note="fare_amount and total_amount written with a thousands separator. "
        "Unlike the distance case the separator is correct formatting here -- a "
        "$1,256.70 fare genuinely needs one -- so the flag means 'over $1,000', "
        "not 'malformed'. All 12 are implausible on inspection anyway: eleven ran "
        "12 to 19 hours (meter left running), and trip 651204 charges $1,024.30 "
        "for 2.03 miles in 18 minutes. Kept SUSPECT rather than INVALID because a "
        "long-haul metered fare is conceivable, so these are for review, not "
        "deletion.",
    ),
    Rule(
        code="NUMERIC_UNPARSEABLE",
        severity=SUSPECT,
        invalidates=(),
        py_predicate=None,
        sql_predicate=None,
        expected=0,
        note="A numeric field that survived neither parsing nor repair. The "
        "column is left NULL and the detail column names it, so the row is still "
        "usable for every other purpose.",
    ),
    # -- Parked -------------------------------------------------------------
    Rule(
        code="TOTAL_MISMATCH",
        severity=SUSPECT,
        invalidates=("total_amount",),
        py_predicate=lambda r: False,
        sql_predicate=None,
        enabled=False,
        expected=115015,
        note="PARKED, not deleted. total_amount minus the sum of its components "
        "disagrees by more than a cent on 14.61% of rows. A rule that fires on "
        "115,015 rows is far more likely to be miscalibrated than to have found "
        "115,015 bad rows -- ehail_fee is entirely NULL and congestion_surcharge "
        "is NULL across the SPARSE_FEED block, so the sum is being computed from "
        "incomplete components. Left here as the worked example of why every "
        "rule needs its hit rate checked before it is trusted.",
    ),
]


# ---------------------------------------------------------------------------
# Accessors.  Everything downstream goes through these rather than touching
# RULES directly, so the enabled/disabled distinction is honoured in one place.
# ---------------------------------------------------------------------------

BY_CODE = {rule.code: rule for rule in RULES}


def active():
    """Rules that participate in flagging."""
    return [r for r in RULES if r.enabled]


def evaluable():
    """Active rules with a Python predicate, i.e. those the loader can apply."""
    return [r for r in active() if r.py_predicate is not None]


def sql_checkable():
    """Active rules whose test can be recomputed from stored columns."""
    return [r for r in active() if r.sql_predicate is not None]


def flags_for(row):
    """Evaluate every applicable rule against one parsed row.

    Returns a list of (code, severity) for the rules that fired.  Parse-time
    flags are not produced here -- the loader appends those, because only it has
    seen the raw text.
    """
    return [(r.code, r.severity) for r in evaluable() if r.py_predicate(row)]


def status_for(severities):
    """Collapse a row's severities into its quality_status.

    INVALID beats SUSPECT beats nothing.  NOTE never affects status, which is
    the whole reason the tier exists.
    """
    if INVALID in severities:
        return INVALID
    if SUSPECT in severities:
        return SUSPECT
    return "OK"


def codes_invalidating(*columns):
    """Flag codes that make any of ``columns`` untrustworthy.

    This is how a workflow states its own data requirements instead of
    hardcoding a list of flag names that goes stale when a rule is added:

        codes_invalidating("trip_distance", "fare_amount")
        -> ['ZERO_DISTANCE', 'NEGATIVE_DISTANCE', 'EXTREME_DISTANCE', ...]
    """
    wanted = set(columns)
    return [r.code for r in active() if wanted & set(r.invalidates)]


def exclusion_clause(alias="t", *columns):
    """A SQL predicate excluding rows whose ``columns`` are untrustworthy.

    This is the mechanism behind "exclude per question, not per dataset".  A
    workflow states which columns it depends on and gets back a clause that
    filters exactly the flags affecting those columns -- nothing more.  A trip
    with a NULL passenger_count is therefore dropped by an occupancy analysis and
    kept by a busiest-zones analysis, from one rule registry and with no
    per-query flag lists to maintain.

        WHERE t.quality_status <> 'INVALID'
          AND <exclusion_clause("t", "trip_distance", "fare_amount")>

    Codes are interpolated as literals rather than bound as parameters because
    they come from the registry above -- they are internal constants, never user
    input -- and because a view definition cannot carry parameters.
    """
    codes = codes_invalidating(*columns)
    if not codes:
        return "1 = 1"
    in_list = ", ".join(f"'{code}'" for code in sorted(codes))
    return (
        f"NOT EXISTS (SELECT 1 FROM Taxi.TripFlag f "
        f"WHERE f.trip_id = {alias}.trip_id AND f.flag_code IN ({in_list}))"
    )


def suspect_view_sql(view="Taxi.SuspectTrip"):
    """Generate the review-queue view from the registry.

    Deliberately built from sql_predicate rather than from ``flag_count > 0``:
    that makes the view an independent SQL expression of the same rules, which
    is what quality.py cross-checks the Python pass against.
    """
    conditions = " OR ".join(
        f"({r.sql_predicate})" for r in sql_checkable() if r.severity != NOTE
    )
    return f"""
        CREATE VIEW {view} AS
        SELECT trip_id, pickup_ts, dropoff_ts, pu_location, do_location,
               trip_distance, fare_amount, tip_amount, total_amount,
               passenger_count, duration_min, implied_mph,
               quality_status, flag_count
        FROM Taxi.Trip
        WHERE {conditions}
    """


if __name__ == "__main__":
    # python src/rules.py -- print the registry as a table.
    print(f"{'code':32s} {'severity':9s} {'sql?':5s} {'expected':>9s}  invalidates")
    print("-" * 110)
    for rule in RULES:
        marker = "" if rule.enabled else "  (parked)"
        print(
            f"{rule.code:32s} {rule.severity:9s} "
            f"{'yes' if rule.sql_predicate else 'no':5s} "
            f"{rule.expected if rule.expected is not None else '?':>9}  "
            f"{','.join(rule.invalidates) or '-'}{marker}"
        )
    print(
        f"\n{len(active())} active rules "
        f"({len(evaluable())} evaluable in Python, "
        f"{len(sql_checkable())} checkable in SQL), "
        f"{len(RULES) - len(active())} parked."
    )
