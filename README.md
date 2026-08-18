# NYC Green Taxi Trip Insights — Python + InterSystems IRIS

A prototype that loads a full year of NYC green-taxi trips (787,060 rows) into
InterSystems IRIS Community Edition, validates and enriches them, and serves the
analysis through a Flask dashboard that IRIS itself hosts.

The organising idea: **IRIS is the compute engine, not a file cabinet.** Every
aggregate — GROUP BY, AVG, ordering, row limits, bitmask decoding, even the
re-flagging loop — runs inside the database, and what crosses into Python is a
finished table of tens of rows. The dashboard has a panel that measures exactly
what that choice is worth ([Push-down](#the-stretch-goal-push-down-comparison)).

---

## Table of contents

- [Quick start](#quick-start)
- [The data model](#the-data-model-three-layers)
- [Pipeline, stage by stage](#pipeline-stage-by-stage)
  - [Stage 1 — schema](#stage-1--schema-stage1_schemapy)
  - [Stage 2 — raw landing](#stage-2--raw-landing-stage2_load_rawpy)
  - [Stage 3 — cast to typed](#stage-3--cast-to-typed-stage3_castpy)
  - [Stage 4 — logical validation](#stage-4--logical-validation-stage4_flagpy)
  - [Stage 5 — the application layer](#stage-5--the-application-layer-stage5_procspy-stage5_webpy)
- [The flag policy](#the-flag-policy-the-part-that-changes-the-answers)
- [User workflows](#user-workflows)
- [The stretch goal: push-down comparison](#the-stretch-goal-push-down-comparison)
- [How this maps to the project guide](#how-this-maps-to-the-project-guide)
- [Where IRIS features are used](#where-iris-features-are-used)
- [Running everything](#running-everything)
- [File map](#file-map)

---

## Quick start

```bash
./run.sh
```

That is the whole thing. It starts IRIS Community Edition (superserver 1972, web
52773), waits for it to accept SQL, runs every stage that needs running, and opens

    http://localhost:52773/taxi/        sign in as _SYSTEM / SYS

The first run builds from the CSVs and takes a few minutes. Re-running is cheap:
stages 1–3 are skipped once `Taxi.Trip` is populated, because reloading 787,060
rows to look at a dashboard is a waste of several minutes.

```bash
./run.sh                  # build if needed, then serve
./run.sh --reload         # drop and rebuild everything from the CSVs
./run.sh --reload 20000   # same, but only cast the first 20,000 raw rows
./run.sh --flags          # re-apply the quality rules only, after editing rules.py
```

Inside the container it is one script, and you can call it directly if the
container is already up:

```bash
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py
```

Editing anything under `src/` needs **no** re-run — `taxi_app.py` reloads the
project modules on the next request. Re-run only after changing the schema (stage
1) or the web application's registration (`stage5_web.py`).

Terminal-only alternative, no browser:

```bash
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py
```

Front-end checks (macOS `jsc`, nothing to install):

```bash
tests/frontend/run.sh
```

---

## The data model: three layers

Everything else follows from this shape, so it is worth reading first.

| Table | Types | Purpose |
|---|---|---|
| `Taxi.TripRaw` | every column `VARCHAR(64)` | Landing zone. Nothing about the *content* of a row can make the load fail, because there is no declared type to violate. `'1,571.97'` lands as that exact string. |
| `Taxi.Trip` | fully typed | The working table. Populated by casting `TripRaw`. Keeps **all** 787,060 rows — suspect trips are flagged, never deleted. |
| `Taxi.TripFlag` | `(trip_id, rule_id)` | One row per (trip, violated rule). Normalised, bitmap-indexed on `rule_id`. |
| `Taxi.TripQuality` | rule definitions | Each quality rule as *data*, including its SQL predicate as text. |
| `Taxi.TripReject` | cast failures | Field, offending raw value, and reason — queryable, not logged and lost. |
| `Taxi.Zone` | 265 rows | The taxi-zone lookup: borough, zone name, service zone. |
| `Taxi.TripEnriched` | view | `Trip` LEFT JOINed to `Zone` twice, so pickup and drop-off carry borough/zone names. Every analytics query reads this, not `Trip`. |

Two decisions here do most of the work:

**Landing raw before typing.** A row rejected at the door is a row you can never
report on — and reporting on questionable rows *is* the deliverable. Because the
raw text survives in `TripRaw`, the dashboard's single-trip view can show a flagged
value next to what the file literally said, and "what failed to parse, and why" is
a SQL query rather than a lost log line.

**Flagging instead of deleting.** The clean set is a predicate (`flag_count = 0`),
not a separate table. The analytics workflow and the quality workflow therefore
read the same table, one `WHERE` clause apart.

---

## Pipeline, stage by stage

`run.py` calls the stages in order. Each stage is its own module and can be re-run
alone — stage 4 in particular is designed to be re-run whenever a threshold
changes.

```
CSV ──LOAD DATA──▶ TripRaw ──cast──▶ Trip ──SQL predicates──▶ TripFlag ──▶ views / API / dashboard
      (stage 2)     (VARCHAR)  (3)   (typed)      (4)        (+ flag_mask)         (5)
        ▲
   taxi_zone_lookup ──▶ Zone
```

### Stage 1 — schema (`stage1_schema.py`)

**Purpose:** create the three layers from scratch, seed the rules table, and
install the SQL helper functions. Idempotent: it drops everything first, so a
re-run is a clean rebuild.

**How it uses IRIS:**

- **DDL through `iris.sql.exec`.** All `CREATE TABLE` / `CREATE INDEX` statements
  are issued in-process.
- **Verified drops.** A `DROP` can fail quietly — anything still holding an object
  reference to `Taxi.Zone` pins it — and the next `CREATE` then reports "already
  exists", which points at the wrong problem. The stage queries
  `INFORMATION_SCHEMA.TABLES` afterwards and raises if a table survived.
- **`DDLPKeyNotIDKey = 0`, temporarily.** This is the subtle one. `Trip.pu_location_id`
  is typed as the class `Taxi.Zone`, not `INTEGER`, so it can be traversed with
  arrow syntax (`pu_location_id->borough`). An object-reference column stores the
  *RowID* of its target, so that only works if `Zone`'s RowID **is** its
  `location_id`. By default a DDL primary key is a separate unique constraint and
  IRIS assigns its own RowID. Flipping this instance-wide option to 0 for the
  duration of the build makes `location_id` the IDKEY, so the reference is correct
  by construction. The original value is restored in a `finally`.
- **Bitmap indexes** on `TripFlag(rule_id)`, `Trip(flag_count)`, both location
  columns, `pickup_hour` and `pickup_month`. Cheap over a 787k-row extent and they
  are what make the clean-set filter and every rollup fast. A conventional index
  goes on `pickup_ts`.
- **`CREATE FUNCTION … LANGUAGE PYTHON`.** IRIS SQL has no bitwise operators
  (`&`, `|` and `BITAND` all fail to parse), so `Trip.flag_mask` would be
  unqueryable. `Taxi.mask_names(mask)` is a Python UDF that turns `34` into
  `'distance_reformatted,fare_negative'`. It is **self-tested at build time** —
  a UDF IRIS cannot invoke does not always raise; applied across a scan it can
  return zero rows, which reads as "no trips matched" rather than "broken".
- **`CREATE VIEW Taxi.TripEnriched`** — the enrichment the brief asks for. Two
  notes earned the hard way: `%EXACT()` is required or string columns collate to
  uppercase and `GROUP BY borough` yields `MANHATTAN`; and the view uses explicit
  `LEFT JOIN` rather than arrow traversal, because two arrow hops into the same
  table *inside a view definition* corrupt the view's cached metadata and make
  every aggregate over it — even a bare `COUNT(*)` — fail with
  `<UNDEFINED>isStatInvalid+2^%qTable`.

**Rules as data.** `rules.py` holds the definitions; `seed()` writes them into
`Taxi.TripQuality`. Each rule carries an explicit `bit_position` (declared, not
derived from list order, so inserting a rule cannot renumber stored masks), a
`phase`, a `severity`, and — for rule-phase rules — its SQL predicate as text.
That text column is what makes "is 100 miles the right cutoff?" an `UPDATE` plus a
re-run rather than a code change.

### Stage 2 — raw landing (`stage2_load_raw.py`)

**Purpose:** get both CSVs into IRIS with nothing lost or coerced.

**How it uses IRIS:**

- **`LOAD DATA FROM FILE`** for the 787k trip rows. IRIS streams the file into the
  table server-side, so **no trip data crosses a process boundary** — the container
  mounts `./data` read-only at `/data` and the database reads it directly.
  `USING {"from":{"file":{"header":1}}}` tells the loader the first line is column
  names. `LOAD DATA` pairs the file's header to the target table's columns **by
  name**, which is why `TripRaw`'s columns reuse the CSV's names verbatim
  (`db.RAW_COLUMNS`).
- **`DELETE FROM Taxi.TripRaw` first.** `LOAD DATA` appends, so without this a
  re-run would silently double the table.
- **A verification query, not a hope:** it counts rows whose `trip_distance` still
  contains a comma, confirming the pathological values arrived intact rather than
  mangled.
- **Zones go in through Python** instead — there are only 265, and the lookup
  file's `Zone` column would force an awkward name match against `LOAD DATA`'s
  header-pairing rule. Inserted with `?` parameter binding.

### Stage 3 — cast to typed (`stage3_cast.py`)

**Purpose:** `TripRaw` → `Trip`. **Only schema validation happens here:** can this
text become the declared type? A failure means the value is unstorable, so the
column becomes NULL and the reason lands in `TripReject`. No business opinion is
applied — `-8.50` is a perfectly valid `NUMERIC` and passes straight through, to be
judged by stage 4.

Three observations can *only* be made at this stage, because they depend on the
original text, which stops existing once it is typed:

| Observation | Why it has to happen here |
|---|---|
| `distance_reformatted` | `'1,571.97'` arrived formatted for display. The separator is stripped to store it, but the fact is recorded — normalising silently would destroy the evidence linking the formatting to the absurd magnitude. |
| `meter_metadata_missing` | Five fields (`passenger_count`, `RatecodeID`, `store_and_fwd_flag`, `payment_type`, `congestion_surcharge`) are absent *as a unit* — a feed lacking meter data. Recorded once per row, because that is a testable hypothesis; five separate NULLs say nothing. |
| `cast_failed` | Something could not be coerced at all. |

Nothing is imputed. Filling in a median would manufacture data and bury the most
interesting structural fact in the file.

**How it uses IRIS:**

- **`iris.sql.prepare()` — prepare once, execute many.** The loader pushes 787,060
  rows through a prepared `INSERT`, so the SQL is parsed once instead of per row.
  This is embedded-only and `db.prepare()` says so loudly: the same pattern over
  DB-API would be 787,060 round trips.
- **`iris.tstart()` / `iris.tcommit()` / `iris.trollback()`, one transaction per
  50,000-row chunk.** Without it every `INSERT` commits on its own and the journal
  write dominates the runtime. A failure rolls the chunk back.
- **Chunked reads by RowID** (`WHERE ID > ? AND ID <= ?`) rather than one giant
  result set.
- **`db.sql_params()`** translates Python `None` into `''`, which is what IRIS reads
  as NULL for `INTEGER`, `NUMERIC`, `VARCHAR` and `TIMESTAMP` alike — neither
  `iris.sql.exec` nor a prepared statement accepts `None`.

**Derived columns** (`duration_min`, `implied_mph`, `pickup_hour`, `pickup_month`,
`pickup_dow`) are computed here and *stored*, so the compound rules in stage 4 stay
plain SQL predicates.

**Timestamps are parsed in Python, deliberately.** The file is
`MM/DD/YYYY hh:mm:ss AM/PM`, and AM/PM is the one silent-corruption risk in the
dataset: get it wrong and every timestamp is still valid and storable, with half of
them off by twelve hours, wrecking the entire hour-of-day analysis. Python's
`%I`/`%p` is explicit and testable, and `self_test()` runs at the top of every
execution asserting that `12:26 AM` → hour 0, `12:26 PM` → hour 12, `06:40 PM` →
hour 18, plus the separator handling. A silent failure mode gets a loud guard.

### Stage 4 — logical validation (`stage4_flag.py`)

**Purpose:** the trip-quality workflow. Everything is typed now, so every rule is
just a SQL predicate.

**How it uses IRIS — this is the most set-based stage:**

- **One `INSERT … SELECT` per rule**, built from the predicate text stored in
  `Taxi.TripQuality`. No trip data crosses into Python: **the rule text goes in, a
  count comes out.**
- **`flag_count` and `flag_mask` denormalised back onto `Trip`** by two correlated
  subqueries in a single `UPDATE`. Two representations of one truth:
  `flag_count = 0` is a single bitmap-indexed predicate (the filter every analytics
  query starts from), while `flag_mask` makes "which *combination* of rules" a
  one-column question via `Taxi.mask_names`. `SUM(POWER(2, bit))` is a valid
  bitwise OR here because `TripFlag`'s primary key guarantees a rule appears at most
  once per trip, so no bit is double-counted.
- **`TUNE TABLE`** on `Trip`, `TripFlag` and `Zone` — collecting optimiser
  statistics is cheap here and every downstream aggregate benefits.
- **The view is rebuilt after tuning, then verified in three shapes** by
  `_verify_view()`: a row read (touches both reference columns), a `COUNT(*)`
  checked against `Trip`'s own count (catches LEFT JOINs that duplicate or drop
  rows), and a `GROUP BY` (what every workflow actually runs). They fail
  independently — the `isStatInvalid` defect kills the aggregate while row reads
  still work, so a single-row smoke test would have passed the whole time the
  analytics were unrunnable.

**Re-runnable by design.** Only `phase='rule'` flags are cleared and recomputed;
`phase='ingest'` flags survive, because the raw text behind them no longer exists.
That split is what lets an analyst change a threshold and re-run in seconds rather
than reloading 787,060 rows.

The 16 rules split by `severity`:

- **`invalid`** — the value cannot describe a real trip: non-positive distance or
  duration, pickup outside 2023, implied speed above 80 mph, a failed cast.
- **`unusual`** — suspicious but possible: negative fares and totals (almost
  certainly refunds or voids — *real business events*), fares above \$500, trips
  over 100 miles or six hours, zero passengers, sub-1-mph crawls, over \$50 per
  mile.

That distinction matters because "unusual" rows are still evidence. Which brings
us to the part of this project that changes the numbers most.

### Stage 5 — the application layer (`stage5_procs.py`, `stage5_web.py`)

`run.py` calls both as its last two steps; both are re-runnable on their own.

**`stage5_procs.py` — `Taxi.apply_rules_now()`.** Stage 4's work is roughly forty
statements. Driven from a client that is forty round trips. Wrapped in a
`CREATE FUNCTION … LANGUAGE PYTHON`, it is **one call** that runs inside the IRIS
process and returns a one-line summary, so re-flagging after editing a rule is
`SELECT Taxi.apply_rules_now()` from anywhere that can issue SQL. The function body `import`s
`stage4_flag` rather than restating it, so there is no second implementation to
keep in step. It is a scalar *function* rather than a procedure because that is the
form any SQL client can invoke and read a return value from with a plain `SELECT`;
a procedure would need `CALL` plus driver-specific output handling. Like stage 1's
UDF, it self-tests by calling itself once.

**`stage5_web.py` — IRIS hosts the Flask app.** IRIS 2024.2+ can serve a WSGI
application: point `Security.Applications` at a directory, module and callable and
the private web server on 52773 serves it. The Flask handlers then run **inside the
IRIS process under Embedded Python**, so `iris.sql.exec` in a request handler is an
in-process call — an aggregate over 787,060 rows crosses the boundary as a few
summary rows, with no DB-API connection and no per-query round trip.

Four settings in there are non-obvious and each cost real time:

| Property | Why |
|---|---|
| `DispatchClass = %SYS.Python.WSGI` | The setting the Management Portal fills in for you, and the reason a hand-built WSGI app 404s. Without it nothing answers — no error, just a 404. |
| `ServeFiles = 0` | With the default, the CSP gateway claims every URL that *looks* like a static file and serves it from the application's physical path, which for a WSGI app is empty. Result: `/taxi/` and `/taxi/api/*` answer perfectly while the `.css` and `.js` 404 empty, so the page loads unstyled with no JavaScript and nothing in the log looks wrong. |
| `Recurse = 1` | So `/taxi/api/...` reaches the app instead of 404ing at the gateway. |
| `AutheEnabled = 32` | Password authentication only. No unauthenticated bit, no `MatchRoles` grant — every request runs as the logged-in user with exactly that user's privileges on `Taxi.*`, which is what bounds every query the dashboard issues. |

The stage runs `SetNamespace("%SYS")` for the `Security.Applications` calls and puts
the namespace back in a `finally` — leaving the process in `%SYS` would make every
later `Taxi.*` query fail with "table not found".

**`WSGIDebug = 1` is not enough on its own,** which is the fifth thing that cost
real time. It re-imports `taxi_app.py` when that file changes — but not the modules
`taxi_app` imports. The IRIS process outlives any number of edits, and `sys.modules`
keeps whatever `dashboard.py` was the first time anything imported it, so editing
`dashboard.py` and reloading the browser serves the old code. The failure is silent
in the worst way: the API answers `200` with a payload from a version that no longer
exists on disk. Here it renamed a key in `/api/meta`, which left the page's header
and filter bar populated and *every tab blank*, with nothing in the log and no error
in the browser.

So `taxi_app.py` reloads its own dependencies — `db`, `rules`, `analytics`,
`dashboard`, in that dependency order — once at import and then whenever a
`before_request` hook notices an `mtime` change. Four `os.stat` calls in front of
handlers that run multi-hundred-millisecond aggregates, and always on, because a
reload flag you have to remember to set is a flag that is off when it matters.

---

## The flag policy (the part that changes the answers)

`analytics.py` opens with a warning worth repeating: **do not default to
`flag_count = 0`.**

Excluding all flagged trips drops the average trip distance from 19.02 to 2.90
miles. It also silently discards the ~55,600-trip `meter_metadata_missing` block —
a *correlated* population, not scatter — which tilts a demand count rather than
just shrinking it. Those trips' fares and distances are sound; the flag marks
missing meter metadata, not a bad measurement.

So relevance is **per measure**, not global (`rules.MEASURE_RULES`):

| Measure | Excludes |
|---|---|
| `count` | only `pickup_outside_2023` — a broken meter reading does not mean nobody got in the cab |
| `distance` | cast failures, non-positive and implausible distances, reformatted distances, implausible speeds |
| `fare` | cast failures, negative fare/total, fare > \$500, extreme \$/mile |
| `duration` | cast failures, non-positive/excessive duration, implausible and crawling speeds |
| `tip` | cast failures, negative tips |

`analytics.exclude_for(measure)` renders that as a `NOT EXISTS` against `TripFlag`
— chosen over a bitmask test on `Trip` because `TripFlag`'s bitmap index on
`rule_id` makes it an index read, while any bitmask expression forces a full 787k
scan.

Two more corrections applied everywhere:

- **Unknown zones.** Lookup IDs 264 and 265 (`Unknown`, `Outside of NYC`) resolve
  through every join and produce a named-but-unusable row — worse than a NULL — so
  geographic rollups exclude them deliberately.
- **Cash tips.** Cash fares record a tip of `0.00` in 100% of cases, because a cash
  tip never reaches the meter: *unobserved*, not zero. Averaging over all payment
  types halves the answer, so tip analysis restricts to card payments
  (`rules.CARD_PAYMENT`).

And every number ships with its denominator. `analytics.report_exclusions` prints
it; in the dashboard each KPI tile carries the row count its average was computed
over (`COUNT` of the same expression, in the same statement), and the trip-quality
tab's policy comparison quantifies the choice in full. A figure derived from a
filtered population is only interpretable next to the size of what was filtered
out.

---

## User workflows

Five in the terminal (`analytics.py`), five tabs in the dashboard. Every one pushes
the whole computation into IRIS.

**Terminal** — `irispython analytics.py [zones|time|fares|pairs|policy]`, or
`analytics.py sql "<statement>"` as an escape hatch for one-off questions:

1. **`zones`** — busiest pickup and drop-off zones, plus a borough rollup.
2. **`time`** — activity by hour of day (with an ASCII bar chart), day of week and
   month.
3. **`fares`** — fares, tips and distances compared across boroughs and zones.
   `HAVING COUNT(*) >= 500` keeps the tail out: a zone with nine trips is not
   evidence about that zone, it is evidence about nine trips.
4. **`pairs`** — most common origin/destination pairs with cost and duration, and
   same-zone round trips reported separately.
5. **`policy`** — the same four numbers under four flag policies, measured at run
   time. Read this one first; it shows the policy choice moving an answer further
   than the question does.

**Dashboard** (`http://localhost:52773/taxi/`) — one filter bar (month, day, hour,
pickup borough) applies across all tabs, so no panel is ever computed over a
different population than the one beside it. Each filter is a dropdown of toggle
chips with quick picks (quarters, weekdays, the two peaks); the closed button states
its own selection, so a narrowed field is visible without opening it:

| Tab | What it shows |
|---|---|
| **Overview** | Five KPI tiles — each computed by its own statement, because trips/distance/fare/duration carry different exclusions and one `SELECT` would force one policy on all four. Monthly trend, hourly profile, and a 24×7 demand heatmap (one `GROUP BY` on two bitmap-indexed columns returning ≤168 rows — the finished heatmap, not raw rows pivoted in Python). |
| **Geography** | Borough rollup, top pickup and drop-off zones, OD pairs, payment mix. |
| **Trip quality** | The three-policy comparison — the same four figures with no flag filter, with `flag_count = 0`, and with the per-measure exclusions — followed by every rule with its total flags *and its sole-flag count*, the number that matters for a policy argument, since a rule that only fires alongside others costs nothing extra to exclude. Selecting a rule opens its flagged trips. |
| **Trips** | The investigation view: filter to flagged records, or to the records one rule flagged, and open any of them. Paged inside IRIS — no `LIMIT`/`OFFSET` in IRIS SQL, so the idiom is `TOP` for the upper bound and `%VID` for the lower. Each row carries its rule names, decoded from `flag_mask` by the `Taxi.mask_names` UDF after `TOP` has cut the result to 50 rows. |
| **Push-down** | The stretch goal, on demand. |

**The flagged-record view is the trip-quality workflow's payoff**, and the only
panel that applies no flag exclusions at all — the exclusions exist to keep a
broken measurement out of an *average*, so applying them to the list of suspect
records would hide the point. Opening a trip shows its flags with each rule's
description *beside the raw `TripRaw` text the row was parsed from*, which is the
only way to settle a flagged value: `'1,571.97'` with a thousands separator that
survived the cast reads differently from `1571.97`.

Rules stay data, not code (`Taxi.TripQuality.predicate`), so changing a threshold
is an `UPDATE` plus one call to `Taxi.apply_rules_now()` — see *Changing a quality
threshold* below.

---

## The stretch goal: push-down comparison

`dashboard.pushdown_compare` computes one aggregate twice and reports both:

- **pushed** — one `GROUP BY`; IRIS aggregates and returns a handful of rows.
- **pulled** — the same rows streamed into Python (`db.iter_rows`) and accumulated
  in a dict.

Measured on the borough rollup over all 787,060 trips. The Push-down tab computes
the first row live; the second is kept from when this project still had a host-side
DB-API transport, because the comparison between the two is the interesting part:

| Transport | Pushed | Pulled | Speed-up | Rows moved |
|---|---|---|---|---|
| Embedded, in-process (what runs now) | 0.15 s | 3.06 s | 21× | 8 vs 787,053 |
| Host Python over DB-API on 1972 (removed) | 0.09 s | 0.77 s | 8.5× | 8 vs 787,053 |

The answers are compared rather than assumed equal: counts agree exactly, averages
to within floating-point noise (SQL `AVG` is decimal, Python's `sum` is not), and
the largest disagreement is reported — it is the number that would grow if one side
were wrong.

Reading it:

- **The wall-clock ratio is the smaller effect.** The durable number is the data
  ratio (~98,000× less moved), identical either way because it is a property of the
  query, not the transport.
- **The row-by-row path was ~4× slower *inside* IRIS than over the socket.**
  Embedded Python removes the network, not the per-row cost of handling a row in
  Python. So this argues for not writing row-by-row code in *either* place — which
  is also why the row-by-row half survives only as the control arm of a measurement.
- **Which would we prefer if the data grew?** Push-down, and not marginally: the
  pushed side's cost scales with the number of *groups*, the pulled side's with the
  number of *rows*.

---

## How this maps to the project guide

The brief is `docs/project_guide.md` (Team 3, Project B — NYC Taxi Trip Insights).

| Requirement | Where |
|---|---|
| Load the taxi data into an IRIS Community Edition instance you set up | `Dockerfile`, `docker-compose.yml`, `iris.script` build and configure the instance; stage 2 loads all 787,060 rows with server-side `LOAD DATA` |
| Python as the primary implementation language | Every stage, all analytics, the dashboard, and two SQL functions written in Python (`LANGUAGE PYTHON`) |
| IRIS as the data platform | Three-layer schema, bitmap indexes, a view, rules stored as data, transactions, prepared statements, `TUNE TABLE`, Python UDFs, IRIS-hosted WSGI application |
| Enrich locations using the taxi-zone lookup | `Taxi.Zone` + the `Taxi.TripEnriched` view; boroughs and zone names on both ends of every trip. `Trip`'s location columns are typed as the `Zone` class, so a single column filters as an integer id *and* traverses to borough |
| Trip-quality workflow identifying records for investigation | 16 rules in `Taxi.TripQuality` across two phases and two severities — invalid/unusual durations, zero and negative distances, negative and extreme fares, and the compound distance/time/fare disagreements the brief specifically names (`speed_implausible`, `fare_per_mile_extreme`, `speed_crawling`) |
| At least two additional user workflows | Five: busiest zones, activity by hour/day/month, fare–tip–distance comparison across zones, OD pairs, and the policy-impact analysis. All five in the terminal and in the dashboard, alongside the flagged-record inspector |
| Present through an interface of your choice | Both: a CLI (`analytics.py`) and a Flask SPA hosted by IRIS itself |
| Demonstrate Python + IRIS making the data more useful than raw files | The raw file cannot answer "which trips are suspect, why, and how much does excluding them move the answer" — that is the whole `TripRaw` → `Trip` → `TripFlag` chain plus the policy comparison. And `'1,571.97'` in a CSV is a string; here it is a stored measurement with its provenance recorded next to it |
| **Stretch:** push more work into IRIS and compare | `dashboard.pushdown_compare` — the same question answered by one `GROUP BY` and by pulling every row into Python, timed side by side, with the agreement check and the analysis above |

**Notes on friction**, since the brief asks for them. The things that cost the most
time were all cases where IRIS did something reasonable but *silently*: the
`DDLPKeyNotIDKey` default quietly breaking object-reference columns; `%EXACT` and
uppercase collation turning `Manhattan` into `MANHATTAN` in every `GROUP BY`;
`TIMESTAMP` columns comparing **lexically** against date string literals (so
`pickup_ts < '2023-01-01'` matched all 787,060 rows instead of 7 — no error, just a
wrong answer, which is why that rule uses `YEAR(pickup_ts)`); the missing
`DispatchClass` and `ServeFiles = 0` on a hand-built WSGI application; and the
`isStatInvalid` view defect that surfaces after unrelated later work rather than at
`CREATE VIEW` time. Each of those is now a comment at the site of the workaround,
and several have a build-time self-test so a regression fails the build instead of
the analytics. Smaller ones: no `LIMIT`/`OFFSET` (use `TOP` + `%VID`), no bitwise
operators in SQL (hence the UDF), `iris.sql.exec` raising `SQLError` with an *empty*
message for the perfectly normal "affected zero rows", `None` not being accepted as
NULL under Embedded Python, and `pip install` needing
`--target /usr/irissys/mgr/python` or the module is invisible to `irispython`.

---

## Where IRIS features are used

| Feature | Used for |
|---|---|
| **Embedded Python** (`irispython`, `iris.sql.exec`) | The whole pipeline *and* every dashboard request — in-process, no serialisation |
| **`LOAD DATA FROM FILE`** | Server-side CSV ingest of 787k rows; the data never enters a Python process |
| **`iris.sql.prepare()`** | Prepare-once/execute-many for the 787k-row cast |
| **`iris.tstart` / `tcommit` / `trollback`** | One transaction per 50k-row chunk |
| **Object-reference columns** | `Trip.pu_location_id` typed as `Taxi.Zone` — arrow traversal resolved as a left outer join, no borough strings duplicated across 787k rows |
| **Bitmap indexes** | `TripFlag(rule_id)`, `Trip(flag_count)`, both locations, hour, month |
| **`CREATE FUNCTION … LANGUAGE PYTHON`** | `Taxi.mask_names()` (bitmask decoding SQL cannot express) and `Taxi.apply_rules_now()` (forty statements collapsed to one call) |
| **Views + `%EXACT`** | `Taxi.TripEnriched` — the zone/borough enrichment layer |
| **`TUNE TABLE`** | Optimiser statistics after the load |
| **`INFORMATION_SCHEMA`** | Verifying drops actually happened |
| **WSGI hosting** (`Security.Applications`, `%SYS.Python.WSGI`) | IRIS serves the Flask dashboard on 52773 |
| **`TOP` + `%VID`** | Server-side paging, IRIS having no `LIMIT`/`OFFSET` |
| **Security services** | `%Service_CallIn` for Embedded Python; password auth on the web app with no role grants |

**One transport, everywhere.** Every statement in this project — loader, rules,
analytics, dashboard request — is issued by `iris.sql.exec` inside the IRIS process.
`db.py` proves that at import: it does `from irisbuiltins import SQLError`, which
only resolves inside IRIS, so the wrong interpreter fails immediately with the right
command to run instead rather than starting cleanly and dying on its first
statement.

An earlier version of this code also ran outside IRIS, over the Python DB-API
(`iris.connect()` to 1972), with `db.py` branching on an `EMBEDDED` flag. That was
removed. The measurement it produced is worth keeping (see the push-down table
above) but the second code path was not: it doubled the surface of every helper —
`None` vs `''` for NULL, cursors vs `iris.sql.exec` result objects, a connection per
thread — to support a transport that nothing in the deliverable used. `/api/meta`
still reports the transport and the footer still displays it, because "the aggregate
ran inside the database" is this project's central claim and it should be legible on
the page making it.

---

## Running everything

### Prerequisites

Docker (with `docker compose`), and the two CSVs in `./data/`:

```
data/2023_Green_Taxi_Trip_Data.csv
data/taxi_zone_lookup(in).csv
```

Both paths are `db.TRIP_CSV` / `db.ZONE_CSV`; the compose file mounts `./data`
read-only at `/data` inside the container.

### 1. Start IRIS

```bash
docker compose up -d --build
```

The image is built rather than used directly for two reasons, both dev-only: a
stock IRIS image ships `_SYSTEM` with its password flagged **expired**, which turns
the first driver login into an auth failure; and `%Service_CallIn` needs enabling
for Embedded Python and the Native API. `iris.script` handles both at build time,
so a fresh `docker compose up` is immediately usable. Flask is installed with
`--target /usr/irissys/mgr/python` because Embedded Python does not use the system
site-packages — a plain `pip install flask` succeeds and the module is then
invisible to IRIS.

Published ports: **1972** (superserver — DB-API/Native/JDBC/ODBC) and **52773**
(web — Management Portal and the dashboard). Credentials on this dev image:
`_SYSTEM` / `SYS`, namespace `USER`.

### 2. Run the pipeline and register the application

```bash
# everything: stages 1-4, the SQL function, the web application
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py

# drop and rebuild, casting only the first 20,000 raw rows -- a fast smoke test
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py --reload 20000

# re-apply the quality rules only, after editing rules.py
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py --flags
```

`run.py` skips stages 1–3 when `Taxi.Trip` is already populated, so the no-argument
form is safe to repeat. `./run.sh` is this plus `docker compose up -d`, a wait for
IRIS to accept SQL, and `open`.

Any stage can also be run alone — they are separate modules with `__main__` blocks:

```bash
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage1_schema.py
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage2_load_raw.py
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage3_cast.py
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage4_flag.py   # ← re-run after a threshold change
```

`-w /src` matters: `./src` is bind-mounted there read-only, so you edit on the host
and IRIS executes the new code with no rebuild between runs.

Stage 5 is the same: `stage5_procs.py` and `stage5_web.py` are re-runnable on their
own, and `stage5_web.py` deletes and recreates the application, so editing its
`PROPS` and re-running changes them.

### 3. Look at the results

**Dashboard**, served by IRIS itself:

```
http://localhost:52773/taxi/
```

The first request redirects to the IRIS login page; sign in as `_SYSTEM` / `SYS`
and the `CSPSESSIONID` cookie carries the SPA's `/taxi/api/*` calls. Editing
anything under `src/` takes effect on the next request — see
[the `WSGIDebug` note in stage 5](#stage-5--the-application-layer-stage5_procspy-stage5_webpy).

**Terminal analytics:**

```bash
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py         # all five
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py policy
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py zones time
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py sql \
    "SELECT TOP 5 pu_zone, COUNT(*) FROM Taxi.TripEnriched GROUP BY pu_zone ORDER BY 2 DESC"
```

**Management Portal**, for SQL by hand: `http://localhost:52773/csp/sys/UtilHome.csp`

### Changing a quality threshold

The loop the schema is built around. The rules are rows, so this is an `UPDATE`:

```sql
UPDATE Taxi.TripQuality
   SET predicate = 'trip_distance > 60',
       description = 'Over 60 miles.'
 WHERE rule_name = 'distance_implausible';
```

then re-flag — seconds, not a reload:

```bash
docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage4_flag.py
# or, in one SQL call:  SELECT Taxi.apply_rules_now()
```

---

## File map

```
run.sh                  the one command: compose up, wait for SQL, run src/run.py, open
Dockerfile              IRIS Community Edition + Flask, expired-password fix, %Service_CallIn
docker-compose.yml      ports 1972/52773; ./data:ro, ./src:ro, ./iris_out
iris.script             dev-only bootstrap applied at build time
docs/project_guide.md   the brief

src/
  run.py                the single entry point: stages 1-4, the SQL function, the
                        web application; skips the load if Taxi.Trip is populated
  db.py                 the SQL boundary: exec_sql/exec_dml/prepare/iter_rows,
                        NULL translation, and the embedded-only import guard
  rules.py              the 16 rules, MEASURE_RULES, unknown zones, CARD_PAYMENT
  stage1_schema.py      tables, indexes, Python UDFs, the enriched view, rule seeding
  stage2_load_raw.py    LOAD DATA for trips, Python insert for zones
  stage3_cast.py        TripRaw -> Trip, rejects, ingest-phase observations, self-test
  stage4_flag.py        rule predicates -> TripFlag, flag_count/flag_mask, TUNE, view check
  stage5_procs.py       Taxi.apply_rules_now() — stage 4 as one SQL call
  stage5_web.py         registers the Flask app as an IRIS-hosted WSGI application
  analytics.py          the five terminal workflows + ad-hoc SQL
  dashboard.py          every panel's SQL — one statement each — the Filters
                        predicate builder, and the push-down experiment
  _qtest.py             scratchpad, not part of the pipeline and imported by nothing
  web/
    taxi_app.py         Flask routes — thin: parse query string, call dashboard,
                        JSON — plus the module reloader WSGIDebug does not do
    static/             index.html, app.js (hand-rolled SVG charts), styles.css

tests/frontend/
  run.sh, run.js        boot app.js against captured responses, assert the DOM
  domshim.js            just enough DOM to run app.js outside a browser
  fixtures/             real /taxi/api/* payloads, and how to refresh them
```

**`tests/frontend/run.sh`** exists because of the failure that prompted this
harness: a blank dashboard where every endpoint answered `200`. `app.js` threw on
one key that had been renamed in `/api/meta`, `boot()` aborted part-way through, and
the page kept its header and filters and lost every panel. No amount of `curl` shows
that, and neither does a Python test — the only place it is visible is the DOM after
`app.js` has run. So the harness loads the real `index.html` and `app.js` against
real captured payloads and asserts on what came out: 5 KPI tiles, 168 heatmap cells,
50 trip rows, marks inside each chart's `<svg>`, and that the push-down tab fetches
*nothing* until its button is pressed.

It runs on `jsc`, which ships with macOS inside JavaScriptCore, so there is nothing
to install and no `node_modules`. The cost is `domshim.js`: a few hundred lines of
element tree, a small HTML parser and the handful of CSS selectors `app.js` uses.
Worth it for a page that is otherwise only testable by looking at it.

`_qtest.py` is run the same way as the stages
(`irispython _qtest.py`) and is where a one-off query goes while working out a rule
threshold or checking a distribution. Its contents are whatever the last question
was; nothing depends on it.
