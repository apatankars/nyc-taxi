#!/usr/bin/env bash
#
# The single command. Starts IRIS if it is not running, waits for it to accept
# work, then builds and serves the dashboard.
#
#   ./run.sh                 build if needed, then serve
#   ./run.sh --reload        drop and rebuild everything from the CSVs
#   ./run.sh --reload 50000  same, but only cast the first 50,000 raw rows
#   ./run.sh --flags         re-apply the quality rules only
#
# Arguments are passed straight through to src/run.py, which documents them.

set -euo pipefail

CONTAINER=nyc-taxi-iris
IRISPYTHON=/usr/irissys/bin/irispython
URL=http://localhost:52773/taxi/

cd "$(dirname "$0")"

# `up -d` is idempotent -- it starts a stopped container and no-ops on a running
# one -- so there is no state to check here.
echo "==> starting IRIS"
docker compose up -d

# IRIS accepts connections on 1972 well before SQL works, so wait on a statement
# rather than on the port. Without this, the first run after `docker compose up`
# fails on stage 1 with an error that reads like a code problem.
echo "==> waiting for IRIS to accept SQL"
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" "$IRISPYTHON" -c \
       'import iris; iris.sql.exec("SELECT 1")' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec -w /src "$CONTAINER" "$IRISPYTHON" run.py "$@"

echo
echo "==> $URL"

# macOS only, and non-fatal: the URL is printed above either way.
command -v open >/dev/null && open "$URL" || true
