# InterSystems IRIS Community Edition, prepared for local Python development.
#
# We build rather than using the image directly for one reason: a stock IRIS image
# ships with the _SYSTEM password flagged as expired, so the first login is forced
# into a password change and any driver connection fails with an auth error.
# The RUN step below clears that (dev-only) and enables the call-in service that
# the Native API and Embedded Python need.

FROM intersystemsdc/iris-community:latest

# Consumed by Embedded Python (irispython) so it can self-connect.
ENV IRISUSERNAME="_SYSTEM" \
    IRISPASSWORD="SYS" \
    IRISNAMESPACE="USER"

COPY iris.script /tmp/iris.script

# Start IRIS, apply the config script, shut down cleanly. Baking this into the
# image means a fresh `docker compose up` is immediately usable.
RUN iris start IRIS \
    && iris session IRIS < /tmp/iris.script \
    && iris stop IRIS quietly
