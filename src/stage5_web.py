"""Stage 5 -- register the Flask dashboard as an IRIS-hosted web application.

IRIS 2024.2+ can host a WSGI application: point it at a directory, module and
callable, and the web server on 52773 serves it. The Flask code then runs inside
the IRIS process under Embedded Python, so `iris.sql.exec` in a request handler is
an in-process call -- an aggregate over 787,060 rows crosses the boundary as a few
summary rows, with no DB-API connection or per-query round trip.

run.py calls this as its last step, so the normal way to get here is `./run.sh`.
On its own, for when only the props below have changed:

    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage5_web.py

Then open http://localhost:52773/taxi/

Re-runnable: the application is deleted and recreated, so editing the props below
and re-running changes them. Editing the *Python* needs nothing -- taxi_app.py
reloads /src on the next request.
"""

import iris

from db import banner, log

APP_PATH = "/taxi"

# WSGIAppLocation is a *container* path -- docker-compose mounts ./src there.
# WSGIAppName is the module (taxi_app.py), WSGICallable the Flask object in it.
PROPS = {
    "NameSpace": "USER",
    "Enabled": 1,
    "Description": "NYC green-taxi dashboard (Flask, hosted by IRIS as WSGI)",
    # The one setting the Management Portal fills in for you, and the reason a
    # hand-built WSGI application 404s: IRIS routes the URL to the DispatchClass,
    # and %SYS.Python.WSGI sets up the WSGI environment. Without it nothing
    # answers -- no error, just a 404.
    "DispatchClass": "%SYS.Python.WSGI",
    "WSGIAppLocation": "/src/web",
    "WSGIAppName": "taxi_app",
    "WSGICallable": "app",
    "WSGIType": 1,      # 1 = WSGI, 2 = ASGI
    # Reload the module when it changes on disk. Worth the overhead here: /src is
    # a bind mount, so this is what makes editing taxi_app.py on the host show up
    # in the browser without restarting IRIS.
    "WSGIDebug": 1,
    # Recurse so /taxi/api/... reaches the app rather than 404ing at the gateway.
    "Recurse": 1,
    # 0 = never serve files from disk, always dispatch. This one is not optional
    # and the symptom is bewildering: with the default (1 = "Always"), the CSP
    # gateway claims every URL whose extension looks like a static file --
    # /taxi/static/app.js, /taxi/static/styles.css -- and serves it from the
    # application's physical Path, which for a WSGI application is empty. The
    # request never reaches Flask. What you get is an empty 404 for exactly the
    # .css and .js files, while /taxi/ and /taxi/api/* answer perfectly, so the
    # page loads unstyled with no JavaScript and nothing in the log looks wrong.
    # Flask's own static route serves those files once the dispatcher sees them.
    "ServeFiles": 0,
    # 32 = password authentication only. The first request redirects to the IRIS
    # login page; after signing in (_SYSTEM / SYS on this dev image) the
    # CSPSESSIONID cookie carries the SPA's /taxi/api/* calls. Deliberately absent:
    # no 64 bit (unauthenticated access), no MatchRoles grant -- every request runs
    # as the logged-in user with exactly that user's privileges on Taxi.*.
    "AutheEnabled": 32,
}


def register():
    banner("STAGE 5 -- web application")

    # Security.Applications lives in %SYS, so the object calls have to happen
    # there. Put the namespace back afterwards: this process may go on to do
    # other work (run.py calls the stages in sequence), and leaving it in %SYS
    # would make every later Taxi.* query fail with "table not found".
    process = iris.system.Process
    previous = process.NameSpace()
    process.SetNamespace("%SYS")
    try:
        apps = iris.cls("Security.Applications")
        if apps.Exists(APP_PATH):
            iris.check_status(apps.Delete(APP_PATH))
            log(f"  deleted existing web application {APP_PATH}")

        # Create() takes its properties as an ObjectScript multidimensional array
        # passed by reference. iris.arrayref is the bridge for that -- a plain
        # dict is rejected.
        iris.check_status(apps.Create(APP_PATH, iris.arrayref(PROPS)))
        log(f"  created web application {APP_PATH}")
    finally:
        process.SetNamespace(previous)

    log("")
    log(f"  dashboard:  http://localhost:52773{APP_PATH}/")
    log(f"  module:     {PROPS['WSGIAppLocation']}/{PROPS['WSGIAppName']}.py "
        f"(callable: {PROPS['WSGICallable']})")
    return APP_PATH


if __name__ == "__main__":
    register()
