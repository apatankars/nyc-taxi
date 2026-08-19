# InterSystems IRIS Community Edition, prepared for local Python development.
#
# We build on top of the stock image rather than using it directly so that a
# fresh `docker compose up` is immediately connectable from the host driver.
# See iris.script for what is applied and why.
FROM intersystemsdc/iris-community:latest

# IRISNAMESPACE pins the namespace that `iris session` and Embedded Python start
# in, so nothing downstream has to switch namespaces first. The credentials are
# for `iris session` and the Management Portal; they are this dev image's
# credentials and this is where someone will look for them.
ENV IRISUSERNAME="_SYSTEM" \
    IRISPASSWORD="SYS" \
    IRISNAMESPACE="USER"

COPY iris.script /tmp/iris.script

# Start, apply config, stop cleanly. Baking this in means the config survives
# container recreation without a manual step.
RUN iris start IRIS \
 && iris session IRIS < /tmp/iris.script \
 && iris stop IRIS quietly
