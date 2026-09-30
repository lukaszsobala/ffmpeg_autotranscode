"""Per-file orchestration and library discovery.

The whole pipeline for one file:

    probe -> skip checks -> grain table -> Av1an encode (stopped early if it will
          not save enough) -> grav1synth apply, calibrated to the source's grain
          -> mux audio/subs/chapters -> size check -> move into place

Each step delegates to a tool that already does that job well; this module decides
what to run and what to do when a step declines to help.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import itertools
import json
import os
import shutil
import signal
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, replace
from pathlib import Path

from . import encode, grain, mux, watch
from .config import Config
from .probe import MediaInfo, probe
from .state import Ledger, Record
from .tools import Toolchain
from .util import CommandError, human_duration, human_size, log


class SkipFile(Exception):
    """This file should not be transcoded; the message says why."""


class OutputExists(Exception):
    """A file took the output's name while the encode ran. The encode is kept
    beside it under another name rather than written over it."""


class NoSaving(SkipFile):
    """The encode would not save enough. `settings` is `settings_key` at the time,
    so that the file is not tried again until something that decides size changes."""

    def __init__(self, message: str, settings: str):
        super().__init__(message)
        self.settings = settings


def settings_key(cfg: Config, chain: Toolchain) -> str:
    """A digest of everything that decides how large an encode comes out.

    A file that did not save enough is skipped while this stays the same: trying
    again would spend the same hours to reach the same size.
    """
    decisive = {
        "encode": asdict(cfg.encode),
        "grain": asdict(cfg.grain),
        "audio": asdict(cfg.audio),
        "min_saving_percent": cfg.output.min_saving_percent,
        "min_saving_mib": cfg.output.min_saving_mib,
        "svt_av1": chain.svtav1_version,
    }
    return hashlib.sha256(json.dumps(decisive, sort_keys=True, default=str).encode()).hexdigest()[:16]


class StopRequest:
    """Whether a stop signal has arrived. See `stop_signals`."""

    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen = False


@contextlib.contextmanager
def stop_signals() -> Iterator[StopRequest]:
    """Make stopping a run deterministic, for both Ctrl-C and `kill`.

    Two problems this solves. Av1an traps SIGINT itself and exits non-zero, which
    races Python's own KeyboardInterrupt: the same Ctrl-C was sometimes recorded as
    a *failed* file rather than an interrupted one. And SIGTERM -- what `systemctl
    stop` and a plain `kill` send -- killed the run outright, skipping the cleanup
    that decides what to keep.

    Both now set a flag and raise KeyboardInterrupt, so there is exactly one stop
    path. A second signal restores the default handler first, so an operator who
    really wants out is never trapped by a tool that is slow to wind down.
    """
    state = StopRequest()

    def handle(signum: int, _frame: object) -> None:
        state.seen = True
        signal.signal(signum, signal.SIG_DFL)
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield state
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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


def library_roots(inputs: list[Path]) -> list[Path]:
    """The directories among the inputs, which an output directory mirrors."""
    return [entry.expanduser().absolute() for entry in inputs if entry.expanduser().is_dir()]


def output_path(info: MediaInfo, cfg: Config, roots: Sequence[Path] = ()) -> Path:
    """Where the encode goes: next to the source, or in output.directory.

    An output directory mirrors the folders below the library directory the source
    was found in, so two films that share a file name in different folders do not
    share an output. Of nested roots the outermost counts, so running on a whole
    library and on one of its folders puts a film in the same place. A file given
    on its own, or through a .txt list, goes to the top of the output directory.
    """
    name = f"{info.path.stem}{cfg.output.suffix}.mkv"
    if not cfg.output.directory:
        return info.path.parent / name
    directory = Path(cfg.output.directory).expanduser()
    source = info.path.expanduser().absolute()
    containing = [root for root in roots if source.is_relative_to(root)]
    if containing:
        outermost = min(containing, key=lambda root: len(root.parts))
        directory /= source.parent.relative_to(outermost)
    return directory / name


def unused_name(path: Path) -> Path:
    """`path` with ".new", ".new2", ... before its suffix: the first that is free."""
    for n in itertools.count(1):
        candidate = path.with_name(f"{path.stem}.new{n if n > 1 else ''}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def check_skip(info: MediaInfo, output: Path, cfg: Config) -> None:
    if info.codec in cfg.library.skip_codecs:
        raise SkipFile(f"already {info.codec}")
    if info.duration and info.duration < cfg.library.min_duration_sec:
        raise SkipFile(f"shorter than {cfg.library.min_duration_sec:.0f}s")
    floor = cfg.library.min_bits_per_pixel
    if floor > 0 and 0 < info.bits_per_pixel < floor:
        # Zero means the bitrate could not be read, which is not evidence of
        # anything, so only a positive measurement can skip a file.
        raise SkipFile(
            f"already {info.bits_per_pixel:.3f} bits/pixel, below {floor:.3f}: "
            "too efficient to be worth re-encoding"
        )
    if info.pix_fmt in {"", "unknown"}:
        # Av1an probes the source itself and aborts with "missing field `pix_fmt`"
        # when ffprobe cannot name the pixel format. Catching it here costs nothing
        # and turns a confusing failure mid-encode into a clear skip.
        raise SkipFile("pixel format unreadable; Av1an cannot probe this file")
    check_dolby_vision(info, cfg)
    if output.resolve() == info.path.resolve():
        raise SkipFile("output path equals source path")
    if output.exists():
        raise SkipFile(f"output already exists: {output.name}")


def check_dolby_vision(info: MediaInfo, cfg: Config) -> None:
    """Decide whether a Dolby Vision source can be transcoded, and say what is lost.

    The RPU is lost either way: AV1 cannot carry it through Av1an's chunked
    pipeline. The question is only whether what remains is a watchable picture.
    For profiles 7, 8.x and 10.x it is -- a correctly tagged HDR10, HLG or SDR
    base layer, which is exactly what Jellyfin serves when it transcodes these
    files on the fly. Only profile 5 (and profile 10 signalling no compatibility)
    decodes to skewed colour without the RPU.
    """
    dv = info.dolby_vision
    if dv is None:
        return

    if cfg.dolby_vision.mode == "skip":
        raise SkipFile(f"Dolby Vision profile {dv.name} (dolby_vision.mode = 'skip')")

    layer = info.base_layer
    if layer == "none":
        if cfg.dolby_vision.profile5 == "tonemap-sdr":
            log.warning(
                "  DV profile %s has no viewable base layer; reshaping to SDR BT.709 "
                "with tonemapx (this discards HDR)", dv.name,
            )
            return
        raise SkipFile(
            f"Dolby Vision profile {dv.name}: no base layer to fall back on "
            "(set dolby_vision.profile5 = 'tonemap-sdr' to convert it to SDR instead)"
        )

    if layer == "invalid":
        log.warning(
            "  DV profile %s signals a base layer its colour tags contradict "
            "(%s/%s/%s); encoding what the tags say",
            dv.name, info.color_primaries or "?", info.color_transfer or "?",
            info.color_space or "?",
        )
        return

    log.info("  %s: encoding the %s base layer, RPU not carried over",
             dv.describe(), layer)


def _calibrate_grain(
    read: MediaInfo, encoded: Path, grainy: Path, table: Path,
    chain: Toolchain, cfg: Config, work: Path,
) -> bool:
    """Rescale the grain table until the synthesised grain matches the source's.

    Returns whether to keep grain at all: calibration doubles as the grain
    detector, and a source with nothing to put back measures near zero (see
    grain.decide_strength). The table is applied after encoding, so all of this
    rewrites stream headers; nothing is re-encoded.
    """
    measured = grain.calibrate_strength(read.path, grainy, chain, cfg, work, read.duration)
    if measured[0] is None:
        log.warning("  grain strength not calibrated: measurements inconclusive")
        return True
    keep, luma, cb, cr = grain.decide_strength(measured, cfg.grain.mode)
    if not keep:
        return False
    if all(abs(f - 1.0) < 0.05 for f in (luma, cb, cr)):
        log.info("  grain strength already matches the source")
        return True
    scaled = table.with_name("grain_calibrated.tbl")
    scaled.write_text(grain.scale_table(table.read_text(encoding="utf-8"), luma, cb, cr),
                      encoding="utf-8")
    grain.apply_table(encoded, scaled, chain, grainy)
    log.info("  grain strength calibrated to the source: luma x%.2f, Cb x%.2f, Cr x%.2f",
             luma, cb, cr)
    return True


def _work_dir(source: Path, cfg: Config) -> Path:
    """Per-file scratch directory, keyed to the source path.

    Takes a Path rather than a MediaInfo on purpose: this digest is what lets
    Av1an resume across runs, so it must follow the real source and never a
    staged copy. Passing the path explicitly makes that hard to get wrong.
    """
    digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    path = cfg.work_dir / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def _staged_path(source: Path, cfg: Config, suffix: str) -> Path:
    """Name staged files by the whole source path, so identically named files in
    different folders cannot collide."""
    digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    return cfg.staging_dir / f"{digest}{suffix}"


def staged_output_path(source: Path, cfg: Config) -> Path:
    return _staged_path(source, cfg, ".out.mkv")


def stage_source(info: MediaInfo, cfg: Config) -> Path | None:
    """Copy the source to local storage, returning the copy's path.

    Returns None when staging is not possible, because this is an optimisation and
    never a reason to refuse a file: the caller falls back to reading in place.

    An interrupted run leaves its copy behind on purpose. A second run finds it,
    checks the size still matches and reuses it, so resuming does not re-read the
    whole source across the network or off a sleeping disk.
    """
    root = cfg.staging_dir
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("  staging directory unusable (%s); reading the source in place", exc)
        return None

    staged = _staged_path(info.path, cfg, info.path.suffix)

    if staged.is_file() and staged.stat().st_size == info.size_bytes:
        log.info("  reusing staged copy from an earlier run: %s", staged)
        return staged
    staged.unlink(missing_ok=True)

    # The multiple covers the copy itself plus the encoded output, which is staged
    # alongside it and is normally a small fraction of the source.
    needed = int(info.size_bytes * cfg.staging.min_free_multiple)
    free = shutil.disk_usage(root).free
    if free < needed:
        log.warning(
            "  not staging: %s free at %s, want %s (%.2fx the source); "
            "reading the source in place",
            human_size(free), root, human_size(needed), cfg.staging.min_free_multiple,
        )
        return None

    log.info("  staging %s to %s", human_size(info.size_bytes), root)
    started = time.monotonic()
    try:
        shutil.copyfile(info.path, staged)
    except OSError as exc:
        log.warning("  staging failed (%s); reading the source in place", exc)
        staged.unlink(missing_ok=True)
        return None

    elapsed = time.monotonic() - started
    rate = info.size_bytes / elapsed if elapsed else 0.0
    log.info("  staged in %s (%s/s)", human_duration(elapsed), human_size(int(rate)))
    return staged


def process_one(
    source: Path, chain: Toolchain, cfg: Config, *, dry_run: bool = False,
    roots: Sequence[Path] = (),
) -> Record:
    started = time.monotonic()
    info = probe(source, chain)
    log.info("%s", source.name)
    log.info("  %s, %s", info.describe(), human_size(info.size_bytes))
    log.info("  %.3f bits/pixel, encoding at crf %s",
             info.bits_per_pixel, encode.crf_text(encode.effective_crf(info, cfg)))

    final = output_path(info, cfg, roots)
    check_skip(info, final, cfg)

    # Everything above is keyed to the real source: the output path, the work
    # directory digest that makes Av1an resumable, and the ledger entry. Only the
    # steps that actually *read* the video move to the staged copy.
    work = _work_dir(source, cfg)

    staged: Path | None = None
    staged_out: Path | None = None
    if cfg.staging.enabled and not dry_run:
        staged = stage_source(info, cfg)
        # Stage the write as well, but only once the read staged successfully:
        # that is what proved the directory usable and the free space sufficient.
        if staged is not None:
            staged_out = staged_output_path(source, cfg)
    read = replace(info, path=staged) if staged is not None else info

    try:
        record = _transcode(
            source, info, read, final, staged_out, work, chain, cfg, started,
            dry_run=dry_run,
        )
    except (SkipFile, OutputExists):
        # A decided skip is a finished outcome, and so is an encode kept under
        # another name; either way the copy has served its purpose.
        _drop_staged(staged, staged_out, cfg)
        raise
    except BaseException:
        # Interrupted or failed: keep the staged source. The next attempt resumes
        # from Av1an's finished chunks and must not re-read the whole file off the
        # library disk to do it -- which is the entire point of staging. A partial
        # output is never reusable, so that one goes.
        if staged_out is not None:
            staged_out.unlink(missing_ok=True)
        if staged is not None:
            log.info("  keeping the staged copy so a rerun resumes without re-reading the source")
        raise
    _drop_staged(staged, staged_out, cfg)
    return record


def _drop_staged(staged: Path | None, staged_out: Path | None, cfg: Config) -> None:
    if staged_out is not None:
        staged_out.unlink(missing_ok=True)
    if staged is not None and not cfg.staging.keep:
        staged.unlink(missing_ok=True)
        log.debug("staged copy removed: %s", staged)


def _transcode(
    source: Path,
    info: MediaInfo,
    read: MediaInfo,
    final: Path,
    staged_out: Path | None,
    work: Path,
    chain: Toolchain,
    cfg: Config,
    started: float,
    *,
    dry_run: bool,
) -> Record:
    """The pipeline proper.

    `info` describes the source, `read` says where to read it, and `staged_out` --
    when set -- says where to write the result before moving it to `final`.
    """
    report: grain.GrainReport | None = None
    table: Path | None = None

    if cfg.grain.mode != "never":
        grainy = True
        if grain.needs_probe(cfg):
            report = grain.measure(read, chain, cfg, work)
            log.info("  %s", report.describe(denoising=cfg.grain.denoise_before_encode))
            grainy = report.grainy
        if grainy and not dry_run:
            table = grain.build_table(read, chain, cfg, report, work)
            if table is None:
                log.warning("  no grain table; encoding without denoising to avoid losing grain")

    # Only ever denoise when there is a table to put the grain back with, and by
    # default not even then: see grain.denoise_before_encode.
    denoise = (report.denoise_filter
               if (report and table and cfg.grain.denoise_before_encode) else "")

    encoded = work / "encoded.mkv"
    encoded.unlink(missing_ok=True)
    argv = encode.build_command(read, encoded, chain, cfg, work / "av1an", denoise)
    check = (watch.SavingWatch(read, cfg, chain, work / "av1an")
             if cfg.output.check_saving_after_percent > 0 and not dry_run else None)
    try:
        stopped = encode.run_encode(argv, cfg, chain, dry_run=dry_run, check=check)
    finally:
        if check is not None:
            check.close()
    if stopped:
        _cleanup(work, cfg, table)
        raise NoSaving(stopped, settings_key(cfg, chain))

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
        # No fallback here on purpose. The encoder strips grain whether or not it
        # was denoised first, so shipping without this step delivers a grainless,
        # banded picture. Failing keeps the work directory, so Av1an resumes from
        # its finished chunks once the cause is fixed.
        grain.apply_table(encoded, table, chain, grainy)
        keep = True
        if cfg.grain.calibrate:
            keep = _calibrate_grain(read, encoded, grainy, table, chain, cfg, work)
        if keep:
            video = grainy
            log.info("  film grain applied from table")
        else:
            log.info("  the encode kept all the source's fine texture; no grain added")

    # When staging, mux locally rather than onto the destination disk. The result
    # only moves once it has earned its place, so an encode that fails the size
    # check never touches that disk at all.
    # Never mux straight to the destination name. An interrupt partway through
    # would leave a short file there, and the next run's "output already exists"
    # check would read it as finished -- silently shipping a truncated film.
    # The output's folders are made only now, so that a dry run or a skipped file
    # leaves no empty ones behind in a mirrored output directory.
    made = _make_folders(final.parent) if staged_out is None else []
    muxed = staged_out if staged_out is not None else final.with_name(final.name + ".part")
    muxed.unlink(missing_ok=True)
    mux.mux(video, read, muxed, chain, cfg)

    output_bytes = muxed.stat().st_size
    reason = watch.shortfall(cfg, info.size_bytes, output_bytes)
    if reason is not None:
        muxed.unlink(missing_ok=True)
        _remove_if_empty(made)
        _cleanup(work, cfg, table)
        raise NoSaving(f"came out {reason}", settings_key(cfg, chain))

    # Checked again now, not only before the encode: hours have passed, and another
    # run or a copy by hand may have put a file under this name since.
    # Neither file is thrown away: the encode goes beside it, and the file is
    # reported as failed so that someone decides which to keep.
    intended = final
    if intended.exists():
        final = unused_name(intended)
    if staged_out is not None:
        _make_folders(final.parent)
        log.info("  writing %s to %s", human_size(output_bytes), final.parent)
        # shutil.move, not os.replace: staging is normally on a different
        # filesystem from the library, so this is a copy-and-delete.
        try:
            shutil.move(str(muxed), str(final))
        except OSError:
            # Never leave a half-written file at the destination: check_skip would
            # read it as "already done" and the title would be silently abandoned.
            final.unlink(missing_ok=True)
            raise
    else:
        # Same directory, so this is an atomic rename: the destination name either
        # does not exist or is the complete file, never anything in between.
        os.replace(muxed, final)

    if final != intended:
        _cleanup(work, cfg, table)
        raise OutputExists(
            f"{intended.name} appeared during the encode, so this encode was kept as "
            f"{final.name} beside it; keep one and delete the other"
        )

    elapsed = time.monotonic() - started
    log.info(
        "  done in %s: %s -> %s (%s)",
        human_duration(elapsed), human_size(info.size_bytes), human_size(output_bytes),
        watch.describe_saving(100.0 * (1 - output_bytes / info.size_bytes)),
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


def _make_folders(folder: Path) -> list[Path]:
    """mkdir -p, returning the folders it made, deepest first."""
    made: list[Path] = []
    for parent in (folder, *folder.parents):
        if parent.exists():
            break
        made.append(parent)
    folder.mkdir(parents=True, exist_ok=True)
    return made


def _remove_if_empty(folders: list[Path]) -> None:
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            return


def _cleanup(work: Path, cfg: Config, table: Path | None) -> None:
    keep = table if (table and cfg.grain.keep_table) else None
    saved = keep.read_text(encoding="utf-8") if keep and keep.is_file() else None
    shutil.rmtree(work, ignore_errors=True)
    if saved is not None and keep is not None:
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(saved, encoding="utf-8")


def process_all(
    sources: list[Path], chain: Toolchain, cfg: Config, ledger: Ledger, *, dry_run: bool = False,
    roots: Sequence[Path] = (),
) -> int:
    with stop_signals() as stop:
        return _process_all(sources, chain, cfg, ledger, stop, dry_run=dry_run, roots=roots)


def _process_all(
    sources: list[Path],
    chain: Toolchain,
    cfg: Config,
    ledger: Ledger,
    stop: StopRequest,
    *,
    dry_run: bool = False,
    roots: Sequence[Path] = (),
) -> int:
    failures = 0
    total = len(sources)
    settings = settings_key(cfg, chain)

    for index, source in enumerate(sources, start=1):
        log.info("[%d/%d] %s", index, total, source)

        if done := ledger.already_done(source):
            log.info("  skipping, already transcoded to %s", done.output)
            continue
        if (tried := ledger.no_saving(source)) and tried.settings == settings:
            log.info("  skipping, did not save enough with the same settings before (%s)",
                     tried.reason)
            continue

        try:
            record = process_one(source, chain, cfg, dry_run=dry_run, roots=roots)
        except NoSaving as exc:
            log.info("  skipped: %s", exc)
            ledger.record(source, Record(source=str(source), status="skipped",
                                         reason=str(exc), settings=exc.settings))
            continue
        except SkipFile as exc:
            log.info("  skipped: %s", exc)
            ledger.record(source, Record(source=str(source), status="skipped", reason=str(exc)))
            continue
        except OutputExists as exc:
            failures += 1
            log.error("  %s", exc)
            ledger.record(source, Record(source=str(source), status="failed", reason=str(exc)))
            continue
        except (CommandError, OSError, ValueError) as exc:
            # A tool killed by our own stop signal exits non-zero before Python
            # gets round to raising KeyboardInterrupt. Without this the ledger
            # would blame the file for the operator's Ctrl-C.
            if stop.seen:
                log.warning("interrupted; Av1an progress is kept, rerun to resume")
                raise KeyboardInterrupt from exc
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
