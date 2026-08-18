"""Load taxi_zone_lookup into Taxi.Zone.

265 rows, so this is the cheap end-to-end rehearsal of the whole path: read a
CSV, coerce types, insert, verify.  Three things about this file are not
incidental:

*   It starts with a UTF-8 byte-order mark.  Read without ``encoding="utf-8-sig"``
    the first column name comes back as '\\ufeffLocationID', so every later
    reference to 'LocationID' raises KeyError for a column you can see in the
    file.  This is the single most confusing five minutes available in the
    supplied data.

*   LocationID 264 and 265 use the literal string 'N/A' as a sentinel, not an
    empty field: 264 is ('Unknown', 'N/A', 'N/A') and 265 is ('N/A', 'Outside of
    NYC', 'N/A').  Worth stating plainly because reading this file with pandas
    hides it -- 'N/A' is in pandas' default ``na_values``, so it silently becomes
    NaN and the file looks like it has blanks.  It does not.  Reading with the
    csv module shows what is actually there.  These labels are stored **exactly
    as the file gives them** and merely reported; see PLACEHOLDER_LABELS below
    for why rewriting them to a single 'Unknown' was a mistake.

*   These are not missing data either way: 264 *means* "Unknown" and 265 *means*
    "Outside of NYC".  The trip data references them on 2,260 pickups and 9,040
    dropoffs, so the rows are genuinely used.  Taxi.Zone is loaded complete,
    1..265, which is what lets the analytics joins be LEFT JOINs that never drop
    a trip.

*   Note on output: because Taxi.Zone's label columns are declared COLLATE EXACT
    (see schema.py), ``GROUP BY borough`` returns 'Manhattan'.  Without that
    declaration IRIS's default SQLUPPER collation returns 'MANHATTAN'.
"""

import csv
import os
import sys

from iris_conn import connect, query
from schema import (
    TABLES,
    ZONE_COLUMNS,
    create_tables,
    drop_tables,
    insert_sql,
    row_count,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ZONE_CSV = os.path.join(DATA_DIR, "taxi_zone_lookup(in).csv")

# Labels this file uses to mean "no meaningful value here".  These are REPORTED,
# not rewritten.
#
# An earlier version of this loader normalised all of them to the single string
# "Unknown".  That was wrong, and the reason is worth keeping: the file says
#
#     264,Unknown,N/A,N/A
#     265,N/A,Outside of NYC,N/A
#
# so 264's borough is genuinely labelled "Unknown" while 265's is labelled
# "N/A", and 265's zone is the meaningful "Outside of NYC".  Collapsing "N/A"
# into "Unknown" asserts the two placeholders mean the same thing, which is a
# guess about intent, and it silently destroys the distinction the source drew.
# There is no way to recover it afterwards from the loaded table.
#
# Storing the labels verbatim costs nothing: 'N/A' is a perfectly good VARCHAR
# for grouping, none of these fields is NULL in the source, and the enrichment
# joins in Taxi.TripAnalytics are LEFT JOINs that show the label as it is.  This
# is the same flag-don't-modify rule the trip loader follows -- record what the
# source said, record that it looks like a placeholder, and let the analyst
# decide.  Only the empty string is unrepresentable as a label, and it does not
# occur here; if a future edition of the file introduces one it will surface in
# the placeholder report below rather than being papered over.
PLACEHOLDER_LABELS = {"", "n/a", "na", "unknown", "none", "null"}


def read_zones(path=ZONE_CSV):
    """Parse the lookup CSV into rows matching ZONE_COLUMNS.

    Labels are stored exactly as the file gives them.  Returns (rows,
    placeholders) where ``placeholders`` records every field whose label looks
    like a "no value here" sentinel, as (location_id, column, value) triples --
    reported for the analyst's benefit, not rewritten.
    """
    rows, placeholders = [], []
    # utf-8-sig strips the BOM if present and is harmless if it is not.
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        expected = {"LocationID", "Borough", "Zone", "service_zone"}
        missing = expected - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                f"{path}: expected columns {sorted(expected)}, "
                f"got {reader.fieldnames}. Missing {sorted(missing)}. "
                "A '\\ufeff' prefix here means the BOM was not stripped."
            )

        for record in reader:
            location_id = int(record["LocationID"])
            values = {
                "borough": (record["Borough"] or "").strip(),
                "zone": (record["Zone"] or "").strip(),
                "service_zone": (record["service_zone"] or "").strip(),
            }
            for column, value in values.items():
                if value.lower() in PLACEHOLDER_LABELS:
                    placeholders.append((location_id, column, value or "(empty)"))
            rows.append(
                (location_id, values["borough"], values["zone"], values["service_zone"])
            )
    return rows, placeholders


def load(rows, replace=True):
    with connect() as conn:
        cur = conn.cursor()
        if replace:
            cur.execute("DELETE FROM Taxi.Zone")
        cur.executemany(insert_sql("Taxi.Zone", ZONE_COLUMNS), rows)
        conn.commit()


def verify():
    """Check the invariants that matter downstream, not just the row count."""
    problems = []

    total = row_count("Taxi.Zone")
    if total != 265:
        problems.append(f"expected 265 zones, found {total}")

    # Contiguous 1..265 is what makes a bad pu_location genuinely a data error
    # rather than a gap in the lookup.
    _, rows = query(
        "SELECT MIN(location_id), MAX(location_id), COUNT(DISTINCT location_id) "
        "FROM Taxi.Zone"
    )
    low, high, distinct = rows[0]
    if (low, high, distinct) != (1, 265, 265):
        problems.append(f"location_id not contiguous 1..265: {low}..{high}, {distinct} distinct")

    _, nulls = query(
        "SELECT COUNT(*) FROM Taxi.Zone "
        "WHERE borough IS NULL OR zone IS NULL OR service_zone IS NULL"
    )
    if nulls[0][0]:
        problems.append(f"{nulls[0][0]} rows still have NULL labels")

    return problems


def main():
    if "--reset" in sys.argv:
        print("Dropping Taxi.Zone:")
        drop_tables(["Taxi.Zone"])
    print("Creating tables if needed:")
    create_tables({"Taxi.Zone": TABLES["Taxi.Zone"]})

    rows, placeholders = read_zones()
    print(f"\nParsed {len(rows)} zones from {os.path.basename(ZONE_CSV)}")
    if placeholders:
        print(f"\n{len(placeholders)} label(s) look like placeholders. Stored "
              "verbatim, listed here so they are not mistaken for real names:")
        for location_id, column, value in placeholders:
            print(f"  location_id {location_id:>3}: {column:12s} {value!r}")

    load(rows)
    print(f"\nLoaded {row_count('Taxi.Zone')} rows into Taxi.Zone")

    problems = verify()
    if problems:
        print("\nVERIFY FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("Verified: 265 zones, location_id contiguous 1..265, no NULL labels")

    cols, sample = query(
        "SELECT location_id, borough, zone, service_zone FROM Taxi.Zone "
        "WHERE location_id IN (1, 166, 263, 264, 265) ORDER BY location_id"
    )
    print(f"\nSample ({', '.join(cols)}):")
    for row in sample:
        print("  " + " | ".join(str(value) for value in row))

    cols, boroughs = query(
        "SELECT borough, COUNT(*) AS zones FROM Taxi.Zone "
        "GROUP BY borough ORDER BY zones DESC"
    )
    print("\nZones per borough:")
    for borough, count in boroughs:
        print(f"  {borough:16s} {count:>4}")


if __name__ == "__main__":
    main()
