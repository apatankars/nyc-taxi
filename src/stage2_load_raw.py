"""Stage 2 -- land the CSVs.

Trips go in via LOAD DATA: IRIS streams the file into the table server-side, so
no trip data crosses a process boundary. TripRaw is all VARCHAR, so awkward values
survive verbatim -- '1,571.97' lands as the string '1,571.97' rather than failing
a cast or being truncated. Judging it is stage 3's job.

Zones go in via Python: there are only 265, and the lookup file's 'Zone' column
would force an awkward name match against LOAD DATA's header-pairing rule.
"""

import csv

from db import (RAW_COLUMNS, TRIP_CSV, ZONE_CSV, banner, exec_dml, exec_sql,
                log, scalar, sql_params)


def load_trips():
    # Idempotent: LOAD DATA appends, so a re-run without this would silently
    # double the table.
    exec_dml("DELETE FROM Taxi.TripRaw")
    log(f"LOAD DATA from {TRIP_CSV} ...")
    # header:1 tells the loader the first line is column names, not data.
    exec_sql(
        f"LOAD DATA FROM FILE '{TRIP_CSV}' "
        f"INTO Taxi.TripRaw "
        f'USING {{"from":{{"file":{{"header":1}}}}}}'
    )
    loaded = scalar("SELECT COUNT(*) FROM Taxi.TripRaw")
    log(f"  Taxi.TripRaw: {loaded:,} rows")

    # Confirm the pathological values arrived intact rather than mangled.
    commas = scalar("SELECT COUNT(*) FROM Taxi.TripRaw WHERE trip_distance LIKE '%,%'")
    log(f"  rows whose trip_distance kept a thousands separator: {commas:,}")
    return loaded


def load_zones():
    log(f"reading {ZONE_CSV} ...")
    with open(ZONE_CSV, newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    insert = (
        "INSERT INTO Taxi.Zone (location_id, borough, zone_name, service_zone) "
        "VALUES (?, ?, ?, ?)"
    )
    stored = 0
    for row in rows:
        try:
            location_id = int(row["LocationID"])
        except (TypeError, ValueError):
            log(f"  skipped zone row with unusable LocationID: {row!r}")
            continue
        exec_dml(insert, *sql_params((
            location_id,
            (row.get("Borough") or "").strip() or None,
            (row.get("Zone") or "").strip() or None,
            (row.get("service_zone") or "").strip() or None,
        )))
        stored += 1
    log(f"  Taxi.Zone: {stored:,} rows")
    return stored


def run():
    banner("STAGE 2 -- raw landing")
    assert len(RAW_COLUMNS) == 20, "trip CSV is expected to have 20 columns"
    trips = load_trips()
    zones = load_zones()
    return trips, zones


if __name__ == "__main__":
    run()
