#!/bin/sh
# Start `autotranscode sample` detached, as a user systemd unit, so it survives
# closing the editor, the SSH session, and logging out (with lingering enabled).
#
#   scripts/library-samples.sh LIBRARY... [sample options, e.g. -n 3 --band 4K]
#
# Output goes to a new directory under ~/.cache/autotranscode/samples unless --out names one.
# Progress:  cat OUTDIR/report.md          (rewritten after every sample)
# Log:       tail -f OUTDIR/sample.log
# Resume:    the same command with --out OUTDIR
# Stop:      systemctl --user stop autotranscode-samples
set -eu

[ $# -ge 1 ] || { sed -n '5p' "$0" | sed 's/^# *//'; exit 2; }

out=""
prev=""
for arg in "$@"; do
    [ "$prev" = "--out" ] && out=$arg
    prev=$arg
done
if [ -z "$out" ]; then
    out="$HOME/.cache/autotranscode/samples/$(date +%Y%m%d-%H%M)"
    set -- "$@" --out "$out"
fi
mkdir -p "$out"
out=$(realpath "$out")

repo=$(cd "$(dirname "$0")/.." && pwd)
command="$repo/.venv/bin/autotranscode"
[ -x "$command" ] || command=autotranscode
unit=autotranscode-samples

if systemctl --user is-active --quiet "$unit"; then
    echo "samples are already running: systemctl --user stop $unit to end them" >&2
    exit 1
fi
systemctl --user reset-failed "$unit" 2>/dev/null || true

# --same-dir, so relative library paths mean what they meant here.
systemd-run --user --quiet --unit="$unit" --collect --nice=10 --same-dir \
    --property=StandardOutput="truncate:$out/sample.log" --property=StandardError=inherit \
    "$command" sample "$@"

echo "started $unit"
echo "report:  $out/report.md"
echo "log:     $out/sample.log"
echo "stop:    systemctl --user stop $unit"
