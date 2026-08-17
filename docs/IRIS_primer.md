# InterSystems IRIS — A Primer for the NYC Taxi Project

Everything in this document was verified against the IRIS instance running on this machine on
2026-08-17 (IRIS 2026.1, Build 234U, ARM64 container). Where something *didn't* work as the docs
suggest, that's called out — those are friction-log material.

---

## 1. What IRIS actually is

Most people meet IRIS as "a database," which is true but misleading. It is more useful to think of it
as **one storage engine with several front doors, plus an application runtime bolted to it.**

### The storage substrate: globals

At the bottom, IRIS stores everything in **globals** — persistent, sparse, ordered multidimensional
arrays living on disk in a B-tree. A global looks like this:

```
^Trip(1, "fare") = 14.9
^Trip(1, "pu")   = 166
^Trip(2, "fare") = 10.7
```

The `^` means "on disk, transactional, shared across processes." There is no schema at this layer,
subscripts can be strings or numbers, and it's always ordered. This is the same design IRIS has used
since its M/Caché ancestry, and it's why IRIS can be very fast for key-ordered access.

### The front doors: SQL, objects, documents

The critical idea: **SQL tables, persistent classes, and JSON documents are not separate copies of
your data — they're different projections over the same globals.**

```
        ┌─── SQL  (tables, views, JDBC/ODBC/DB-API) ───┐
        ├─── Objects (ObjectScript / Python classes) ──┤ ──► same global storage
        ├─── Native API (direct global read/write) ────┤
        └─── Documents / vectors ──────────────────────┘
```

Practically, this means: when you `CREATE TABLE Taxi.Trip`, IRIS silently generates a class called
`Taxi.Trip` with a storage definition. When you define a persistent class `Taxi.Trip`, you
automatically get a SQL table. Same bytes. **You pick the access path per task, not per dataset.**
For this project that's the single most useful fact — you can bulk-load over SQL, then reach the same
rows as objects if that's more convenient, with no ETL between them.

### The runtime half

IRIS is also an application server. It contains:

- **ObjectScript** — its native language. You'll read some; you don't need to write much.
- **Embedded Python** — a real CPython interpreter *inside* the database process. You can write class
  methods in Python and call IRIS objects directly, with no network hop.
- **A web server + REST framework** (`%CSP.REST`) — you can serve HTTP endpoints from the database.
- **Interoperability / Productions** — an integration engine (message routing, HL7/FHIR adapters).
  Irrelevant to this project, but it's why `Ens.*` tables clutter your schema list.
- **Analytics extras** — vector search (verified working), IntegratedML (`CREATE MODEL` in SQL),
  columnar storage.

That "runtime inside the database" property is the thing IRIS is actually selling. The stretch goal in
your project brief is a direct probe of it: *how much work can you move to the data instead of moving
data to your code?*

---

## 2. Your current environment (as found)

**An IRIS instance is already running on this machine** — you started it before this conversation.
It is not, however, tied to this project directory.

| Thing | Value |
|---|---|
| Container | `objectscript-docker-template-master-iris-1` |
| Image built from | `/Users/apatanka/Desktop/objectscript-docker-template-master` |
| IRIS version | 2026.1 (Build 234U), Ubuntu ARM64 |
| Superserver (SQL/driver) port | container `1972` → host **52792** |
| Management Portal | container `52773` → host **52794** |
| Credentials | `_SYSTEM` / `SYS` |
| Default namespace | `USER` |
| Volume mount | template dir → `/home/irisowner/dev` |

Management Portal: <http://localhost:52794/csp/sys/UtilHome.csp> (returns HTTP 200 right now).

### Two gotchas baked into that setup

**1. The host ports are randomized.** The template's `docker-compose.yml` declares ports without a
host side:

```yaml
ports:
  - 1972
  - 52773
```

Docker therefore assigns a *random free host port* each time the container is recreated. `52792`
will change. Any connection string you hardcode will silently break tomorrow. Either pin them:

```yaml
ports:
  - "1972:1972"
  - "52773:52773"
```

…or read the port at runtime: `docker port objectscript-docker-template-master-iris-1 1972`.

**2. IRIS cannot see your taxi CSVs.** The only mount is the template directory. Verified: there is
no `2023_Green_Taxi_Trip_Data.csv` anywhere in the container filesystem. This matters because
server-side loading (`LOAD DATA FROM FILE`) resolves paths **inside the container**, not on your Mac.
See §5.

### Do you need Docker?

**No — but it's the least painful option, and you already have it working.** Your three choices:

- **Docker (current)** — fastest to reset, no host pollution, and the brief explicitly permits it.
  Downside: a filesystem boundary between your data and the database, plus the port issue above.
- **Native install** — Community Edition from evaluation.intersystems.com installs directly on macOS.
  IRIS then sees your CSVs at their real paths and gets fixed ports (1972 / 52773). Downside: a
  real system install to manage and uninstall.
- **Neither** — not viable. The brief requires an IRIS instance you set up.

My recommendation: **stay on Docker**, but spin up a container dedicated to this project that mounts
`/Users/apatanka/Desktop/nyc_taxi`, rather than borrowing the ObjectScript template's container. The
template exists to demo ObjectScript class development, which isn't what Project B asks for.

> **This has since been written.** `docker-compose.yml`, `Dockerfile` and `iris.script` in the project
> root define a dedicated instance with **pinned ports (1972 / 52773)** and the `data/` directory
> mounted at `/data` inside the container. See
> [setup_and_workflow.md](setup_and_workflow.md) for the swap procedure and the reasoning.
> Once you've swapped, the port table above becomes: superserver **1972**, Portal
> **52773** (<http://localhost:52773/csp/sys/UtilHome.csp>), and the trip CSV is visible to IRIS at
> `/data/2023_Green_Taxi_Trip_Data.csv`.

One subtlety that forced a build step rather than a plain `image:` reference: a stock IRIS image ships
with the `_SYSTEM` password **expired**, so the first login is diverted into a password change and
driver connections fail on auth. The template's Dockerfile quietly fixes this with
`##class(Security.Users).UnExpireUserPasswords("*")` — which is the only reason `_SYSTEM`/`SYS` works
today. Our `iris.script` does the same, plus enables `%Service_CallIn` (needed by the Native API and
Embedded Python).

---

## 3. The five ways to interact with IRIS

Ranked by how much you'll use them on this project.

### (a) Python DB-API — your main workhorse

The driver is `intersystems-irispython` on PyPI, and it imports as `iris`. **Verified working from
this Mac against the running container:**

```python
import iris

conn = iris.connect("localhost", 52792, "USER", "_SYSTEM", "SYS")
cur = conn.cursor()

cur.execute("SELECT TOP 5 TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
for row in cur.fetchall():
    print(row[0], row[1])

conn.close()
```

Notes that will save you time:

- Rows come back as `iris.dbapi.DataRow`, not tuples. Index them (`row[0]`) or convert
  (`list(row)`, `dict(zip(cols, row))`). Printing a row bare gives you
  `<iris.dbapi.DataRow object at 0x…>`, which looks like a bug and isn't.
- **Calling `fetchall()` after a DDL statement raises `<USAGE ERROR>`** — "execute has not yet been
  called, or the previous call did not return a result set." The DDL *succeeded*; there's just no
  result set. Don't let this fool you into thinking your `CREATE TABLE` failed. (It fooled me once
  while writing this primer.)
- `executemany` works and is your bulk-load path. Verified: 1,000 rows in one call.
- It's DB-API 2.0, so pandas works: `pd.read_sql(query, conn)`.

### (b) Management Portal — the browser UI

<http://localhost:52794/csp/sys/UtilHome.csp>, log in as `_SYSTEM` / `SYS`.

The two places worth knowing:

- **System Explorer → SQL** — an interactive query editor with a **Show Plan** button. That plan
  view is how you'll answer the stretch goal credibly: it tells you which index a query used and
  what it cost. Also has a table-import wizard.
- **System Explorer → Globals** — browse the raw globals. Worth ten minutes of poking after you load
  data, just to *see* that your SQL table is a global underneath. It makes the architecture click.

### (c) Native API — direct global and method access

Same `iris` package, different entry point. Skips SQL entirely. **Verified working:**

```python
import iris

nc = iris.createConnection("localhost", 52792, "USER", "_SYSTEM", "SYS")
n = iris.createIRIS(nc)

n.set("hello", "^demoGlobal", "a")          # ^demoGlobal("a") = "hello"
print(n.get("^demoGlobal", "a"))            # -> hello
print(n.classMethodValue("%SYSTEM.Version", "GetNumber"))   # -> 2026.1
n.kill("^demoGlobal")

nc.close()
```

Use this to call an ObjectScript or embedded-Python class method from your Python app —
`classMethodValue` is the bridge. Direct global manipulation is fast but schema-less; don't store
your trip data this way when SQL gives you aggregation for free.

### (d) Embedded Python — flipping the relationship

Instead of Python reaching into IRIS, run Python *inside* IRIS. The container ships
`/usr/irissys/bin/irispython`, a CPython whose `iris` module is already connected — no host, no port,
no serialization:

```bash
docker exec -it objectscript-docker-template-master-iris-1 /usr/irissys/bin/irispython
```

```python
import iris
rs = iris.sql.exec("SELECT COUNT(*) FROM Taxi.Trip")
print(rs.dataframe())          # no data crosses a network boundary
```

You can also write persistent class methods in Python (`Method Foo() [ Language = python ]`). For
the stretch goal, this is the most interesting comparison point: *same Python code, zero network
transfer.*

Caveat: the host's `python3` (3.9.6, Command Line Tools) has no `iris` module and shouldn't be given
one — use a venv. The container's own `python3` (3.12.3) also lacks `iris`; only `irispython` has it.
That's why `irissqlcli` is installed in the container but crashes with
`ModuleNotFoundError: No module named 'iris'` if you run it under plain `python3`.

### (e) ObjectScript terminal & VS Code

```bash
docker exec -it objectscript-docker-template-master-iris-1 iris session IRIS -U USER
```

You get a `USER>` prompt. `write $ZV` prints the version; `halt` exits. You need this rarely —
mainly for `%SYS`-level admin. The **InterSystems ObjectScript** VS Code extension pack gives you
class editing, a SQL runner, and server-side source control; useful if you end up writing class
definitions, optional if you stay in pure Python + DDL.

---

## 4. The one concept that will change your design

**A persistent class and a SQL table are the same object.** Two routes to the same place:

```sql
-- DDL route: familiar, works from any SQL client
CREATE TABLE Taxi.Trip (
    pickup_ts    TIMESTAMP,
    dropoff_ts   TIMESTAMP,
    pu_location  INTEGER,
    do_location  INTEGER,
    trip_distance DOUBLE,
    fare_amount  DOUBLE,
    tip_amount   DOUBLE,
    total_amount DOUBLE
)
```

```objectscript
// Class route: same table, plus you can attach behavior
Class Taxi.Trip Extends %Persistent [ SqlTableName = Trip ]
{
Property pickupTs As %TimeStamp [ SqlFieldName = pickup_ts ];
Property tripDistance As %Double;

Method DurationMinutes() As %Integer [ Language = python ]
{
    # real Python, running inside the database
    ...
}
}
```

Why care? Because the class route lets you hang **validation and derived values on the data itself**
— which is precisely what your trip-quality workflow needs. A calculated property like
`impliedMph` or a validating datatype (`%Integer(MINVAL=0)`) enforces quality at the storage layer,
so every access path sees it. That's a genuinely different design option than "clean it in pandas and
hope."

You don't have to use it. But knowing it's there is the difference between "I used IRIS as a
Postgres substitute" and "I understood what I was given" — and your presentation is graded on the
latter.

---

## 5. Mapping IRIS features to your actual deliverables

### Loading ~787,000 rows

Two approaches, and the choice is itself a finding worth presenting.

**Server-side `LOAD DATA`** — fastest, but the file must be visible *to IRIS*:

```sql
LOAD DATA FROM FILE '/data/data/2023_Green_Taxi_Trip_Data.csv'
INTO Taxi.Trip
USING {"from": {"file": {"header": true}}}
```

Verified present on your build (it throws `FileNotFoundException` for a bad path, not a syntax
error). **With your current container this will fail** — the CSV isn't mounted. Fix by mounting
`nyc_taxi` (see §2) or `docker cp`-ing the file in.

**Client-side `executemany`** — slower (every row crosses the wire) but no mount needed and you can
transform/validate in Python en route. Batch it; don't do 787,000 single inserts:

```python
BATCH = 10_000
cur.executemany("INSERT INTO Taxi.Trip (...) VALUES (?,?,?,?,?)", batch_rows)
conn.commit()
```

Timing both, and reporting the gap, is a strong slide.

Two data facts you'll hit immediately, from the raw file:
- Timestamps are `MM/DD/YYYY HH:MM:SS AM/PM` — **not** ISO. IRIS wants `YYYY-MM-DD HH:MM:SS`.
  Convert in Python or via `TO_TIMESTAMP`.
- `ehail_fee` is empty on every row sampled. Decide early: drop the column or carry it as NULL.
- `taxi_zone_lookup(in).csv` starts with a **UTF-8 BOM** (`﻿` before `LocationID`), which will
  corrupt your first column name if you read it naively. Use `encoding="utf-8-sig"`.

### Enrichment with zone names

Load `taxi_zone_lookup` as `Taxi.Zone (location_id, borough, zone, service_zone)` and join. Keep it a
join rather than denormalizing the strings into 787k rows — but then *also* try the denormalized
version and compare query times. That contrast is exactly the kind of thing the discussion asks
about.

### Trip-quality workflow

SQL gets you most of the way, and IRIS has the date functions you need (both verified):

```sql
SELECT DATEDIFF('minute', pickup_ts, dropoff_ts) AS duration_min,
       DATEPART('hour',  pickup_ts)              AS pickup_hour
FROM Taxi.Trip
```

So flags become expressible directly:

```sql
CREATE VIEW Taxi.SuspectTrip AS
SELECT %ID AS trip_id, pickup_ts, trip_distance, fare_amount,
       DATEDIFF('minute', pickup_ts, dropoff_ts) AS duration_min
FROM Taxi.Trip
WHERE trip_distance <= 0
   OR fare_amount < 0
   OR DATEDIFF('minute', pickup_ts, dropoff_ts) NOT BETWEEN 1 AND 300
   OR (trip_distance > 0 AND
       trip_distance / NULLIF(DATEDIFF('second', pickup_ts, dropoff_ts), 0) * 3600 > 80)
```

`%ID` is the IRIS row identifier — every table has one for free.

### Analytics workflows

Window functions work (`RANK() OVER (ORDER BY …)` verified). `TOP n` and `LIMIT n` both work — `TOP`
is the IRIS-native form, `LIMIT` is accepted for compatibility. Busiest zones, hourly profiles, and
O/D pair frequencies are all plain `GROUP BY`.

### Interface

Pick based on what you want to *show*. Jupyter is the lowest-risk demo (pandas + `read_sql` is two
lines). A REST API via `%CSP.REST` would demonstrate more of IRIS but costs you ObjectScript time.
Streamlit over the DB-API is a good middle path.

---

## 6. The stretch goal, properly framed

The question is **push-down vs. pull-up**. Same analysis, two placements:

```python
# Pull-up: 787k rows cross the wire, pandas aggregates
df = pd.read_sql("SELECT pu_location, fare_amount FROM Taxi.Trip", conn)
result = df.groupby("pu_location")["fare_amount"].mean()

# Push-down: IRIS aggregates, ~260 rows cross the wire
result = pd.read_sql("""
    SELECT z.zone, AVG(t.fare_amount) AS avg_fare, COUNT(*) AS trips
    FROM Taxi.Trip t JOIN Taxi.Zone z ON t.pu_location = z.location_id
    GROUP BY z.zone ORDER BY trips DESC
""", conn)
```

Measure three things and you have your slide: **rows transferred, wall-clock time, lines of code.**

IRIS gives you real levers on the push-down side, all verified on your instance:

- **Bitmap indexes** — `CREATE BITMAP INDEX puIdx ON Taxi.Trip(pu_location)`. Ideal for
  low-cardinality columns (265 location IDs, 6 payment types) and cheap to combine with `AND`/`OR`.
- **Columnar storage** — `CREATE TABLE … WITH STORAGETYPE = COLUMNAR`. Aimed squarely at analytical
  scans over a few columns of many rows, which is your entire workload. Loading the trips twice,
  row-wise and columnar, and comparing aggregate times is probably the single best experiment
  available to you here.
- **Parallel query** — **the documented `SELECT %PARALLEL …` syntax is rejected on this build**
  (`SQLCODE -12`). The options-comment form does work:
  `SELECT /*#OPTIONS {"ParallelThreads":4} */ COUNT(*) FROM Taxi.Trip`. Straight into the friction log.
- **Show Plan** in the Portal, to prove *why* something got faster rather than just asserting it.

Also present, if you want to go further than asked: **IntegratedML** (the
`INFORMATION_SCHEMA.ML_MODELS` table exists and is empty, so `CREATE MODEL … PREDICTING (…)` is
available for e.g. predicting tip amount) and **vector search** (`VECTOR_COSINE` /
`TO_VECTOR` verified working). Neither is required. Both are the kind of "wait, that's *in* the
database?" moment that makes a 10-minute presentation land.

---

## 7. Friction already found (free material for your log)

1. `objectscript-docker-template`'s compose file leaves host ports unpinned → connection strings
   break on every container recreate, with a confusing "connection refused."
2. `fetchall()` after DDL raises `<USAGE ERROR>` that reads like the DDL failed. Actively misleading.
3. `DataRow` objects print as opaque `<object at 0x…>` instead of their values.
4. Three Pythons in play (host 3.9.6, container 3.12.3, `irispython`) and **only** `irispython` has
   the `iris` module. The container's own bundled `irissqlcli` is broken under `python3` for exactly
   this reason — a shipped tool that doesn't run out of the box.
5. `%PARALLEL` — widely documented, rejected by the 2026.1 parser.
6. `LOAD DATA` paths are container-relative with no hint in the error that a mount is the issue;
   `FileNotFoundException` for a path that plainly exists on your Mac is a solid 20-minute detour.
7. Supplied data quirks that no one mentions: BOM in the lookup CSV, `MM/DD/YYYY hh:mm AM/PM`
   timestamps, fully-empty `ehail_fee`.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| **Namespace** | Logical workspace (code + data). Yours is `USER`. Roughly a "database" in PG terms. |
| **Global** | The on-disk sparse array all data ultimately lives in. `^Name(sub1, sub2)`. |
| **ObjectScript** | IRIS's native language. Terse; `$`-prefixed built-ins. |
| **`%` prefix** | System-supplied. `%Persistent`, `%String`, `%ID`, `%SYS`. |
| **Persistent class** | A class whose instances are stored — and simultaneously a SQL table. |
| **Superserver** | The binary protocol port (1972) that drivers connect to. |
| **`$ZV` / `$ZVERSION`** | Version string. `write $ZV` in a terminal. |
| **`%SYS`** | The administrative namespace. |
| **Ens.\*** | Interoperability framework tables. Noise for your purposes. |
| **IRISSYS / irisowner** | The install dir (`/usr/irissys`) and OS user inside the container. |

---

## 9. Where to look things up

- **Docs** — <https://docs.intersystems.com>. For this project: *Using Python with InterSystems IRIS*,
  *InterSystems SQL Reference* (the dialect differs from Postgres in small annoying ways), and
  *Columnar Storage*.
- **Developer Community** — <https://community.intersystems.com>. Often more useful than the formal
  docs for "how do I actually…" questions.
- **Open Exchange** — <https://openexchange.intersystems.com>. Sample apps and templates.
- **In-instance** — the Management Portal's SQL area documents available functions, and
  `INFORMATION_SCHEMA` is queryable for everything about your own schema.

---

## Appendix: what was done to produce this document

Full disclosure, since this touched your machine and your instance.

**Installed on your Mac:** nothing permanent. A throwaway virtualenv at `/tmp/irisvenv` with
`intersystems-irispython` 5.4.0 (wheel downloaded to `/tmp/irisdl`), used purely to verify that the
driver installs and connects from this host. Both live in `/tmp` and vanish on reboot; delete now
with `rm -rf /tmp/irisvenv /tmp/irisdl` if you prefer. Your system `python3` was not modified. When
you build the real project, make a proper venv in `nyc_taxi/`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install intersystems-irispython pandas
```

**Changed in your IRIS instance:** temporary scratch objects, all removed —
tables `Scratch.T`, `Scratch.T2`, `Scratch.P`, `Scratch.L` (created, queried, dropped) and a global
`^demoGlobal` (set, read, killed). No configuration, users, or namespaces were altered, and nothing
was written to the `Taxi` schema. Your container was not restarted, so the port table above is
still valid.

**Read:** `docs/project_guide.pdf`, `docs/project_guide.md`, and the headers plus first rows of both
CSVs. `/Users/apatanka/Desktop/objectscript-docker-template-master/docker-compose.yml` was read to
explain the port and mount behavior.
