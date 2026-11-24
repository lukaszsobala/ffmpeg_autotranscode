"""A small JSON ledger so a library crawl can be stopped and resumed.

Deliberately not a database. The unit of work is one file taking hours, and a
file you can read and hand-edit is worth more here. Contention is rare but real: a
`run` going for days and an `estimate` started beside it share the file, so each
change is made under a lock against what is on disk at that moment.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Collection, Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .util import log


@dataclass(slots=True)
class Record:
    source: str
    status: str  # done | failed | skipped
    reason: str = ""
    output: str = ""
    source_bytes: int = 0
    output_bytes: int = 0
    grain_psnr: float | None = None
    denoise: str = ""
    seconds: float = 0.0
    finished_at: str = ""
    # For a skip that stands while its settings do: pipeline.settings_key for a
    # file that did not save enough, pipeline.skip_key for one the library's skip
    # settings rule out.
    settings: str = ""

    @property
    def saving_percent(self) -> float:
        if not self.source_bytes or not self.output_bytes:
            return 0.0
        return 100.0 * (1 - self.output_bytes / self.source_bytes)


@dataclass(slots=True)
class Guess:
    """What `estimate` projected for a source. Kept apart from its Record: a guess
    is not a verdict, and a newer estimate simply replaces it."""

    source: str
    source_bytes: int
    projected_bytes: int
    encode_seconds: float
    crf: float
    # pipeline.size_key when it was sampled; a guess made with other settings is ignored.
    settings: str
    estimated_at: str = ""

    @property
    def saved_bytes(self) -> int:
        return self.source_bytes - self.projected_bytes

    @property
    def saved_per_hour(self) -> float:
        hours = self.encode_seconds / 3600
        return self.saved_bytes / hours if hours else 0.0


def _key(path: Path) -> str:
    """Identify a source by path plus size, so an edited file is reprocessed."""
    try:
        return f"{path.resolve()}::{path.stat().st_size}"
    except OSError:
        return str(path.resolve())


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, Record] = {}
        self.guesses: dict[str, Guess] = {}
        self._load()

    def _load(self) -> None:
        """Replace what this process holds with what is on disk. A file that cannot
        be read leaves it as it was, so a bad edit by hand costs nothing more."""
        if not self.path.is_file():
            self.records, self.guesses = {}, {}
            return
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read state file %s (%s); not using it", self.path, exc)
            return
        records: dict[str, Record] = {}
        guesses: dict[str, Guess] = {}
        # Fields this version does not know -- a newer one's -- are left out rather
        # than the entry: dropping it, and saving, would lose a finished film's
        # record to an older process running beside a newer one.
        record_fields = {field.name for field in fields(Record)}
        guess_fields = {field.name for field in fields(Guess)}
        for key, value in raw.get("records", {}).items():
            try:
                records[key] = Record(**{k: v for k, v in value.items() if k in record_fields})
            except (TypeError, AttributeError):
                log.debug("dropping incompatible state entry %s", key)
        for key, value in raw.get("estimates", {}).items():
            try:
                guesses[key] = Guess(**{k: v for k, v in value.items() if k in guess_fields})
            except (TypeError, AttributeError):
                log.debug("dropping incompatible estimate %s", key)
        self.records, self.guesses = records, guesses

    @contextlib.contextmanager
    def _changing(self) -> Iterator[None]:
        """Hold the lock, re-read the file, let the caller make one change, save.

        Each process loads the ledger once, and used to write its whole copy back
        on every save: a run's "done" could erase an estimate made since, and the
        estimate's next save erase the "done" -- a finished film then encoded
        again if its output was moved.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)   # released when the file closes
            self._load()
            yield
            self.save()

    def save(self) -> None:
        """Write atomically: a crash mid-write must not destroy hours of bookkeeping."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "records": {key: asdict(record) for key, record in self.records.items()},
            "estimates": {key: asdict(guess) for key, guess in self.guesses.items()},
        }
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
            "w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp"
        )
        try:
            with handle:
                json.dump(payload, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except OSError:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def get(self, source: Path) -> Record | None:
        return self.records.get(_key(source))

    def already_done(self, source: Path) -> Record | None:
        record = self.get(source)
        return record if record and record.status == "done" else None

    def standing_skip(self, source: Path) -> Record | None:
        """The record of a skip that stands while its `settings` do, if that is the
        last word: an encode that came out too large, or a file ruled out."""
        record = self.get(source)
        return record if record and record.status == "skipped" and record.settings else None

    def outputs(self) -> set[Path]:
        """Where the finished encodes went."""
        return {Path(record.output).resolve() for record in self.records.values()
                if record.status == "done" and record.output}

    def record(self, source: Path, record: Record) -> None:
        with self._changing():
            self.records[_key(source)] = record

    def guess(self, source: Path, settings: str | Collection[str]) -> Guess | None:
        """The estimate for `source` made with these settings, if there is one.
        `settings` is a digest, or several that count as the same settings."""
        accepted = {settings} if isinstance(settings, str) else set(settings)
        guess = self.guesses.get(_key(source))
        return guess if guess and guess.settings in accepted else None

    def record_guess(self, source: Path, guess: Guess) -> None:
        with self._changing():
            self.guesses[_key(source)] = guess

    def summary(self) -> str:
        done = [r for r in self.records.values() if r.status == "done"]
        failed = [r for r in self.records.values() if r.status == "failed"]
        skipped = [r for r in self.records.values() if r.status == "skipped"]
        saved = sum(r.source_bytes - r.output_bytes for r in done)
        lines = [f"{len(done)} done, {len(failed)} failed, {len(skipped)} skipped"]
        if self.guesses:
            lines.append(f"{len(self.guesses)} estimated")
        if done:
            mean = sum(r.saving_percent for r in done) / len(done)
            lines.append(f"mean saving {mean:.1f}%, {saved / 2**30:.1f} GiB reclaimed")
        return "; ".join(lines)
