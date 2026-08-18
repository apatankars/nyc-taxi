"""Materialise Taxi.TripEnriched, in row storage and in columnar storage.

Run:  python src/build_flat.py     (after build_quality.py and build_enriched.py)

Why this file exists: the first run of src/compare_approaches.py made the
database look bad. Push-down took 0.38–0.78 s per question while pandas answered
the same question in under 0.10 s from a frame it had already built. That is a
real measurement and it has a real cause — `Taxi.TripEnriched` is a *view*, so
every read re-evaluates 16 CASE expressions, two joins against Taxi.Zone and
four DATEPART calls across all 787,060 rows, while the pandas arm did that work
once and kept the result.

So the honest comparison is not view-vs-frame. It is "each side allowed to
prepare its data once":

    Taxi.TripEnriched        the view — recomputed on every read
    Taxi.TripFlat           the same columns, stored (row storage)
    Taxi.TripFlatColumnar   the same columns, stored column-wise

Columnar is the interesting one for this workload, and it is the thing the
stretch goal is really pointing at. An aggregate reads a handful of columns out
of twenty-six; row storage has to walk whole rows to find them, while columnar
reads only the columns named and can work on them in vectorised batches.

Both are filled by `INSERT INTO … SELECT * FROM Taxi.TripEnriched`, so the rule
definitions in build_quality.py remain the only place "suspect" is defined. The
cost of materialising is that these copies *can* go stale — the same trap the
pandas cache fell into in pure_python.py — so `verify()` re-checks them against
the view rather than trusting the build.

Two IRIS specifics, both of which cost real time here:

**Storage clause placement.** It goes at the *end*, after the SELECT:

    CREATE TABLE x AS SELECT * FROM y WITH STORAGETYPE = COLUMNAR   -- works
    CREATE TABLE x WITH STORAGETYPE = COLUMNAR AS SELECT * FROM y   -- syntax error

and it is confirmable rather than assumed:

    SELECT _Default FROM %Dictionary.CompiledParameter
     WHERE parent = 'Taxi.TripFlatColumnar' AND Name = 'STORAGEDEFAULT'
    -> 'columnar'

**`%EXACT` does not survive materialisation.** This is why the DDL below is
generated column by column instead of using the one-line CREATE TABLE AS SELECT
that this file started with. The view already selects `%EXACT(pu.borough)`, and
that is enough for anything reading the view — but copying the result into a new
VARCHAR column gives that column the default SQLUPPER collation again, so
`GROUP BY pu_borough` against the copy returns 'MANHATTAN'. Nothing errors and no
number changes; every label in every chart simply starts shouting.

It was found by the agreement check in compare_approaches.py, which flagged the
row-store copy as disagreeing with the view on text keys — and, oddly, the
columnar copy did *not* have the problem, so testing one layout would have missed
it. The fix is to declare the copy's text columns `COLLATE EXACT`, which puts the
decision in the schema instead of requiring every future query to remember
%EXACT.
"""

import sys
import time

from iris_conn import execute, query

SOURCE = "Taxi.TripEnriched"
FLAT = "Taxi.TripFlat"
COLUMNAR = "Taxi.TripFlatColumnar"

# name -> (row storage, columnar storage)
TARGETS = ((FLAT, False), (COLUMNAR, True))


def source_columns():
    """The view's columns and types, read from the catalogue rather than retyped.

    Deriving the DDL from INFORMATION_SCHEMA means adding a column to
    build_enriched.py does not silently leave it out of the materialised copies —
    which is the failure mode of every hand-maintained parallel column list.
    """
    _, rows = query(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
        "NUMERIC_PRECISION, NUMERIC_SCALE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        SOURCE.split("."))
    return [tuple(row) for row in rows]


def column_ddl(name, data_type, char_len, precision, scale):
    """One column declaration, with COLLATE EXACT on anything textual."""
    if data_type == "varchar":
        return f"{name} VARCHAR({char_len}) COLLATE EXACT"
    if data_type == "numeric":
        return f"{name} NUMERIC({precision},{scale})"
    # trip_id is the view's %ID; stored as a plain BIGINT column here rather than
    # as an identity, because these copies are read-only projections.
    return f"{name} {data_type.upper()}"


def create_ddl(target, columnar):
    body = ",\n    ".join(column_ddl(*column) for column in source_columns())
    storage = " WITH STORAGETYPE = COLUMNAR" if columnar else ""
    return f"CREATE TABLE {target} (\n    {body}\n){storage}"


def insert_ddl(target):
    names = ", ".join(column[0] for column in source_columns())
    return f"INSERT INTO {target} ({names}) SELECT {names} FROM {SOURCE}"


def table_exists(qualified):
    schema, table = qualified.split(".")
    _, rows = query(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, table])
    return rows[0][0] > 0


def storage_type(qualified):
    """'columnar' or 'row', read back from the dictionary rather than assumed."""
    _, rows = query(
        "SELECT _Default FROM %Dictionary.CompiledParameter "
        "WHERE parent = ? AND Name = 'STORAGEDEFAULT'", [qualified])
    return (rows[0][0] if rows and rows[0][0] else "row")


def build():
    """Drop and rebuild both materialised copies, reporting what each one cost."""
    timings = {}
    for name, columnar in TARGETS:
        if table_exists(name):
            execute(f"DROP TABLE {name}")
        started = time.perf_counter()
        execute(create_ddl(name, columnar))
        execute(insert_ddl(name))
        timings[name] = time.perf_counter() - started
        print(f"built {name:<26} in {timings[name]:>6.2f}s  "
              f"storage={storage_type(name)}")
    return timings


def verify():
    """Check each copy still matches the view it was made from.

    Row count and suspect count, because those are the two numbers every
    downstream answer depends on. A materialised copy that has drifted from its
    source produces plausible answers, which is worse than producing none.
    """
    _, rows = query(f"SELECT COUNT(*), SUM(is_suspect) FROM {SOURCE}")
    expected = rows[0]
    print(f"\n{SOURCE:<26} {expected[0]:>9,} rows  "
          f"{expected[1]:>7,} suspect   (the definition)")
    ok = True
    for name, _ in TARGETS:
        _, rows = query(f"SELECT COUNT(*), SUM(is_suspect) FROM {name}")
        got = rows[0]
        match = "matches" if tuple(got) == tuple(expected) else "DRIFTED"
        ok = ok and match == "matches"
        print(f"{name:<26} {got[0]:>9,} rows  {got[1]:>7,} suspect   {match}")
    return ok


def main():
    build()
    if not verify():
        print("\nA materialised copy disagrees with the view. Rebuild before "
              "trusting any timing taken against it.")
        return 1
    print("\nNow: python src/compare_approaches.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
