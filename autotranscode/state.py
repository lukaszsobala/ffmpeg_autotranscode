"""A small JSON ledger so a library crawl can be stopped and resumed.

Deliberately not a database. The unit of work is one file taking hours; contention
is not a concern, and a file you can read and hand-edit is worth more here.
"""

from __future__ import annotations

import json
import os
import tempfile
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

    @property
    def saving_percent(self) -> float:
        if not self.source_bytes or not self.output_bytes:
            return 0.0
        return 100.0 * (1 - self.output_bytes / self.source_bytes)


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

    def save(self) -> None:
        """Write atomically: a crash mid-write must not destroy hours of bookkeeping."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "records": {key: asdict(record) for key, record in self.records.items()},
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

    def record(self, source: Path, record: Record) -> None:
        self.records[_key(source)] = record
        self.save()

    def summary(self) -> str:
        done = [r for r in self.records.values() if r.status == "done"]
        failed = [r for r in self.records.values() if r.status == "failed"]
        skipped = [r for r in self.records.values() if r.status == "skipped"]
        saved = sum(r.source_bytes - r.output_bytes for r in done)
        lines = [f"{len(done)} done, {len(failed)} failed, {len(skipped)} skipped"]
        if done:
            mean = sum(r.saving_percent for r in done) / len(done)
            lines.append(f"mean saving {mean:.1f}%, {saved / 2**30:.1f} GiB reclaimed")
        return "; ".join(lines)
