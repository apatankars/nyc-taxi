"""Can the corrupt trip_distance values be reconstructed from the rest of the row?

Partly.  This module measures how well, exposes the estimates as a view, and
deliberately does not write them into Taxi.Trip.

The question is worth asking because the 554 rows flagged SEPARATOR_IN_DISTANCE
are otherwise intact: pickup and dropoff timestamps, zones, fare, tip and total
all look ordinary.  Only the distance is destroyed.  So is there enough
redundancy elsewhere in the row to recover it?

Three routes were tested, and they are not equally good.

1.  The corrupt value itself -- no signal at all.
    If the corruption were a consistent scale error or digit shift, the ratio
    trip_distance / fare_amount would be roughly constant across the 554.  It
    ranges from 21.7 to 21,326.4, a span of 981x.  The stored number is noise
    with respect to the true distance, so nothing can be recovered from it.

2.  Fare, or fare plus duration -- too weak to use.
    Within a joint cell of +/-$1 of fare and +/-2 minutes of duration, the clean
    rows' distance still has a relative standard deviation of 23% to 65%, and the
    observed range inside one such cell runs from 0.25 to 13.84 miles.  The
    reason is structural rather than statistical: NYC's meter charges the greater
    of $0.50 per 1/5 mile (above 12 mph) or $0.50 per 60 seconds (at or below
    it), so one (fare, duration) pair is consistent with many
    distance-and-traffic combinations.  The fare is not invertible.

3.  The origin-destination pair -- usable for some rows, and measurably so.
    pu_location and do_location pin down the geometry in a way the fare cannot.
    Taking the clean trips that ran the same route as an empirical route length
    puts 247 of the 554 rows (44.6%) in the "usable" class defined below.

    That class was validated rather than assumed.  validate() below predicts each
    *clean* trip's distance from the other clean trips on its route -- a
    leave-one-out holdout, so the answer is known and never used in its own
    estimate -- and compares against what the meter reported:

        route class                        n        mean err   <=25%   <=10%
        usable    (cross-zone, rel sd <=25%)   345,359    20.7%   88.9%   56.4%
        weak      (cross-zone, rel sd > 25%)   321,189    59.3%   57.9%   26.4%
        same-zone (pu = do)                     76,725   394.1%   36.7%   16.2%

    Two things follow.  The confidence label earns its keep -- it separates 21%
    error from 59% and 394%, so it is discriminating real signal, not decorating
    a uniform guess.  And even the good class is only good to about a fifth: a
    typical estimate is off by 20.7%, and roughly one row in nine is off by more
    than 25%.

    Note that 20.7% is worse than the 14.8% mean relative sd the view reports for
    the same rows.  The peer-set sd is an optimistic error estimate, because the
    relative error of a short trip is inflated by its small denominator.  Trust
    the holdout number, not the sd.

    The method collapses when origin and destination are the same zone: 193->193
    gives 0.99 +/- 1.00 miles, and the holdout above puts same-zone error at
    394%, because a trip that begins and ends inside one zone can be any length
    at all.  Peer coverage varies too -- of the 554 rows, 326 have at least 30
    clean peers on their route, 445 have at least 5, and 36 have none.

So: the values can be *bounded* for most of these rows and *estimated to about
+/-20%* for 45% of them, but they cannot be *reconstructed*.  A distance known to
one part in five is fine for "how far do airport trips run"; it is not the
measurement the meter took, and 55% of the rows do not even get that.

Why this is a view and not an UPDATE
------------------------------------
Even the best of these is an estimate with a stated error, not a recovered
measurement, and writing it into trip_distance would make a guess
indistinguishable from a reading.  Every downstream query would then treat
"about 8 miles, inferred from 33 similar trips" exactly as it treats a value the
meter reported, and the distinction could never be recovered from the table.

So the estimate lives beside the data instead:

*   Taxi.DistanceEstimate is a VIEW.  It is computed on read, writes nothing, and
    cannot drift from the flags or the clean population it is derived from.
*   Every row carries peer_count and est_rel_error, so a caller can see how much
    to trust it, and confidence spells that out in words.
*   trip_distance keeps the value the source actually claimed, which stays the
    evidence that the upstream feed has a formatting bug.

An analyst who wants the estimates opts in by joining to this view.  Nobody gets
them by accident.

Usage:
    python src/reconstruct.py              # create the view and report
    python src/reconstruct.py --recreate   # drop and rebuild it first
"""

import sys

import rules
from iris_conn import execute, query

VIEW = "Taxi.DistanceEstimate"

# The flag whose rows this module tries to reconstruct.
TARGET_FLAG = "SEPARATOR_IN_DISTANCE"

# An estimate needs enough peers to be meaningful and needs the route to actually
# constrain the geometry.  Both are thresholds, so both are stated here rather
# than buried in the SQL.
MIN_PEERS = 5
MAX_REL_ERROR = 0.25


def view_sql(view=VIEW):
    """Route-median distance for every row whose distance is unusable.

    The clean peer population is built from rules.exclusion_clause, so it is the
    same definition of "trustworthy distance" the analytics views use.  AVG is
    safe here precisely because the peer set already excludes the flagged rows --
    the outliers that would wreck a mean are gone by construction.
    """
    clean_peers = (
        "c.quality_status <> 'INVALID' AND c.trip_distance > 0 AND "
        + rules.exclusion_clause("c", "trip_distance")
    )
    return f"""
        CREATE VIEW {view} AS
        SELECT t.trip_id,
               t.trip_distance                AS stored_value,
               t.pu_location, t.do_location,
               t.fare_amount, t.duration_min,
               r.peer_count,
               r.est_distance,
               r.est_sd,
               CASE WHEN r.est_distance > 0
                    THEN r.est_sd / r.est_distance END AS est_rel_error,
               CASE
                 WHEN r.peer_count IS NULL OR r.peer_count < {MIN_PEERS}
                   THEN 'none: too few comparable trips'
                 WHEN t.pu_location = t.do_location
                   THEN 'none: route begins and ends in one zone'
                 WHEN r.est_distance > 0
                      AND r.est_sd / r.est_distance > {MAX_REL_ERROR}
                   THEN 'weak: route length varies too much'
                 ELSE 'usable'
               END AS confidence
        FROM Taxi.Trip t
        LEFT JOIN (
            SELECT c.pu_location, c.do_location,
                   COUNT(*)              AS peer_count,
                   AVG(c.trip_distance)  AS est_distance,
                   STDDEV(c.trip_distance) AS est_sd
            FROM Taxi.Trip c
            WHERE {clean_peers}
            GROUP BY c.pu_location, c.do_location
        ) r ON r.pu_location = t.pu_location AND r.do_location = t.do_location
        WHERE EXISTS (
            SELECT 1 FROM Taxi.TripFlag f
            WHERE f.trip_id = t.trip_id AND f.flag_code = '{TARGET_FLAG}'
        )
    """


def drop_view(view=VIEW):
    try:
        execute(f"DROP VIEW {view}")
        print(f"  dropped {view}")
    except Exception as exc:
        message = str(exc)
        if "does not exist" not in message and "not found" not in message.lower():
            raise


def create_view(view=VIEW):
    try:
        execute(view_sql(view))
        print(f"  created {view}")
    except Exception as exc:
        if "already exists" in str(exc):
            print(f"  {view} already exists, left alone")
        else:
            raise


def report():
    total = query(f"SELECT COUNT(*) FROM {VIEW}")[1][0][0]
    print(f"\n{VIEW}: {total:,} rows (one per {TARGET_FLAG} flag)")

    _, rows = query(
        f"SELECT confidence, COUNT(*), AVG(est_rel_error) FROM {VIEW} "
        f"GROUP BY confidence ORDER BY COUNT(*) DESC"
    )
    print(f"\n  {'confidence':38s} {'rows':>7s} {'% ':>7s} {'mean rel. error':>16s}")
    for confidence, count, rel in rows:
        shown = f"{rel:.1%}" if rel is not None else "n/a"
        print(f"  {confidence:38s} {count:>7,} {100 * count / total:>6.1f}% {shown:>16s}")

    _, rows = query(
        f"SELECT COUNT(*), AVG(est_rel_error), MIN(est_rel_error), MAX(est_rel_error) "
        f"FROM {VIEW} WHERE confidence = 'usable'"
    )
    count, mean_rel, min_rel, max_rel = rows[0]
    if count:
        print(
            f"\n  Of the {total:,} unusable distances, {count:,} "
            f"({100 * count / total:.1f}%) have an estimate good to "
            f"{MAX_REL_ERROR:.0%} or better.\n"
            f"  Relative error across those: mean {mean_rel:.1%}, "
            f"best {min_rel:.1%}, worst {max_rel:.1%}."
        )

    print("\n  sample of the usable estimates:")
    _, rows = query(
        f"SELECT TOP 8 trip_id, pu_location, do_location, stored_value, "
        f"est_distance, est_sd, peer_count FROM {VIEW} "
        f"WHERE confidence = 'usable' ORDER BY peer_count DESC"
    )
    print(f"    {'trip_id':>8s} {'route':>10s} {'stored (bad)':>14s} "
          f"{'estimate':>10s} {'+/-':>7s} {'peers':>7s}")
    for tid, pu, do, stored, est, sd, peers in rows:
        print(f"    {tid:>8} {f'{pu}->{do}':>10s} {stored:>14,.2f} "
              f"{est:>10.2f} {sd or 0:>7.2f} {peers:>7,}")

    # The point that matters most: nothing was written.
    _, rows = query(
        "SELECT COUNT(*) FROM Taxi.Trip t WHERE EXISTS (SELECT 1 FROM Taxi.TripFlag f "
        f"WHERE f.trip_id = t.trip_id AND f.flag_code = '{TARGET_FLAG}') "
        "AND t.trip_distance < 1000"
    )
    print(
        f"\n  Taxi.Trip is unchanged: {rows[0][0]} of the {total:,} flagged rows have "
        f"had their\n  trip_distance altered. The estimates exist only in this view, "
        f"and every row\n  still carries the value the source actually claimed."
    )


def main():
    if row_count_or_die() == 0:
        raise SystemExit("Taxi.Trip is empty -- run src/load_trips.py --reset first.")
    if "--recreate" in sys.argv:
        print("Dropping view:")
        drop_view()
    print("Creating view:")
    create_view()
    report()


def row_count_or_die():
    return query("SELECT COUNT(*) FROM Taxi.Trip")[1][0][0]


if __name__ == "__main__":
    main()
