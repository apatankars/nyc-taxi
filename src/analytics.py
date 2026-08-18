"""The user-facing analytics workflows, all pushed down into IRIS.

Run:  python src/analytics.py            all four workflows
      python src/analytics.py zones      one of them
      (workflow names: zones, activity, pairs, money)

Every function here returns a DataFrame, so the same code serves the command
line and the notebook. None of them pulls trip rows into Python: the GROUP BY
happens in IRIS and what crosses the wire is the answer — tens of rows instead
of 787,060.

Measured, not assumed (notebook Part 6, the busiest-zones question): 0.74 s
through IRIS against 2.46 s re-parsing the CSV in pandas. Three times, not a
hundred — and the CSV route was given every advantage, parsing 6 of the 20
columns and applying 4 of the 13 quality rules. The durable difference is not
that ratio but what it is a ratio of: the CSV work is repeated for every
question asked, while the parse, the index and the rule definitions are paid for
once here.

Two rules of the house, applied by every query:

`WHERE is_suspect = 0`
    The thirteen quality rules defined in build_quality.py are applied to the
    analytics, not just reported. 57,058 trips (7.25%) are excluded — including
    the 554 comma-mangled distances that put a 278,990-mile trip in the file.
    Leave them in and Manhattan's average trip distance reads 130 miles.

card payments only, for anything about tips
    Cash tips are not recorded: of 228,487 clean cash trips, exactly 2 carry a
    non-zero tip, against 406,093 of 446,136 card trips. Averaging tips over all
    payment types therefore does not measure generosity, it measures the card
    share — and it understates the real tip rate by about a third.
"""

import sys
import warnings

import pandas as pd

from iris_conn import connect

# pandas warns that a DB-API connection is not SQLAlchemy. It is correct and it
# does not matter: read_sql works fine against the IRIS driver, and SQLAlchemy
# would be a dependency bought for nothing. Silenced so the output stays
# readable rather than dressed in a stack trace nobody should act on.
warnings.filterwarnings(
    "ignore", message="pandas only supports SQLAlchemy connectable")


# The table every workflow reads. A view by default; build_flat.py creates
# materialised row-store and columnar copies with the same columns, and
# compare_approaches.py points this at each of them in turn. One name in one
# place is the difference between "we measured three storage layouts" and seven
# near-identical queries copied three times.
TABLE = "Taxi.TripEnriched"


def use_table(name):
    """Point every workflow below at a different table with the same columns."""
    global TABLE
    TABLE = name


def read(sql, params=None):
    """Run one aggregate query and return the (small) result as a DataFrame.

    Numbers do not always arrive as numbers, in two different ways:

    * NUMERIC columns come back as `decimal.Decimal`, which pandas parks in an
      object column. matplotlib cannot plot a Decimal.
    * `ROUND()` applied to a *division* comes back as a **string**:
      `ROUND(SUM(tip_amount) / SUM(fare_amount) * 100, 1)` yields '23.8'. IRIS
      still sorts it numerically server-side, so ORDER BY is unaffected and
      nothing looks wrong — until pandas sorts or plots it and '9.5' lands above
      '23'. This is the more dangerous of the two, because it is silent.

    So instead of special-casing, every object column is offered to to_numeric
    and kept as-is if any value refuses. Zone and borough names refuse; measures
    convert. One fix here, and both the CLI and the notebook get real floats.
    """
    with connect() as conn:
        frame = pd.read_sql(sql, conn, params=params)
    for name in frame.columns:
        if frame[name].dtype == object:
            converted = pd.to_numeric(frame[name], errors="coerce")
            if converted.notna().equals(frame[name].notna()):
                frame[name] = converted
    return frame


# ---------------------------------------------------------------------------
# Workflow 1 — busiest zones
# ---------------------------------------------------------------------------

def busiest_zones(direction="pickup", top=15):
    """Rank zones by trip count, with what an average trip there looks like.

    `direction` switches between pickup and drop-off ends, because they are
    genuinely different questions: pickups tell you where to send cabs,
    drop-offs tell you where demand ends up. For green taxis the answer is
    lopsided — East Harlem and Morningside Heights dominate pickups, since green
    cabs may not take street hails in the Manhattan core below 96th Street.
    """
    side = "pu" if direction == "pickup" else "do"
    return read(f"""
        SELECT TOP ? {side}_zone AS zone,
               {side}_borough    AS borough,
               COUNT(*)                        AS trips,
               ROUND(AVG(fare_amount), 2)      AS avg_fare,
               ROUND(AVG(tip_amount), 2)       AS avg_tip,
               ROUND(AVG(trip_distance), 2)    AS avg_miles,
               ROUND(AVG(duration_min), 1)     AS avg_minutes
        FROM {TABLE}
        WHERE is_suspect = 0
        GROUP BY {side}_zone, {side}_borough
        ORDER BY trips DESC
    """, [top])


# ---------------------------------------------------------------------------
# Workflow 2 — activity over time
# ---------------------------------------------------------------------------

def hourly_activity():
    """Trips, average fare and revenue by hour of the pickup.

    The hour comes from DATEPART in the view, so IRIS returns 24 rows. Doing it
    in pandas means shipping 787k timestamps to compute the same 24 numbers.
    """
    # `hour`, `day` and `month` are all reserved words in IRIS SQL — aliasing a
    # column to any of them fails at prepare time with "IDENTIFIER expected,
    # reserved word HOUR found", which reads like a syntax error in the wrong
    # place. Hence hour_of_day / day_of_week / month_no.
    return read(f"""
        SELECT pickup_hour                       AS hour_of_day,
               COUNT(*)                          AS trips,
               ROUND(AVG(fare_amount), 2)        AS avg_fare,
               ROUND(AVG(trip_distance), 2)      AS avg_miles,
               ROUND(SUM(total_amount), 0)       AS revenue
        FROM {TABLE}
        WHERE is_suspect = 0
        GROUP BY pickup_hour
        ORDER BY pickup_hour
    """)


def weekday_activity():
    """Same, by day of week. Sorted by IRIS's dow number (1 = Sunday), labelled
    by DAYNAME so the axis reads correctly without anyone memorising the offset.
    """
    return read(f"""
        SELECT pickup_dow                        AS dow,
               pickup_day_name                   AS day_of_week,
               COUNT(*)                          AS trips,
               ROUND(AVG(fare_amount), 2)        AS avg_fare,
               ROUND(AVG(tip_amount), 2)         AS avg_tip
        FROM {TABLE}
        WHERE is_suspect = 0
        GROUP BY pickup_dow, pickup_day_name
        ORDER BY pickup_dow
    """)


def monthly_activity():
    """Same, by month — the view of the year that shows whether volume is
    drifting. The 7 pickups dated outside 2023 are flagged informational rather
    than suspect, so they are not excluded here; at 7 rows out of 787k they
    cannot move a monthly total, and hiding them would be the bigger distortion.
    """
    return read(f"""
        SELECT pickup_month                      AS month_no,
               pickup_month_name                 AS month_name,
               COUNT(*)                          AS trips,
               ROUND(AVG(fare_amount), 2)        AS avg_fare,
               ROUND(SUM(total_amount), 0)       AS revenue
        FROM {TABLE}
        WHERE is_suspect = 0
        GROUP BY pickup_month, pickup_month_name
        ORDER BY pickup_month
    """)


# ---------------------------------------------------------------------------
# Workflow 3 — origin/destination pairs
# ---------------------------------------------------------------------------

def od_pairs(top=15, cross_borough_only=False):
    """The most common origin -> destination zone pairs.

    `cross_borough_only` is the more interesting cut. Without it the ranking is
    dominated by short hops that start and end in the same zone, which is true
    but not informative; restricting to trips that leave the borough shows the
    corridors that actually connect the city.
    """
    where = "WHERE is_suspect = 0"
    if cross_borough_only:
        where += " AND pu_borough <> do_borough"
    return read(f"""
        SELECT TOP ? pu_zone AS origin,
               pu_borough     AS origin_borough,
               do_zone        AS destination,
               do_borough     AS dest_borough,
               COUNT(*)                        AS trips,
               ROUND(AVG(fare_amount), 2)      AS avg_fare,
               ROUND(AVG(trip_distance), 2)    AS avg_miles,
               ROUND(AVG(duration_min), 1)     AS avg_minutes
        FROM {TABLE}
        {where}
        GROUP BY pu_zone, pu_borough, do_zone, do_borough
        ORDER BY trips DESC
    """, [top])


# ---------------------------------------------------------------------------
# Workflow 4 — what a trip costs, compared across places
# ---------------------------------------------------------------------------

def borough_economics():
    """Fare, distance and price-per-mile by pickup borough.

    Price per mile is computed as SUM(fare) / SUM(miles), not AVG(fare/miles).
    The two are different numbers and only the first is the fare per mile
    actually paid across the borough: the per-row ratio is dominated by short
    trips, where the flag drop and minimum fare make every mile look expensive.
    """
    return read(f"""
        SELECT pu_borough                              AS borough,
               COUNT(*)                                AS trips,
               ROUND(AVG(trip_distance), 2)            AS avg_miles,
               ROUND(AVG(duration_min), 1)             AS avg_minutes,
               ROUND(AVG(fare_amount), 2)              AS avg_fare,
               ROUND(SUM(fare_amount) / SUM(trip_distance), 2) AS fare_per_mile,
               ROUND(SUM(total_amount), 0)             AS revenue
        FROM {TABLE}
        WHERE is_suspect = 0 AND trip_distance > 0
        GROUP BY pu_borough
        ORDER BY trips DESC
    """)


def tipping_by_zone(top=15, min_trips=1000):
    """Tip rate by pickup zone, card payments only.

    `min_trips` exists to keep the ranking honest. Without a floor the top of
    the list is zones with a few dozen trips and a freak average, which is
    noise presented as insight. 1,000 trips is roughly the point where the
    ordering stops changing between quarters.

    Tip rate is SUM(tip) / SUM(fare) for the same reason price-per-mile is a
    ratio of sums: it answers "what share of fares was tipped" rather than
    averaging percentages that each carry a different weight.
    """
    return read(f"""
        SELECT TOP ? pu_zone AS zone,
               pu_borough     AS borough,
               COUNT(*)                                     AS card_trips,
               ROUND(AVG(fare_amount), 2)                   AS avg_fare,
               ROUND(AVG(tip_amount), 2)                    AS avg_tip,
               ROUND(SUM(tip_amount) / SUM(fare_amount) * 100, 1) AS tip_pct
        FROM {TABLE}
        WHERE is_suspect = 0 AND payment_type = 1 AND fare_amount > 0
        GROUP BY pu_zone, pu_borough
        HAVING COUNT(*) >= ?
        ORDER BY tip_pct DESC
    """, [top, min_trips])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _show(title, frame):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(frame.to_string(index=False))


WORKFLOWS = {
    "zones": lambda: [
        ("BUSIEST PICKUP ZONES", busiest_zones("pickup")),
        ("BUSIEST DROP-OFF ZONES", busiest_zones("dropoff")),
    ],
    "activity": lambda: [
        ("ACTIVITY BY HOUR OF DAY", hourly_activity()),
        ("ACTIVITY BY DAY OF WEEK", weekday_activity()),
        ("ACTIVITY BY MONTH", monthly_activity()),
    ],
    "pairs": lambda: [
        ("MOST COMMON ORIGIN -> DESTINATION PAIRS", od_pairs()),
        ("MOST COMMON PAIRS THAT CROSS A BOROUGH LINE",
         od_pairs(cross_borough_only=True)),
    ],
    "money": lambda: [
        ("FARE ECONOMICS BY PICKUP BOROUGH", borough_economics()),
        ("BEST-TIPPING ZONES (card payments, >= 1,000 trips)", tipping_by_zone()),
    ],
}


def main(argv):
    names = argv[1:] or list(WORKFLOWS)
    unknown = [n for n in names if n not in WORKFLOWS]
    if unknown:
        print(f"unknown workflow(s): {', '.join(unknown)}")
        print(f"available: {', '.join(WORKFLOWS)}")
        return 1
    for name in names:
        for title, frame in WORKFLOWS[name]():
            _show(title, frame)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
