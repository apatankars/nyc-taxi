"""The Flask application, hosted by IRIS as a WSGI application.

run.py registers this module (see stage5_web.py), so the IRIS web server on 52773
serves it and every handler runs inside the IRIS process under Embedded Python:
`iris.sql.exec` in a request handler is an in-process call.

    http://localhost:52773/taxi/

Handlers are thin: parse the query string, call dashboard.py, return JSON.
"""

import importlib
import os
import sys
import threading

# Path setup before importing dashboard: IRIS puts only WSGIAppLocation (/src/web)
# on sys.path, so the parent -- holding dashboard.py, db.py, rules.py -- has to be
# added.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
for path in (_SRC, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

# --------------------------------------------------------------------------
# picking up edits to /src
# --------------------------------------------------------------------------
# stage5_web.py sets WSGIDebug, which re-imports *this* module when it changes on
# disk -- but not the modules it imports. The IRIS process outlives any number of
# edits, and sys.modules keeps whatever dashboard.py was the first time anything
# imported it. Editing dashboard.py then reloading the browser serves old code.
#
# That failure is quiet and expensive: the API answers 200 with a payload from a
# previous version. It cost an afternoon here, when a renamed /api/meta key left
# the filter bar populated and every tab blank -- the JSON looked fine, it was
# just from code that no longer existed on disk.
#
# Two halves, because there are two ways to get stale:
#   * this module has just been re-imported, but sys.modules still holds the old
#     dependencies -- so reload them now, before importing dashboard;
#   * a dependency changes while this module does not -- so re-check on request.
# Order is dependency order: dashboard.py holds `from db import ...` names, so db
# has to be new before dashboard rebinds them.
_RELOADABLE = ["db", "rules", "analytics", "dashboard"]
_reload_lock = threading.Lock()


def _module_mtimes():
    """Last-modified time per loaded module, for spotting an edit. Modules not yet
    imported are simply absent, and appear when they load."""
    stamps = {}
    for name in _RELOADABLE:
        path = getattr(sys.modules.get(name), "__file__", None)
        if path and os.path.exists(path):
            stamps[name] = os.stat(path).st_mtime
    return stamps


def _reload_all():
    for name in _RELOADABLE:
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)


_reload_all()

import dashboard  # noqa: E402

_mtimes = _module_mtimes()

app = Flask(__name__, static_folder=os.path.join(_HERE, "static"),
            static_url_path="/static")


@app.before_request
def _reload_changed_modules():
    """Four os.stat calls, in front of handlers that run multi-hundred-millisecond
    aggregates. Cheap enough to leave on, and always-on is the point: a reload flag
    you have to remember to set is a flag that is off when it matters.
    """
    global _mtimes, dashboard
    stamps = _module_mtimes()
    with _reload_lock:
        if stamps == _mtimes:
            return
        _mtimes = stamps
        _reload_all()
        # reload() updates the module object in place, so this rebinding is
        # belt-and-braces -- but it is also what documents that the handlers below
        # go through the module, never through names imported out of it.
        dashboard = sys.modules["dashboard"]
        app.logger.info("reloaded %s after an edit under /src",
                        ", ".join(_RELOADABLE))


def _filters():
    return dashboard.Filters.from_request(request.args)


@app.errorhandler(Exception)
def _on_error(exc):
    """Return every failure as JSON with the message intact.

    Flask's HTML error page arrives at a fetch() as "unexpected token '<'" and the
    SQLCODE never reaches anyone. IRIS SQL errors are worth reading, and the
    application requires a password and grants no roles, so whoever sees the error
    already has query access to the same tables.
    """
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description, status=exc.code), exc.code
    app.logger.exception("dashboard request failed")
    return jsonify(error=str(exc).strip() or exc.__class__.__name__,
                   type=exc.__class__.__name__), 500


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/meta")
def meta():
    """Filter options, row counts and transport mode. Fetched once."""
    return jsonify(dashboard.meta())


# One endpoint per tab rather than one per panel: the browser fetches only the tab
# it is showing, and a tab's panels share one filter predicate.

@app.get("/api/overview")
def overview():
    filters = _filters()
    return jsonify(
        filters=filters.as_dict(),
        kpis=dashboard.kpis(filters),
        monthly=dashboard.monthly(filters),
        hourly=dashboard.hourly(filters),
        heatmap=dashboard.hour_dow(filters),
    )


@app.get("/api/geography")
def geography():
    filters = _filters()
    return jsonify(
        boroughs=dashboard.boroughs(filters),
        pickup_zones=dashboard.zones(filters, top=12, role="pickup"),
        dropoff_zones=dashboard.zones(filters, top=12, role="dropoff"),
        pairs=dashboard.od_pairs(filters, top=15),
        payments=dashboard.payment_mix(filters),
    )


@app.get("/api/quality")
def quality():
    """Rule counts and distributions are unfiltered on purpose: how much of the
    data a rule touches, and how its values compare with the unflagged
    population's, are facts about the dataset -- recomputing them inside the
    current filter would read as the rules changing rather than the selection. The
    policy comparison is the exception, since its job is to describe the population
    being looked at."""
    return jsonify(
        coverage=dashboard.flag_coverage(),
        rules=dashboard.rule_impact(),
        profiles=dashboard.flag_profiles(),
        policies=dashboard.policy_comparison(_filters()),
    )


@app.get("/api/quality/distribution")
def quality_distribution():
    """One rule's flagged values, bucketed, against the unflagged population's on
    the same axis. Its own endpoint because the panel loads one rule at a time:
    computing every rule up front would be sixteen scans for one chart."""
    rule_id = request.args.get("rule")
    payload = dashboard.flag_distribution(rule_id)
    if payload is None:
        return jsonify(error=f"no comparable field for rule {rule_id}"), 404
    return jsonify(payload)


@app.get("/api/trips")
def trips():
    """The investigation view. `show` and `rule` narrow it to flagged records."""
    return jsonify(dashboard.trip_page(
        _filters(),
        page=request.args.get("page", 1),
        size=request.args.get("size", dashboard.PAGE_SIZE),
        order=request.args.get("order", "trip_id"),
        show=request.args.get("show", "all"),
        rule_id=request.args.get("rule"),
    ))


@app.get("/api/trip/<int:trip_id>")
def trip(trip_id):
    detail = dashboard.trip_detail(trip_id)
    if detail is None:
        return jsonify(error=f"no trip with trip_id {trip_id}"), 404
    return jsonify(detail)


@app.get("/api/pushdown")
def pushdown():
    """The stretch goal, run on demand. Never on page load: the pulled half moves
    every matching trip into Python, which is the measurement, not a side effect
    of opening a tab."""
    return jsonify(dashboard.pushdown_compare(
        _filters(), case=request.args.get("case", "borough")))
