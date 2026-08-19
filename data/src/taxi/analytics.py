"""The user-facing analytical workflows.

Every function here follows the same rule: **IRIS aggregates, Python presents.**
Each query does its grouping, filtering and ordering server-side and returns tens
or hundreds of rows, which pandas then labels and formats. No function pulls a
row-per-trip result set across the wire -- bench.py exists to show what that
would cost.

The ``exclude_invalid`` argument on most functions is the point where the quality
workflow feeds the analytics -- and it is deliberately *selective* rather than
blunt. Excluding every flagged row from every figure would be easy and wrong: a
row with a missing passenger count says nothing about its own fare, so dropping
it from an average fare discards good evidence for no reason.

So each rule declares which measures it invalidates (``quality.MEASURES``) and
each query states what it depends on. Two mechanisms come out of that:

* **A row filter** on the measures that define the *population* -- that the trip
  happened, plus whatever the query groups by. These decide which rows are
  countable at all.
* **A per-column guard** (``quality.guard``) on each aggregate, so an average is
  computed only over the rows that can support that particular number.

The consequence, which is the point: one query reports a trip count over 784,808
rows while the average fare beside it is drawn from the 784,778 with a usable fare
and the average speed from the 737,852 with a usable speed. The denominators
differ by design; ``quality.measure_coverage()`` prints them.

This is not only a matter of using more of the file. Filtering bluntly biases the
answers, because the rows it drops are not a random sample -- see
``cleaning_impact``, which measures the difference.
"""

from typing import Any, Dict, Optional

import pandas as pd

from . import db, quality
from .config import IrisConfig, TRIPS

# DAYOFWEEK in IRIS is 1=Sunday through 7=Saturday.
DAY_NAMES = {
    1: "Sunday",
    2: "Monday",
    3: "Tuesday",
    4: "Wednesday",
    5: "Thursday",
    6: "Friday",
    7: "Saturday",
}

MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

PAYMENT_TYPES = {
    1: "Credit card",
    2: "Cash",
    3: "No charge",
    4: "Dispute",
    5: "Unknown",
    6: "Voided trip",
}


def _where(*conditions: str) -> str:
    """Join non-empty conditions into a WHERE clause, or nothing at all."""
    live = [c for c in conditions if c]
    return ("WHERE " + " AND ".join(live)) if live else ""


class _Scope:
    """The relevance helpers, bound to one query's ``exclude_invalid`` setting.

    Exists so the flag is threaded once per query instead of being passed to every
    aggregate, and so that turning exclusion off is a single switch that disables
    the row filter and every column guard together.
    """

    def __init__(self, exclude_invalid: bool) -> None:
        self.on = exclude_invalid

    def population(self, *measures: str) -> str:
        """The row filter: which rows this query may count at all.

        Only ever given the measures that define the population -- "trip_count"
        plus whatever the query groups by. A measure that merely appears in an
        average goes to ``avg``/``total`` instead, so that an unusable fare costs
        the row its fare rather than its existence.
        """
        return quality.relevance_condition(*measures) if self.on else ""

    def avg(self, expression: str, places: int, *measures: str) -> str:
        return f"ROUND(AVG({self._guard(expression, *measures)}), {places})"

    def total(self, expression: str, places: int, *measures: str) -> str:
        return f"ROUND(SUM({self._guard(expression, *measures)}), {places})"

    def _guard(self, expression: str, *measures: str) -> str:
        return quality.guard(expression, *measures) if self.on else expression


# --------------------------------------------------------------------------
# Workflow 1: where is the activity?
# --------------------------------------------------------------------------

def busiest_zones(
    direction: str = "pickup",
    limit: int = 15,
    exclude_invalid: bool = True,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """Ranked pickup or drop-off zones, with their fare and tip profile.

    ``direction`` is "pickup" or "dropoff". The COUNT, AVG and ORDER BY all run
    in IRIS; ``limit`` rows come back.

    The ranking is by trip count, so the population is filtered only on the two
    things a count by zone needs -- a real trip, and a zone to attribute it to.
    The averages alongside are each guarded separately, so a row with a broken
    odometer still counts towards its zone's volume and still contributes its
    fare, while contributing nothing to avg_miles.
    """
    if direction not in ("pickup", "dropoff"):
        raise ValueError("direction must be 'pickup' or 'dropoff'")
    zone_col = "PUZoneName" if direction == "pickup" else "DOZoneName"
    borough_col = "PUBorough" if direction == "pickup" else "DOBorough"
    q = _Scope(exclude_invalid)

    return db.query_df(
        f"""
        SELECT TOP {int(limit)}
               {borough_col} AS borough,
               {zone_col}    AS zone,
               COUNT(*)      AS trips,
               {q.avg('TripDistance', 2, 'distance')} AS avg_miles,
               {q.avg('TripMinutes', 1, 'duration')}  AS avg_minutes,
               {q.avg('FareAmount', 2, 'fare')}       AS avg_fare,
               {q.avg('TipPct', 1, 'tip', 'fare')}    AS avg_tip_pct
        FROM {TRIPS}
        {_where(q.population('trip_count', 'location'))}
        GROUP BY {borough_col}, {zone_col}
        ORDER BY COUNT(*) DESC
        """,
        (),
        cfg,
    )


def borough_summary(
    exclude_invalid: bool = True, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """One row per pickup borough -- the coarsest view of the enrichment."""
    q = _Scope(exclude_invalid)
    return db.query_df(
        f"""
        SELECT PUBorough AS borough,
               COUNT(*)  AS trips,
               {q.avg('TripDistance', 2, 'distance')}   AS avg_miles,
               {q.avg('FareAmount', 2, 'fare')}         AS avg_fare,
               {q.avg('TotalAmount', 2, 'total')}       AS avg_total,
               {q.avg('TipPct', 1, 'tip', 'fare')}      AS avg_tip_pct,
               {q.total('TotalAmount', 0, 'total')}     AS revenue
        FROM {TRIPS}
        {_where(q.population('trip_count', 'location'))}
        GROUP BY PUBorough
        ORDER BY COUNT(*) DESC
        """,
        (),
        cfg,
    )


# --------------------------------------------------------------------------
# Workflow 2: when is the activity?
# --------------------------------------------------------------------------

def activity_by_hour(
    exclude_invalid: bool = True, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Trips, average speed and average fare for each hour of the day.

    Average speed by hour is the most legible congestion signal in the dataset:
    it is the same journey costing more time at 5pm than at 5am.

    Note the two different populations at work. The trip count per hour needs only
    a usable pickup timestamp, so it keeps every row with one -- including the
    39,494 with no distance recorded, which are perfectly good evidence of *when*
    someone was picked up. avg_mph, which those rows cannot support, is guarded.
    """
    q = _Scope(exclude_invalid)
    return db.query_df(
        f"""
        SELECT PickupHour AS pickup_hour,
               COUNT(*)   AS trips,
               {q.avg('AvgMph', 2, 'speed')}         AS avg_mph,
               {q.avg('TripMinutes', 1, 'duration')} AS avg_minutes,
               {q.avg('FareAmount', 2, 'fare')}      AS avg_fare
        FROM {TRIPS}
        {_where(q.population('trip_count', 'time'))}
        GROUP BY PickupHour
        ORDER BY PickupHour
        """,
        (),
        cfg,
    )


def activity_by_day_of_week(
    exclude_invalid: bool = True, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    q = _Scope(exclude_invalid)
    frame = db.query_df(
        f"""
        SELECT PickupDayOfWeek AS dow,
               COUNT(*)        AS trips,
               {q.avg('TripDistance', 2, 'distance')} AS avg_miles,
               {q.avg('FareAmount', 2, 'fare')}       AS avg_fare,
               {q.avg('TipPct', 1, 'tip', 'fare')}    AS avg_tip_pct
        FROM {TRIPS}
        {_where(q.population('trip_count', 'time'))}
        GROUP BY PickupDayOfWeek
        ORDER BY PickupDayOfWeek
        """,
        (),
        cfg,
    )
    frame.insert(1, "day", frame["dow"].map(DAY_NAMES))
    return frame


def activity_by_month(
    exclude_invalid: bool = True, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    q = _Scope(exclude_invalid)
    frame = db.query_df(
        f"""
        SELECT PickupMonth AS month_num,
               COUNT(*)    AS trips,
               {q.avg('TripDistance', 2, 'distance')} AS avg_miles,
               {q.avg('FareAmount', 2, 'fare')}       AS avg_fare,
               {q.total('TotalAmount', 0, 'total')}   AS revenue
        FROM {TRIPS}
        {_where('PickupMonth IS NOT NULL', q.population('trip_count', 'time'))}
        GROUP BY PickupMonth
        ORDER BY PickupMonth
        """,
        (),
        cfg,
    )
    frame.insert(1, "month_name", frame["month_num"].map(MONTH_NAMES))
    return frame


def hourly_heatmap(
    exclude_invalid: bool = True, cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Day-of-week x hour trip counts, pivoted for a heatmap.

    168 cells, aggregated in IRIS. The pivot itself is presentation, so pandas
    does it -- on 168 rows, not 787,060.

    This query reports nothing but counts, so it is the clearest case for the
    relevance model: the only rules that bear on it are the three that make a row
    uncountable or untimeable. The other eleven -- including total_mismatch and
    missing_passenger_count, 176,000 rows between them -- have no say in when
    someone hailed a cab.
    """
    q = _Scope(exclude_invalid)
    frame = db.query_df(
        f"""
        SELECT PickupDayOfWeek AS dow, PickupHour AS pickup_hour, COUNT(*) AS trips
        FROM {TRIPS}
        {_where(q.population('trip_count', 'time'))}
        GROUP BY PickupDayOfWeek, PickupHour
        ORDER BY PickupDayOfWeek, PickupHour
        """,
        (),
        cfg,
    )
    frame["day"] = frame["dow"].map(DAY_NAMES)
    pivot = frame.pivot_table(index="day", columns="pickup_hour", values="trips", fill_value=0)
    return pivot.reindex([DAY_NAMES[i] for i in range(1, 8) if DAY_NAMES[i] in pivot.index])


# --------------------------------------------------------------------------
# Workflow 3: comparing fares and tips
# --------------------------------------------------------------------------

def tipping_by_borough(
    min_trips: int = 500,
    exclude_invalid: bool = True,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """Tipping behaviour by borough, split by payment type.

    Cash tips are not recorded by the meter, so a cash row's TipAmount is almost
    always zero. Splitting by payment type keeps that reporting artefact from
    being read as "this borough does not tip"; the HAVING clause drops
    combinations too small to mean anything.

    Every column here is about money, so the population filter includes "fare" and
    "tip" directly rather than guarding them: a row that cannot support any of the
    three figures has no business setting the denominator either.
    """
    q = _Scope(exclude_invalid)
    frame = db.query_df(
        f"""
        SELECT PUBorough   AS borough,
               PaymentType AS payment_type,
               COUNT(*)    AS trips,
               ROUND(AVG(TipPct), 2)     AS avg_tip_pct,
               ROUND(AVG(TipAmount), 2)  AS avg_tip,
               ROUND(AVG(FareAmount), 2) AS avg_fare
        FROM {TRIPS}
        {_where(
            'FareAmount > 0',
            q.population('trip_count', 'location', 'fare', 'tip'),
        )}
        GROUP BY PUBorough, PaymentType
        HAVING COUNT(*) >= {int(min_trips)}
        ORDER BY PUBorough, COUNT(*) DESC
        """,
        (),
        cfg,
    )
    frame.insert(2, "payment", frame["payment_type"].map(PAYMENT_TYPES).fillna("Other"))
    return frame


def fare_comparison_across_zones(
    limit: int = 15,
    min_trips: int = 1000,
    exclude_invalid: bool = True,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """Zones ranked by fare per mile -- a cost-efficiency view, not a volume one.

    Fare per mile is computed from the summed fare and summed distance rather
    than as an average of per-trip ratios, so short trips do not dominate.

    A ratio must take both of its inputs from the same rows, or the numerator and
    denominator describe different populations and the result is meaningless. So
    fare and distance are filtered at the row level here rather than guarded
    independently -- this is the case where the coarser tool is the correct one.
    """
    q = _Scope(exclude_invalid)
    return db.query_df(
        f"""
        SELECT TOP {int(limit)}
               PUBorough AS borough,
               PUZoneName AS zone,
               COUNT(*)   AS trips,
               ROUND(SUM(FareAmount) / SUM(TripDistance), 2) AS fare_per_mile,
               ROUND(AVG(TripDistance), 2)   AS avg_miles,
               ROUND(AVG(FareAmount), 2)     AS avg_fare,
               {q.avg('AvgMph', 1, 'speed')} AS avg_mph
        FROM {TRIPS}
        {_where(
            'TripDistance > 0',
            'FareAmount > 0',
            q.population('trip_count', 'location', 'fare', 'distance'),
        )}
        GROUP BY PUBorough, PUZoneName
        HAVING COUNT(*) >= {int(min_trips)} AND SUM(TripDistance) > 0
        ORDER BY SUM(FareAmount) / SUM(TripDistance) DESC
        """,
        (),
        cfg,
    )


# --------------------------------------------------------------------------
# Workflow 4: origin/destination pairs
# --------------------------------------------------------------------------

def common_od_pairs(
    limit: int = 20,
    cross_borough_only: bool = False,
    exclude_invalid: bool = True,
    cfg: Optional[IrisConfig] = None,
) -> pd.DataFrame:
    """The most travelled origin/destination zone pairs.

    ``cross_borough_only`` drops within-borough hops, which otherwise fill the
    whole ranking, and shows where the boroughs actually connect to each other.
    """
    q = _Scope(exclude_invalid)
    return db.query_df(
        f"""
        SELECT TOP {int(limit)}
               PUBorough  AS from_borough,
               PUZoneName AS from_zone,
               DOBorough  AS to_borough,
               DOZoneName AS to_zone,
               COUNT(*)   AS trips,
               {q.avg('TripDistance', 2, 'distance')} AS avg_miles,
               {q.avg('TripMinutes', 1, 'duration')}  AS avg_minutes,
               {q.avg('TotalAmount', 2, 'total')}     AS avg_total
        FROM {TRIPS}
        {_where(
            'PUBorough <> DOBorough' if cross_borough_only else '',
            q.population('trip_count', 'location'),
        )}
        GROUP BY PUBorough, PUZoneName, DOBorough, DOZoneName
        ORDER BY COUNT(*) DESC
        """,
        (),
        cfg,
    )


# --------------------------------------------------------------------------
# The reconciliation finding, as a reusable query
# --------------------------------------------------------------------------

def total_reconciliation(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """Break the total_mismatch flag down by the size of its discrepancy.

    This is the query that turned the project's largest flag from "14.6% of rows
    look wrong" into a specific, explainable source defect. The discrepancies do
    not scatter, they land on three exact values:

    * **-1.00** (73,169 rows): mta_tax is recorded as 1.50 where the reconciling
      rows record 0.50, but total_amount was computed with 0.50. The tax field is
      overstated by exactly a dollar.
    * **-3.75** (25,208 rows): the same dollar, plus a congestion_surcharge of
      2.75 that is recorded but not included in the total.
    * **+2.75** (15,684 rows): congestion_surcharge is NULL while total_amount
      still includes the 2.75. These belong to the block of rows with
      systematically missing fields.

    So the flag is a *reconciliation* signal about how fees were recorded, not
    evidence that these trips did not happen.
    """
    components = (
        "COALESCE(FareAmount,0) + COALESCE(Extra,0) + COALESCE(MtaTax,0) + "
        "COALESCE(TipAmount,0) + COALESCE(TollsAmount,0) + "
        "COALESCE(ImprovementSurcharge,0) + COALESCE(CongestionSurcharge,0)"
    )
    diff = f"ROUND(TotalAmount - ({components}), 2)"
    return db.query_df(
        f"""
        SELECT {diff} AS discrepancy,
               COUNT(*) AS trips,
               ROUND(AVG(MtaTax), 3)              AS avg_mta_tax,
               ROUND(AVG(ImprovementSurcharge), 3) AS avg_improvement,
               ROUND(AVG(CongestionSurcharge), 3)  AS avg_congestion,
               SUM(CASE WHEN CongestionSurcharge IS NULL THEN 1 ELSE 0 END) AS congestion_null
        FROM {TRIPS}
        WHERE QTotalMismatch = 1
        GROUP BY {diff}
        HAVING COUNT(*) >= 500
        ORDER BY COUNT(*) DESC
        """,
        (),
        cfg,
    )


def cleaning_impact(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """The same figures computed three ways, to justify both design decisions.

    This is the project's central result, and the reason the quality workflow is
    not optional decoration. Three columns:

    ``all_rows``
        No filtering. The "average green taxi trip" is 19 miles long at an average
        speed of 81 mph -- a claim that is not merely imprecise, it is impossible.
        A few hundred rows carrying distances like 278,990 miles do that.

    ``strict_clean``
        The blunt filter: drop every row any rule flagged. Recognisably urban
        again -- 2.88 miles at 11.73 mph -- but computed on 76% of the file, and
        most of what it discarded has nothing to do with the figure at hand.

    ``selective``
        Each figure computed over the rows that can actually support it, which is
        99.7% of the file for fare and 94% for distance.

    The interesting part is that the last two columns do **not** agree, and the
    disagreement is the argument for doing this. Average fare goes from $17.35 to
    $18.37 -- a 5.9% move, and *upward past the unfiltered figure*. The blunt
    filter was not merely wasteful, it was biased: the rows it dropped carry
    systematically higher fares than the ones it kept ($19.43 for a fee mismatch,
    $24.44 for a missing passenger count, $44.09 for an unrecognised zone, against
    $17.35 for rows that pass everything). Excluding them from a fare average for
    reasons unconnected to their fares pulled that average down by a dollar.

    ``max_miles`` shows the same effect concretely. The blunt filter's longest
    trip is 96.26 miles; the selective one finds a 111.66-mile, 203-minute, $400
    trip whose only flag is ``unknown_zone`` -- a wholly plausible long-haul run
    that was excluded from a distance figure because its drop-off point is missing
    from the lookup table.
    """
    # (name, aggregate, column expression, decimal places, measures it needs)
    metrics = [
        ("avg_miles", "AVG", "TripDistance", 2, ("distance",)),
        ("avg_minutes", "AVG", "TripMinutes", 1, ("duration",)),
        ("avg_mph", "AVG", "AvgMph", 2, ("speed",)),
        ("max_miles", "MAX", "TripDistance", 2, ("distance",)),
        ("avg_fare", "AVG", "FareAmount", 2, ("fare",)),
        ("avg_tip_pct", "AVG", "TipPct", 2, ("tip", "fare")),
    ]

    def one_column(where: str, guarded: bool) -> Dict[str, Any]:
        countable = quality.relevance_condition("trip_count")
        selects = [
            f"SUM(CASE WHEN {countable} THEN 1 ELSE 0 END) AS trips"
            if guarded
            else "COUNT(*) AS trips"
        ]
        for name, agg, inner, places, measures in metrics:
            expr = quality.guard(inner, *measures) if guarded else inner
            selects.append(f"ROUND({agg}({expr}), {places}) AS {name}")
        _, rows = db.query(
            f"SELECT {', '.join(selects)} FROM {TRIPS} WHERE {where}", (), cfg
        )
        names = ["trips"] + [m[0] for m in metrics]
        return dict(zip(names, rows[0]))

    frame = pd.DataFrame(
        {
            "all_rows": one_column("1 = 1", guarded=False),
            "strict_clean": one_column(quality.strict_condition(), guarded=False),
            "selective": one_column("1 = 1", guarded=True),
        }
    )
    frame.index.name = "metric"
    frame = frame.reset_index()
    # Name the measures each row depends on, so the middle and right columns can
    # be read as "these are the rules that had any business excluding a row here".
    depends = {"trips": "trip_count", **{m[0]: ", ".join(m[4]) for m in metrics}}
    frame.insert(1, "depends_on", frame["metric"].map(depends))
    return frame


def headline_numbers(cfg: Optional[IrisConfig] = None) -> pd.Series:
    """A handful of scalars for the top of a report. One query, one row.

    Each average is guarded by its own measure rather than by "no rule fired", for
    the reason set out in cleaning_impact(): the fare of a trip with no recorded
    passenger count is still a fare. ``clean_trips`` keeps the strict definition,
    because that headline is genuinely about how much of the file is unblemished.
    """
    _, rows = db.query(
        f"""
        SELECT COUNT(*)                   AS trips,
               SUM(CASE WHEN {quality.strict_condition()} THEN 1 ELSE 0 END)
                                          AS clean_trips,
               COUNT(DISTINCT PUZoneName) AS pickup_zones,
               MIN(PickupDate)            AS first_pickup,
               MAX(PickupDate)            AS last_pickup,
               ROUND(SUM({quality.guard('TotalAmount', 'total')}), 0)
                                          AS total_revenue,
               ROUND(AVG({quality.guard('TripDistance', 'distance')}), 2)
                                          AS typical_miles,
               ROUND(AVG({quality.guard('TripMinutes', 'duration')}), 1)
                                          AS typical_minutes,
               ROUND(AVG({quality.guard('FareAmount', 'fare')}), 2)
                                          AS typical_fare
        FROM {TRIPS}
        """,
        (),
        cfg,
    )
    columns = [
        "trips", "clean_trips", "pickup_zones", "first_pickup", "last_pickup",
        "total_revenue", "typical_miles", "typical_minutes", "typical_fare",
    ]
    return pd.Series(dict(zip(columns, rows[0])))
