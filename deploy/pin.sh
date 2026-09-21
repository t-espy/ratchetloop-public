#!/bin/sh
# Pin a commit of ratchetloop as a runtime under $RATCHETLOOP_OPT (default ~/opt/ratchetloop): the
# tree from `git archive`, its own venv built in place, the pinned suite as the smoke, `.verified`
# written last. Refuse flags, a non-commit, and the live current; stage, then
# move; never relocate a venv (console-script shebangs hardcode its path).
# Usage, from the ratchetloop checkout: deploy/pin.sh SHA
set -eu
sha=${1:-}
case "$sha" in
  ""|-*) echo "usage: pin.sh SHA" >&2; exit 2 ;;
esac
if [ $# -ne 1 ]; then
  echo "usage: pin.sh SHA" >&2
  exit 2
fi
full=$(git rev-parse --verify -q "${sha}^{commit}") || { echo "error: not a commit: $sha" >&2; exit 2; }
opt=${RATCHETLOOP_OPT:-$HOME/opt/ratchetloop}
# The first python3 on PATH can be another project's venv (README): name the system one.
python=${RATCHETLOOP_PYTHON:-/usr/bin/python3}
mkdir -p "$opt"
dest="$opt/$full"
if [ -e "$opt/current" ] && [ "$(readlink -f "$opt/current")" = "$(readlink -f "$dest" 2>/dev/null || echo "$dest")" ]; then
  echo "error: refusing to re-pin the live current ($full)" >&2
  exit 2
fi
if [ -e "$dest" ]; then
  # Never delete a pin to rebuild it: it may be the only good copy, or the one promote.sh is about
  # to switch to (Phase 7 review, finding 1). A person removes it first.
  echo "error: $dest exists; pin.sh never deletes a pin (remove it, or roll back to it)" >&2
  exit 2
fi
stage="$opt/.staging-$full.$$"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage"
git archive "$full" | tar -C "$stage" -xf -
mv -T "$stage" "$dest"  # fails, rather than nests, if a pin of this sha appeared meanwhile
trap 'rm -rf "$dest"' EXIT  # a pin that fails below is removed, never left half-built
"$python" -m venv "$dest/.venv"
"$dest/.venv/bin/pip" install -q "$dest[dev]"
"$dest/.venv/bin/ratchetloop" --help >/dev/null
# The smoke: the pinned suite against the installed package, the tree kept off sys.path.
(cd "$dest" && PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 .venv/bin/python -m pytest -q -p no:cacheprovider)
printf '%s\n' "$full" > "$dest/.verified"
trap - EXIT
echo "pinned $full at $dest"
