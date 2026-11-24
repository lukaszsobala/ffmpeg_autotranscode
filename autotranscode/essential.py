"""SvtFgEssential: SVT-AV1-Essential's grain estimator, over the whole film.

`SvtFgEssential --strength -1` estimates grain from each frame's difference to
a motion-compensated neighbour on flat blocks, fits it per brightness band and
checks the AR filter for resonance, then re-estimates every 11 frames, blending
with what came before, and starts afresh at every keyframe it is given. It
writes one table entry per frame.

Its analysis is single-threaded: 20 frames/s at 1080p on one Cortex-A76. But it
keeps nothing across a scene cut, so the film is split at Av1an's scene starts
into parts of whole scenes, each part estimated by its own process, and the
parts' entries simply follow each other: the same table as one pass, in a
fraction of the time. Each part is decoded from a seek, on the Rockchip video
engine when that is bit-exact for the codec (verify.decode_args), and found by
its frames' timestamps, so the frames are exactly the encode's.

The parts are kept as they finish, so a stop loses only those in flight. The
table they make is in frame order and timed by frame number; grain.retime times
it by the packets of the stream it goes into.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import encode, grain, verify
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, child_env, digest, human_duration, log, run

# How the film is cut into parts (plan_parts): each part about the frames not yet
# planned / (GUIDED x processes), but no shorter than SMALLEST_PART frames (about
# 25 s on one core at 1080p) unless the film ends. Big parts first, small ones
# last, so the processes finish within a small part of each other.
GUIDED = 2
SMALLEST_PART = 600
# A part is decoded from this many seconds before its first frame, which is then
# found by its timestamp. The seek lands on the keyframe before, wherever that is.
SEEK_MARGIN = 3.0
FRAMES_TABLE = "grain_frames.tbl"
KEY_FILE = "svtfg.key"


class Mismatch(ValueError):
    """The frames the estimate saw are not the encode's, one for one."""


def jobs(cfg: Config, chain: Toolchain) -> int:
    """How many SvtFgEssential processes: grain.svtfg_jobs, else one per big core."""
    if cfg.grain.svtfg_jobs:
        return cfg.grain.svtfg_jobs
    big = [cpu for cpu in chain.big_cores.split(",") if cpu]
    return len(big) or os.cpu_count() or 1


@dataclass(frozen=True, slots=True)
class Part:
    start: int              # its first frame
    end: int                # one past its last
    cuts: tuple[int, ...]   # its scene starts, counted from `start`; always 0 first

    @property
    def frames(self) -> int:
        return self.end - self.start


def plan_parts(scene_starts: Sequence[int], frames: int, processes: int,
               smallest: int = SMALLEST_PART) -> list[Part]:
    """Runs of whole scenes for `processes` processes, in frame order, shrinking
    as they go (guided self-scheduling).

    Equal parts end badly: Film A in 14 parts for 4 processes spent the
    last 8 of its 31 minutes with half of them idle. Here each part takes a
    share of what is left, so the last few are small, and frames_table hands out
    the longest first, which also puts a long scene that cannot be split (a
    four-minute take) at the start rather than the end.
    """
    if frames <= 0:
        return []
    starts = sorted({start for start in scene_starts if 0 < start < frames} | {0})
    parts: list[Part] = []
    begin = 0
    for start in [*starts[1:], frames]:
        target = max(smallest, (frames - begin) / (GUIDED * max(1, processes)))
        if start - begin >= target or start == frames:
            parts.append(Part(begin, start, tuple(s - begin for s in starts
                                                  if begin <= s < start)))
            begin = start
    return parts


def feed_format(info: MediaInfo) -> str:
    """What SvtFgEssential reads: 4:2:0 at 8 or 10 bits. An 8-bit 4:2:0 source
    goes as it is; anything else as 10-bit 4:2:0."""
    return "yuv420p" if info.pix_fmt in {"yuv420p", "yuvj420p", "nv12"} else "yuv420p10le"


def start_time(chain: Toolchain, path: Path) -> float:
    """Where ffmpeg's -ss counts from: the container's start time."""
    out = run([chain.ffprobe, "-v", "error", "-show_entries", "format=start_time",
               "-of", "csv=p=0", str(path)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def decode_command(chain: Toolchain, path: Path, times: Sequence[float], origin: float,
                   part: Part, prefilter: str, pix_fmt: str) -> list[str]:
    """ffmpeg serving frames [part.start, part.end) of `path` as Y4M.

    Times are kept as they are (-copyts), so `select` finds the first frame by
    its own timestamp, halfway past the one before: exact whatever the seek
    lands on, and whatever the frame rate does. Any deinterlacing comes before
    the selection, so its first frame has the one before to work with.
    """
    argv = [chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error",
            *verify.decode_args(chain, path, grain=True)]
    filters = [prefilter] if prefilter else []
    if part.start > 0:
        seek = max(0.0, times[part.start] - origin - SEEK_MARGIN)
        middle = (times[part.start - 1] + times[part.start]) / 2
        argv += ["-noaccurate_seek", "-ss", f"{seek:.3f}"]
        filters.append(f"select=gte(t\\,{middle:.6f})")
    argv += ["-copyts", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn"]
    if filters:
        argv += ["-vf", ",".join(filters)]
    return [*argv, "-frames:v", str(part.frames), "-fps_mode", "passthrough",
            "-pix_fmt", pix_fmt, "-strict", "-1", "-f", "yuv4mpegpipe", "-"]


def estimate_command(chain: Toolchain, table: Path, cuts: Path | None) -> list[str]:
    assert chain.svtfg is not None
    return [chain.svtfg, "--source", "-", "--table", str(table), "--strength", "-1",
            "--seed", "-1", "--keyint", "0",
            *(["--keyframes", f"@{cuts}"] if cuts is not None else [])]


def _part_path(folder: Path, part: Part) -> Path:
    return folder / f"{part.start:07d}-{part.end:07d}.tbl"


def _whole(table: Path, frames: int) -> bool:
    try:
        return len(grain.parse_entries(table.read_text(encoding="utf-8"))) == frames
    except (OSError, ValueError):
        return False


def _run_part(chain: Toolchain, cfg: Config, read: MediaInfo, times: Sequence[float],
              origin: float, part: Part, prefilter: str, folder: Path,
              stopping: threading.Event, children: set[subprocess.Popen]) -> None:
    done = _part_path(folder, part)
    written = done.with_suffix(".new")
    cuts = None
    if len(part.cuts) > 1:
        cuts = done.with_suffix(".cuts")
        cuts.write_text(",".join(map(str, part.cuts)), encoding="ascii")
    nice = ["nice", "-n", str(cfg.machine.nice)] if cfg.machine.nice > 0 else []
    pin = ["taskset", "-c", chain.big_cores] if chain.big_cores else []
    env = child_env(chain.bin_dirs)
    decode = decode_command(chain, read.path, times, origin, part, prefilter,
                            feed_format(read))
    if stopping.is_set():
        return
    estimate = [*nice, *pin, *estimate_command(chain, written, cuts)]
    with subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env) as source:
        with subprocess.Popen(estimate,
                              stdin=source.stdout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, env=env) as estimator:
            children.update((source, estimator))
            assert source.stdout is not None
            source.stdout.close()   # the estimator's now; a SIGPIPE reaches ffmpeg
            _, err = estimator.communicate()
        decode_err = source.stderr.read() if source.stderr else b""
    children.difference_update((source, estimator))
    if stopping.is_set():
        return
    if estimator.returncode or source.returncode:
        failed, argv, detail = ((estimator, estimate, err) if estimator.returncode
                                else (source, decode, decode_err))
        raise CommandError(argv, failed.returncode,
                           detail.decode(errors="replace").strip()[-400:])
    if not _whole(written, part.frames):
        raise Mismatch(f"frames {part.start}-{part.end}: the estimate did not cover "
                       f"{part.frames} frames (the source ends early, or its timestamps "
                       "are not the frames')")
    os.replace(written, done)
    if cuts is not None:
        cuts.unlink(missing_ok=True)


def key(read: MediaInfo, chain: Toolchain, cfg: Config, scene_starts: Sequence[int]) -> str:
    """A digest of everything the per-frame table depends on."""
    return digest({"svtfg": [chain.svtfg_commit, feed_format(read),
                             encode.deinterlace_filter(read, cfg)],
                   "scenes": list(scene_starts),
                   "source": [read.size_bytes, read.duration]})


def frames_table(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path,
                 scene_starts: Sequence[int]) -> Path:
    """The film's grain, one entry per frame in frame order, kept in `work`.

    Kept between runs while nothing it depends on changes (`key`). Raises
    Mismatch when the source's frames cannot be the encode's one for one.
    """
    table = work / FRAMES_TABLE
    marker = work / KEY_FILE
    wanted = key(read, chain, cfg, scene_starts)
    try:
        if table.is_file() and marker.read_text(encoding="ascii").strip() == wanted:
            log.info("  grain estimate kept from an earlier run (settings unchanged)")
            return table
    except OSError:
        pass
    table.unlink(missing_ok=True)

    times = encode.frame_times(chain, read.path)
    if not times:
        raise Mismatch(f"{read.path.name} has no video frames to estimate")
    origin = start_time(chain, read.path)
    count = jobs(cfg, chain)
    parts = plan_parts(scene_starts, len(times), count)
    folder = work / f"svtfg-{wanted}"
    for old in work.glob("svtfg-*"):
        if old != folder:
            shutil.rmtree(old, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    todo = sorted((part for part in parts if not _whole(_part_path(folder, part), part.frames)),
                  key=lambda part: -part.frames)
    prefilter = encode.deinterlace_filter(read, cfg)
    log.info("  grain estimate (SvtFgEssential): %d frames in %d parts of whole scenes, "
             "%d at a time%s", len(times), len(parts), count,
             f" ({len(parts) - len(todo)} done before)" if len(todo) < len(parts) else "")

    started = time.monotonic()
    stopping = threading.Event()
    children: set[subprocess.Popen] = set()
    estimating = sum(part.frames for part in todo)
    finished = len(times) - estimating
    reported = 10 * finished // len(times)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = {pool.submit(_run_part, chain, cfg, read, times, origin, part,
                                   prefilter, folder, stopping, children): part
                       for part in todo}
            for future in concurrent.futures.as_completed(futures):
                future.result()
                finished += futures[future].frames
                if 10 * finished // len(times) > reported:
                    reported = 10 * finished // len(times)
                    log.info("  grain estimate: %d%%", 10 * reported)
    except BaseException:
        stopping.set()
        for child in list(children):
            child.kill()
        raise

    entries: list[grain.Entry] = []
    for part in parts:
        entries += grain.parse_entries(_part_path(folder, part).read_text(encoding="utf-8"))
    if len(entries) != len(times):
        raise Mismatch(f"the estimate has {len(entries)} frames, the source {len(times)}")
    written = table.with_suffix(".new")
    written.write_text(grain.frame_table(entries), encoding="utf-8")
    os.replace(written, table)
    marker.write_text(wanted, encoding="ascii")
    shutil.rmtree(folder, ignore_errors=True)
    elapsed = time.monotonic() - started
    if estimating:
        log.info("  grain estimate done in %s (%.1f frames/s)", human_duration(elapsed),
                 estimating / elapsed if elapsed else 0.0)
    return table


def load(table: Path) -> list[grain.Entry]:
    """The per-frame entries frames_table wrote."""
    return grain.parse_entries(table.read_text(encoding="utf-8"))
