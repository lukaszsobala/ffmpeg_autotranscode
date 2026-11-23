#!/usr/bin/env bash
# Cut a release: what `autotranscode update` offers machines running an older one.
#
#   scripts/release.sh 0.3.0           # check, set the version, test, commit, tag
#   scripts/release.sh 0.3.0 --push    # and push main and the tag, and make a
#                                      # GitHub release when gh is installed
#
# A release is an annotated tag vX.Y.Z on main, on the commit whose __version__ is
# X.Y.Z. Whatever is on main between releases stays there: `update` only moves a
# checkout from one release to the next. Release whenever autotranscode/toolchain.toml
# changes, since a version names one toolchain.
set -euo pipefail

die() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

version=${1:-}
push=${2:-}
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: scripts/release.sh X.Y.Z [--push]"
[[ -z $push || $push == --push ]] || die "unknown option: $push"
tag="v$version"

cd "$(dirname "${BASH_SOURCE[0]}")/.."
python=python3
[[ -x .venv/bin/python ]] && python=.venv/bin/python

[[ $(git branch --show-current) == main ]] || die "releases are cut from main"
[[ -z $(git status --porcelain --untracked-files=no) ]] || die "commit or stash your changes first"
git fetch --quiet --tags origin
[[ -z $(git rev-list HEAD..origin/main) ]] || die "main is behind origin/main; pull first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "$tag exists already"

previous=$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)
if [[ -n $previous ]]; then
  newest=$(printf '%s\n%s\n' "${previous#v}" "$version" | sort -V | tail -1)
  [[ $newest == "$version" && ${previous#v} != "$version" ]] \
    || die "$version is not newer than the last release, $previous"
fi

sed -i.bak -E "s/^__version__ = \".*\"/__version__ = \"$version\"/" autotranscode/__init__.py
rm -f autotranscode/__init__.py.bak
grep -q "^__version__ = \"$version\"$" autotranscode/__init__.py || die "could not set the version"

printf 'Running the tests\n'
if ! "$python" -m unittest discover -s tests -q; then
  git checkout -- autotranscode/__init__.py
  die "the tests failed; nothing released"
fi

# The tools that change since the last release, as `update` will name them.
notes=$("$python" - "$previous" <<'EOF'
import subprocess
import sys

from autotranscode import update, versions

previous = sys.argv[1]
new = versions.load()
old = {}
if previous:
    text = subprocess.run(["git", "show", f"{previous}:autotranscode/toolchain.toml"],
                          capture_output=True, text=True, check=False).stdout
    old = versions.parse(text) if text else {}
def short(version):
    return version[:12] if len(version) == 40 else version

tools = {name: pin for name, pin in new.items() if pin.stage not in update.SYSTEM_STAGES}
if not old:
    print("Tools: " + ", ".join(f"{update.NAMES.get(name, name)} {short(pin.version)}"
                                for name, pin in tools.items()))
else:
    moved = [f"{update.NAMES.get(name, name)} "
             f"{short(old[name].version) if name in old else 'new'} -> {short(pin.version)}"
             for name, pin in tools.items()
             if name not in old or old[name].version != pin.version]
    print("Tools: " + ", ".join(moved) if moved else "No tool changes.")
EOF
)

git add autotranscode/__init__.py
git diff --cached --quiet || git commit -q -m "Release $version"
git tag -a "$tag" -m "autotranscode $version" -m "$notes"
printf 'Tagged %s on %s\n%s\n' "$tag" "$(git rev-parse --short HEAD)" "$notes"

if [[ $push != --push ]]; then
  printf '\nNot pushed. To publish:\n  git push origin main %s\n' "$tag"
  exit 0
fi
git push origin main "$tag"
if command -v gh >/dev/null; then
  gh release create "$tag" --title "autotranscode $version" --notes "$notes" --verify-tag \
    || printf 'No GitHub release page (see above); the tag, which `update` reads, is pushed.\n'
else
  printf 'gh is not installed: no GitHub release page, but the tag is what `update` reads.\n'
fi
