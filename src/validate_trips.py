"""Validate the trip CSV against src/schema.py before loading anything.

Run:  python src/validate_trips.py

Touches no database. It answers one question — "which rows can be typed, and for
the ones that cannot, exactly why" — and answers it as a report you can read,
before a 787,060-row load has a chance to fail two thirds of the way through.

Deliberately *not* a quality check. Rows with a zero distance or a negative fare
pass here, because they are structurally fine. Plausibility is the quality view's
job, downstream of the load.
"""

import csv
import os
import sys
from collections import Counter, defaultdict

from schema import COLUMNS, coerce_row

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "2023_Green_Taxi_Trip_Data.csv",
)

SAMPLES_PER_REASON = 3


def validate(path=CSV_PATH, samples_per_reason=SAMPLES_PER_REASON):
    """Stream the file and tally structural failures.

    Streaming, not pandas: the point is to behave exactly as the loader will,
    row by row, through the same schema.coerce_row(). A vectorised check could
    disagree with the loader in edge cases, which would defeat the purpose.
    """
    total = 0
    bad_rows = 0
    reasons = Counter()
    samples = defaultdict(list)
    header_mismatch = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        expected = {c.csv_name for c in COLUMNS}
        present = set(reader.fieldnames or ())
        header_mismatch = [
            ("missing from file", sorted(expected - present)),
            ("present but unused", sorted(present - expected)),
        ]

        for line_no, row in enumerate(reader, start=2):  # line 1 is the header
            total += 1
            _, errors = coerce_row(row)
            if errors:
                bad_rows += 1
                for err in errors:
                    reasons[err.split(":")[0] + ": " + err.split(":", 1)[1].strip()] += 1
                    key = err
                    if len(samples[key]) < samples_per_reason:
                        samples[key].append((line_no, row.get(err.split(":")[0])))

    return {
        "total": total, "bad_rows": bad_rows, "reasons": reasons,
        "samples": samples, "header": header_mismatch,
    }


def main():
    print(f"validating {os.path.basename(CSV_PATH)} against schema.py ...\n")
    result = validate()
    total, bad = result["total"], result["bad_rows"]

    print("=" * 72)
    print("HEADER")
    print("=" * 72)
    for label, cols in result["header"]:
        print(f"  {label:<22} {cols if cols else 'none'}")
    print("  (ehail_fee is expected under 'present but unused' — empty in every "
          "row,\n   so schema.py has no column for it)")

    print()
    print("=" * 72)
    print("STRUCTURAL FAILURES BY REASON")
    print("=" * 72)
    if not result["reasons"]:
        print("  none — every row can be typed")
    else:
        for reason, n in result["reasons"].most_common():
            print(f"  {n:>7,}  {reason}")
            for line_no, raw in result["samples"].get(reason, []):
                print(f"           line {line_no}: {raw!r}")

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    good = total - bad
    print(f"  rows read       {total:>9,}")
    print(f"  loadable        {good:>9,}  ({good / total * 100:.4f}%)")
    print(f"  not loadable    {bad:>9,}  ({bad / total * 100:.4f}%)")
    print()
    print("  Reminder: 'loadable' means structurally typeable, not clean.")
    print("  Profiling puts 49,329 rows (6.27%) in scope for the quality view.")

    # Non-zero exit if anything failed, so this can gate the load in a script.
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
