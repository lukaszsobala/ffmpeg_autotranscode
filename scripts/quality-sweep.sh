#!/bin/sh
# Start scripts/quality_sweep.py detached, as a user systemd unit, so it survives
# closing the editor, the SSH session, and logging out (with lingering enabled).
#
#   scripts/quality-sweep.sh SOURCE ENCODED [OUTDIR] [-- extra quality_sweep.py args]
#
# Progress:  cat OUTDIR/report.md          (rewritten after every step)
# Log:       tail -f OUTDIR/sweep.log
# Running?   systemctl --user is-active autotranscode-quality
# Stop:      systemctl --user stop autotranscode-quality
set -eu

[ $# -ge 2 ] || { sed -n '5p' "$0" | sed 's/^# *//'; exit 2; }
source=$(realpath "$1")
encoded=$(realpath "$2")
shift 2
out="$HOME/.cache/autotranscode/quality/$(date +%Y%m%d-%H%M)"
if [ $# -ge 1 ] && [ "$1" != "--" ]; then out=$1; shift; fi
if [ $# -ge 1 ] && [ "$1" = "--" ]; then shift; fi

repo=$(cd "$(dirname "$0")/.." && pwd)
python="$repo/.venv/bin/python"
[ -x "$python" ] || python=python3
unit=autotranscode-quality

if systemctl --user is-active --quiet "$unit"; then
    echo "a sweep is already running: systemctl --user stop $unit to end it" >&2
    exit 1
fi
systemctl --user reset-failed "$unit" 2>/dev/null || true

out=$(realpath -m "$out")   # the unit runs from the repo, not from here
mkdir -p "$out"
# The single quotes are deliberate: the inner shell expands them, not this one.
# shellcheck disable=SC2016
systemd-run --user --quiet --unit="$unit" --collect --nice=10 \
    --working-directory="$repo" \
    sh -c 'py=$0 src=$1 enc=$2 dir=$3; shift 3
           exec "$py" scripts/quality_sweep.py "$src" "$enc" --out "$dir" "$@" >"$dir/sweep.log" 2>&1' \
    "$python" "$source" "$encoded" "$out" "$@"

echo "started $unit"
echo "report:  $out/report.md"
echo "log:     $out/sweep.log"
echo "stop:    systemctl --user stop $unit"
