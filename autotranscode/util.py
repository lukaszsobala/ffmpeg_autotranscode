"""Small shared helpers: process execution, logging, formatting."""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("autotranscode")


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
