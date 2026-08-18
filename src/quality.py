"""Build the quality views from the rule registry, then verify and report.

Three views, all generated rather than hand-written, so that adding a rule to
rules.py is the only edit needed:

Taxi.SuspectTrip    The review queue.  Built from each rule's ``sql_predicate``
                    rather than from ``flag_count > 0``, deliberately: that makes
                    it an *independent* SQL expression of the same rules, which
                    is what the cross-check below compares the Python flagging
                    pass against.  If the two ever disagree, one of them is wrong
                    and the report says which rule.

Taxi.TripAnalytics  What an analyst should query by default.  Trips enriched with
                    pickup and dropoff borough/zone names, with INVALID rows
                    excluded -- and nothing else excluded.  The joins are LEFT
                    JOINs: Taxi.Zone is complete over 1..265, but a LEFT JOIN
                    means a future bad location_id degrades to a NULL zone name
                    instead of silently deleting the trip from every report.

Taxi.TripQuality    Flag hit counts by rule and severity.  This is the
                    calibration tool: a rule firing on a fifth of the table is
                    far more likely to be miscalibrated than to have found that
                    much bad data, which is exactly how TOTAL_MISMATCH ended up
                    parked in the registry.

Usage:
    python src/quality.py            # create views, cross-check, print report
    python src/quality.py --recreate # drop and rebuild the views first
"""

import argparse
import sys

import rules
from iris_conn import connect, execute, query
from schema import row_count

VIEWS = ["Taxi.SuspectTrip", "Taxi.TripAnalytics", "Taxi.TripQuality"]


def analytics_view_sql(view="Taxi.TripAnalytics"):
    return f"""
        CREATE VIEW {view} AS
        SELECT t.trip_id,
               t.pickup_ts, t.dropoff_ts, t.duration_min, t.implied_mph,
               t.pu_location, pz.borough AS pu_borough, pz.zone AS pu_zone,
               t.do_location, dz.borough AS do_borough, dz.zone AS do_zone,
               t.passenger_count, t.trip_distance,
               t.fare_amount, t.tip_amount, t.total_amount,
               t.payment_type, t.vendor_id,
               t.quality_status, t.flag_count
        FROM Taxi.Trip t
        LEFT JOIN Taxi.Zone pz ON t.pu_location = pz.location_id
        LEFT JOIN Taxi.Zone dz ON t.do_location = dz.location_id
        WHERE t.quality_status <> 'INVALID'
    """


def quality_view_sql(view="Taxi.TripQuality"):
    return f"""
        CREATE VIEW {view} AS
        SELECT flag_code, severity, COUNT(*) AS trips_flagged
        FROM Taxi.TripFlag
        GROUP BY flag_code, severity
    """


def drop_views():
    for view in reversed(VIEWS):
        try:
            execute(f"DROP VIEW {view}")
            print(f"  dropped {view}")
        except Exception as exc:
            if "does not exist" not in str(exc) and "not found" not in str(exc).lower():
                raise


def create_views():
    definitions = [
        ("Taxi.SuspectTrip", rules.suspect_view_sql()),
        ("Taxi.TripAnalytics", analytics_view_sql()),
        ("Taxi.TripQuality", quality_view_sql()),
    ]
    for name, ddl in definitions:
        try:
            execute(ddl)
            print(f"  created {name}")
        except Exception as exc:
            if "already exists" in str(exc):
                print(f"  {name} already exists, left alone")
            else:
                print(f"  FAILED {name}: {str(exc)[:200]}")
                raise


# ---------------------------------------------------------------------------
# Cross-check: does the SQL form of each rule agree with the Python form?
# ---------------------------------------------------------------------------


def cross_check():
    """Compare each rule's stored flag count against its SQL predicate count.

    The Python predicate ran at load time over parsed values; the SQL predicate
    runs now over stored columns.  They are two independent expressions of one
    rule, so a disagreement means the rule has drifted -- which is the risk that
    comes with holding it in two forms, made visible instead of assumed away.

    Returns a list of (code, python_count, sql_count, note) for every SQL-
    checkable rule, plus a list of the ones that disagree.
    """
    results, disagreements = [], []

    with connect() as conn:
        cur = conn.cursor()
        for rule in rules.sql_checkable():
            cur.execute(
                "SELECT COUNT(*) FROM Taxi.TripFlag WHERE flag_code = ?", [rule.code]
            )
            python_count = cur.fetchall()[0][0]

            try:
                cur.execute(f"SELECT COUNT(*) FROM Taxi.Trip WHERE {rule.sql_predicate}")
                sql_count = cur.fetchall()[0][0]
                note = ""
            except Exception as exc:
                sql_count = None
                note = f"SQL failed: {str(exc)[:80]}"

            results.append((rule.code, python_count, sql_count, note))
            if sql_count is None or sql_count != python_count:
                disagreements.append((rule.code, python_count, sql_count, note))

    return results, disagreements


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report_counts():
    trips = row_count("Taxi.Trip")
    flags = row_count("Taxi.TripFlag")
    rejects = row_count("Taxi.TripReject")
    print(f"Taxi.Trip {trips:,} | Taxi.TripFlag {flags:,} | Taxi.TripReject {rejects:,}")

    _, status_rows = query(
        "SELECT quality_status, COUNT(*) FROM Taxi.Trip "
        "GROUP BY quality_status ORDER BY COUNT(*) DESC"
    )
    print("\nquality_status:")
    for status, count in status_rows:
        print(f"  {status:8s} {count:>9,}  {100 * count / trips:5.2f}%")

    _, suspect = query("SELECT COUNT(*) FROM Taxi.SuspectTrip")
    _, analytics = query("SELECT COUNT(*) FROM Taxi.TripAnalytics")
    print(f"\nTaxi.SuspectTrip   {suspect[0][0]:>9,}  rows in the review queue")
    print(f"Taxi.TripAnalytics {analytics[0][0]:>9,}  rows available to analysts "
          f"({100 * analytics[0][0] / trips:.2f}% of the table)")


def report_flag_hits():
    _, rows = query(
        "SELECT flag_code, severity, trips_flagged FROM Taxi.TripQuality "
        "ORDER BY trips_flagged DESC"
    )
    trips = row_count("Taxi.Trip")
    print(f"\nflag hits ({len(rows)} rules fired):")
    print(f"  {'code':32s} {'severity':9s} {'trips':>9s} {'% of table':>11s}")
    for code, severity, count in rows:
        print(f"  {code:32s} {severity:9s} {count:>9,} {100 * count / trips:>10.2f}%")

    parked = [r for r in rules.RULES if not r.enabled]
    if parked:
        print("\n  parked rules (defined, not applied):")
        for rule in parked:
            print(f"    {rule.code:30s} would have fired on ~{rule.expected:,} rows")


def report_flags_per_row():
    _, rows = query(
        "SELECT flag_count, COUNT(*) FROM Taxi.Trip "
        "GROUP BY flag_count ORDER BY flag_count"
    )
    print("\nflags per row:")
    for flag_count, trips in rows:
        print(f"  {flag_count} flag(s) {trips:>9,}")


def report_per_question_exclusions():
    """Show that different workflows keep different numbers of rows.

    This is the payoff of storing flags instead of filtering on them: one load,
    one rule registry, and each question applies only the exclusions that bear
    on the columns it actually reads.
    """
    workflows = [
        ("busiest pickup zones", ("pu_location",)),
        ("hourly activity profile", ("pickup_ts",)),
        ("fare per mile by zone", ("trip_distance", "fare_amount")),
        ("average occupancy", ("passenger_count",)),
        ("tipping behaviour", ("tip_amount", "fare_amount")),
    ]
    trips = row_count("Taxi.Trip")
    print("\nrows available per workflow (INVALID excluded, plus only the flags "
          "that affect\nthe columns that workflow reads):")
    print(f"  {'workflow':26s} {'usable rows':>12s} {'% of table':>11s}  excluded by")
    for label, columns in workflows:
        clause = rules.exclusion_clause("t", *columns)
        _, rows = query(
            f"SELECT COUNT(*) FROM Taxi.Trip t "
            f"WHERE t.quality_status <> 'INVALID' AND {clause}"
        )
        usable = rows[0][0]
        codes = sorted(rules.codes_invalidating(*columns))
        summary = f"{len(codes)} flag(s)" if codes else "INVALID only"
        print(f"  {label:26s} {usable:>12,} {100 * usable / trips:>10.2f}%  {summary}")


def report_cross_check(results, disagreements):
    print(f"\nPython vs. SQL cross-check ({len(results)} rules checkable in both):")
    if not disagreements:
        print("  all rules agree -- the load-time pass and the SQL predicates "
              "produce identical counts")
    else:
        print(f"  {len(disagreements)} DISAGREEMENT(S):")
        print(f"  {'code':32s} {'python':>9s} {'sql':>9s}  note")
        for code, python_count, sql_count, note in disagreements:
            shown = "error" if sql_count is None else f"{sql_count:,}"
            print(f"  {code:32s} {python_count:>9,} {shown:>9s}  {note}")

    unmatched = [r for r in rules.active() if r.sql_predicate is None]
    if unmatched:
        print(f"\n  {len(unmatched)} rule(s) have no SQL form and cannot be "
              "cross-checked at all:")
        for rule in unmatched:
            print(f"    {rule.code:32s} {rule.severity}")
        print("  These record what happened while parsing the raw text, which is "
              "gone\n  once the row is in a table. A server-side LOAD DATA would "
              "lose them silently.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--recreate", action="store_true",
                        help="drop the views before creating them")
    args = parser.parse_args()

    if row_count("Taxi.Trip") == 0:
        raise SystemExit("Taxi.Trip is empty -- run src/load_trips.py --reset first.")

    if args.recreate:
        print("Dropping views:")
        drop_views()
    print("Creating views:")
    create_views()

    print()
    report_counts()
    report_flag_hits()
    report_flags_per_row()
    report_per_question_exclusions()

    results, disagreements = cross_check()
    report_cross_check(results, disagreements)

    if disagreements:
        print("\nThe Python and SQL forms of at least one rule disagree. Fix the "
              "rule before trusting either count.")
        sys.exit(1)


if __name__ == "__main__":
    main()
