"""Build Taxi.TripEnriched — the one view every analytics workflow reads.

Run:  python src/build_enriched.py     (after load_trips.py and build_quality.py)

Three things get added to the raw trip row, and each one exists because the
alternative is worse:

1. **Zone names, both ends.** `pu_location_id = 74` is not an answer to "where
   are the busiest pickups". Joining Taxi.Zone turns it into 'East Harlem
   North, Manhattan'. The brief asks for this enrichment specifically; doing it
   in a view rather than in each query means the join condition is written once.

2. **Calendar parts of the pickup timestamp** (hour, day-of-week, month, and
   their names). "Summarize activity by hour, day, or month" then becomes a
   plain GROUP BY that IRIS can answer from an index, instead of 787k
   timestamps crossing the wire to be turned into hours by pandas.

3. **`is_suspect`**, computed from build_quality.SUSPECT_RULES — the *same*
   thirteen predicates that define Taxi.SuspectTrip, imported rather than
   retyped. This is the point of the split: the quality workflow is not a report
   that gets read once and forgotten, it is a gate the analytics can apply.
   `WHERE is_suspect = 0` is how a fare average stops being dragged around by
   278,990-mile trips, and the number is reproducible because there is exactly
   one definition of the word.

Kept as a view, not a materialised table. 787k rows joined to a 265-row lookup
is a fast join in IRIS (the aggregate queries in analytics.py come back in well
under a second), and a view cannot go stale after a reload. The runbook suggests
also trying the denormalised form and timing the difference — that belongs to
the stretch goal, where "we measured both" is the deliverable.
"""

from build_quality import SUSPECT_RULES
from iris_conn import execute, query

# 1 = Sunday in IRIS's DATEPART('dw'), which is worth writing down: the numbers
# are for sorting, and DAYNAME() supplies the label so nobody has to remember
# the offset when reading a chart axis.
DDL = f"""
CREATE VIEW Taxi.TripEnriched AS
SELECT t.%ID AS trip_id,
       CAST(t.pickup_ts AS TIMESTAMP)  AS pickup_ts,
       CAST(t.dropoff_ts AS TIMESTAMP) AS dropoff_ts,
       DATEPART('hour',  t.pickup_ts)  AS pickup_hour,
       DATEPART('dw',    t.pickup_ts)  AS pickup_dow,
       DAYNAME(t.pickup_ts)            AS pickup_day_name,
       DATEPART('month', t.pickup_ts)  AS pickup_month,
       MONTHNAME(t.pickup_ts)          AS pickup_month_name,
       t.duration_min,
       t.trip_distance,
       t.passenger_count,
       t.payment_type,
       t.vendor_id,
       t.trip_type,
       t.fare_amount,
       t.tip_amount,
       t.tolls_amount,
       t.congestion_surcharge,
       t.total_amount,
       t.pu_location_id,
       %EXACT(pu.borough)      AS pu_borough,
       %EXACT(pu.zone)         AS pu_zone,
       %EXACT(pu.service_zone) AS pu_service_zone,
       t.do_location_id,
       %EXACT(do.borough)      AS do_borough,
       %EXACT(do.zone)         AS do_zone,
       %EXACT(do.service_zone) AS do_service_zone,
       CASE WHEN {" OR ".join(f"({r.sql})" for r in SUSPECT_RULES)}
            THEN 1 ELSE 0 END AS is_suspect
FROM Taxi.Trip t
     LEFT JOIN Taxi.Zone pu ON t.pu_location_id = pu.location_id
     LEFT JOIN Taxi.Zone do ON t.do_location_id = do.location_id
"""


def view_exists(schema, view):
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, view])
    return rows[0][0] > 0


def main():
    if view_exists("Taxi", "TripEnriched"):
        execute("DROP VIEW Taxi.TripEnriched")
    execute(DDL)
    print("created Taxi.TripEnriched")

    # Sanity checks, not decoration. Each one has failed at least once during
    # development, and each failure is silent: a join that drops rows still
    # returns a perfectly plausible-looking answer.
    _, rows = query(
        "SELECT COUNT(*), SUM(is_suspect), "
        "SUM(CASE WHEN pu_borough IS NULL THEN 1 ELSE 0 END) "
        "FROM Taxi.TripEnriched")
    total, suspect, unjoined = rows[0]
    print(f"  rows                 {total:>9,}   (must equal Taxi.Trip)")
    print(f"  is_suspect = 1       {suspect:>9,}   "
          f"(must equal Taxi.SuspectTrip)")
    print(f"  pickup zone missing  {unjoined:>9,}   (LEFT JOIN found no zone)")

    _, rows = query("SELECT COUNT(*) FROM Taxi.SuspectTrip")
    if rows[0][0] != suspect:
        print(f"  WARNING: SuspectTrip has {rows[0][0]:,} rows — the two views "
              "disagree, so a rule is not being applied identically")

    print("\n  trips by borough of pickup (suspect rows excluded):")
    _, rows = query(
        "SELECT pu_borough, COUNT(*) FROM Taxi.TripEnriched "
        "WHERE is_suspect = 0 GROUP BY pu_borough ORDER BY 2 DESC")
    for borough, n in rows:
        print(f"    {borough or '(no zone)':<16} {n:>9,}")


if __name__ == "__main__":
    main()
