#!/bin/sh
# Point the live runtime back at an earlier verified pin (the same checks as promote.sh).
# Usage: deploy/rollback.sh SHA
set -eu
sha=${1:-}
case "$sha" in
  ""|-*) echo "usage: rollback.sh SHA" >&2; exit 2 ;;
esac
if [ $# -ne 1 ]; then
  echo "usage: rollback.sh SHA" >&2
  exit 2
fi
. "$(dirname "$0")/_switch.sh"
switch_current "$sha" "rolled back"
