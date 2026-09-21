#!/bin/sh
# Make a verified pin the live runtime: <opt>/current switched to it atomically, the switch logged
# with who approved it. The approval is a person's name.
# Usage: deploy/promote.sh SHA --approved-by NAME
set -eu
sha=${1:-}
case "$sha" in
  ""|-*) echo "usage: promote.sh SHA --approved-by NAME" >&2; exit 2 ;;
esac
if [ "${2:-}" != "--approved-by" ] || [ -z "${3:-}" ] || [ $# -ne 3 ]; then
  echo "usage: promote.sh SHA --approved-by NAME" >&2
  exit 2
fi
. "$(dirname "$0")/_switch.sh"
switch_current "$sha" "promoted, approved by $3"
