"""The trip-quality workflow: rules defined in Python, evaluated in IRIS.

This module is the clearest example in the project of the division of labour we
are aiming for. The rules -- what counts as a questionable trip, and at what
threshold -- are ordinary Python objects, so they are easy to read, review and
extend. Nothing evaluates them row by row in Python. Instead the registry is
compiled into two ``UPDATE`` statements that IRIS runs across all 787k rows.

Adding a rule is a one-line change here: ``schema.py`` reads the registry to
generate its flag column, and ``apply_rules`` reads it to generate the SQL.

Flagging is deliberately a *separate, re-runnable* stage rather than something
folded into the load. Tuning a threshold and re-flagging takes seconds and does
not require re-reading the CSV.

Each rule also declares **which measures it invalidates**, and that is what makes
the analytics selective rather than blunt. A row with a missing passenger count
tells you nothing about its fare, so it should not be dropped from an average
fare; a row with a 278,990-mile distance must be. See ``MEASURES`` below and
``relevance_condition`` / ``guard``, which are how analytics.py consumes it.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from . import db
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS


@dataclass(frozen=True)
class Rule:
    """One quality check.

    ``predicate`` is a SQL boolean expression over a row of Taxi.Trip. It may
    contain ``{placeholder}`` names, which are filled from the thresholds dict so
    that the numeric cutoffs are stated in one place instead of buried in SQL.

    ``severity`` is advisory: "error" means the row is almost certainly not a
    real trip, "warn" means it is worth a human look.

    ``invalidates`` names the measures this rule makes untrustworthy, drawn from
    ``MEASURES``. It is the reason the analytics can be selective: a rule is only
    grounds for excluding a row from a figure that depends on something the rule
    actually casts doubt on.
    """

    name: str
    column: str
    severity: str
    description: str
    predicate: str
    invalidates: Tuple[str, ...]


# Cutoffs for the rules that need one. derive_thresholds() can replace these with
# values measured from the data actually loaded; see its docstring for why you
# might prefer that.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    # A green-taxi trip longer than 12 hours is not a trip, it is a meter left on.
    "max_trip_minutes": 720.0,
    # Under 30 seconds with distance on the clock is a meter error, not a ride.
    "min_trip_minutes": 0.5,
    # Faster than this is not achievable in NYC traffic over a whole trip.
    "max_mph": 80.0,
    # Moving a real distance at less than this average speed implies the
    # timestamps, not the ride, are wrong.
    "min_moving_mph": 0.5,
    # ~1.5x the Manhattan-to-JFK flat fare plus extras; above this is an outlier
    # worth reading rather than a fare.
    "max_fare": 500.0,
    # Longest plausible green-taxi trip; the record file contains 1000+ mile rows.
    "max_distance_miles": 150.0,
    # Component amounts should reconcile with total_amount to the cent. Allow a
    # small epsilon for rounding in the source system.
    "total_tolerance": 1.00,
    # A tip larger than this multiple of the fare is a data-entry artifact.
    "max_tip_ratio": 3.0,
}


# The vocabulary rules use to say what they cast doubt on, and analytics uses to
# say what it depends on. Deliberately a small closed set: these are the only
# facts about a trip that anything in this project reads.
MEASURES: Dict[str, str] = {
    "trip_count": "that the row describes a trip that actually took place",
    "location": "the pickup and drop-off zone and borough",
    "time": "when the trip started -- hour, weekday, month",
    "distance": "TripDistance",
    "duration": "TripMinutes",
    "speed": "AvgMph, which depends on both distance and duration",
    "fare": "FareAmount",
    "tip": "TipAmount and TipPct",
    "total": "TotalAmount, and the revenue summed from it",
    "fee_breakdown": "the individual surcharge columns, itemised",
    "passengers": "PassengerCount",
}


RULES: List[Rule] = [
    Rule(
        name="non_positive_distance",
        column="QNonPositiveDistance",
        severity="error",
        description="Trip distance is missing, zero or negative",
        predicate="TripDistance IS NULL OR TripDistance <= 0",
        # The ride still happened and its fare is still what was charged; it is
        # only the odometer reading that is unusable.
        invalidates=("distance", "speed"),
    ),
    Rule(
        name="non_positive_duration",
        column="QNonPositiveDuration",
        severity="error",
        description="Drop-off is at or before pickup",
        predicate="TripMinutes IS NULL OR TripMinutes <= 0",
        # "time" as well as "duration": if drop-off precedes pickup, or either
        # timestamp is NULL, we do not know which of the two is wrong, so the
        # pickup hour is not trustworthy either.
        invalidates=("duration", "speed", "time"),
    ),
    Rule(
        name="implausibly_short",
        column="QImplausiblyShort",
        severity="warn",
        description="Under {min_trip_minutes} minutes but distance was recorded",
        predicate=(
            "TripMinutes > 0 AND TripMinutes < {min_trip_minutes} "
            "AND TripDistance > 0"
        ),
        # The distance and the duration contradict each other and we cannot tell
        # which one lied, so neither is usable.
        invalidates=("distance", "duration", "speed"),
    ),
    Rule(
        name="excessive_duration",
        column="QExcessiveDuration",
        severity="error",
        description="Longer than {max_trip_minutes} minutes",
        predicate="TripMinutes > {max_trip_minutes}",
        invalidates=("duration", "speed"),
    ),
    Rule(
        name="excessive_distance",
        column="QExcessiveDistance",
        severity="error",
        description="Further than {max_distance_miles} miles",
        predicate="TripDistance > {max_distance_miles}",
        invalidates=("distance", "speed"),
    ),
    Rule(
        name="negative_amount",
        column="QNegativeAmount",
        severity="error",
        description="Fare or total is negative (a refund or reversal, not a trip)",
        predicate="FareAmount < 0 OR TotalAmount < 0",
        # The only rule that invalidates "trip_count", and therefore the only one
        # that excludes a row from every figure: a reversal is an accounting entry
        # against a trip already counted elsewhere in the file, not a second trip.
        invalidates=("trip_count", "fare", "tip", "total", "fee_breakdown"),
    ),
    Rule(
        name="excessive_fare",
        column="QExcessiveFare",
        severity="warn",
        description="Fare above {max_fare}",
        predicate="FareAmount > {max_fare}",
        invalidates=("fare", "total"),
    ),
    Rule(
        name="implausible_speed",
        column="QImplausibleSpeed",
        severity="error",
        description="Average speed above {max_mph} mph",
        predicate="AvgMph > {max_mph}",
        # Not "time": 80+ mph over a whole trip is far more often an inflated
        # distance than a broken clock, and the pickup hour survives either way.
        invalidates=("distance", "duration", "speed"),
    ),
    Rule(
        name="stalled_trip",
        column="QStalledTrip",
        severity="warn",
        description=(
            "Real distance covered at under {min_moving_mph} mph average"
        ),
        predicate=(
            "TripDistance > 0 AND TripMinutes > 0 AND AvgMph < {min_moving_mph}"
        ),
        invalidates=("duration", "speed"),
    ),
    Rule(
        name="total_mismatch",
        column="QTotalMismatch",
        severity="warn",
        description="Component amounts do not reconcile with the total",
        predicate=(
            "TotalAmount IS NOT NULL AND ABS(TotalAmount - ("
            "COALESCE(FareAmount,0) + COALESCE(Extra,0) + COALESCE(MtaTax,0) + "
            "COALESCE(TipAmount,0) + COALESCE(TollsAmount,0) + "
            "COALESCE(ImprovementSurcharge,0) + COALESCE(CongestionSurcharge,0)"
            ")) > {total_tolerance}"
        ),
        # Only "fee_breakdown", which is the whole point of the relevance model
        # and the single biggest thing it changes. This rule fires on 14.6% of
        # rows -- filtering bluntly on "any flag" therefore threw away one row in
        # seven of every average in the project. analytics.total_reconciliation()
        # shows why that was the wrong call: 99.2% of the mismatches land on three
        # exact values (-1.00, -3.75, +2.75), all traceable to mta_tax and
        # congestion_surcharge being written inconsistently with the total. So
        # TotalAmount -- the figure the meter reported -- is fine, FareAmount is
        # fine, and the trip is fine. What you cannot do with these rows is
        # itemise the fees, and nothing here does.
        invalidates=("fee_breakdown",),
    ),
    Rule(
        name="disproportionate_tip",
        column="QDisproportionateTip",
        severity="warn",
        description="Tip more than {max_tip_ratio}x the fare",
        predicate=(
            "FareAmount > 0 AND TipAmount > {max_tip_ratio} * FareAmount"
        ),
        # "total" too: an implausible tip is inside TotalAmount, so it distorts
        # revenue as well as the tip figures. The fare itself is untouched.
        invalidates=("tip", "total"),
    ),
    Rule(
        name="outside_2023",
        column="QOutside2023",
        severity="error",
        description="Pickup timestamp is not in 2023, the year this file covers",
        # Plain string literals rather than ODBC {ts '...'} escapes: the DB-API
        # driver rewrites braces in statement text, and IRIS converts these
        # literals to TIMESTAMP on comparison anyway.
        predicate=(
            "PickupDateTime IS NULL OR PickupDateTime < '2023-01-01 00:00:00' "
            "OR PickupDateTime >= '2024-01-01 00:00:00'"
        ),
        # The trip happened; we just cannot place it on a calendar. Its distance
        # and fare are still perfectly good rows of evidence.
        invalidates=("time",),
    ),
    Rule(
        name="unknown_zone",
        column="QUnknownZone",
        severity="warn",
        description="Pickup or drop-off location is not a recognised taxi zone",
        # The lookup file carries two sentinel rows of its own: LocationID 264 is
        # Borough 'Unknown' / Zone 'N/A', and 265 is Borough 'N/A' / Zone 'Outside
        # of NYC'. Both must be caught here, alongside the NULL that a location id
        # missing from the lookup entirely would produce.
        predicate=(
            "PUBorough IS NULL OR DOBorough IS NULL "
            "OR PUBorough IN ('Unknown', 'N/A') "
            "OR DOBorough IN ('Unknown', 'N/A')"
        ),
        invalidates=("location",),
    ),
    Rule(
        name="missing_passenger_count",
        column="QMissingPassengerCount",
        severity="warn",
        description="Passenger count is missing or zero",
        predicate="PassengerCount IS NULL OR PassengerCount = 0",
        # Pure metadata, and the clearest case for the relevance model: 61,756
        # rows whose fare, distance, timing and zones are entirely usable. The one
        # thing they cannot tell you is how many people were in the cab.
        invalidates=("passengers",),
    ),
]


# Fail at import rather than at query time if a rule names a measure that does
# not exist, or names none at all -- a rule that invalidates nothing would be
# silently unenforceable.
for _rule in RULES:
    if not _rule.invalidates:
        raise ValueError(f"Rule {_rule.name!r} declares no measures")
    for _measure in _rule.invalidates:
        if _measure not in MEASURES:
            raise KeyError(f"Rule {_rule.name!r} names unknown measure {_measure!r}")


# --------------------------------------------------------------------------
# Relevance: which rows a given figure is entitled to ignore
# --------------------------------------------------------------------------

def rules_affecting(*measures: str) -> List[Rule]:
    """Every rule that casts doubt on any of ``measures``, in registry order."""
    unknown = set(measures) - set(MEASURES)
    if unknown:
        raise KeyError(f"Unknown measure(s): {sorted(unknown)}. Known: {sorted(MEASURES)}")
    wanted = set(measures)
    return [r for r in RULES if wanted & set(r.invalidates)]


def invalidating_columns(*measures: str) -> List[str]:
    return [r.column for r in rules_affecting(*measures)]


def relevance_condition(*measures: str) -> str:
    """A SQL boolean: true for rows whose flags do not affect ``measures``.

    Returns "" when no rule bears on the measures asked for, which callers should
    treat as "no filter needed" rather than as an error.
    """
    columns = invalidating_columns(*measures)
    return " AND ".join(f"{c} = 0" for c in columns)


def guard(expression: str, *measures: str) -> str:
    """Wrap a column expression so invalid rows contribute NULL to an aggregate.

    This is the finer-grained alternative to filtering whole rows, and it is what
    lets one query report a trip count over the widest defensible population while
    each average beside it is computed only over the rows that can support it::

        SELECT COUNT(*), AVG(<guard('FareAmount', 'fare')>) ...

    IRIS's AVG and SUM skip NULLs, so each guarded aggregate gets its own
    denominator without a separate query. Guard several measures together when a
    ratio needs both of its inputs drawn from the same rows.
    """
    condition = relevance_condition(*measures)
    return expression if not condition else f"CASE WHEN {condition} THEN {expression} END"


def strict_condition() -> str:
    """"No rule fired at all" -- the blunt filter, kept for comparison.

    Equivalent to ``relevance_condition(*MEASURES)`` since every rule invalidates
    at least one measure, but expressed against QualityIssueCount so it can use
    that column's bitmap index. ``analytics.cleaning_impact`` reports it alongside
    the selective filter to show what the difference is worth.
    """
    return "QualityIssueCount = 0"


def measure_coverage(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """How many rows are usable for each measure, as one pass over the table.

    This is the denominator table for everything in analytics.py. It exists
    because the selective filter gives different figures different populations,
    and a reader is entitled to see how many rows each one rests on.
    """
    total = db.row_count(TRIPS, cfg)
    # Aliased with an m_ prefix rather than using the measure names directly:
    # "time", "total" and "duration" are all reserved words in IRIS SQL, and an
    # alias that collides fails at prepare time (see the HOUR/MONTH note in
    # analytics.py).
    selects = ",\n    ".join(
        f"SUM(CASE WHEN {relevance_condition(m) or '1 = 1'} THEN 1 ELSE 0 END) AS m_{m}"
        for m in MEASURES
    )
    columns, rows = db.query(
        f"SELECT\n    {selects},\n    "
        f"SUM(CASE WHEN {strict_condition()} THEN 1 ELSE 0 END) AS m_strict\n"
        f"FROM {TRIPS}",
        (),
        cfg,
    )
    usable = {str(k).lower(): v for k, v in zip(columns, rows[0])} if rows else {}

    records = [
        {
            "measure": name,
            "meaning": meaning,
            "rules": len(rules_affecting(name)),
            "usable_rows": int(usable.get(f"m_{name}", 0) or 0),
        }
        for name, meaning in MEASURES.items()
    ]
    records.append(
        {
            "measure": "(all rules)",
            "meaning": "the blunt filter: no rule fired anywhere on the row",
            "rules": len(RULES),
            "usable_rows": int(usable.get("m_strict", 0) or 0),
        }
    )
    frame = pd.DataFrame(records)
    frame["pct_of_trips"] = (frame["usable_rows"] / total * 100).round(2) if total else 0.0
    return frame


def _thresholds(overrides: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    if overrides:
        unknown = set(overrides) - set(DEFAULT_THRESHOLDS)
        if unknown:
            raise KeyError(f"Unknown threshold(s): {sorted(unknown)}")
        merged.update(overrides)
    return merged


def rendered_predicate(rule: Rule, overrides: Optional[Dict[str, float]] = None) -> str:
    """The rule's SQL with its thresholds substituted in."""
    return rule.predicate.format(**_thresholds(overrides))


def rendered_description(rule: Rule, overrides: Optional[Dict[str, float]] = None) -> str:
    return rule.description.format(**_thresholds(overrides))


def apply_rules(
    overrides: Optional[Dict[str, float]] = None, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Evaluate every rule against every row, server-side, and return the counts.

    Two statements, each a single pass over the table:

    1. set all flag columns from their predicates;
    2. set QualityIssueCount by summing the flag columns.

    Splitting them keeps the SQL readable -- a single statement would have to
    repeat every predicate a second time to compute the count.
    """
    thresholds = _thresholds(overrides)

    flag_assignments = ",\n    ".join(
        f"{r.column} = CASE WHEN {rendered_predicate(r, thresholds)} THEN 1 ELSE 0 END"
        for r in RULES
    )
    count_expression = " + ".join(r.column for r in RULES)

    print(f"Applying {len(RULES)} quality rules to {TRIPS}")
    with db.timed("flag_rows"):
        db.execute(f"UPDATE {TRIPS} SET\n    {flag_assignments}", (), cfg)
    with db.timed("count_issues"):
        db.execute(f"UPDATE {TRIPS} SET QualityIssueCount = {count_expression}", (), cfg)

    return summary(thresholds, cfg)


def summary(
    overrides: Optional[Dict[str, float]] = None, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Per-rule hit counts, as one aggregate query rather than one query per rule."""
    thresholds = _thresholds(overrides)
    total = db.row_count(TRIPS, cfg)

    selects = ",\n    ".join(f"SUM({r.column}) AS {r.column}" for r in RULES)
    columns, rows = db.query(f"SELECT\n    {selects}\nFROM {TRIPS}", (), cfg)
    counts = dict(zip(columns, rows[0])) if rows else {}

    frame = pd.DataFrame(
        [
            {
                "rule": r.name,
                "severity": r.severity,
                "flagged": int(counts.get(r.column, 0) or 0),
                "description": rendered_description(r, thresholds),
                # So a reader can see what being flagged actually costs a row:
                # missing_passenger_count excludes it from one figure, while
                # negative_amount excludes it from all of them.
                "invalidates": ", ".join(r.invalidates),
            }
            for r in RULES
        ]
    )
    frame["pct_of_trips"] = (frame["flagged"] / total * 100).round(3) if total else 0.0
    return frame.sort_values("flagged", ascending=False).reset_index(drop=True)


def overall(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """How many trips are clean, and how many carry 1, 2, 3+ issues."""
    return db.query_df(
        f"""
        SELECT QualityIssueCount AS issues,
               COUNT(*)          AS trips,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM {TRIPS}), 3) AS pct
        FROM {TRIPS}
        GROUP BY QualityIssueCount
        ORDER BY QualityIssueCount
        """,
        (),
        cfg,
    )


def sample(rule_name: str, limit: int = 10, cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """Show actual rows a rule caught, so a threshold can be judged, not guessed."""
    rule = next((r for r in RULES if r.name == rule_name), None)
    if rule is None:
        raise KeyError(f"No such rule: {rule_name}. Known: {[r.name for r in RULES]}")
    return db.query_df(
        f"""
        SELECT TOP {int(limit)}
               PickupDateTime, DropoffDateTime, TripMinutes, TripDistance, AvgMph,
               FareAmount, TipAmount, TotalAmount, PUZoneName, DOZoneName,
               QualityIssueCount
        FROM {TRIPS}
        WHERE {rule.column} = 1
        ORDER BY TripDistance DESC
        """,
        (),
        cfg,
    )


# What the row viewer shows, as select expressions.
#
# The source columns are read from the *landing* table, not the typed one. A flag
# is a comment on the value that arrived, so that is the value to show: the
# "2,032.67" with the thousands separator still in it, the blank passenger_count,
# the 12-hour timestamp. Showing the repaired number instead makes several rules
# look arbitrary -- excessive_distance on a tidy "2032.67" reads as a threshold
# quibble, where the raw text shows the string a plain CAST would have choked on.
#
# TripMinutes, AvgMph and the zone names have no counterpart in the file. IRIS
# derives them during the cast, so they come from the typed table and are the only
# computed values here.
BROWSE_SELECT: Tuple[str, ...] = (
    "r.lpep_pickup_datetime AS PickupDateTime",
    "r.lpep_dropoff_datetime AS DropoffDateTime",
    "t.PUZoneName",
    "t.DOZoneName",
    "r.passenger_count AS PassengerCount",
    "r.trip_distance AS TripDistance",
    "t.TripMinutes",
    "t.AvgMph",
    "r.fare_amount AS FareAmount",
    "r.tip_amount AS TipAmount",
    "r.total_amount AS TotalAmount",
)

# The one cell the viewer points at when a rule fires.
#
# A predicate almost always reads more than one column -- total_mismatch adds up
# seven of them -- but only one is the value a reader needs to look at, so the
# subject is named here rather than derived from the SQL. Deriving it shaded
# every column the predicate mentioned, which on a row that broke five rules was
# ten columns of twelve: a wash over most of the row says nothing about which
# number is the problem, and it accuses values that are perfectly good.
#
# The two rules that test a pair are missing from this map on purpose, because
# either side can be the one at fault and usually only one of them is: three
# quarters of unknown_zone rows have a real pickup zone and an unrecognised
# drop-off. Those are decided per row -- see :func:`_pair_subject`.
TESTED_COLUMN: Dict[str, str] = {
    "non_positive_distance": "TripDistance",
    "non_positive_duration": "TripMinutes",
    "implausibly_short": "TripMinutes",
    "excessive_duration": "TripMinutes",
    "excessive_distance": "TripDistance",
    "excessive_fare": "FareAmount",
    "implausible_speed": "AvgMph",
    "stalled_trip": "AvgMph",
    # Not the fare and the tip as well: the components reconcile *to* the total,
    # and total_reconciliation() shows the discrepancy is in the surcharges, which
    # the viewer does not show. TotalAmount is the figure that does not add up.
    "total_mismatch": "TotalAmount",
    "disproportionate_tip": "TipAmount",
    "outside_2023": "PickupDateTime",
    "missing_passenger_count": "PassengerCount",
}

# Rules whose subject depends on the row rather than on the rule.
_PAIR_RULES: Tuple[str, ...] = ("unknown_zone", "negative_amount")

# Fetched only to work out which side of a pair tripped the rule, and dropped
# again before the frame reaches the page: the tests in _pair_subject need the
# typed and enriched values, while the viewer shows the raw text they were cast
# from.
BROWSE_HIDDEN: Tuple[str, ...] = (
    "t.PUBorough AS TestPUBorough",
    "t.DOBorough AS TestDOBorough",
    "t.FareAmount AS TestFareAmount",
    "t.TotalAmount AS TestTotalAmount",
)

# The two sentinel boroughs the lookup file carries for its own 264/265 rows.
# Kept in step with the unknown_zone predicate, which tests the same two.
_UNKNOWN_BOROUGHS: Tuple[str, ...] = ("Unknown", "N/A")


def _pair_subject(rule_name: str, row: Any) -> Tuple[str, ...]:
    """Which side(s) of a two-column rule this particular row tripped.

    Both, when both are at fault -- 1,702 trips have neither end recognised, and
    2,239 of the 2,252 reversals have a negative fare *and* a negative total.
    """
    if rule_name == "unknown_zone":
        return tuple(
            shown
            for borough, shown in (
                ("TestPUBorough", "PUZoneName"),
                ("TestDOBorough", "DOZoneName"),
            )
            if pd.isna(row[borough]) or str(row[borough]) in _UNKNOWN_BOROUGHS
        )
    if rule_name == "negative_amount":
        return tuple(
            shown
            for typed, shown in (
                ("TestFareAmount", "FareAmount"),
                ("TestTotalAmount", "TotalAmount"),
            )
            if not pd.isna(row[typed]) and float(row[typed]) < 0
        )
    return ()


def tested_cells(fired: Sequence[str], row: Any) -> Dict[str, List[str]]:
    """Rule name -> the viewer columns to shade, for the rules that fired on ``row``.

    Sent with the row so the page needs no idea what any rule tests; it shades
    what it is told to, either for the one rule being browsed or for all of them.
    """
    cells: Dict[str, List[str]] = {}
    for name in fired:
        subject = TESTED_COLUMN.get(name)
        columns = [subject] if subject else list(_pair_subject(name, row))
        if columns:
            cells[name] = columns
    return cells


def browse_columns() -> Tuple[str, ...]:
    """The viewer's column names, taken from the aliases in ``BROWSE_SELECT``."""
    return _aliases(BROWSE_SELECT)


def _aliases(exprs: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(expr.rsplit(" ", 1)[-1].split(".")[-1] for expr in exprs)


# A rule with no subject would fire and point at nothing, and a subject naming a
# column the viewer does not show would point off the edge of the table. Both are
# caught at import rather than on the row that happens to trip the rule.
for _rule in RULES:
    _subject = TESTED_COLUMN.get(_rule.name)
    if _subject is None and _rule.name not in _PAIR_RULES:
        raise KeyError(f"Rule {_rule.name!r} names no tested column")
    if _subject is not None and _subject not in browse_columns():
        raise KeyError(
            f"Rule {_rule.name!r} tests {_subject!r}, which the row viewer does "
            f"not show. Known columns: {list(browse_columns())}"
        )

# Selectors that are not a single rule name.
BROWSE_ALL = "all"
BROWSE_FLAGGED = "flagged"
BROWSE_CLEAN = "clean"


def browse(
    selector: str = BROWSE_FLAGGED,
    limit: int = 25,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """Read actual rows out of Taxi.Trip, with the rules that fired named.

    ``selector`` is a rule name, or one of ``all`` / ``flagged`` / ``clean``.

    Rows come back most-flagged first, then by pickup time. Ordering by pickup
    time alone looked more neutral but was much less useful: every out-of-range
    row sorts to the front, so the first page of "any flag" was 25 rows from 2008
    all carrying nothing but ``outside_2023``. Worst-first opens instead on rows
    that broke five different rules at once, which is what someone reading rows is
    trying to see. For ``clean`` every count is 0, so this degrades to pickup
    order on its own.

    Every flag column comes back so the *names* of the rules that fired can be
    listed per row: a flag count on its own tells a reader a row is suspect but
    not why, which is exactly the question looking at rows is meant to answer.
    Alongside the names goes a ``Tested`` mapping of rule name to the cells that
    rule read on this row, which is how the page knows what to shade. It is the
    last column and is not one to display -- see ``renderBrowse`` in app.js.

    Source values are read from Taxi.TripRaw -- see :func:`_raw_join`, which is
    also where the one precondition on that join is checked.
    """
    _require_aligned_raw_ids(cfg)
    flag_cols = [r.column for r in RULES]
    hidden = list(_aliases(BROWSE_HIDDEN))
    frame = db.query_df(
        f"""
        SELECT TOP {int(limit)}
               {', '.join(BROWSE_SELECT)}, {', '.join(BROWSE_HIDDEN)},
               t.QualityIssueCount,
               {', '.join('t.' + c for c in flag_cols)}
        FROM {_raw_join()}
        WHERE {_browse_condition(selector)}
        ORDER BY t.QualityIssueCount DESC, t.PickupDateTime,
                 t.PULocationID, t.DOLocationID
        """,
        (),
        cfg,
    )
    frame.columns = (
        list(browse_columns()) + hidden + ["QualityIssueCount"] + flag_cols
    )

    by_column = {r.column: r.name for r in RULES}
    names, tested = [], []
    for _, row in frame.iterrows():
        fired = [by_column[c] for c in flag_cols if int(row[c] or 0) == 1]
        names.append(", ".join(fired) if fired else "—")
        tested.append(tested_cells(fired, row))
    frame = frame.drop(columns=flag_cols + hidden)
    frame.insert(len(BROWSE_SELECT), "Flags", names)
    frame = frame.rename(columns={"QualityIssueCount": "Issues"})
    frame["Tested"] = tested
    return frame


def browse_count(selector: str = BROWSE_FLAGGED, cfg: Optional[IrisConfig] = None) -> int:
    """How many rows the selector matches, so the viewer can say what it is a slice of."""
    return int(
        db.scalar(
            f"SELECT COUNT(*) FROM {TRIPS} WHERE {_browse_condition(selector)}", (), cfg
        )
        or 0
    )


def _raw_join() -> str:
    """The typed table joined to the landing row each of its rows was cast from.

    The join key is ``%ID``, because Taxi.Trip carries no foreign key back to
    Taxi.TripRaw. That is sound exactly while nothing was rejected: the cast is a
    single ``INSERT ... SELECT ... WHERE <timestamps parse>``, so with an empty
    Taxi.TripReject it inserts every landing row, in landing order, and IRIS
    assigns ids in insert order. One rejected row shifts every id after it.

    So the condition is not assumed -- :func:`_require_aligned_raw_ids` checks it,
    and the viewer refuses to render rather than pairing a flag with a
    neighbouring row's values. The durable fix is a RawID column written during
    the cast, which needs a pipeline re-run to populate.

    Unqualified column names in the caller's WHERE resolve to the typed table:
    the landing table's columns all keep the CSV's lowercase spelling, so nothing
    the quality conditions name exists on both sides.
    """
    return f"{TRIPS} t JOIN {RAW_TRIPS} r ON r.%ID = t.%ID"


def _require_aligned_raw_ids(cfg: Optional[IrisConfig] = None) -> None:
    rejected = int(db.scalar(f"SELECT COUNT(*) FROM {SCHEMA}.TripReject", (), cfg) or 0)
    if rejected:
        raise RuntimeError(
            f"Cannot show source values: {rejected} row(s) were rejected by the "
            f"cast, so {TRIPS}.%ID no longer lines up with {RAW_TRIPS}.%ID. Add a "
            f"RawID column to the cast and re-run the pipeline."
        )


def _browse_condition(selector: str) -> str:
    if selector == BROWSE_ALL:
        return "1 = 1"
    if selector == BROWSE_FLAGGED:
        return "QualityIssueCount > 0"
    if selector == BROWSE_CLEAN:
        return strict_condition()
    rule = next((r for r in RULES if r.name == selector), None)
    if rule is None:
        raise KeyError(
            f"No such rule: {selector}. Known: "
            f"{[BROWSE_ALL, BROWSE_FLAGGED, BROWSE_CLEAN] + [r.name for r in RULES]}"
        )
    return f"{rule.column} = 1"


def derive_thresholds(
    cfg: Optional[IrisConfig] = None, tail_fraction: float = 0.001
) -> Dict[str, float]:
    """Measure outlier cutoffs from the loaded data instead of asserting them.

    Returns candidate values for the fare, distance and duration thresholds, set
    at the ``tail_fraction`` upper tail (0.001 = the top 0.1% of rows). This is
    offered as an alternative to DEFAULT_THRESHOLDS, not a replacement: a
    percentile always flags exactly 0.1% of rows whether or not that 0.1% is
    actually wrong, whereas "no green-taxi trip is 12 hours long" is a claim
    about taxis. Compare the two before choosing.

    The ordering and cutoff both happen in IRIS; only three numbers come back.
    """
    n = db.row_count(TRIPS, cfg)
    tail = max(1, int(n * tail_fraction))

    def upper_tail(column: str) -> float:
        # MIN over the top-N descending is the (1 - tail_fraction) percentile.
        # Expressed this way because it relies only on TOP and ORDER BY.
        value = db.scalar(
            f"SELECT MIN({column}) FROM (SELECT TOP {tail} {column} "
            f"FROM {TRIPS} WHERE {column} IS NOT NULL ORDER BY {column} DESC)",
            (),
            cfg,
        )
        return float(value) if value is not None else 0.0

    return {
        "max_fare": upper_tail("FareAmount"),
        "max_distance_miles": upper_tail("TripDistance"),
        "max_trip_minutes": upper_tail("TripMinutes"),
    }
