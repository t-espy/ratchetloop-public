# Shared by promote.sh and rollback.sh: point <opt>/current at one verified, runnable pin — named by
# its sha or a unique prefix — atomically, and log the switch. Sourced, not run.
switch_current() {
  opt=${RATCHETLOOP_OPT:-$HOME/opt/ratchetloop}
  matches=$(find "$opt" -mindepth 1 -maxdepth 1 -type d -name "$1*" ! -name '.*' 2>/dev/null || true)
  count=$(printf '%s' "$matches" | grep -c . || true)
  if [ "$count" != 1 ]; then
    echo "error: $count pins under $opt match $1" >&2
    exit 2
  fi
  pin=$matches
  name=$(basename "$pin")
  if [ ! -f "$pin/.verified" ]; then
    echo "error: pin not verified: $name (deploy/pin.sh writes .verified after its smoke)" >&2
    exit 2
  fi
  if ! "$pin/.venv/bin/ratchetloop" --help >/dev/null 2>&1; then
    echo "error: pin does not run: $name" >&2
    exit 2
  fi
  prev=$(readlink "$opt/current" 2>/dev/null || echo none)
  if [ "$prev" = "$name" ]; then
    echo "$name is already current"
    return 0
  fi
  ln -s "$name" "$opt/.current.$$"
  mv -T "$opt/.current.$$" "$opt/current"
  printf '%s %s %s (was %s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$2" "$prev" \
    >> "$opt/promotions.log"
  echo "current -> $name ($2)"
}
