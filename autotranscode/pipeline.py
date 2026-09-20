"""Per-file orchestration and library discovery.

The whole pipeline for one file:

    probe -> skip checks -> grain probe -> grain table -> Av1an encode
          -> grav1synth apply -> mux audio/subs/chapters -> size sanity check

Each step delegates to a tool that already does that job well; this module decides
what to run and what to do when a step declines to help.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import time
from pathlib import Path

from . import encode, grain, mux
from .config import Config
from .probe import MediaInfo, probe
from .state import Ledger, Record
from .tools import Toolchain
from .util import CommandError, human_duration, human_size, log


class SkipFile(Exception):
    """This file should not be transcoded; the message says why."""


def discover(inputs: list[Path], cfg: Config) -> list[Path]:
    """Expand files, directories and .txt lists into a sorted list of media files."""
    extensions = {ext.lower() for ext in cfg.library.extensions}
    found: list[Path] = []

    for entry in inputs:
        entry = entry.expanduser()
        if entry.is_dir():
            pattern = "**/*" if cfg.library.recursive else "*"
            found += [
                p for p in sorted(entry.glob(pattern))
                if p.is_file() and p.suffix.lower() in extensions
            ]
        elif entry.suffix.lower() == ".txt":
            for line in entry.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    listed = Path(line).expanduser()
                    if listed.is_file():
                        found.append(listed)
                    else:
                        log.warning("listed file does not exist: %s", listed)
        elif entry.is_file():
            found.append(entry)
        else:
            log.warning("not found: %s", entry)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def output_path(info: MediaInfo, cfg: Config) -> Path:
    directory = (
        Path(cfg.output.directory).expanduser() if cfg.output.directory else info.path.parent
    )
    name = f"{info.path.stem}{cfg.output.suffix}.{cfg.output.container}"
    return directory / name


def check_skip(info: MediaInfo, output: Path, cfg: Config) -> None:
    if info.codec in cfg.library.skip_codecs:
        raise SkipFile(f"already {info.codec}")
    if info.duration and info.duration < cfg.library.min_duration_sec:
        raise SkipFile(f"shorter than {cfg.library.min_duration_sec:.0f}s")
    if info.has_dolby_vision:
        # The DV RPU travels as side data that Av1an's chunked pipeline does not
        # carry through; transcoding would silently drop it to plain HDR10.
        raise SkipFile("Dolby Vision RPU would be lost")
    if output.resolve() == info.path.resolve():
        raise SkipFile("output path equals source path")
    if output.exists():
        raise SkipFile(f"output already exists: {output.name}")


def _work_dir(info: MediaInfo, cfg: Config) -> Path:
    digest = hashlib.sha256(str(info.path.resolve()).encode()).hexdigest()[:16]
    path = cfg.work_dir / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_one(
    source: Path, chain: Toolchain, cfg: Config, *, dry_run: bool = False
) -> Record:
    started = time.monotonic()
    info = probe(source, chain)
    log.info("%s", source.name)
    log.info("  %s, %s", info.describe(), human_size(info.size_bytes))

    final = output_path(info, cfg)
    check_skip(info, final, cfg)
    final.parent.mkdir(parents=True, exist_ok=True)

    work = _work_dir(info, cfg)
    report: grain.GrainReport | None = None
    table: Path | None = None

    if cfg.grain.mode != "never":
        report = grain.measure(info, chain, cfg, work)
        log.info("  %s", report.describe())
        if report.grainy and not dry_run:
            table = grain.build_table(info, chain, cfg, report, work)
            if table is None:
                log.warning("  no grain table; encoding without denoising to avoid losing grain")

    # Only denoise when we actually have a table to put the grain back with.
    denoise = report.denoise_filter if (report and table) else ""

    encoded = work / "encoded.mkv"
    encoded.unlink(missing_ok=True)
    argv = encode.build_command(info, encoded, chain, cfg, work / "av1an", denoise)
    encode.run_encode(argv, cfg, dry_run=dry_run)

    if dry_run:
        return Record(
            source=str(source), status="skipped", reason="dry run",
            source_bytes=info.size_bytes,
            grain_psnr=report.mean_psnr if report else None,
            denoise=denoise,
        )

    if not encoded.is_file() or not encoded.stat().st_size:
        raise CommandError(argv, 1, "Av1an produced no output")

    video = encoded
    if table:
        grainy = work / "grainy.mkv"
        try:
            grain.apply_table(encoded, table, chain, grainy, cfg.grain.chroma)
            video = grainy
            log.info("  film grain applied from table")
        except CommandError as exc:
            log.warning("  grav1synth apply failed (%s); shipping the denoised encode",
                        exc.stderr.strip()[:200])

    mux.mux(video, info, final, chain, cfg)

    output_bytes = final.stat().st_size
    saving = 100.0 * (1 - output_bytes / info.size_bytes) if info.size_bytes else 0.0
    elapsed = time.monotonic() - started

    if saving < cfg.output.min_saving_percent:
        final.unlink(missing_ok=True)
        _cleanup(work, cfg, table)
        raise SkipFile(
            f"only {saving:.1f}% smaller, below the {cfg.output.min_saving_percent:.0f}% threshold"
        )

    log.info(
        "  done in %s: %s -> %s (%.1f%% smaller)",
        human_duration(elapsed), human_size(info.size_bytes), human_size(output_bytes), saving,
    )

    if cfg.output.replace_source:
        source.unlink()
        log.info("  source deleted (output.replace_source is on)")

    _cleanup(work, cfg, table)

    return Record(
        source=str(source),
        status="done",
        output=str(final),
        source_bytes=info.size_bytes,
        output_bytes=output_bytes,
        grain_psnr=report.mean_psnr if report else None,
        denoise=denoise,
        seconds=elapsed,
        finished_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


def _cleanup(work: Path, cfg: Config, table: Path | None) -> None:
    keep = table if (table and cfg.grain.keep_table) else None
    saved = keep.read_text(encoding="utf-8") if keep and keep.is_file() else None
    shutil.rmtree(work, ignore_errors=True)
    if saved is not None and keep is not None:
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(saved, encoding="utf-8")


def process_all(
    sources: list[Path], chain: Toolchain, cfg: Config, ledger: Ledger, *, dry_run: bool = False
) -> int:
    failures = 0
    total = len(sources)

    for index, source in enumerate(sources, start=1):
        log.info("[%d/%d] %s", index, total, source)

        if done := ledger.already_done(source):
            log.info("  skipping, already transcoded to %s", done.output)
            continue

        try:
            record = process_one(source, chain, cfg, dry_run=dry_run)
        except SkipFile as exc:
            log.info("  skipped: %s", exc)
            ledger.record(source, Record(source=str(source), status="skipped", reason=str(exc)))
            continue
        except (CommandError, OSError, ValueError) as exc:
            failures += 1
            log.error("  failed: %s", str(exc).splitlines()[0] if str(exc) else exc)
            log.debug("failure detail", exc_info=True)
            ledger.record(
                source, Record(source=str(source), status="failed", reason=str(exc)[:500])
            )
            continue
        except KeyboardInterrupt:
            log.warning("interrupted; Av1an progress is kept, rerun to resume")
            raise

        ledger.record(source, record)

    log.info("library run complete: %s", ledger.summary())
    return failures
