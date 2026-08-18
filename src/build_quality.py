"""Build the trip-quality workflow: Taxi.TripQuality and Taxi.SuspectTrip.

Run:  python src/build_quality.py      (after src/load_trips.py)

This is the other half of the split that src/schema.py sets up. The loader asks
"can this row be typed?" and the answer for the supplied file is yes, 787,060
times out of 787,060. This file asks the different question — "is this row
*believable*?" — and it asks it in SQL, against the loaded table, so a threshold
can change in seconds without re-reading 100 MB of CSV.

Two views, from one rule list:

    Taxi.TripQuality   every trip, one 0/1 column per rule, plus flag counts.
                       Use it to measure: how many rows does rule X hit, how do
                       the rules overlap, what share of the year is affected.
    Taxi.SuspectTrip   only the flagged trips, enriched with pickup/drop-off
                       borough and zone names, with a readable `flags` string.
                       Use it to investigate: this is the analyst-facing view.

Why views and not a flag column on Taxi.Trip: the rules are opinions, and
opinions get argued with. A view means "unusually high fare" can be re-defined
after someone objects to $500 without touching a single stored row. It also
means every consumer — notebook, Portal, dashboard — reads one definition
instead of each re-implementing the WHERE clause slightly differently.

Severity: `suspect` rules describe a trip that cannot be true as recorded (a
paid trip covering zero miles, a drop-off before its pickup). `info` rules
describe something worth knowing about the *file* that would be wrong to call a
bad row — the 7% metadata gap and the fare-component mismatch below are both
systematic, and drowning 13 real rules in 115,015 arithmetic notices would make
the view useless. Only `suspect` rules put a row into Taxi.SuspectTrip.

Every count in the comments below was measured on the loaded table, and each one
that also exists in src/profile_trips.py agrees with the pandas number exactly —
which is the cheapest available check that the load did not distort anything.
"""

from dataclasses import dataclass

from iris_conn import execute, query

SUSPECT = "suspect"
INFO = "info"

# What total_amount ought to equal. congestion_surcharge is COALESCE'd because
# it is NULL on the 55,613-row metadata block; without that the whole expression
# goes NULL and the rule silently stops firing on exactly those rows.
COMPONENT_SUM = (
    "(fare_amount + extra + mta_tax + tip_amount + tolls_amount"
    " + improvement_surcharge + COALESCE(congestion_surcharge, 0))"
)


@dataclass(frozen=True)
class Rule:
    code: str        # becomes the view's column name, so keep it SQL-safe
    sql: str         # predicate over Taxi.Trip, true = flagged
    severity: str    # SUSPECT -> lands in Taxi.SuspectTrip; INFO -> reported only
    note: str        # what it means and why this threshold, not a rounder one


RULES = (
    # ---- distance -------------------------------------------------------
    Rule("zero_distance", "trip_distance = 0", SUSPECT,
         "39,494 rows (5.02%). The single largest quality issue in the file. "
         "No row has a negative distance, so this is the whole of the "
         "'went nowhere' population."),
    Rule("impossible_distance", "trip_distance > 100", SUSPECT,
         "573 rows (0.07%). Not a conservative outlier cut: the 99.99th "
         "percentile is 34,896 miles because 554 distances were written with a "
         "thousands separator ('1,069.67'). They parse — schema.py strips the "
         "comma on purpose — but no rescaling of them tracks fare (r=0.02, "
         "against r=0.63 for normal rows), so they are unrecoverable, not "
         "merely mis-scaled. 100 miles is past any real green-cab trip."),

    # ---- duration -------------------------------------------------------
    Rule("dropoff_before_pickup", "duration_min <= 0", SUSPECT,
         "1,033 rows (0.13%). Visible only because schema.py stores "
         "duration_min signed instead of clamping it at zero."),
    Rule("duration_over_12h", "duration_min > 720", SUSPECT,
         "2,487 rows (0.32%). Threshold picked from the shape of the tail, not "
         "a round number: 1,851 of them sit between 23 and 24 hours, and "
         "nothing exceeds 24 hours at all. That is a meter left running to the "
         "day boundary, not a long trip."),
    Rule("duration_under_1min", "duration_min > 0 AND duration_min < 1", SUSPECT,
         "20,563 rows (2.61%). Mostly the same trips as zero_distance — a fare "
         "opened and closed on the spot."),

    # ---- money ----------------------------------------------------------
    Rule("negative_fare", "fare_amount < 0", SUSPECT,
         "2,217 rows (0.28%), down to -$500. Refunds and corrections booked as "
         "trips. Loaded rather than dropped so the accounting still balances."),
    Rule("extreme_fare", "fare_amount > 500", SUSPECT,
         "30 rows (0.004%). The 99.99th percentile fare is $444.99, so $500 "
         "sits just past the believable tail rather than inside it."),
    Rule("negative_total", "total_amount < 0", SUSPECT,
         "2,239 rows (0.28%). Tracks negative_fare closely but not exactly, "
         "which is why both exist."),

    # ---- money vs. distance: the two directions are different problems ---
    Rule("charged_without_moving", "fare_amount > 0 AND trip_distance = 0", SUSPECT,
         "38,087 rows (4.84%). The actionable subset of zero_distance: money "
         "changed hands for a trip that covered no ground."),
    Rule("moved_without_charge", "trip_distance > 0 AND fare_amount = 0", SUSPECT,
         "415 rows (0.05%). The mirror image, and two orders of magnitude "
         "rarer — worth separating for exactly that reason."),
    Rule("implausible_speed",
         "duration_min > 0 AND trip_distance / (duration_min / 60.0) > 80", SUSPECT,
         "2,697 rows (0.34%). 80 mph average over a whole trip is impossible "
         "inside NYC. Catches rows where distance and duration are each "
         "plausible alone and only their ratio gives the error away."),

    # ---- passengers -----------------------------------------------------
    Rule("zero_passengers", "passenger_count = 0", SUSPECT,
         "6,143 rows (0.78%). A trip with a fare and no passengers. NULL is a "
         "separate matter, handled by missing_trip_metadata."),
    Rule("excess_passengers", "passenger_count > 6", SUSPECT,
         "84 rows (0.01%), maximum 9. A green cab is licensed for at most 6."),

    # ---- informational: properties of the file, not of the row ----------
    Rule("pickup_outside_2023",
         "pickup_ts < '2023-01-01 00:00:00' OR pickup_ts >= '2024-01-01 00:00:00'",
         INFO,
         "7 rows, in 2008, 2009 and 2022. The data set is billed as calendar "
         "2023. A 2008 timestamp is a perfectly valid timestamp, so this is a "
         "note about the file's scope rather than a defect in the row."),
    Rule("missing_trip_metadata", "ratecode_id IS NULL", INFO,
         "55,613 rows (7.07%). ratecode_id, store_and_fwd_flag, "
         "passenger_count, payment_type, trip_type and congestion_surcharge go "
         "empty together on exactly these rows — one upstream feed that does "
         "not populate them, not 55,613 independent omissions. The fare and "
         "distance fields are fully present, so the rows stay usable for most "
         "analysis; they just cannot be segmented by payment type."),
    Rule("component_sum_mismatch",
         f"ABS(total_amount - {COMPONENT_SUM}) > 0.01", INFO,
         "115,015 rows (14.61%) — and INFO precisely because 14.61% cannot all "
         "be bad rows. The differences are not noise, they are three exact "
         "signatures: -$1.00 on 73,169 rows that carry mta_tax = 1.50 instead "
         "of the standard 0.50; -$3.75 on 25,208 rows that also repeat the "
         "$2.75 congestion surcharge inside `extra`; and +$2.75 on 15,684 "
         "rows, almost all from the metadata block, where a congestion "
         "surcharge is clearly in the total but its column is NULL. So the "
         "components disagree with the total in a *documented, repeatable* "
         "way: this flags a column-semantics problem in the feed, and pointing "
         "an analyst at it is more useful than pretending the totals add up."),
)

SUSPECT_RULES = tuple(r for r in RULES if r.severity == SUSPECT)
INFO_RULES = tuple(r for r in RULES if r.severity == INFO)

# One flag can be NULL-poisoned into neither-true-nor-false (passenger_count is
# nullable), so every predicate is wrapped in a CASE that resolves to 0/1 and
# never NULL. Counting NULLs as "not flagged" is the right default: a missing
# passenger count is missing_trip_metadata's business, not zero_passengers'.
def _flag(rule):
    return f"CASE WHEN {rule.sql} THEN 1 ELSE 0 END AS {rule.code}"


def _sum_of(rules):
    return " + ".join(f"CASE WHEN {r.sql} THEN 1 ELSE 0 END" for r in rules)


def quality_view_ddl():
    """Taxi.TripQuality — every trip, one column per rule, plus roll-ups."""
    flags = ",\n       ".join(_flag(r) for r in RULES)
    return f"""
CREATE VIEW Taxi.TripQuality AS
SELECT %ID AS trip_id,
       pickup_ts,
       duration_min,
       trip_distance,
       fare_amount,
       total_amount,
       {flags},
       {_sum_of(SUSPECT_RULES)} AS suspect_flags,
       {_sum_of(INFO_RULES)} AS info_flags
FROM Taxi.Trip
"""


def suspect_view_ddl():
    """Taxi.SuspectTrip — flagged trips only, enriched, with readable reasons.

    Three deliberate choices:

    CAST(... AS TIMESTAMP)
        A view is queried with ORDER BY and date filters, and an IRIS TIMESTAMP
        comes back as its internal integer ('1154152267907846976') the moment
        the query plan has to collate it. Casting inside the view definition
        means every consumer gets a real datetime without knowing this.

    LEFT JOIN, twice
        Every LocationID in the file is in 1..265 so an inner join would lose
        nothing today, but a quality view that drops rows when a lookup misses
        is the last thing you want. Ids 264/265 are the lookup's own
        'Unknown'/'N/A' entries, so they resolve rather than dangle.

    %EXACT(borough), %EXACT(zone)
        VARCHAR columns collate as SQLUPPER in IRIS, so a plain
        `GROUP BY pu_borough` returns 'QUEENS' — the stored case is intact but
        every grouped result shouts. Wrapping the columns here, once, means the
        analytics workflows downstream can group on them normally and get
        'Queens'. Verified: the fix survives through the view.

    the `flags` string
        Concatenated from the same predicates, so the row explains itself. The
        0/1 columns in Taxi.TripQuality are for counting; this is for reading.
    """
    reasons = "\n       || ".join(
        f"CASE WHEN {r.sql} THEN '{r.code} ' ELSE '' END" for r in SUSPECT_RULES
    )
    return f"""
CREATE VIEW Taxi.SuspectTrip AS
SELECT t.%ID AS trip_id,
       CAST(t.pickup_ts AS TIMESTAMP)  AS pickup_ts,
       CAST(t.dropoff_ts AS TIMESTAMP) AS dropoff_ts,
       t.duration_min,
       t.trip_distance,
       CASE WHEN t.duration_min > 0
            THEN ROUND(t.trip_distance / (t.duration_min / 60.0), 1) END AS implied_mph,
       t.passenger_count,
       t.payment_type,
       t.fare_amount,
       t.tip_amount,
       t.total_amount,
       t.pu_location_id,
       %EXACT(pu.borough) AS pu_borough,
       %EXACT(pu.zone)    AS pu_zone,
       t.do_location_id,
       %EXACT(do.borough) AS do_borough,
       %EXACT(do.zone)    AS do_zone,
       {_sum_of(SUSPECT_RULES)} AS flag_count,
       {reasons} AS flags
FROM Taxi.Trip t
     LEFT JOIN Taxi.Zone pu ON t.pu_location_id = pu.location_id
     LEFT JOIN Taxi.Zone do ON t.do_location_id = do.location_id
WHERE {" OR ".join(f"({r.sql})" for r in SUSPECT_RULES)}
"""


VIEWS = (("TripQuality", quality_view_ddl), ("SuspectTrip", suspect_view_ddl))


def view_exists(schema, view):
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, view])
    return rows[0][0] > 0


def recreate_views():
    """Drop and rebuild both views, so this script is safe to re-run.

    Rebuilding is free — a view holds no data — which is the entire argument for
    keeping the quality rules here instead of in a loaded column.
    """
    for name, ddl in VIEWS:
        if view_exists("Taxi", name):
            execute(f"DROP VIEW Taxi.{name}")
        execute(ddl())
        print(f"created Taxi.{name}")


def report():
    """Print per-rule hit counts, rule overlap, and a few example rows."""
    total = query("SELECT COUNT(*) FROM Taxi.Trip")[1][0][0]

    for label, rules in (("SUSPECT RULES", SUSPECT_RULES),
                         ("INFORMATIONAL RULES", INFO_RULES)):
        print()
        print("=" * 72)
        print(f"{label}  (counted through Taxi.TripQuality)")
        print("=" * 72)
        # One pass over the view for all of a severity's rules, rather than one
        # query per rule: same numbers, and it doubles as a demonstration that
        # the aggregation happens in IRIS.
        sums = ", ".join(f"SUM({r.code})" for r in rules)
        _, rows = query(f"SELECT {sums} FROM Taxi.TripQuality")
        for rule, n in zip(rules, rows[0]):
            print(f"  {rule.code:<24} {n:>8,}  {n / total * 100:>6.3f}%")

    print()
    print("=" * 72)
    print("HOW MANY FLAGS PER TRIP")
    print("=" * 72)
    _, rows = query(
        "SELECT suspect_flags, COUNT(*) FROM Taxi.TripQuality "
        "GROUP BY suspect_flags ORDER BY suspect_flags")
    for n_flags, n_rows in rows:
        label = "clean" if n_flags == 0 else f"{n_flags} flag(s)"
        print(f"  {label:<24} {n_rows:>8,}  {n_rows / total * 100:>6.3f}%")

    suspect = query("SELECT COUNT(*) FROM Taxi.SuspectTrip")[1][0][0]
    print()
    print(f"  Taxi.SuspectTrip holds {suspect:,} trips "
          f"({suspect / total * 100:.3f}% of {total:,}).")
    print("  Rows are flagged by more than one rule far more often than not, "
          "which is\n  the useful finding: the bad records cluster rather than "
          "spreading out.")

    print()
    print("=" * 72)
    print("WORST OFFENDERS  (most rules broken at once)")
    print("=" * 72)
    cols, rows = query(
        "SELECT TOP 5 trip_id, pu_borough, trip_distance, duration_min, "
        "fare_amount, total_amount, flag_count, flags "
        "FROM Taxi.SuspectTrip ORDER BY flag_count DESC")
    for row in rows:
        trip_id, borough, dist, dur, fare, total, n, flags = row
        print(f"  trip {trip_id:<8} {borough or '?':<10} {dist:>10} mi  "
              f"{dur:>8.1f} min  fare {fare:>8}  total {total:>8}  ({n} flags)")
        print(f"       {flags.strip()}")


def main():
    recreate_views()
    report()


if __name__ == "__main__":
    main()
