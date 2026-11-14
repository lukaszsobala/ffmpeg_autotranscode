"""Small shared helpers: process execution, logging, formatting."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import locale
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import progress

log = logging.getLogger("autotranscode")


@functools.cache
def child_locale() -> str:
    """A locale name verified to exist, forced on every child process.

    Two reasons not to inherit whatever the shell happens to have.

    First, a locale that is *named* but never generated breaks C++ tools outright.
    A stock DietPi image sets LC_NUMERIC=en_US.UTF-8 while generating only en_GB,
    and mkvmerge then aborts with "locale::facet::_S_create_c_locale name not
    valid" -- on every invocation, which kills Av1an's concatenation step after a
    full encode has already been paid for.

    Second, we hand decimal numbers to these tools ("--ac-bias 0.5",
    "hqdn3d=1.4:1.0:6:6", "L(1000.0000,0.0001)"). A locale using comma as the
    decimal separator would change how they are parsed.
    """
    previous = locale.setlocale(locale.LC_ALL)
    try:
        for candidate in ("C.UTF-8", "C.utf8", "C"):
            try:
                locale.setlocale(locale.LC_ALL, candidate)
            except locale.Error:
                continue
            return candidate
        return "C"
    finally:
        with contextlib.suppress(locale.Error):
            locale.setlocale(locale.LC_ALL, previous)


def digest(settings: dict[str, object]) -> str:
    """A short, stable digest of `settings`, for naming what was made with them."""
    text = json.dumps(settings, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def child_env(extra_path: Sequence[str] = ()) -> dict[str, str]:
    """os.environ with the locale pinned. LC_ALL overrides LANG and every LC_*.

    `extra_path` is prepended to PATH. A child that resolves its own helpers by
    bare name -- Av1an looks up SvtAv1EncApp, ffmpeg and mkvmerge that way -- would
    otherwise miss the binaries we located through the config, and would silently
    prefer a different copy to the one `doctor` reported on.

    Our own virtualenv's bin directory is left out. VapourSynth embeds Python and
    takes the first python3 on PATH as its home: the venv's, with the user site --
    where the vapoursynth module lives -- switched off. Av1an then panics with
    "Failed to get VSScript API", even for --version, whenever autotranscode runs
    from an activated venv.
    """
    env = {**os.environ, "LC_ALL": child_locale()}
    path = [entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry and entry not in _own_venv_bins()]
    env["PATH"] = os.pathsep.join(dict.fromkeys([*extra_path, *path]))
    return env


def _own_venv_bins() -> set[str]:
    if sys.prefix == sys.base_prefix:
        return set()
    bin_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    return {str(bin_dir), str(bin_dir.resolve())}


def unusable_locale_vars() -> list[str]:
    """Locale variables in *our own* environment naming a locale this machine lacks.

    Never a problem for a run -- every child gets LC_ALL forced -- but a real one
    for the commands `plan` prints, which are meant to be pasted into the same
    shell, where mkvmerge dies with "_S_create_c_locale name not valid". SSH is the
    usual source: `AcceptEnv LANG LC_*` imports the client's locales wholesale, and
    a headless board has generated almost none of them.
    """
    bad: list[str] = []
    previous = locale.setlocale(locale.LC_ALL)
    try:
        for name, value in sorted(os.environ.items()):
            if not value or (name != "LANG" and not name.startswith("LC_")):
                continue
            try:
                locale.setlocale(locale.LC_ALL, value)
            except locale.Error:
                bad.append(f"{name}={value}")
    finally:
        with contextlib.suppress(locale.Error):
            locale.setlocale(locale.LC_ALL, previous)
    return bad


class CommandError(RuntimeError):
    """A subprocess exited non-zero."""

    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"command failed with exit {returncode}: {shlex.join(argv)}\n{stderr.strip()[-4000:]}"
        )


@dataclass(slots=True)
class Completed:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def run(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    dry_run: bool = False,
    extra_path: Sequence[str] = (),
) -> Completed:
    """Run a command, logging it first so failures are always reproducible by hand."""
    log.debug("exec: %s", shlex.join(argv))
    if dry_run:
        return Completed(argv, 0, "", "", 0.0)

    started = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=capture,
        text=True,
        errors="replace",
        env=child_env(extra_path),
    )
    elapsed = time.monotonic() - started
    result = Completed(argv, proc.returncode, proc.stdout or "", proc.stderr or "", elapsed)

    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, result.stderr)
    return result


MIB = 1024 * 1024


def available_memory(meminfo: Path = Path("/proc/meminfo")) -> int:
    """Bytes the kernel could hand out now without swapping, or 0 when unknown.

    Linux's MemAvailable: free memory plus the caches it would drop for it.
    """
    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            name, _, value = line.partition(":")
            if name == "MemAvailable":
                return int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:,.1f} {unit}"
        value /= 1024.0
    return f"{value:,.1f} PiB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def setup_logging(verbosity: int, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbosity > 1 else logging.INFO if verbosity == 1 else logging.WARNING
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def run_watched(
    argv: list[str],
    check: Callable[[], str | None],
    *,
    every: float = 60.0,
    extra_path: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    redraw: bool = False,
    quiet: bool = False,
) -> str | None:
    """Run a long command with its output on the terminal, asking `check` every
    `every` seconds whether to stop it. Returns why it was stopped, or None when it
    ran to the end. `extra_env` is added to its environment. `redraw` is for Av1an:
    its progress bar is redrawn to fit the terminal (`progress`); `quiet` also
    drops its progress reports and erases the bar at the end (`progress.QUIET`).

    The command gets a process group of its own, so that stopping it stops its
    children too: Av1an's ffmpeg and encoder processes would otherwise run on to
    the end of their chunk. Outside the terminal's foreground group it no longer
    sees Ctrl-C, so an interrupt arriving here is passed on to it.
    """
    log.debug("exec: %s", shlex.join(argv))
    env = {**child_env(extra_path), **(extra_env or {})}
    relay = progress.Relay(quiet=quiet) if redraw and progress.usable(sys.stderr) else None
    output = relay.child_fd if relay else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                env=env, process_group=0)
        if relay:
            relay.started()
        try:
            while True:
                try:
                    returncode = proc.wait(timeout=every)
                    break
                except subprocess.TimeoutExpired:
                    if reason := check():
                        if relay:
                            relay.stop()
                        _stop_group(proc)
                        return reason
        except BaseException:
            if relay:
                relay.stop()
            _stop_group(proc)
            raise
    finally:
        if relay:
            relay.close()
    if returncode != 0:
        raise CommandError(argv, returncode, "")
    return None


def run_progress(
    argv: list[str],
    label: str,
    *,
    frames: int | None = None,
    every: float = 60.0,
    extra_path: Sequence[str] = (),
) -> float:
    """Run an ffmpeg pass that writes nothing to the terminal, saying what it is,
    how far it has got every `every` seconds, and how long it took. Returns the
    seconds taken; raises CommandError like run().

    `argv` must ask for machine-readable progress on stdout (PROGRESS_ARGS). Its
    frame count is the output's: `frames` is how many that will be, for a share
    done and a time left. 0 when unknown shows the count alone; None hides it,
    for passes whose output is a handful of selected frames.
    """
    log.debug("exec: %s", shlex.join(argv))
    log.info("  %s...", label)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace",
                            env=child_env(extra_path))
    assert proc.stdout is not None and proc.stderr is not None
    stderr: list[str] = []
    err = proc.stderr   # the assert does not narrow inside a lambda
    drain = threading.Thread(target=lambda: stderr.append(err.read()), daemon=True)
    drain.start()
    started = last = time.monotonic()
    done = 0
    try:
        with proc.stdout:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "frame" and value.isdigit():
                    done = int(value)
                elif value == "continue" and time.monotonic() - last >= every:
                    last = time.monotonic()
                    log.info("  %s: %s", label, _progress(done, frames, last - started))
        returncode = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        drain.join()
        proc.stderr.close()
    elapsed = time.monotonic() - started
    if returncode != 0:
        raise CommandError(argv, returncode, "".join(stderr))
    log.info("  %s: done in %s", label, human_duration(elapsed))
    return elapsed


# ffmpeg options for run_progress: key=value blocks on stdout every 5 seconds.
PROGRESS_ARGS = ("-progress", "pipe:1", "-stats_period", "5")


def _progress(done: int, total: int | None, elapsed: float) -> str:
    so_far = f"{human_duration(elapsed)} so far"
    if total is None:
        return f"still running, {so_far}"
    if not total:
        return f"{done:,} frames, {so_far}"
    share = min(done / total, 1.0)
    text = f"{done:,} of {total:,} frames ({share:.0%}), {so_far}"
    if 0 < share < 1:
        text += f", about {human_duration(elapsed / share - elapsed)} left"
    return text


def _stop_group(proc: subprocess.Popen[bytes], grace: float = 10.0) -> None:
    """SIGTERM the whole group, then SIGKILL whatever is left after `grace`."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
