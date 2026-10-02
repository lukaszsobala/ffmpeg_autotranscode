"""A small JSON ledger so a library crawl can be stopped and resumed.

Deliberately not a database. The unit of work is one file taking hours; contention
is not a concern, and a file you can read and hand-edit is worth more here.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Collection
from dataclasses import asdict, dataclass
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
    # For a file that did not save enough: pipeline.settings_key when it was tried.
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
        if not self.path.is_file():
            return
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read state file %s (%s); starting fresh", self.path, exc)
            return
        for key, value in raw.get("records", {}).items():
            try:
                self.records[key] = Record(**value)
            except TypeError:
                log.debug("dropping incompatible state entry %s", key)
        for key, value in raw.get("estimates", {}).items():
            try:
                self.guesses[key] = Guess(**value)
            except TypeError:
                log.debug("dropping incompatible estimate %s", key)

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

    def no_saving(self, source: Path) -> Record | None:
        """The record of an encode that came out too large, if that is the last word."""
        record = self.get(source)
        return record if record and record.status == "skipped" and record.settings else None

    def record(self, source: Path, record: Record) -> None:
        self.records[_key(source)] = record
        self.save()

    def guess(self, source: Path, settings: str | Collection[str]) -> Guess | None:
        """The estimate for `source` made with these settings, if there is one.
        `settings` is a digest, or several that count as the same settings."""
        accepted = {settings} if isinstance(settings, str) else set(settings)
        guess = self.guesses.get(_key(source))
        return guess if guess and guess.settings in accepted else None

    def record_guess(self, source: Path, guess: Guess) -> None:
        self.guesses[_key(source)] = guess
        self.save()

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
