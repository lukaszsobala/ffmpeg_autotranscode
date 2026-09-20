"""Small shared helpers: process execution, logging, formatting."""

from __future__ import annotations

import contextlib
import functools
import locale
import logging
import os
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("autotranscode")


@functools.cache
def _child_locale() -> str:
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


def child_env(extra_path: Sequence[str] = ()) -> dict[str, str]:
    """os.environ with the locale pinned. LC_ALL overrides LANG and every LC_*.

    `extra_path` is prepended to PATH. A child that resolves its own helpers by
    bare name -- Av1an looks up SvtAv1EncApp, ffmpeg and mkvmerge that way -- would
    otherwise miss the binaries we located through the config, and would silently
    prefer a different copy to the one `doctor` reported on.
    """
    env = {**os.environ, "LC_ALL": _child_locale()}
    if extra_path:
        unique = list(dict.fromkeys(extra_path))
        env["PATH"] = os.pathsep.join([*unique, env.get("PATH", "")]).rstrip(os.pathsep)
    return env


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
