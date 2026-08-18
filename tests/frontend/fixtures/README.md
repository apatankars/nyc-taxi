# Captured API responses

These are real `/taxi/api/*` responses from a fully loaded database (787,060 trips,
16 rules), not hand-written payloads. That matters: the point of the harness is to
run `app.js` against what the API actually returns, and a fixture I wrote by hand
would only test `app.js` against my idea of the API.

Refresh them after changing a response shape in `dashboard.py` or `taxi_app.py` —
with the stack running (`./run.sh`), from the repository root:

```bash
cd tests/frontend/fixtures
B='http://localhost:52773/taxi/api'
curl -su _SYSTEM:SYS "$B/meta"                > meta.json
curl -su _SYSTEM:SYS "$B/overview"            > overview.json
curl -su _SYSTEM:SYS "$B/geography"           > geography.json
curl -su _SYSTEM:SYS "$B/quality"             > quality.json
curl -su _SYSTEM:SYS "$B/trips?page=1"        > trips.json
curl -su _SYSTEM:SYS "$B/trip/50"             > trip50.json
curl -su _SYSTEM:SYS "$B/pushdown?case=borough" > pushdown.json
```

Then re-run `tests/frontend/run.sh`. Several assertions are tied to these numbers
(50 trip rows, 16 + 1 rule options, 168 heatmap cells, 3 policy rows), so a fixture
captured against a `--reload 20000` subset will fail the counts rather than the
code. Capture against a full load.
