"""The stretch goal: measure six ways of answering the same seven questions.

Run:  python src/compare_approaches.py            everything, then the tables
      python src/compare_approaches.py --arm csv_cold     one arm, as JSON

The brief asks how much data has to move between IRIS and Python, which approach
is easier to develop and maintain, and which one you would want if the data set
grew. Those are answerable rather than debatable, so this script answers them with
numbers.

Six arms, all producing the *same seven answers*:

    pushdown         src/analytics.py — GROUP BY in IRIS over the view
    pushdown_flat    the same SQL over the materialised row-store copy
    pushdown_col     the same SQL over the columnar copy
    pullup           IRIS as a dumb store: SELECT every row into pandas and
                     aggregate client-side
    csv_cold         no database at all: re-parse the 105 MB CSV, then aggregate
    csv_cached       no database, but parse once and reuse a pickled frame

Three decisions make the comparison honest rather than flattering:

**Each arm runs in its own process.** `ru_maxrss` is a high-water mark, so six
arms in one process would all report the peak of the greediest.

**Both sides are allowed to prepare once.** The first version of this script
compared an IRIS *view* — 16 CASE expressions and two joins re-evaluated on every
read — against a pandas frame that had already been built, and unsurprisingly
pandas won. src/build_flat.py exists because of that result. Preparation cost is
not hidden either; it is measured in its own table.

**Cold and warm are reported separately.** The first execution of a query pays
for statement preparation on the IRIS side and for Python's import and allocation
warm-up on the pandas side. Quoting either number alone is a way of choosing the
winner in advance.

Every arm also writes its seven result frames to a temp directory so the driver
can check they *agree*. A speed comparison between implementations that return
different answers is not a comparison — and reproducing the SQL numbers in pandas
turned out to be the genuinely hard part. Two bugs in the pandas arm were found
by this check and by nothing else: pandas reading the borough literally named
'N/A' as a missing value, and a pickle cache with no invalidation quietly serving
answers computed under an older rule.
"""

import json
import os
import pickle
import resource
import subprocess
import sys
import time
import tokenize
import warnings

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pure_python  # noqa: E402  (path juggling has to come first)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join("/tmp", "nyc_taxi_arms")

# The seven questions, named once. Each arm implements them its own way; the
# names are how the driver lines up two arms' answers for comparison.
QUESTIONS = ("busiest_pickup", "busiest_dropoff", "hourly", "weekday",
             "monthly", "od_pairs", "borough_economics")

CSV_BYTES = os.path.getsize(pure_python.TRIPS_CSV)


def _timed(thunk):
    started = time.perf_counter()
    value = thunk()
    return value, time.perf_counter() - started


def _best(thunk, runs=3):
    """Fastest of `runs` executions — the warm number.

    Best-of rather than mean: this machine is a laptop with a browser on it, so
    the distribution has a long right tail made of things that have nothing to do
    with either approach. The minimum is the closest available estimate of what
    the work actually costs.
    """
    best = None
    for _ in range(runs):
        _, secs = _timed(thunk)
        best = secs if best is None else min(best, secs)
    return best


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------

def _iris_arm(table):
    """Push-down against `table`: IRIS aggregates, Python receives answers.

    The same seven SQL statements for all three IRIS arms — analytics.use_table()
    swaps the FROM clause. Running three storage layouts through one
    implementation is the only way the timings compare like with like.
    """
    import analytics
    analytics.use_table(table)

    started = time.perf_counter()
    # "Setup" here is one connection: 15 ms. There is nothing to parse, because
    # the parse happened once, at load time, in a different week.
    analytics.read(f"SELECT COUNT(*) AS n FROM {table}")
    setup = time.perf_counter() - started

    def answer():
        return {
            "busiest_pickup": analytics.busiest_zones("pickup", top=15),
            "busiest_dropoff": analytics.busiest_zones("dropoff", top=15),
            "hourly": analytics.hourly_activity(),
            "weekday": analytics.weekday_activity(),
            "monthly": analytics.monthly_activity(),
            "od_pairs": analytics.od_pairs(top=15),
            "borough_economics": analytics.borough_economics(),
        }

    one_question = lambda: analytics.busiest_zones("pickup", top=15)
    _, cold = _timed(one_question)
    warm = _best(one_question)
    results, allq = _timed(answer)
    rows = sum(len(frame) for frame in results.values())
    return dict(setup=setup, one_cold=cold, one_warm=warm, all_questions=allq,
                rows_into_python=rows, bytes_read=0), results


def arm_pushdown():
    return _iris_arm("Taxi.TripEnriched")


def arm_pushdown_flat():
    return _iris_arm("Taxi.TripFlat")


def arm_pushdown_col():
    return _iris_arm("Taxi.TripFlatColumnar")


def arm_pullup():
    """IRIS as a bucket: pull all 787,060 rows, do everything in pandas.

    This is the arm the stretch goal is really about, and the one that looks most
    reasonable in a code review — a single SELECT, then familiar pandas. The cost
    is invisible in the source: 20 columns × 787,060 rows crossing a socket and
    being rebuilt as Python objects, so that the 96 rows of the answer can be
    computed on this side of the wire instead of that one.

    The Decimal conversion is not incidental either. NUMERIC(12,2) columns arrive
    as `decimal.Decimal` — except where the stored value is whole, which arrives
    as `int` — so every money column lands as an object column that pandas will
    not mean() or sum(). Client-side aggregation has to undo the database's exact
    numeric type in order to use its own float64 one.
    """
    from iris_conn import connect
    started = time.perf_counter()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM Taxi.Trip")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.execute("SELECT location_id, borough, zone, service_zone "
                    "FROM Taxi.Zone")
        zone_rows = cur.fetchall()

    frame = pd.DataFrame.from_records(rows, columns=columns)
    del rows
    for column in pure_python.MONEY + ("trip_distance", "duration_min"):
        if frame[column].dtype == object:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    zones = pd.DataFrame(zone_rows, columns=["location_id", "borough", "zone",
                                             "service_zone"])
    frame = pure_python.enrich(frame, zones)
    setup = time.perf_counter() - started
    return _pandas_metrics(frame, setup, len(frame) + len(zones), 0)


def arm_csv_cold():
    """No database. Re-parse 105 MB, then aggregate — the honest no-IRIS route."""
    started = time.perf_counter()
    frame = pure_python.build(use_cache=False)
    setup = time.perf_counter() - started
    return _pandas_metrics(frame, setup, len(frame), CSV_BYTES)


def arm_csv_cached():
    """No database, but parse once. The strongest form of the no-IRIS argument.

    The cache stands in for what a database gives you for free: a parsed, typed,
    enriched copy that survives between questions. Pickle rather than Parquet only
    because pyarrow is not installed here; Parquet would be faster and would let
    you read one column at a time, which is the point at which this arm starts
    turning into a column store with none of the guarantees.
    """
    started = time.perf_counter()
    frame = pure_python.build(use_cache=True)
    setup = time.perf_counter() - started
    return _pandas_metrics(frame, setup, len(frame),
                           os.path.getsize(pure_python.CACHE))


def _pandas_metrics(frame, setup, rows_moved, bytes_read):
    one_question = lambda: pure_python.busiest_zones(frame, "pickup", top=15)
    _, cold = _timed(one_question)
    warm = _best(one_question)
    results, allq = _timed(lambda: _pandas_answers(frame))
    return dict(setup=setup, one_cold=cold, one_warm=warm, all_questions=allq,
                rows_into_python=rows_moved, bytes_read=bytes_read), results


def _pandas_answers(frame):
    return {
        "busiest_pickup": pure_python.busiest_zones(frame, "pickup", top=15),
        "busiest_dropoff": pure_python.busiest_zones(frame, "dropoff", top=15),
        "hourly": pure_python.hourly_activity(frame),
        "weekday": pure_python.weekday_activity(frame),
        "monthly": pure_python.monthly_activity(frame),
        "od_pairs": pure_python.od_pairs(frame, top=15),
        "borough_economics": pure_python.borough_economics(frame),
    }


ARMS = {
    "pushdown": arm_pushdown,
    "pushdown_flat": arm_pushdown_flat,
    "pushdown_col": arm_pushdown_col,
    "pullup": arm_pullup,
    "csv_cold": arm_csv_cold,
    "csv_cached": arm_csv_cached,
}

LABELS = {
    "pushdown": "push-down, IRIS view",
    "pushdown_flat": "push-down, materialised (row)",
    "pushdown_col": "push-down, materialised (columnar)",
    "pullup": "pull-up, all rows -> pandas",
    "csv_cold": "no IRIS, cold (re-parse CSV)",
    "csv_cached": "no IRIS, cached (pickled frame)",
}


# ---------------------------------------------------------------------------
# Lines of code, counted rather than estimated
# ---------------------------------------------------------------------------

def code_lines(path):
    """Physical lines that are neither blank, comment, nor docstring.

    Counted with tokenize instead of grep because this project comments heavily
    and a raw line count would say more about the prose than about the work. A
    string that is the only thing on its logical line is a docstring; anything
    else counts once per line it touches.
    """
    with open(path, "rb") as f:
        tokens = list(tokenize.tokenize(f.readline))
    lines, only_string_so_far = set(), True
    for token in tokens:
        if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                          tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                          tokenize.ENDMARKER):
            if token.type == tokenize.NEWLINE:
                only_string_so_far = True
            continue
        if token.type == tokenize.STRING and only_string_so_far:
            continue
        only_string_so_far = False
        lines.update(range(token.start[0], token.end[0] + 1))
    return len(lines)


# Everything each route needs to get from the two supplied CSVs to the seven
# answers. The IRIS side legitimately includes its loaders: that code runs once,
# but it is code that exists and has to be maintained. build_flat.py is optional
# on that side, which is why it is listed separately.
IRIS_FILES = ("iris_conn.py", "schema.py", "load_zones.py", "load_trips.py",
              "build_quality.py", "build_enriched.py", "analytics.py")
IRIS_OPTIONAL = ("build_flat.py",)
PURE_FILES = ("pure_python.py",)


def loc_table():
    src = os.path.join(ROOT, "src")
    rows = []
    for label, files in (("IRIS route", IRIS_FILES),
                         ("IRIS route (optional)", IRIS_OPTIONAL),
                         ("pandas route", PURE_FILES)):
        for name in files:
            rows.append({"route": label, "file": name,
                         "code lines": code_lines(os.path.join(src, name))})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Answer agreement
# ---------------------------------------------------------------------------

def compare_answers(left, right):
    """Do two arms give the same seven answers? Reported per question.

    Deliberately not assert_frame_equal: what matters is not that the frames are
    identical objects but that an analyst reading either one reaches the same
    conclusion. So this reports whether the key columns line up in the same order
    and the worst absolute disagreement across the numeric columns.
    """
    rows = []
    for question in QUESTIONS:
        a, b = left.get(question), right.get(question)
        if a is None or b is None:
            rows.append({"question": question, "verdict": "missing"})
            continue
        if len(a) != len(b):
            rows.append({"question": question,
                         "verdict": f"row count {len(a)} vs {len(b)}"})
            continue
        text_cols = [c for c in a.columns if a[c].dtype == object]
        num_cols = [c for c in a.columns
                    if c in b.columns and c not in text_cols]
        same_keys = all(
            a[c].reset_index(drop=True).equals(b[c].reset_index(drop=True))
            for c in text_cols)
        worst, worst_col = 0.0, ""
        for c in num_cols:
            delta = (pd.to_numeric(a[c], errors="coerce").reset_index(drop=True)
                     - pd.to_numeric(b[c], errors="coerce").reset_index(drop=True)
                     ).abs().max()
            if pd.notna(delta) and delta > worst:
                worst, worst_col = float(delta), c
        rows.append({
            "question": question,
            "rows": len(a),
            "keys identical": same_keys,
            "largest numeric gap": round(worst, 4),
            "in column": worst_col or "-",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Child-process entry point
# ---------------------------------------------------------------------------

def run_one(name):
    """Run a single arm, print its metrics as JSON, pickle its answers.

    ru_maxrss is bytes on macOS and kilobytes on Linux — a documented trap rather
    than a rumour, so the unit is decided by platform instead of by hoping. The
    baseline is reported separately because ~60 MB of the peak is the interpreter,
    pandas and numpy: real, but not attributable to the approach.
    """
    warnings.filterwarnings("ignore")
    scale = 1 if sys.platform == "darwin" else 1024
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    metrics, results = ARMS[name]()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, f"{name}.pkl"), "wb") as f:
        pickle.dump(results, f)

    metrics.update(arm=name, peak_mb=round(peak / 1e6, 1),
                   baseline_mb=round(before / 1e6, 1))
    print("METRICS " + json.dumps(metrics))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def spawn(name, progress=print):
    progress(f"  {name} …")
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--arm", name],
        capture_output=True, text=True, cwd=ROOT)
    for line in proc.stdout.splitlines():
        if line.startswith("METRICS "):
            return json.loads(line[len("METRICS "):])
    print(proc.stdout[-2000:])
    print(proc.stderr[-2000:])
    raise SystemExit(f"arm {name} produced no metrics")


def prepare(rebuild=True):
    """Do both sides' one-time preparation, and time whatever it actually built.

    `rebuild=True` (the script default) tears down and rebuilds everything, so
    that every number in the preparation table was measured on this machine in
    this run. `rebuild=False` (what the notebook uses) only builds what is
    missing, and returns timings only for what it built — reporting a cache *load*
    under the heading "one-time preparation" would be the same kind of quiet lie
    the rest of this file is trying to avoid.

    Preparation is the expensive part of the script, and it is the point: "which
    approach do you prefer" is largely a question about what you are willing to
    pay once against what you pay per question.
    """
    import build_flat
    timings = {}
    missing = [n for n, _ in build_flat.TARGETS if not build_flat.table_exists(n)]
    if rebuild or missing:
        timings.update(build_flat.build())
        if not build_flat.verify():
            raise SystemExit("materialised copies disagree with the view")

    if rebuild and os.path.exists(pure_python.CACHE):
        os.remove(pure_python.CACHE)
    fresh = not os.path.exists(pure_python.CACHE)
    started = time.perf_counter()
    pure_python.build(use_cache=True)
    if fresh:
        timings["pandas pickle cache"] = time.perf_counter() - started

    return pd.DataFrame([
        {"one-time preparation": key, "seconds": round(secs, 2)}
        for key, secs in timings.items()
    ])


def run_all(rebuild=True, progress=print):
    """Run every arm and return the frames, so the notebook shows live numbers.

    Returns a dict of DataFrames: `preparation`, `timings`, `agreement`,
    `agreement_detail`, `code`. main() prints them; the notebook renders them and
    charts two of the columns. Same code path either way, which is the only way
    the notebook's table cannot drift from the script's.
    """
    progress("Preparing both sides (materialised IRIS copies, pandas cache) …")
    prep = prepare(rebuild=rebuild)

    progress("Running each arm in its own process, for honest peak memory:")
    metrics = {name: spawn(name, progress=progress) for name in ARMS}

    timings = pd.DataFrame([{
        "approach": LABELS[name],
        "setup (s)": round(m["setup"], 2),
        "1 question cold (s)": round(m["one_cold"], 3),
        "1 question warm (s)": round(m["one_warm"], 3),
        "7 questions (s)": round(m["all_questions"], 2),
        "rows into Python": m["rows_into_python"],
        "MB off disk": round(m["bytes_read"] / 1e6, 1),
        "peak RAM (MB)": m["peak_mb"],
    } for name, m in metrics.items()])

    loaded = {}
    for name in ARMS:
        with open(os.path.join(RESULTS, f"{name}.pkl"), "rb") as f:
            loaded[name] = pickle.load(f)

    rows = []
    for other in [n for n in ARMS if n != "pushdown"]:
        frame = compare_answers(loaded["pushdown"], loaded[other])
        worst = float(frame["largest numeric gap"].max())
        keys = bool(frame["keys identical"].all())
        rows.append({"arm": LABELS[other], "same keys": keys,
                     "largest numeric gap": worst,
                     "verdict": "identical" if keys and worst == 0
                                else "DIFFERS"})

    return {
        "preparation": prep,
        "timings": timings,
        "agreement": pd.DataFrame(rows),
        "agreement_detail": compare_answers(loaded["pushdown"], loaded["pullup"]),
        "code": loc_table(),
    }


def main():
    if "--arm" in sys.argv:
        return run_one(sys.argv[sys.argv.index("--arm") + 1])

    frames = run_all(rebuild=True)

    for title, key in (("ONE-TIME PREPARATION", "preparation"),
                       ("SIX WAYS TO ANSWER THE SAME SEVEN QUESTIONS",
                        "timings"),
                       ("DO THEY AGREE?  every arm vs push-down over the view",
                        "agreement"),
                       ("PER QUESTION: push-down vs pull-up",
                        "agreement_detail"),
                       ("CODE TO MAINTAIN", "code")):
        print()
        print("=" * 108)
        print(title)
        print("=" * 108)
        print(frames[key].to_string(index=False))

    print()
    print(frames["code"].groupby("route")["code lines"].sum().to_string())
    print("\n(Loading the CSV into Taxi.Trip is a further one-time cost on the "
          "IRIS side, measured by src/load_trips.py.)")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
