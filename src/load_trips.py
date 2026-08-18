"""Load the green taxi CSV into Taxi.Trip, flagging quality issues en route.

This is Route B from the runbook -- Python reads and transforms, IRIS receives
finished rows -- and it is where the "Python as the primary implementation
language" requirement is actually met: parsing, repair, type coercion and every
quality rule run here, in code that can be read, tested and diffed.

The order of operations per row matters:

    1. shape      Does the line split into the expected number of fields?  If
                  not it goes to Taxi.TripReject with its raw text and stops.
                  This is the only reason a row is ever turned away.
    2. parse      Text to Python types.  Timestamps are MM/DD/YYYY hh:mm:ss
                  AM/PM, not ISO.  Numbers may carry thousands separators.
    3. repair     A comma inside a quoted number ('2,032.67') has exactly one
                  sensible reading, so strip it and record that we did.  A field
                  that still will not parse is left NULL and flagged; the rest of
                  the row is kept.
    4. derive     duration_min and implied_mph, computed once and stored.
    5. flag       Every rule in the registry, then the parse-time flags that only
                  this function has the evidence for.
    6. classify   Collapse severities into quality_status.

No row is dropped for having bad *values* -- only for having the wrong shape.
That is deliberate and it is the central design decision of the whole workflow:
23.8% of rows trip at least one rule while 0.13% are genuinely unusable, so
filtering on flags would discard roughly 187,000 rows to guard against 1,000.

Usage:
    python src/load_trips.py --reset            # full load, ~787k rows
    python src/load_trips.py --reset --limit 5000
    python src/load_trips.py --reset --batch 20000
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import rules
from iris_conn import connect, query
from schema import (
    FLAG_COLUMNS,
    REJECT_COLUMNS,
    TABLES,
    TRIP_COLUMNS,
    create_indexes,
    create_tables,
    drop_tables,
    insert_sql,
    row_count,
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
TRIP_CSV = os.path.join(DATA_DIR, "2023_Green_Taxi_Trip_Data.csv")

TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M:%S %p"

# Header of the supplied file, in order.  Checked at startup rather than
# trusted: if the file is ever reissued with different columns we want a clear
# error, not 787,000 rows of silently misaligned data.
EXPECTED_HEADER = [
    "VendorID",
    "lpep_pickup_datetime",
    "lpep_dropoff_datetime",
    "store_and_fwd_flag",
    "RatecodeID",
    "PULocationID",
    "DOLocationID",
    "passenger_count",
    "trip_distance",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "ehail_fee",
    "improvement_surcharge",
    "total_amount",
    "payment_type",
    "trip_type",
    "congestion_surcharge",
]

# CSV column -> (our column, kind).  'ehail_fee' is absent on purpose: it is
# empty on all 787,060 rows, and the loader verifies that rather than assuming.
FIELD_MAP = [
    ("VendorID", "vendor_id", "int"),
    ("lpep_pickup_datetime", "pickup_ts", "ts"),
    ("lpep_dropoff_datetime", "dropoff_ts", "ts"),
    ("store_and_fwd_flag", "store_and_fwd_flag", "str"),
    ("RatecodeID", "ratecode_id", "int"),
    ("PULocationID", "pu_location", "int"),
    ("DOLocationID", "do_location", "int"),
    ("passenger_count", "passenger_count", "int"),
    ("trip_distance", "trip_distance", "float"),
    ("fare_amount", "fare_amount", "float"),
    ("extra", "extra", "float"),
    ("mta_tax", "mta_tax", "float"),
    ("tip_amount", "tip_amount", "float"),
    ("tolls_amount", "tolls_amount", "float"),
    ("improvement_surcharge", "improvement_surcharge", "float"),
    ("total_amount", "total_amount", "float"),
    ("payment_type", "payment_type", "int"),
    ("trip_type", "trip_type", "int"),
    ("congestion_surcharge", "congestion_surcharge", "float"),
]

DEFAULT_BATCH = 10_000


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_timestamp(raw):
    """MM/DD/YYYY hh:mm:ss AM/PM -> datetime, or None if it will not parse."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), TIMESTAMP_FORMAT)
    except ValueError:
        return None


def parse_number(raw, as_int=False):
    """Text -> number, tolerating thousands separators.

    Returns (value, repaired, failed):

        repaired  a comma was stripped before parsing.  554 trip_distance, 12
                  fare_amount and 12 total_amount values in this file are
                  written '2,032.67'.  float() raises on those, and pandas'
                  errors='coerce' turns them into NaN -- and because they are
                  the largest values in the file, that quietly deletes exactly
                  the outliers a quality workflow exists to find.
        failed    the text is not a number even after repair.  The caller stores
                  NULL and flags the row; it does not discard it.
    """
    if raw is None:
        return None, False, False
    text = raw.strip()
    if not text:
        return None, False, False

    repaired = False
    if "," in text:
        text = text.replace(",", "")
        repaired = True

    try:
        # int() rejects '1.0', which is how whole numbers appear in this file,
        # so route integers through float() first.
        return (int(float(text)) if as_int else float(text)), repaired, False
    except ValueError:
        return None, repaired, True


def parse_row(fields, index_of):
    """Text fields -> (row dict, repaired columns, unparseable columns)."""
    row = {}
    repaired, failed = [], []

    for csv_name, column, kind in FIELD_MAP:
        raw = fields[index_of[csv_name]]
        if kind == "ts":
            row[column] = parse_timestamp(raw)
        elif kind == "str":
            text = (raw or "").strip()
            row[column] = text or None
        else:
            value, was_repaired, did_fail = parse_number(raw, as_int=(kind == "int"))
            row[column] = value
            if was_repaired:
                repaired.append(column)
            if did_fail:
                failed.append(column)

    # Derived columns.  Guarded rather than try/except because both inputs being
    # absent is ordinary here, not exceptional.
    pickup, dropoff = row["pickup_ts"], row["dropoff_ts"]
    if pickup is not None and dropoff is not None:
        row["duration_min"] = (dropoff - pickup).total_seconds() / 60.0
    else:
        row["duration_min"] = None

    distance, duration = row["trip_distance"], row["duration_min"]
    if distance is not None and duration is not None and duration > 0:
        row["implied_mph"] = distance / (duration / 60.0)
    else:
        row["implied_mph"] = None

    return row, repaired, failed


def flag_row(row, repaired, failed):
    """Run the registry, then add the flags only the parser can know about.

    Returns (flags, quality_status) where flags is a list of
    (code, severity, detail).
    """
    flags = [(code, severity, None) for code, severity in rules.flags_for(row)]

    # Parse-time flags.  These cannot be expressed as SQL over stored columns,
    # because the evidence is the raw text and the raw text is gone once the row
    # is in a table.  A pure server-side LOAD DATA would lose them silently.
    # One flag per group of repaired columns rather than one per row, so that
    # each flag's `invalidates` names only the columns actually affected. A row
    # with a comma in trip_distance must not have its fare marked unusable.
    for code, columns in (
        ("SEPARATOR_IN_DISTANCE", [c for c in repaired if c == "trip_distance"]),
        ("SEPARATOR_IN_AMOUNT", [c for c in repaired if c != "trip_distance"]),
    ):
        if columns:
            flags.append(
                (code, rules.BY_CODE[code].severity, ",".join(columns)[:120])
            )
    if failed:
        flags.append(
            (
                "NUMERIC_UNPARSEABLE",
                rules.BY_CODE["NUMERIC_UNPARSEABLE"].severity,
                ",".join(failed)[:120],
            )
        )

    status = rules.status_for({severity for _, severity, _ in flags})
    return flags, status


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class Batcher:
    """Accumulate rows for the three tables and flush them together.

    Trips and their flags are committed in the same transaction, so the flag
    table can never end up describing rows that are not there.
    """

    def __init__(self, conn, batch_size):
        self.conn = conn
        self.cursor = conn.cursor()
        self.batch_size = batch_size
        self.trips, self.flags, self.rejects = [], [], []
        self.trips_written = self.flags_written = self.rejects_written = 0

    def add_trip(self, trip_tuple, flag_tuples):
        self.trips.append(trip_tuple)
        self.flags.extend(flag_tuples)
        if len(self.trips) >= self.batch_size:
            self.flush()

    def add_reject(self, reject_tuple):
        self.rejects.append(reject_tuple)

    def flush(self):
        if self.trips:
            self.cursor.executemany(insert_sql("Taxi.Trip", TRIP_COLUMNS), self.trips)
            self.trips_written += len(self.trips)
        if self.flags:
            self.cursor.executemany(
                insert_sql("Taxi.TripFlag", FLAG_COLUMNS), self.flags
            )
            self.flags_written += len(self.flags)
        if self.rejects:
            self.cursor.executemany(
                insert_sql("Taxi.TripReject", REJECT_COLUMNS), self.rejects
            )
            self.rejects_written += len(self.rejects)
        self.conn.commit()
        self.trips, self.flags, self.rejects = [], [], []


def load(path=TRIP_CSV, batch_size=DEFAULT_BATCH, limit=None):
    """Read, parse, flag and insert.  Returns a stats dict."""
    stats = {
        "lines_read": 0,
        "trips": 0,
        "rejects": 0,
        "flags": 0,
        "flag_counts": {},
        "status_counts": {"OK": 0, "SUSPECT": 0, "INVALID": 0},
        "ehail_fee_nonempty": 0,
        "first_trip_id": None,
        "last_trip_id": None,
    }
    started = time.time()

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != EXPECTED_HEADER:
            raise SystemExit(
                "CSV header does not match what this loader was written for.\n"
                f"  expected: {EXPECTED_HEADER}\n  found:    {header}"
            )
        index_of = {name: position for position, name in enumerate(header)}
        expected_fields = len(header)
        ehail_index = index_of["ehail_fee"]

        with connect() as conn:
            batcher = Batcher(conn, batch_size)

            # trip_id is the physical line number, so enumerate starts at 2.
            for line_no, fields in enumerate(reader, start=2):
                stats["lines_read"] += 1

                if len(fields) != expected_fields:
                    batcher.add_reject(
                        (
                            line_no,
                            ",".join(fields)[:4000],
                            f"expected {expected_fields} fields, got {len(fields)}",
                        )
                    )
                    stats["rejects"] += 1
                    continue

                # Check, rather than assume, the claim that justified dropping
                # this column from the schema.
                if fields[ehail_index].strip():
                    stats["ehail_fee_nonempty"] += 1

                row, repaired, failed = parse_row(fields, index_of)
                flags, status = flag_row(row, repaired, failed)

                row["trip_id"] = line_no
                row["quality_status"] = status
                row["flag_count"] = len(flags)

                batcher.add_trip(
                    tuple(row[column] for column in TRIP_COLUMNS),
                    [(line_no, code, severity, detail) for code, severity, detail in flags],
                )

                stats["trips"] += 1
                stats["flags"] += len(flags)
                stats["status_counts"][status] += 1
                for code, _, _ in flags:
                    stats["flag_counts"][code] = stats["flag_counts"].get(code, 0) + 1
                if stats["first_trip_id"] is None:
                    stats["first_trip_id"] = line_no
                stats["last_trip_id"] = line_no

                if stats["lines_read"] % 100_000 == 0:
                    rate = stats["lines_read"] / (time.time() - started)
                    print(
                        f"  {stats['lines_read']:>9,} lines  "
                        f"{rate:>8,.0f} rows/s",
                        flush=True,
                    )

                if limit and stats["lines_read"] >= limit:
                    break

            batcher.flush()

    stats["seconds"] = time.time() - started
    stats["rows_per_second"] = stats["trips"] / stats["seconds"] if stats["seconds"] else 0
    return stats


# ---------------------------------------------------------------------------
# Reporting and verification
# ---------------------------------------------------------------------------


def reconcile(stats):
    """Every data line must be accounted for, in exactly one table."""
    problems = []

    trips = row_count("Taxi.Trip")
    rejects = row_count("Taxi.TripReject")
    flags = row_count("Taxi.TripFlag")

    if trips != stats["trips"]:
        problems.append(f"Taxi.Trip has {trips:,}, loader counted {stats['trips']:,}")
    if rejects != stats["rejects"]:
        problems.append(
            f"Taxi.TripReject has {rejects:,}, loader counted {stats['rejects']:,}"
        )
    if flags != stats["flags"]:
        problems.append(f"Taxi.TripFlag has {flags:,}, loader counted {stats['flags']:,}")
    if trips + rejects != stats["lines_read"]:
        problems.append(
            f"{stats['lines_read']:,} data lines read but "
            f"{trips:,} + {rejects:,} = {trips + rejects:,} rows stored"
        )

    # trip_id is the source line number, so it must be a contiguous run.
    _, rows = query("SELECT MIN(trip_id), MAX(trip_id), COUNT(DISTINCT trip_id) FROM Taxi.Trip")
    low, high, distinct = rows[0]
    if distinct != trips:
        problems.append(f"{trips:,} rows but only {distinct:,} distinct trip_ids")
    if trips and rejects == 0 and (high - low + 1) != trips:
        problems.append(
            f"trip_id range {low}..{high} spans {high - low + 1:,} lines "
            f"but {trips:,} rows were stored"
        )

    # A flag must never reference a trip that is not there.
    _, orphans = query(
        "SELECT COUNT(*) FROM Taxi.TripFlag f "
        "WHERE NOT EXISTS (SELECT 1 FROM Taxi.Trip t WHERE t.trip_id = f.trip_id)"
    )
    if orphans[0][0]:
        problems.append(f"{orphans[0][0]:,} flags reference a missing trip")

    return problems


def report(stats):
    print(f"\nRead {stats['lines_read']:,} data lines in {stats['seconds']:.1f}s "
          f"({stats['rows_per_second']:,.0f} rows/s)")
    print(f"  Taxi.Trip        {stats['trips']:>9,}")
    print(f"  Taxi.TripReject  {stats['rejects']:>9,}")
    print(f"  Taxi.TripFlag    {stats['flags']:>9,}")

    total = stats["trips"] or 1
    print("\nquality_status:")
    for status in ("OK", "SUSPECT", "INVALID"):
        count = stats["status_counts"][status]
        print(f"  {status:8s} {count:>9,}  {100 * count / total:5.2f}%")
    excluded = stats["status_counts"]["INVALID"]
    flagged = total - stats["status_counts"]["OK"]
    print(
        f"\n  {flagged:,} rows carry at least one SUSPECT or INVALID flag "
        f"({100 * flagged / total:.2f}%),\n"
        f"  but only {excluded:,} ({100 * excluded / total:.2f}%) are excluded from "
        "analytics."
    )

    print("\nrule hits (actual vs. expected from profiling the raw CSV):")
    print(f"  {'code':32s} {'severity':9s} {'actual':>9s} {'expected':>9s}  {'delta':>8s}")
    for rule in rules.RULES:
        if not rule.enabled:
            continue
        actual = stats["flag_counts"].get(rule.code, 0)
        expected = rule.expected
        if expected is None:
            delta = "-"
        elif expected == actual:
            delta = "ok"
        else:
            delta = f"{actual - expected:+,}"
        print(
            f"  {rule.code:32s} {rule.severity:9s} {actual:>9,} "
            f"{expected if expected is not None else '?':>9}  {delta:>8s}"
        )

    if stats["ehail_fee_nonempty"]:
        print(
            f"\nNOTE: ehail_fee was non-empty on {stats['ehail_fee_nonempty']:,} rows. "
            "The schema drops that column on the assumption it is always empty -- "
            "that assumption no longer holds."
        )
    else:
        print("\nehail_fee: empty on every row, as assumed. Dropping it loses nothing.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate the trip tables first")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N data lines (for a quick test run)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help=f"rows per executemany call (default {DEFAULT_BATCH:,})")
    parser.add_argument("--skip-indexes", action="store_true",
                        help="do not build indexes at all")
    parser.add_argument("--index-after", action="store_true",
                        help="build indexes after loading instead of before, which "
                             "is much slower here -- see the note in main()")
    args = parser.parse_args()

    trip_tables = {
        name: TABLES[name] for name in ("Taxi.Trip", "Taxi.TripFlag", "Taxi.TripReject")
    }

    if args.reset:
        # Views must go first: IRIS refuses to DROP a table a view references
        # (SQLCODE -321), so a --reset after quality.py has run fails halfway
        # through the teardown, leaving some tables dropped and some not.
        # Imported here rather than at module scope to keep the dependency
        # one-directional -- quality.py reads what this module writes, and only
        # this teardown path needs to know views exist at all.
        import quality

        print("Dropping dependent views:")
        quality.drop_views()
        print("Dropping trip tables:")
        drop_tables(list(trip_tables))
    print("Creating trip tables:")
    create_tables(trip_tables)

    existing = row_count("Taxi.Trip")
    if existing:
        raise SystemExit(
            f"\nTaxi.Trip already holds {existing:,} rows. Re-run with --reset to "
            "replace them (trip_id is the source line number, so loading twice "
            "would violate the primary key anyway)."
        )

    # Indexes are built BEFORE the load, which is the opposite of the usual
    # bulk-load advice and was measured on this instance:
    #
    #   indexes on the empty tables, then load   0.4s to index
    #   load, then indexes over 20,000 rows      120.6s to index
    #
    # Two orders of magnitude, on 20k rows. The per-row cost of maintaining the
    # seven indexes during insert is in the noise by comparison (20k rows took
    # 0.12s with no indexes, 0.14s with four live). The slow path gives no hint
    # that anything is wrong -- it just sits there -- so --index-after exists to
    # make the comparison reproducible rather than a claim.
    if not args.skip_indexes and not args.index_after:
        print("\nBuilding indexes (before load):")
        started = time.time()
        create_indexes()
        print(f"  built in {time.time() - started:.1f}s")

    print(f"\nLoading {os.path.basename(TRIP_CSV)}"
          + (f" (limit {args.limit:,})" if args.limit else "") + ":")
    stats = load(batch_size=args.batch, limit=args.limit)
    report(stats)

    if not args.skip_indexes and args.index_after:
        print("\nBuilding indexes (after load):")
        started = time.time()
        create_indexes()
        print(f"  built in {time.time() - started:.1f}s")

    print("\nReconciling:")
    problems = reconcile(stats)
    if problems:
        print("RECONCILIATION FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(f"  {stats['lines_read']:,} data lines = {stats['trips']:,} trips + "
          f"{stats['rejects']:,} rejects, trip_ids contiguous, no orphan flags")


if __name__ == "__main__":
    main()
