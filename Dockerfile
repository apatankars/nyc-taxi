# InterSystems IRIS Community Edition, prepared for local Python development.
#
# We build rather than using the image directly for one reason: a stock IRIS image
# ships with the _SYSTEM password flagged as expired, so the first login is forced
# into a password change and any driver connection fails with an auth error.
# The RUN step below clears that (dev-only) and enables the call-in service that
# the Native API and Embedded Python need.

FROM intersystemsdc/iris-community:latest

# IRISNAMESPACE is the one that does work: it pins the namespace irispython starts
# in, so the stages do not have to switch. Verified by setting it -- with
# IRISNAMESPACE=%SYS, `iris.system.Process.NameSpace()` reports %SYS.
#
# The credentials are for `iris session` and the Management Portal, not for
# irispython: Embedded Python runs as the instance and needs no login (irispython
# starts fine with IRISUSERNAME set to a user that does not exist). They are kept
# here because they are this dev image's credentials and this is where someone looks
# for them.
ENV IRISUSERNAME="_SYSTEM" \
    IRISPASSWORD="SYS" \
    IRISNAMESPACE="USER"

# Flask, for the dashboard IRIS hosts itself as a WSGI application (stage 5).
#
# --target is the part that matters. Embedded Python does not use the system
# site-packages; it looks in the instance's own python directory, which is already
# on irispython's sys.path (see `irispython -c "import sys; print(sys.path)"`). A
# plain `pip install flask` succeeds and the module is then invisible to IRIS.
RUN pip install --no-cache-dir --target /usr/irissys/mgr/python flask

COPY iris.script /tmp/iris.script

# Start IRIS, apply the config script, shut down cleanly. Baking this into the
# image means a fresh `docker compose up` is immediately usable.
RUN iris start IRIS \
    && iris session IRIS < /tmp/iris.script \
    && iris stop IRIS quietly
