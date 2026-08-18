"""Stage 4 -- logical validation.

Everything is typed now, so each rule is a SQL predicate and the stage is
set-based: one INSERT ... SELECT per rule, inside IRIS. No trip data crosses into
Python -- only the rule text in and a count out.

Re-runnable by design: only phase='rule' flags are cleared and recomputed;
phase='ingest' flags survive, because the raw text behind them no longer exists.
That split lets an analyst change a threshold in Taxi.TripQuality and re-run in
seconds rather than reloading 787,060 rows.

Nothing is deleted: Trip keeps every row; the clean set is a predicate
(flag_count = 0), not a separate table.
"""

import stage1_schema
from db import banner, exec_dml, exec_sql, log, scalar


def apply_rules():
    active = exec_sql(
        "SELECT rule_id, rule_name, severity, predicate FROM Taxi.TripQuality "
        "WHERE phase = 'rule' AND enabled = 1 ORDER BY rule_id"
    )
    rule_ids = [row[0] for row in active]

    # Clear only this phase's verdicts.
    placeholders = ",".join(str(int(r)) for r in rule_ids)
    removed = exec_dml(f"DELETE FROM Taxi.TripFlag WHERE rule_id IN ({placeholders})")
    log(f"  cleared {removed:,} previous rule-phase flags")

    results = []
    for rule_id, name, severity, predicate in active:
        matched = exec_dml(
            f"INSERT INTO Taxi.TripFlag (trip_id, rule_id) "
            f"SELECT trip_id, {int(rule_id)} FROM Taxi.Trip WHERE {predicate}"
        )
        results.append((name, severity, matched))
        log(f"    {name:26s} {severity:8s} {matched:>8,}")
    return results


def refresh_summaries():
    """Denormalise flag_count and flag_mask back onto Trip.

    Two representations of the same truth: flag_count makes the clean set a single
    indexed predicate (flag_count = 0), the filter every analytics query starts
    with; flag_mask makes "which combination" answerable in one column via
    Taxi.mask_names.

    SUM over POWER(2, bit) is a valid bitwise OR here because TripFlag's primary
    key guarantees a rule appears at most once per trip -- no bit is double-counted.
    """
    exec_dml("UPDATE Taxi.Trip SET flag_count = 0, flag_mask = 0 WHERE flag_count <> 0 OR flag_mask <> 0")

    updated = exec_dml("""
        UPDATE Taxi.Trip t
           SET flag_count = (SELECT COUNT(*) FROM Taxi.TripFlag f
                              WHERE f.trip_id = t.trip_id),
               flag_mask  = (SELECT SUM(POWER(2, q.bit_position))
                               FROM Taxi.TripFlag f
                               JOIN Taxi.TripQuality q ON q.rule_id = f.rule_id
                              WHERE f.trip_id = t.trip_id)
         WHERE EXISTS (SELECT 1 FROM Taxi.TripFlag f WHERE f.trip_id = t.trip_id)
    """)
    log(f"  refreshed flag_count/flag_mask on {updated:,} flagged trips")
    return updated


def run():
    banner("STAGE 4 -- logical validation")
    apply_rules()
    refresh_summaries()

    total = scalar("SELECT COUNT(*) FROM Taxi.Trip")
    clean = scalar("SELECT COUNT(*) FROM Taxi.Trip WHERE flag_count = 0")
    flagged = total - clean
    flag_rows = scalar("SELECT COUNT(*) FROM Taxi.TripFlag")

    log("")
    log(f"  trips total   : {total:,}")
    log(f"  clean         : {clean:,} ({100.0 * clean / total:.2f}%)")
    log(f"  flagged       : {flagged:,} ({100.0 * flagged / total:.2f}%)")
    log(f"  flag rows     : {flag_rows:,} (a trip may break several rules)")

    # Optimiser statistics. Cheap here, and every downstream aggregate benefits.
    for table in ["Taxi.Trip", "Taxi.TripFlag", "Taxi.Zone"]:
        exec_dml(f"TUNE TABLE {table}")
    log("  ran TUNE TABLE on Trip, TripFlag, Zone")

    # Rebuild the enriched view *after* tuning, then query it the way the
    # analytics will. Both halves earn their place: TUNE TABLE replaces the
    # statistics the view's plan was compiled against, and the aggregate shape is
    # the one that broke -- see build_views() for the defect this guards.
    stage1_schema.build_views()
    _verify_view()
    return total, clean, flagged


def _verify_view():
    """Query the view the way the analytics will, so a broken plan fails here.

    Three shapes, because they fail independently. A row read touches both
    reference columns at once; the aggregate is what the isStatInvalid defect
    kills (a bare COUNT(*) is enough to trigger it, while row reads still work);
    the GROUP BY is what every workflow actually runs. A single-row smoke test
    would have passed the whole time the analytics were unrunnable.
    """
    row = exec_sql("""
        SELECT TOP 1 trip_id, pu_borough, pu_zone, do_borough, do_zone
        FROM Taxi.TripEnriched ORDER BY trip_id
    """)[0]
    if not row[1] or not row[3]:
        raise RuntimeError(f"enrichment returned empty borough names: {row}")
    log(f"  view check: trip {row[0]}  {row[1]}/{row[2]} -> {row[3]}/{row[4]}")

    counted = int(exec_sql("SELECT COUNT(*) FROM Taxi.TripEnriched")[0][0])
    expected = int(scalar("SELECT COUNT(*) FROM Taxi.Trip"))
    if counted != expected:
        raise RuntimeError(
            f"view returned {counted:,} rows but Taxi.Trip holds {expected:,} -- "
            f"the LEFT JOINs are duplicating or dropping rows"
        )

    grouped = exec_sql("""
        SELECT pu_borough, COUNT(*) FROM Taxi.TripEnriched
        GROUP BY pu_borough ORDER BY 2 DESC
    """)
    if not grouped:
        raise RuntimeError("view GROUP BY returned no rows")
    log(f"  view check: {counted:,} rows, {len(grouped)} pickup boroughs, "
        f"largest {grouped[0][0]} at {int(grouped[0][1]):,}")


if __name__ == "__main__":
    run()
