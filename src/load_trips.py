"""Load the trip CSV into Taxi.Trip, using the same schema the validator uses.

Run:  python src/load_trips.py       (run src/validate_trips.py first)

Route B from docs/setup_and_workflow.md: Python types the rows, IRIS receives
finished ones. Chosen over server-side LOAD DATA because of what profiling found:
'MM/DD/YYYY hh:mm:ss AM/PM' timestamps will not land in a TIMESTAMP column, and
578 numeric values carry a thousands separator that has to be stripped before the
value parses at all.

Nothing plausible-looking is filtered here. A zero distance and a -$500 fare both
load; flagging them belongs to the quality view, which can then be rewritten
without touching the 787k rows. Only rows that cannot be *typed* go to
Taxi.TripReject, with the raw row kept so they stay auditable — and against the
supplied file that table comes out empty, which is itself worth reporting.
"""

import csv
import os
import time

from iris_conn import connect, execute, query
from schema import ALL_COLUMNS, coerce_row, create_table_ddl, insert_sql

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "2023_Green_Taxi_Trip_Data.csv",
)

# Big enough that per-statement overhead stops dominating, small enough that a
# failure does not roll back minutes of work.
BATCH = 10_000

DDL_REJECT = """
CREATE TABLE Taxi.TripReject (
    line_no  INTEGER,
    reason   VARCHAR(500),
    raw_row  VARCHAR(2000)
)
"""

# Created after the load, not before: maintaining them across 787k inserts is
# pure overhead. Bitmap for the low-cardinality code columns — payment_type has
# 5 distinct values over 787k rows — which is one of the things IRIS offers that
# a CSV cannot.
INDEXES = (
    "CREATE INDEX idx_trip_pickup ON Taxi.Trip (pickup_ts)",
    "CREATE INDEX idx_trip_pu     ON Taxi.Trip (pu_location_id)",
    "CREATE INDEX idx_trip_do     ON Taxi.Trip (do_location_id)",
    "CREATE BITMAP INDEX idx_trip_payment ON Taxi.Trip (payment_type)",
    "CREATE BITMAP INDEX idx_trip_type    ON Taxi.Trip (trip_type)",
    "CREATE BITMAP INDEX idx_trip_vendor  ON Taxi.Trip (vendor_id)",
)


def table_exists(schema, table):
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, table])
    return rows[0][0] > 0


def recreate_tables():
    """Drop and rebuild, so the script is safe to re-run while iterating."""
    for table, ddl in (("Trip", create_table_ddl()), ("TripReject", DDL_REJECT)):
        if table_exists("Taxi", table):
            execute(f"DROP TABLE Taxi.{table}")
        execute(ddl)
    print(f"created Taxi.Trip ({len(ALL_COLUMNS)} columns) and Taxi.TripReject")


def main():
    recreate_tables()
    sql = insert_sql()

    inserted = 0
    batch, rejects = [], []
    started = time.perf_counter()

    with connect() as conn:
        cur = conn.cursor()
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            for line_no, row in enumerate(csv.DictReader(f), start=2):
                values, errors = coerce_row(row)
                if errors:
                    rejects.append((line_no, "; ".join(errors)[:500], str(row)[:2000]))
                    continue
                batch.append(values)

                if len(batch) >= BATCH:
                    cur.executemany(sql, batch)
                    conn.commit()
                    inserted += len(batch)
                    batch = []
                    elapsed = time.perf_counter() - started
                    print(f"  {inserted:>7,} rows  {elapsed:>6.1f}s  "
                          f"{inserted / elapsed:>7,.0f} rows/s",
                          end="\r", flush=True)

        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)

        if rejects:
            cur.executemany(
                "INSERT INTO Taxi.TripReject (line_no, reason, raw_row) "
                "VALUES (?,?,?)", rejects)
            conn.commit()

    load_secs = time.perf_counter() - started
    print(f"\ninserted {inserted:,} rows in {load_secs:.1f}s "
          f"({inserted / load_secs:,.0f} rows/s); rejected {len(rejects):,}")

    print("building indexes ...")
    idx_started = time.perf_counter()
    for ddl in INDEXES:
        execute(ddl)
    idx_secs = time.perf_counter() - idx_started
    print(f"indexes built in {idx_secs:.1f}s")

    _, rows = query("SELECT COUNT(*) FROM Taxi.Trip")
    print(f"\nTaxi.Trip       {rows[0][0]:>9,} rows")
    _, rows = query("SELECT COUNT(*) FROM Taxi.TripReject")
    print(f"Taxi.TripReject {rows[0][0]:>9,} rows")
    print(f"total wall clock: {load_secs + idx_secs:.1f}s")


if __name__ == "__main__":
    main()
