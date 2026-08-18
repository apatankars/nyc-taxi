#!/usr/bin/env bash
# Run the front-end checks. No dependencies to install: jsc ships with macOS as
# part of JavaScriptCore, which is why the harness targets it rather than node.
#
#   tests/frontend/run.sh
#
# Exit status is 0 only if every check passed.
set -uo pipefail

JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ ! -x "$JSC" ]]; then
  echo "no jsc at $JSC -- this harness needs macOS JavaScriptCore" >&2
  exit 2
fi

# jsc resolves load()/readFile() against the working directory, so the paths in
# run.js are repo-relative and this has to run from the root.
cd "$ROOT" || exit 2

output=$("$JSC" tests/frontend/run.js 2>&1)
echo "$output"

if grep -q '^FAILED$' <<<"$output"; then exit 1; fi
grep -q 'all checks passed' <<<"$output" || {
  echo "harness did not finish -- see output above" >&2
  exit 1
}
