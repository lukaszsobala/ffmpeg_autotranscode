"""A per-frame grain table, applied and calibrated scene by scene.

Calibration measures the grain the source has, the fine texture the encode kept
of it, and the grain the table adds, and scales the table to match (see
grain.strength_factor). One factor for the whole film left scenes apart: on To
Live and Die in L.A., SvtFgEssential's table wanted x0.59 in one window and x1.5
in another. So each scene gets its own factor, from windows inside it.

The measure (a 3x3 median, verify.GRAIN_FILTER, on 10-bit) costs about 0.4
core-seconds a 1080p frame, and decoding a whole film twice more than that, so
it reads windows, not the film: a few frames just after keyframes of the encode
(Av1an starts every chunk with one), where a seek costs nothing in the encode,
and the same frames of the source, found by their timestamps. The keyframe
itself is left out: an intra frame keeps more texture than the frames around it.

A scene with few windows measures noisily, so its factor is pulled towards the
film's, the more the fewer frames it had; a scene with none takes the film's.
"""

from __future__ import annotations

import bisect
import concurrent.futures
import math
import os
import shlex
import statistics
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import essential, grain, verify
from .config import Config
from .encode import frame_times
from .probe import MediaInfo
from .tools import Toolchain
from .util import Children, CommandError, child_env, log, run

# Frames of measurement that count as much as the film's factor: a scene with
# this many measured frames takes half its own factor and half the film's (in
# the log domain), one with four times as many four fifths its own.
PRIOR_FRAMES = 8
# A scene's own factor is trusted within these bounds before the pull towards
# the film's; the result is clamped to LOWEST..grain.MAX_FACTOR.
RAW_BOUNDS = (0.1, 4.0)
LOWEST = 0.1
# The measure's frame budget is set for this many pixels (grain.calibration_frames).
BUDGET_PIXELS = 1920 * 1080
MIN_BUDGET = 300
# grav1synth reads Matroska timestamps as milliseconds; see grain.retime.
TICKS_PER_MS = 10_000


@dataclass(frozen=True, slots=True)
class Stream:
    """A stream's frames as grav1synth and the windows see them, in display order."""

    stamps: list[int]          # grav1synth's timestamps (ms x 10,000)
    times: list[float]         # seconds
    keyframes: list[int]       # frame numbers


def read_stream(chain: Toolchain, path: Path) -> Stream:
    """The video packets of `path`: their timestamps and which are keyframes."""
    base = run([chain.ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=time_base", "-of", "csv=p=0", str(path)]).stdout.strip()
    if base != "1/1000":
        raise ValueError(f"{path.name}: grav1synth reads timestamps as milliseconds, and "
                         f"this stream counts in {base or 'unknown units'}")
    out = run([chain.ffprobe, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "packet=pts,flags", "-of", "csv=p=0", str(path)]).stdout
    packets: list[tuple[int, bool]] = []
    for line in out.splitlines():
        fields = line.strip().split(",")
        if len(fields) >= 2 and fields[0].lstrip("-").isdigit():
            packets.append((int(fields[0]), "K" in fields[1]))
    packets.sort()
    return Stream([pts * TICKS_PER_MS for pts, _ in packets],
                  [pts / 1000 for pts, _ in packets],
                  [index for index, (_, key) in enumerate(packets) if key])


@dataclass(frozen=True, slots=True)
class Window:
    scene: int      # index into the scene starts
    keyframe: int   # the encode's keyframe it is decoded from
    start: int      # its first frame
    frames: int


def scene_of(starts: Sequence[int], frame: int) -> int:
    return max(0, bisect.bisect_right(starts, frame) - 1)


def _spread(count: int) -> list[int]:
    """0..count-1 in an order that keeps any prefix spread out: the middle first,
    then the quarters, and so on."""
    order: list[int] = []
    step = 1.0
    while len(order) < count:
        step /= 2
        for k in range(1, int(1 / step), 2):
            index = min(count - 1, int(k * step * count))
            if index not in order:
                order.append(index)
        if step < 1 / (4 * count):
            order += [index for index in range(count) if index not in order]
    return order[:count]


def plan_windows(scene_starts: Sequence[int], keyframes: Sequence[int], frames: int,
                 budget: int, length: int) -> list[Window]:
    """Windows of up to `length` frames just after keyframes, about `budget`
    frames in all: first one in each scene, longest scenes first, then a second
    in each that has room, and so on, spread out within each scene."""
    starts = sorted({s for s in scene_starts if 0 <= s < frames} | {0})
    keys = sorted(k for k in set(keyframes) if 0 <= k < frames)
    candidates: dict[int, list[Window]] = {}
    for number, key in enumerate(keys):
        scene = scene_of(starts, key)
        scene_end = starts[scene + 1] if scene + 1 < len(starts) else frames
        stop = min(scene_end, keys[number + 1] if number + 1 < len(keys) else frames)
        count = min(length, stop - key - 1)
        if count >= 1:
            candidates.setdefault(scene, []).append(Window(scene, key, key + 1, count))
    ordered = {scene: [found[i] for i in _spread(len(found))]
               for scene, found in candidates.items()}
    by_length = sorted(ordered, key=lambda scene: -((starts[scene + 1] if scene + 1 < len(starts)
                                                     else frames) - starts[scene]))
    chosen: list[Window] = []
    total = 0
    depth = 0
    while total < budget and any(len(ordered[s]) > depth for s in by_length):
        for scene in by_length:
            if depth < len(ordered[scene]) and total < budget:
                window = ordered[scene][depth]
                chosen.append(window)
                total += window.frames
        depth += 1
    return sorted(chosen, key=lambda window: window.start)


def budget(info: MediaInfo, cfg: Config) -> int:
    pixels = max(1, info.width * info.height)
    return max(MIN_BUDGET, round(cfg.grain.calibration_frames * BUDGET_PIXELS / pixels))


# --- measuring ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Energies:
    """Mean grain energy per plane (Y, U, V) over `frames` frames of each stream."""

    frames: int
    wanted: tuple[float, float, float]
    underneath: tuple[float, float, float]
    with_grain: tuple[float, float, float]


def window_command(chain: Toolchain, window: Window, source: Path, source_times: Sequence[float],
                   source_origin: float, base: Path, grainy: Path, stream: Stream,
                   stream_origin: float, prefilter: str, stats: Sequence[Path],
                   software: bool = False) -> list[str]:
    """One ffmpeg measuring a window in the source, the encode without grain and
    the encode with it, each into its own psnr stats file (verify's grain measure).
    `software` decodes all three in software, whatever hardware matched."""
    first = window.start
    inputs: list[str] = []
    graph: list[str] = []

    def decoder(path: Path, codec: str, grain: bool) -> list[str]:
        if software:
            return verify.software_decode_args(chain, codec)
        return verify.decode_args(chain, path, grain=grain)

    # One thread each for dav1d and the graph: several windows run at once, one
    # per core, and ffmpeg's default of a thread per core in each made ~25 wait.
    single = ["-threads", "1"]
    plan = [(source, source_times, source_origin, decoder(source, "", True), prefilter,
             SOURCE_SEEK),
            (base, stream.times, stream_origin,
             [*decoder(base, "av1", False), *single, *verify.GRAIN_OFF], "", 0.0),
            (grainy, stream.times, stream_origin,
             [*decoder(grainy, "av1", True), *single], "", 0.0)]
    for number, (path, times, origin, decode, pre, margin) in enumerate(plan):
        seek_to = times[window.keyframe] if margin == 0.0 else times[first] - margin
        seek = max(0.0, seek_to - origin + (0.0005 if margin == 0.0 else 0.0))
        middle = (times[first - 1] + times[first]) / 2 if first > 0 else -1.0
        inputs += [*decode, "-noaccurate_seek", "-ss", f"{seek:.4f}", "-copyts",
                   "-i", str(path)]
        chain_filters = ([pre] if pre else []) + [
            f"select=gte(t\\,{middle:.6f})", f"trim=end_frame={window.frames}",
            "format=yuv420p10le", f"split=2[r{number}][p{number}]"]
        graph.append(f"[{number}:v]{','.join(chain_filters)};"
                     f"[p{number}]{verify.GRAIN_FILTER}[d{number}];"
                     f"[d{number}][r{number}]psnr=stats_file={stats[number].as_posix()}[o{number}]")
    outputs: list[str] = []
    for number in range(len(plan)):
        outputs += ["-map", f"[o{number}]", "-f", "null", "-"]
    return [chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y", *inputs,
            "-filter_complex_threads", "1", "-filter_complex", ";".join(graph), *outputs]


# Seconds a calibration window may take; a few is usual.
WINDOW_TIMEOUT = 180

# The source is sought from this far before the window: its keyframes are not
# the encode's, so the seek lands wherever the source's last one before was.
SOURCE_SEEK = 3.0


def _means(text: str) -> tuple[list[float], list[float], list[float]]:
    y, u, v = (verify.parse_mse_log(text, plane) for plane in grain.PLANES)
    return y, u, v


def measure(chain: Toolchain, windows: Sequence[Window], source: Path, base: Path,
            grainy: Path, stream: Stream, prefilter: str, work: Path,
            processes: int = 2) -> dict[int, Energies]:
    """Energies per scene, from every window in it."""
    source_times = frame_times(chain, source)
    if len(source_times) != len(stream.times):
        raise essential.Mismatch(f"the source has {len(source_times)} frames, the encode "
                                 f"{len(stream.times)}")
    source_origin = essential.start_time(chain, source)
    stream_origin = essential.start_time(chain, base)
    folder = work / "calibration"
    folder.mkdir(parents=True, exist_ok=True)

    # One graph decoding the source on h264_rkmpp and the encode twice once hung
    # for hours on jellyfin-ffmpeg 8.1.3-2 (TECHNICAL.md, "Why verify decodes each
    # file three times"). A window takes seconds; one that takes minutes is
    # stopped, and everything decoded in software from then on. A window that
    # fails or hangs on hardware is measured again in software.
    software = threading.Event()

    def one(number: int, window: Window) -> tuple[int, list[list[float]]]:
        if software.is_set():
            return attempt(number, window)
        return verify.with_fallback(chain, [(source, True), (base, False), (grainy, True)],
                                    lambda: attempt(number, window), children.stopping)

    def attempt(number: int, window: Window) -> tuple[int, list[list[float]]]:
        stats = [folder / f"{number:05d}-{name}.log" for name in ("src", "base", "grain")]
        argv = window_command(chain, window, source, source_times, source_origin, base,
                              grainy, stream, stream_origin, prefilter, stats,
                              software.is_set())
        if children.stopping.is_set():
            raise CommandError(argv, -1, "stopped")
        log.debug("exec: %s", shlex.join(argv))
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE,
                              text=True, errors="replace",
                              env=child_env(chain.bin_dirs)) as child:
            children.add(child)
            try:
                _, err = child.communicate(timeout=WINDOW_TIMEOUT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                if not software.is_set():
                    software.set()
                    log.warning("  a calibration window hung; decoding in software "
                                "from now on")
                raise CommandError(argv, -1, f"timed out after {WINDOW_TIMEOUT}s") from None
            finally:
                children.discard(child)
        if child.returncode:
            raise CommandError(argv, child.returncode, err)
        planes: list[list[float]] = []
        for path in stats:
            text = path.read_text(encoding="utf-8", errors="replace")
            planes += list(_means(text))
            path.unlink(missing_ok=True)
        return window.scene, planes

    sums: dict[int, list[float]] = {}
    counts: dict[int, int] = {}
    done = 0
    children = Children()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, processes)) as pool:
        futures = [pool.submit(one, number, window) for number, window in enumerate(windows)]
        try:
            for future in concurrent.futures.as_completed(futures):
                try:
                    scene, planes = future.result()
                except CommandError as exc:
                    log.debug("a calibration window failed: %s", exc.stderr.strip()[-200:])
                    continue
                frames = min(len(values) for values in planes)
                if frames == 0:
                    continue
                totals = sums.setdefault(scene, [0.0] * 9)
                for index, values in enumerate(planes):
                    totals[index] += sum(values[:frames])
                counts[scene] = counts.get(scene, 0) + frames
                done += 1
                if done % max(1, len(windows) // 10) == 0:
                    log.info("  grain calibration: %d%%", round(100 * done / len(windows)))
        except BaseException:
            children.stop(pool)
            raise
    return {scene: Energies(counts[scene],
                            (t[0] / counts[scene], t[1] / counts[scene], t[2] / counts[scene]),
                            (t[3] / counts[scene], t[4] / counts[scene], t[5] / counts[scene]),
                            (t[6] / counts[scene], t[7] / counts[scene], t[8] / counts[scene]))
            for scene, t in sums.items()}


# --- factors --------------------------------------------------------------------


def overall(energies: dict[int, Energies]) -> Energies | None:
    """The film's energies: every measured frame alike."""
    frames = sum(e.frames for e in energies.values())
    if not frames:
        return None

    def mean(pick: str) -> tuple[float, float, float]:
        y, u, v = (sum(getattr(e, pick)[plane] * e.frames for e in energies.values()) / frames
                   for plane in range(3))
        return y, u, v

    return Energies(frames, mean("wanted"), mean("underneath"), mean("with_grain"))


def factors(e: Energies) -> tuple[float | None, float | None, float | None]:
    return tuple(grain.strength_factor(e.wanted[p], e.underneath[p], e.with_grain[p])
                 for p in range(3))  # type: ignore[return-value]


def shrink(raw: float | None, film: float, frames: int, prior: int = PRIOR_FRAMES) -> float:
    """A scene's factor: its own measure `raw`, pulled towards the film's the
    fewer frames it rests on. None (nothing synthesised to measure) is the film's."""
    if raw is None or film <= 0:
        return film
    own = min(max(raw, RAW_BOUNDS[0]), RAW_BOUNDS[1])
    weight = frames / (frames + prior)
    blended = math.exp(weight * math.log(own) + (1 - weight) * math.log(film))
    return min(max(blended, LOWEST), grain.MAX_FACTOR)


@dataclass(frozen=True, slots=True)
class Calibration:
    keep: bool
    film: tuple[float, float, float]
    scenes: dict[int, tuple[float, float, float]]   # scenes with windows; others take `film`

    def for_scene(self, scene: int) -> tuple[float, float, float]:
        return self.scenes.get(scene, self.film)

    def describe(self) -> str:
        luma = sorted(f[0] for f in self.scenes.values())
        spread = ""
        if len(luma) >= 5:
            deciles = statistics.quantiles(luma, n=10)
            spread = (f"; luma by scene x{deciles[0]:.2f} to x{deciles[-1]:.2f} "
                      f"(10th to 90th percentile) over {len(luma)} scenes")
        return (f"luma x{self.film[0]:.2f}, Cb x{self.film[1]:.2f}, Cr x{self.film[2]:.2f} "
                f"for the film{spread}")


def calibrate(energies: dict[int, Energies], mode: str,
              prior: int = PRIOR_FRAMES) -> Calibration | None:
    """Factors for the film and for each measured scene; None with nothing measured."""
    whole = overall(energies)
    if whole is None:
        return None
    keep, ly, cb, cr = grain.decide_strength(factors(whole), mode)
    film = (ly, cb, cr)
    scenes = {}
    for scene, e in energies.items():
        own = factors(e)
        scenes[scene] = tuple(shrink(own[p], film[p], e.frames, prior) for p in range(3))
    return Calibration(keep, film, scenes)  # type: ignore[arg-type]


def scaled(entries: Sequence[grain.Entry], scene_starts: Sequence[int],
           calibration: Calibration) -> list[grain.Entry]:
    """Each frame's entry scaled by its scene's factors."""
    starts = sorted({s for s in scene_starts if s >= 0} | {0})
    cache: dict[tuple[tuple[str, ...], tuple[float, float, float]], tuple[str, ...]] = {}
    out: list[grain.Entry] = []
    for frame, entry in enumerate(entries):
        if not entry.apply:
            out.append(entry)
            continue
        f = tuple(round(x, 3) for x in calibration.for_scene(scene_of(starts, frame)))
        body = cache.get((entry.body, f))  # type: ignore[arg-type]
        if body is None:
            body = grain.scale_entry(entry, *f).body
            cache[(entry.body, f)] = body  # type: ignore[index]
        out.append(grain.replace_body(entry, body))
    return out


# --- the whole step -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result:
    keep: bool              # False: the source has no grain to put back
    video: Path | None      # the stream with grain, when kept
    table: Path | None      # the table it carries


def apply(read: MediaInfo, base: Path, table: Path, scene_starts: Sequence[int],
          chain: Toolchain, cfg: Config, work: Path, prefilter: str) -> Result:
    """Write the per-frame `table` into `base` (an encode, with grain or without),
    calibrated scene by scene against the source `read`.

    Raises essential.Mismatch when the stream's frames are not the table's.
    """
    entries = essential.load(table)
    stream = read_stream(chain, base)
    if len(entries) != len(stream.stamps):
        raise essential.Mismatch(f"the grain estimate has {len(entries)} frames, the encode "
                                 f"{len(stream.stamps)}")
    starts = sorted({s for s in scene_starts if 0 <= s < len(entries)} | {0})
    first = work / "grain_uncalibrated.tbl"
    first.write_text(grain.retime(entries, stream.stamps), encoding="utf-8")
    grainy = work / "grainy.mkv"
    grain.apply_table(base, first, chain, grainy)
    if not cfg.grain.calibrate:
        return Result(True, grainy, first)

    windows = plan_windows(starts, stream.keyframes, len(entries), budget(read, cfg),
                           cfg.grain.calibration_window)
    log.info("  grain calibration: %d windows of up to %d frames in %d of %d scenes",
             len(windows), cfg.grain.calibration_window,
             len({w.scene for w in windows}), len(starts))
    # A window per core, each single-threaded (window_command): 0.89 s a window on
    # an RK3588 against 1.09 for four at ffmpeg's default threads. Twelve, or two
    # threads in the graph, gave no more (TECHNICAL.md, "Calibration's threads").
    energies = measure(chain, windows, read.path, base, grainy, stream, prefilter, work,
                       processes=os.cpu_count() or 2)
    calibration = calibrate(energies, cfg.grain.mode, prior=2 * cfg.grain.calibration_window)
    if calibration is None:
        log.warning("  grain strength not calibrated: measurements inconclusive")
        return Result(True, grainy, first)
    if not calibration.keep:
        return Result(False, None, None)
    final = work / "grain.tbl"
    final.write_text(grain.retime(scaled(entries, starts, calibration), stream.stamps),
                     encoding="utf-8")
    grain.apply_table(base, final, chain, grainy)
    first.unlink(missing_ok=True)
    log.info("  grain calibrated scene by scene: %s", calibration.describe())
    return Result(True, grainy, final)
