"""Load taxi_zone_lookup into Taxi.Zone.

Run:  python src/load_zones.py

Only 265 rows, so the loading strategy barely matters for speed. It is worth
doing first anyway: it exercises the whole path — create table, insert, verify —
against a data set small enough that a mistake is obvious rather than buried in
787,000 rows.

Client-side insert (not server-side LOAD DATA) is deliberate here, for one
reason: this file has a UTF-8 byte-order mark. Python can strip it with an
encoding argument; getting LOAD DATA to ignore it is more trouble than the
265 rows are worth.
"""

import csv
import os

from iris_conn import connect, execute, query

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "taxi_zone_lookup(in).csv",
)

# location_id is the natural key and the join target for every enrichment query,
# so it is the primary key rather than letting IRIS assign a surrogate %ID.
# Widths are roughly 2x the observed maxima (borough 13, zone 45, service 11).
DDL = """
CREATE TABLE Taxi.Zone (
    location_id   INTEGER NOT NULL PRIMARY KEY,
    borough       VARCHAR(50),
    zone          VARCHAR(100),
    service_zone  VARCHAR(50)
)
"""


def read_zones():
    """Parse the lookup CSV into insertable tuples.

    Two file quirks are handled here, and both are silent corruption if missed:

    encoding="utf-8-sig"
        The file starts with a UTF-8 BOM (EF BB BF). Read as plain utf-8, the
        first column name becomes '\\ufeffLocationID', so row["LocationID"]
        raises KeyError while the header *looks* correct in any editor.

    newline=""
        The file has CRLF line endings. This is what the csv module documents as
        required; without it the trailing \\r can end up inside the last field.
    """
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        return [
            (
                int(row["LocationID"]),
                row["Borough"].strip(),
                row["Zone"].strip(),
                row["service_zone"].strip(),
            )
            for row in csv.DictReader(f)
        ]


def table_exists(schema, table):
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        [schema, table],
    )
    return rows[0][0] > 0


def main():
    zones = read_zones()
    print(f"parsed {len(zones)} zones from {os.path.basename(CSV_PATH)}")

    # Rebuild from scratch every run, so this script is safe to re-run while
    # you are still iterating on the schema.
    if table_exists("Taxi", "Zone"):
        execute("DROP TABLE Taxi.Zone")
        print("dropped existing Taxi.Zone")
    execute(DDL)
    print("created Taxi.Zone")

    with connect() as conn:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO Taxi.Zone (location_id, borough, zone, service_zone) "
            "VALUES (?,?,?,?)",
            zones,
        )
        conn.commit()

    _, rows = query("SELECT COUNT(*) FROM Taxi.Zone")
    print(f"Taxi.Zone now holds {rows[0][0]} rows")

    _, sample = query(
        "SELECT borough, COUNT(*) FROM Taxi.Zone GROUP BY borough ORDER BY 2 DESC"
    )
    print("zones per borough:")
    for borough, n in sample:
        print(f"  {borough:<15} {n}")


if __name__ == "__main__":
    main()
