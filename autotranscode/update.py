"""`autotranscode update`: a newer autotranscode, and the tools it was tested
with, installed between films.

Something is nearly always encoding: a film takes the best part of a day, and a
library weeks. So `update` does not install anything under a run. It says whether
there is a newer version and which tools that rebuilds, and queues it. `run`
installs a queued update itself, before it starts a film no run had started yet:
the film in progress, or one stopped part way, is finished with the tools it was
started with, so no film is ever half one encoder and half another. Then it pulls,
rebuilds those tools, and starts its own command again, updated.

Only the tools scripts/bootstrap.sh builds into the user's prefix take part.
jellyfin-ffmpeg and mkvmerge come from apt, which keeps them current, and would
need sudo, which an unattended run does not have.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, versions
from .config import Config
from .tools import Toolchain
from .util import log

QUEUED = "update.queued"     # in the work directory, beside run.lock
BUILD_LOG = "update.log"     # bootstrap.sh's output, when a run installs an update
# Installed and kept current by the system's package manager, not by `update`.
SYSTEM_STAGES = {"deps", "ffmpeg"}
NAMES = {"svtav1": "SVT-AV1", "av1an": "Av1an", "grav1synth": "grav1synth",
         "vapoursynth": "VapourSynth", "lsmash": "L-SMASH-Works", "svtfg": "SvtFgEssential"}
GIT_TIMEOUT = 60


class UpdateError(RuntimeError):
    pass


class InstallQueued(Exception):
    """`run` reached a film no run had started, with an update queued."""


# ---- git ----------------------------------------------------------------------------


def checkout() -> Path | None:
    """The git checkout this package runs from, or None (Docker, pip install)."""
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists() and (root / "scripts" / "bootstrap.sh").is_file():
        return root
    return None


def _git(root: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
            timeout=GIT_TIMEOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} took longer than {GIT_TIMEOUT}s") from None
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError(f"git {args[0]} failed: {detail[-1] if detail else '?'}")
    return result.stdout.strip()


# ---- what there is ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    name: str
    installed: str | None
    version: str

    def __str__(self) -> str:
        def short(text: str) -> str:
            """A commit to 12 characters; "0.6.0-unstable" to 0.6.0, keeping "+patch"."""
            if re.fullmatch(r"[0-9a-f]{13,40}", text):
                return text[:12]
            match = re.match(r"v?(\d+(?:\.\d+)*(?:\+\S+)?)", text)
            return match.group(1) if match else text
        was = "not installed" if self.installed is None else short(self.installed) or "?"
        return f"{NAMES.get(self.name, self.name)} {was} -> {short(self.version)}"


@dataclass(frozen=True, slots=True)
class Update:
    version: str           # the newest release's when newer, else this one
    newer: bool
    changes: list[Change]  # tools that version needs and that are not installed as such
    blocker: str           # why the checkout cannot simply move forward, or ""

    @property
    def wanted(self) -> bool:
        return self.newer or bool(self.changes)


def changes(chain: Toolchain, pins: dict[str, versions.Pin]) -> list[Change]:
    """The tools bootstrap builds that `pins` would have rebuilt; apt's left out."""
    return [Change(item.pin.name, item.installed, item.pin.version)
            for item in versions.check(chain, pins)
            if item.failed and item.pin.stage not in SYSTEM_STAGES]


def stages(chain: Toolchain, pins: dict[str, versions.Pin]) -> str:
    """The bootstrap stages `changes` needs, in bootstrap's order."""
    return versions.fixes([item for item in versions.check(chain, pins)
                           if item.pin.stage not in SYSTEM_STAGES])


def blocking(chain: Toolchain, root: Path) -> list[Change]:
    """The tools this version refuses to encode with that bootstrap can rebuild.

    While there are any, no run can start, so none can finish a film first and
    queueing would wait forever.
    """
    pins = versions.load(root / "autotranscode" / versions.MANIFEST.name)
    return [Change(item.pin.name, item.installed, item.pin.version)
            for item in versions.check(chain, pins)
            if item.failed and item.pin.enforce and item.pin.stage not in SYSTEM_STAGES]


# A release is an annotated tag vX.Y.Z on the commit whose __version__ is X.Y.Z;
# scripts/release.sh makes them. Work on the branch between releases is not offered.
RELEASE_TAG = re.compile(r"v(\d+\.\d+\.\d+)")


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
                          capture_output=True, check=False).returncode == 0


def latest_release(root: Path, *, fetch: bool = True) -> str | None:
    """The newest release tag, fetched from the remote first; None without any."""
    if fetch:
        _git(root, "fetch", "--quiet", "--tags")
    tags = [tag for tag in _git(root, "tag", "--list", "v*").split()
            if RELEASE_TAG.fullmatch(tag)]
    return max(tags, key=lambda tag: versions.numbers(tag[1:]), default=None)


def find(root: Path, chain: Toolchain, *, fetch: bool = True) -> Update:
    """Whether there is a newer release, and what installing it rebuilds."""
    tag = latest_release(root, fetch=fetch)
    newer = tag is not None and versions.numbers(tag[1:]) > versions.numbers(__version__)
    if newer:
        pins = versions.parse(_git(root, "show", f"{tag}:autotranscode/{versions.MANIFEST.name}"),
                              f"{tag}:{versions.MANIFEST.name}")
    else:
        pins = versions.load(root / "autotranscode" / versions.MANIFEST.name)
    blocker = ""
    if newer and tag is not None:
        if not _is_ancestor(root, "HEAD", tag):
            blocker = (f"this checkout has commits that {tag} does not, so it cannot simply "
                       f"move to it. If they are yours, merge {tag} yourself; if not, "
                       f"`git reset --hard {tag}`")
        elif _git(root, "status", "--porcelain", "--untracked-files=no"):
            blocker = "this checkout has uncommitted changes; commit or stash them first"
    return Update(tag[1:] if newer and tag else __version__, newer, changes(chain, pins),
                  blocker)


def describe(update: Update) -> str:
    rebuilds = ", ".join(map(str, update.changes))
    if update.newer:
        return (f"autotranscode {update.version} is available (this is {__version__}).\n"
                + (f"It updates: {rebuilds}" if rebuilds else "It updates no tools."))
    if update.changes:
        return (f"autotranscode {__version__} is the newest, but these tools are not the "
                f"ones it was tested with: {rebuilds}")
    return f"autotranscode {__version__} is the newest, with the tools it was tested with."


# ---- the queue ----------------------------------------------------------------------


def queued(cfg: Config) -> dict | None:
    try:
        entry = json.loads((cfg.work_dir / QUEUED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def queue(cfg: Config, update: Update) -> None:
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.work_dir / QUEUED).write_text(json.dumps({
        "version": update.version, "changes": [str(change) for change in update.changes],
        "queued_at": time.strftime("%Y-%m-%d %H:%M"),
    }), encoding="utf-8")


def unqueue(cfg: Config) -> bool:
    try:
        (cfg.work_dir / QUEUED).unlink()
    except FileNotFoundError:
        return False
    return True


def queued_note(cfg: Config) -> str:
    """What `run` says at the start about a queued update, or ""."""
    if (entry := queued(cfg)) is None:
        return ""
    return (f"an update to autotranscode {entry.get('version', '?')} is queued; it installs "
            "once the films already started are finished")


# ---- installing ---------------------------------------------------------------------


def install(root: Path, chain: Toolchain, *, build_log: Path | None = None) -> bool:
    """Move to the newest release, then rebuild what its pins need. False when
    either failed.

    With `build_log`, bootstrap.sh's long output goes there rather than to the
    terminal: a run's own log should say what happened, not hold every compiler line.
    """
    try:
        tag = latest_release(root)
        if tag and not _is_ancestor(root, tag, "HEAD"):
            # Fast-forward the branch to the release: still on it, at that commit.
            _git(root, "merge", "--ff-only", "--quiet", tag)
    except UpdateError as exc:
        log.error("update not installed: %s", exc)
        return False
    # Against the manifest now on disk: the new version's.
    needed = stages(chain, versions.load(root / "autotranscode" / versions.MANIFEST.name))
    if not needed:
        return True
    log.info("building %s with scripts/bootstrap.sh%s", needed,
             f"; its output goes to {build_log}" if build_log else "")
    argv = ["bash", str(root / "scripts" / "bootstrap.sh"), *needed.split()]
    if build_log is None:
        return subprocess.run(argv, check=False).returncode == 0
    with build_log.open("w", encoding="utf-8") as out:
        ok = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT).returncode == 0
    if not ok:
        tail = build_log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        log.error("bootstrap.sh %s failed; the end of %s:\n%s", needed, build_log,
                  "\n".join(tail))
    return ok


def install_queued(cfg: Config, chain: Toolchain,
                   execute: Callable[[str, list[str]], object] = os.execv) -> None:
    """In a run, between films: install the queued update and start this command
    again. The run still holds the work directory, so nothing else starts in it;
    exec lets go of it (Python opens files close-on-exec) and the new process,
    running the new code, takes it again."""
    entry = queued(cfg) or {}
    unqueue(cfg)   # once: a failing build is not retried before every film
    root = checkout()
    if root is None:
        log.error("an update is queued, but this is not a git checkout; ignoring it")
        restart(execute)
        return
    log.info("installing the queued update to autotranscode %s before the next film",
             entry.get("version", "?"))
    if not install(root, chain, build_log=cfg.work_dir / BUILD_LOG):
        log.error("starting again regardless; with tools other than the tested ones the "
                  "run refuses to encode, and `autotranscode doctor` says what to do")
    restart(execute)


def restart(execute: Callable[[str, list[str]], object] = os.execv) -> None:
    """Run this same command line again, in this process, with the code on disk."""
    argv = list(sys.orig_argv)
    log.info("starting again: %s", shlex.join(argv))
    for handler in logging.getLogger().handlers:
        handler.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    execute(sys.executable, argv)


# ---- the command --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    pid: int
    film: str   # what it is encoding, or "" between films


def running(cfg: Config) -> Run | None:
    """The `run` holding this work directory, if any."""
    from .pipeline import RUN_LOCK, RUNNING_FILE, SOURCE_FILE

    try:
        handle = (cfg.work_dir / RUN_LOCK).open("r", encoding="ascii")
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = handle.read().strip()
        else:
            return None   # nobody holds it
    pid = int(text) if text.isdigit() else 0
    for marker in cfg.work_dir.glob(f"*/{RUNNING_FILE}"):
        try:
            if marker.read_text(encoding="ascii").strip() == str(pid):
                source = (marker.parent / SOURCE_FILE).read_text(encoding="utf-8").strip()
                return Run(pid, Path(source).name)
        except OSError:
            continue
    return Run(pid, "")


def main(
    cfg: Config, chain: Toolchain, *, check_only: bool = False, yes: bool = False,
    cancel: bool = False, root: Path | None = None, ask: Callable[[str], str] = input,
    doctor: Callable[[], int] | None = None,
) -> int:
    """`autotranscode update`. 0: up to date, queued or installed; 1: an update is
    available (with --check), or declined, blocked or failed."""
    from .pipeline import AlreadyRunning, only_run, started_films

    if cancel:
        print("Queued update cancelled." if unqueue(cfg) else "No update is queued.")
        return 0
    root = root or checkout()
    if root is None:
        print("This autotranscode is not a git checkout, so it cannot update itself.\n"
              "In Docker: update the checkout on the host, then `docker compose build`.")
        return 1
    try:
        found = find(root, chain)
    except UpdateError as exc:
        print(f"Cannot check for an update: {exc}")
        return 1
    print(describe(found))
    if not found.wanted:
        return 0
    if found.blocker:
        print(f"It cannot be installed: {found.blocker}.")
        return 1
    if check_only:
        return 1
    if entry := queued(cfg):
        print(f"It is queued already (since {entry.get('queued_at', '?')}); "
              "`autotranscode update --cancel` unqueues it.")
        return 0
    if not (yes or ask is not input or sys.stdin.isatty()):
        print("No terminal to ask on; rerun with --yes.")
        return 1

    def agreed(question: str) -> bool:
        return yes or ask(f"{question} [Y/n] ").strip().lower() not in {"n", "no"}

    run = running(cfg)
    started = started_films(cfg)
    stuck = blocking(chain, root) if run is None else []
    if stuck:
        # No run can start with these tools, so none could finish a film first.
        print("No run can encode until " + ", ".join(NAMES.get(c.name, c.name) for c in stuck)
              + (" is" if len(stuck) == 1 else " are") + " rebuilt"
              + (f"; {started[0].name} resumes after that." if started else "."))
        when = ""
    elif run is not None:
        when = f"after the run (pid {run.pid}) finishes {run.film or 'its film'}"
    elif started:
        names = ", ".join(path.name for path in started[:3])
        more = f" and {len(started) - 3} more" if len(started) > 3 else ""
        when = f"once the next `autotranscode run` has finished {names}{more}"
    else:
        when = ""
    if when:
        if not agreed(f"Queue it to install {when}?"):
            return 1
        queue(cfg, found)
        print(f"Queued: it installs {when}, and the run carries on updated.")
        return 0

    # Nothing is encoding, and nothing can, or waits to be finished: install it now.
    if not agreed("Install it now?" if stuck else "Nothing is encoding. Install it now?"):
        return 1
    try:
        with only_run(cfg):
            ok = install(root, chain)
    except AlreadyRunning:
        print("A run started meanwhile; rerun `autotranscode update` to queue it.")
        return 1
    if ok and doctor is not None:
        ok = doctor() == 0
    print("Updated." if ok else "Updated, with problems left; see above.")
    return 0 if ok else 1
