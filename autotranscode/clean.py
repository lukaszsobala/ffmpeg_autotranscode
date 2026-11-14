"""Remove what earlier runs left behind and no run will use again.

A film's work directory holds its Av1an chunks, scene list and grain table, and
for 4K that is tens of gigabytes. A run removes it once the film is finished or
rejected, but not when the film is later skipped, removed from the library,
replaced, or found already finished after a stop. Staged copies, half-written
outputs and the scratch of `estimate`, `measure` and `verify` stay behind too.

What can still be resumed is kept unless asked otherwise, and what a run is
working on right now is never touched. `sample`'s reports, excerpts and encodes
are results, not leftovers: only the chunks and scratch beside them go, unless
asked otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import pipeline, resume, watch
from .config import Config
from .state import Ledger
from .tools import Toolchain
from .util import human_size

DIGEST = re.compile(r"[0-9a-f]{16}")
# Left by other commands; nothing in them is wanted once the command has ended.
SCRATCH = ("estimate", "measure", "verify")
# Scratch touched this recently may belong to a command still running.
SCRATCH_QUIET_SECONDS = 600
# Where `sample` puts its sample sets unless given --out; each holds its picks here.
SAMPLES = "samples"
PICKS = "picks.json"


@dataclass(slots=True)
class Entry:
    path: Path
    size: int
    remove: bool
    reason: str
    # Only these are removed from `path`, a directory, when given: what else is
    # in it stays.
    only: tuple[Path, ...] | None = None


def plan(
    cfg: Config, chain: Toolchain, ledger: Ledger, sources: Sequence[Path] = (),
    roots: Sequence[Path] = (), *, unknown: bool = False, everything: bool = False,
    sample_sets: Sequence[Path] = (), samples: bool = False,
) -> list[Entry]:
    """What `clean` would remove and keep, and why.

    `sources` -- the library, when given -- names work directories made before
    they recorded their source. `unknown` removes those it still cannot name;
    `everything` removes resumable work too. `sample_sets` are `sample` output
    directories besides those in the work directory; `samples` removes them whole.
    """
    known = _sources(cfg, ledger, sources)
    settings = pipeline.settings_keys(cfg, chain)
    sizes = pipeline.size_keys(cfg, chain)
    entries: list[Entry] = []
    decided: dict[str, bool] = {}   # digest -> whether its work goes

    for work in _children(cfg.work_dir):
        if not (work.is_dir() and DIGEST.fullmatch(work.name)):
            continue
        source = _recorded_source(work) or known.get(work.name)
        remove, reason = _judge(work, source, cfg, ledger, settings, sizes, roots,
                                unknown=unknown, everything=everything)
        decided[work.name] = remove
        only = None
        if remove and reason == "finished" and cfg.grain.keep_table:
            only = tuple(path for path in _children(work) if not _is_table(path))
            reason = "finished; its grain table stays (grain.keep_table)"
        entries.append(Entry(work, _size(only or [work]), remove, reason, only))

    for staged in _children(cfg.staging_dir):
        if not staged.is_file():
            continue
        digest = staged.name[:16]
        if not DIGEST.fullmatch(digest):
            continue
        if _running(cfg.work_dir / digest):
            entries.append(Entry(staged, _size([staged]), False, "in use by a running encode"))
        elif staged.name.endswith(".out.mkv"):
            entries.append(Entry(staged, _size([staged]), True, "a half-written output"))
        elif digest in decided:
            entries.append(Entry(staged, _size([staged]), decided[digest],
                                 "staged copy of the work above"))
        elif cfg.staging.keep and _still_the_source(known.get(digest), staged):
            entries.append(Entry(staged, _size([staged]), False,
                                 "staged copy kept (staging.keep)"))
        else:
            entries.append(Entry(staged, _size([staged]), True, "staged copy nothing uses"))

    for source in {*known.values()}:
        output = _output(source, cfg, roots)
        part = output.with_name(output.name + ".part") if output else None
        if part and part.is_file() and not _running(cfg.work_dir / pipeline.source_digest(source)):
            entries.append(Entry(part, _size([part]), True, "a half-written output"))

    now = time.time()
    for name in SCRATCH:
        scratch = cfg.work_dir / name
        if not scratch.is_dir():
            continue
        quiet = now - _newest(scratch) >= SCRATCH_QUIET_SECONDS
        entries.append(Entry(scratch, _size([scratch]), quiet,
                             f"scratch of `{name}`" if quiet else
                             f"scratch of `{name}`, written to minutes ago: in use?"))

    found = [path for path in _children(cfg.work_dir / SAMPLES) if (path / PICKS).is_file()]
    for sample_set in dict.fromkeys([*found, *sample_sets]):
        entries += _sample_set(sample_set, now, everything=everything, whole=samples)
    return entries


def _sample_set(sample_set: Path, now: float, *, everything: bool, whole: bool) -> list[Entry]:
    """A `sample` output directory: its report, excerpts and encodes are kept unless
    `whole`; its Av1an chunks and scratch go once every sample is done, or with
    `everything`. A rerun into it would resume from them."""
    work = sample_set / "work"
    if (any(_running(path) for path in _children(work))
            or now - _newest(sample_set, folders=False) < SCRATCH_QUIET_SECONDS):
        return [Entry(sample_set, _size([sample_set]), False,
                      "samples, written to minutes ago: `sample` running?")]
    if whole:
        return [Entry(sample_set, _size([sample_set]), True, "samples, removed by --samples")]
    entries = []
    # Excerpts cut short by a stop; a rerun cuts them again from the start.
    cut_short = [*sample_set.glob("*/*.part")]
    leftovers = list(cut_short)
    if work.is_dir():
        if _samples_done(sample_set):
            leftovers.append(work)
        elif everything:
            entries.append(Entry(work, _size([work]), True,
                                 "chunks of an unfinished sample run, removed by --all"))
        else:
            entries.append(Entry(work, _size([work]), False,
                                 "chunks of an unfinished sample run: `sample --out` "
                                 "this directory again resumes it"))
    if leftovers:
        entries.append(Entry(sample_set, _size(leftovers), True,
                             "chunks and scratch of a finished sample run"
                             if work in leftovers else "excerpts cut short by a stop",
                             tuple(leftovers)))
    results = [path for path in _children(sample_set) if path != work]
    entries.append(Entry(sample_set, _size(results) - _size(cut_short), False,
                         "samples: report, excerpts and encodes (--samples removes them)"))
    return entries


def _samples_done(sample_set: Path) -> bool:
    """Whether every pick in a sample set has been encoded."""
    try:
        picks = json.loads((sample_set / PICKS).read_text(encoding="utf-8"))["picks"]
        return all(json.loads((sample_set / pick["id"] / "result.json")
                              .read_text(encoding="utf-8")).get("ok") for pick in picks)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _judge(
    work: Path, source: Path | None, cfg: Config, ledger: Ledger, settings: set[str],
    sizes: set[str], roots: Sequence[Path], *, unknown: bool, everything: bool,
) -> tuple[bool, str]:
    """Whether a work directory goes, and why."""
    if _running(work):
        return False, "in use by a running encode"
    if source is None:
        if unknown or everything:
            return True, "source unknown"
        return False, ("source unknown (from before work directories named theirs): "
                       "pass the library to match it, or --unknown to remove it")
    if not source.exists():
        if not source.parent.is_dir() and not (unknown or everything):
            # Its whole folder is missing: more likely a disk not mounted than a
            # library deleted.
            return False, f"source's folder not found (not mounted?): {source.parent}"
        return True, f"source gone: {source}"
    if ledger.already_done(source):
        return True, "finished"
    if (output := _output(source, cfg, roots)) and output.exists():
        return True, f"output already there: {output}"
    earlier = resume.fingerprint(work)
    if earlier and earlier.get("source") and earlier["source"][0] != source.stat().st_size:
        return True, f"source replaced since: {source.name}"
    if (tried := ledger.standing_skip(source)) and tried.settings in settings:
        return True, f"would not save enough ({tried.reason}); `run` skips it"
    guess = ledger.guess(source, sizes)
    if guess and (short := watch.shortfall(cfg, guess.source_bytes, guess.projected_bytes)):
        return True, f"estimated to come out {short}; `run` skips it"
    if everything:
        return True, f"resumable, removed by --all: {source.name}"
    return False, f"resumable: {source.name}"


def _output(source: Path, cfg: Config, roots: Sequence[Path]) -> Path | None:
    """Where `source`'s output goes, when that is certain. In an output directory
    that mirrors the library it depends on the library's root, and without one
    the guess could be another film of the same name."""
    absolute = source.expanduser().absolute()
    if cfg.output.directory and not any(absolute.is_relative_to(root) for root in roots):
        return None
    return pipeline.output_for(source, cfg, roots)


def _sources(cfg: Config, ledger: Ledger, sources: Sequence[Path]) -> dict[str, Path]:
    """Work directory names, for every source the ledger or the library knows."""
    paths = [Path(record.source) for record in ledger.records.values()]
    paths += [Path(guess.source) for guess in ledger.guesses.values()]
    paths += list(sources)
    known: dict[str, Path] = {}
    for path in paths:
        with contextlib.suppress(OSError):
            known[pipeline.source_digest(path)] = path
    return known


def _recorded_source(work: Path) -> Path | None:
    try:
        return Path((work / pipeline.SOURCE_FILE).read_text(encoding="utf-8").strip())
    except OSError:
        return None


def _running(work: Path) -> bool:
    """Whether the run that marked `work` as its own is still alive."""
    try:
        pid = int((work / pipeline.RUNNING_FILE).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _still_the_source(source: Path | None, staged: Path) -> bool:
    try:
        return source is not None and source.stat().st_size == staged.stat().st_size
    except OSError:
        return False


def _is_table(path: Path) -> bool:
    return path.is_file() and path.suffix == ".tbl"


def _children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _size(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        if path.is_file() or path.is_symlink():
            with contextlib.suppress(OSError):
                total += path.lstat().st_size
            continue
        for folder, _, files in os.walk(path):
            for name in files:
                with contextlib.suppress(OSError):
                    total += (Path(folder) / name).lstat().st_size
    return total


def _newest(directory: Path, *, folders: bool = True) -> float:
    """When anything in `directory` last changed; with `folders` false, any file:
    removing a file changes its folder, so a `clean` would look like work."""
    newest = 0.0
    for folder, _, files in os.walk(directory):
        for name in [*files, "."] if folders else files:
            with contextlib.suppress(OSError):
                newest = max(newest, (Path(folder) / name).stat().st_mtime)
    return newest


def remove(entries: Iterable[Entry]) -> int:
    """Remove what `plan` said goes. Returns the bytes freed."""
    freed = 0
    for entry in entries:
        if not entry.remove:
            continue
        for path in entry.only if entry.only is not None else (entry.path,):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        freed += entry.size
    return freed


def report(entries: list[Entry]) -> str:
    lines = []
    for entry in sorted(entries, key=lambda e: (not e.remove, -e.size)):
        mark = "remove" if entry.remove else "keep  "
        lines.append(f"{mark} {human_size(entry.size):>10}  {entry.path}\n"
                     f"{'':18}{entry.reason}")
    gone = sum(e.size for e in entries if e.remove)
    kept = sum(e.size for e in entries if not e.remove)
    lines.append(f"\n{human_size(gone)} to remove, {human_size(kept)} kept")
    return "\n".join(lines)
