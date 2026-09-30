"""Stop an encode early when it will not save enough.

The size check at the end of the pipeline only learns that a file grew once the
whole encode is paid for, and for a grainy 4K film that can be days. Av1an keeps
the answer on disk as it goes: done.json lists every finished chunk with its
frames and bytes, and scenes.json places each chunk in the film. Set against the
bytes the source spends on the same frames, the finished chunks give the ratio
the whole file is heading for, and from that the size of the result.

The source's bytes come from its packet sizes, read in the background while the
encode starts. Av1an finishes the longest chunks first, so the early ones are no
random sample of the film, but comparing like frames with like keeps the
projection honest about where the source spends its bits.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import mux
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import MIB, child_env, human_size, log


@dataclass(slots=True)
class Chunk:
    start: int  # first frame, counted from the start of the film
    end: int    # one past the last
    size: int   # encoded bytes


def finished_chunks(temp: Path) -> tuple[int, list[Chunk]]:
    """The film's frame count and Av1an's finished chunks; (0, []) until there are any.

    Chunk i is split_scenes[i] in scenes.json. chunks.json has frame ranges too,
    but with the hybrid method they count from the start of each split segment.
    """
    try:
        done = json.loads((temp / "done.json").read_text(encoding="utf-8"))
        scenes = json.loads((temp / "scenes.json").read_text(encoding="utf-8"))["split_scenes"]
        total = int(done.get("frames") or 0)
        entries = dict(done.get("done") or {})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0, []   # not written yet, or caught mid-write
    chunks: list[Chunk] = []
    for name, entry in entries.items():
        try:
            scene = scenes[int(name)]
            start, end = int(scene["start_frame"]), int(scene["end_frame"])
            if end - start == int(entry["frames"]):
                chunks.append(Chunk(start, end, int(entry["size_bytes"])))
        except (ValueError, IndexError, KeyError, TypeError):
            continue
    return total, chunks


def frame_sizes(lines: list[str]) -> list[int]:
    """Packet sizes in display order, from ffprobe's `pts,size` lines.

    Decode order when any timestamp is missing, as in an AVI with packed B-frames;
    over a chunk hundreds of frames long the difference is noise.
    """
    packets: list[tuple[int | None, int]] = []
    for line in lines:
        pts, _, size = line.strip().partition(",")
        if size.isdigit():
            packets.append((int(pts) if pts.lstrip("-").isdigit() else None, int(size)))
    if all(pts is not None for pts, _ in packets):
        packets.sort(key=lambda packet: packet[0])  # type: ignore[arg-type,return-value]
    return [size for _, size in packets]


class SourceSizes:
    """The source's video bytes per frame, read in the background.

    Reading every packet header of a large file takes minutes, and the first
    check is hours away, so it runs alongside the encode rather than before it.
    """

    def __init__(self, path: Path, chain: Toolchain):
        self.sizes: list[int] | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        try:
            self._proc = subprocess.Popen(
                [chain.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts,size", "-of", "csv=p=0", str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors="replace", env=child_env(),
            )
        except OSError as exc:
            log.warning("  cannot read the source's packet sizes (%s); "
                        "not checking the saving early", exc)
            return
        self._thread = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._thread.start()

    def _read(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        with proc.stdout:
            lines = proc.stdout.readlines()
        if proc.wait() == 0:
            self.sizes = frame_sizes(lines) or None
        if self.sizes is None and proc.returncode >= 0:   # not killed by close()
            log.warning("  could not read the source's packet sizes; "
                        "not checking the saving early")

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=10)


@dataclass(slots=True)
class Projection:
    frames_done: int
    frames: int
    ratio: float         # encoded bytes over the source's, for the frames done
    output_bytes: int    # the whole result: video, audio, subtitles
    saving_percent: float

    @property
    def done_percent(self) -> float:
        return 100.0 * self.frames_done / self.frames if self.frames else 0.0


def project(info: MediaInfo, cfg: Config, frames: int, chunks: list[Chunk],
            sizes: list[int]) -> Projection | None:
    """Where the finished chunks say the whole file is heading."""
    if not frames or not chunks or not sizes or not info.size_bytes:
        return None
    # Av1an and ffprobe can disagree by a frame or two; scale rather than trust either.
    scale = len(sizes) / frames
    running = [0, *itertools.accumulate(sizes)]

    def source_bytes(chunk: Chunk) -> int:
        return (running[min(len(sizes), round(chunk.end * scale))]
                - running[min(len(sizes), round(chunk.start * scale))])

    source = sum(source_bytes(chunk) for chunk in chunks)
    if source <= 0:
        return None
    ratio = sum(chunk.size for chunk in chunks) / source
    video = running[-1]
    # Audio, subtitles and chapters are copied, apart from the audio that is
    # re-encoded to Opus. Grain tables and the mux add almost nothing.
    other = max(0, info.size_bytes - video) - mux.audio_saving_bytes(info, cfg)
    output = int(video * ratio) + max(0, other)
    return Projection(
        frames_done=sum(chunk.end - chunk.start for chunk in chunks),
        frames=frames,
        ratio=ratio,
        output_bytes=output,
        saving_percent=100.0 * (1 - output / info.size_bytes),
    )


def describe_saving(percent: float) -> str:
    return f"{percent:.1f}% smaller" if percent >= 0 else f"{-percent:.1f}% larger"


def shortfall(cfg: Config, source_bytes: int, output_bytes: int) -> str | None:
    """Why an output of `output_bytes` is not worth keeping, or None if it is.

    It has to clear both minimums: the percentage and the absolute amount.
    """
    saved = source_bytes - output_bytes
    percent = 100.0 * saved / source_bytes if source_bytes else 0.0
    if percent < cfg.output.min_saving_percent:
        return (f"{describe_saving(percent)}, "
                f"below the {cfg.output.min_saving_percent:.0f}% threshold")
    if saved < cfg.output.min_saving_mib * MIB:
        change = "smaller" if saved >= 0 else "larger"
        return (f"{human_size(abs(saved))} {change}, "
                f"below the {cfg.output.min_saving_mib:g} MiB threshold")
    return None


class SavingWatch:
    """Asked during the encode whether to stop it: a reason, or None to carry on."""

    LOG_EVERY = 10.0  # percent of the frames between progress lines

    def __init__(self, info: MediaInfo, cfg: Config, chain: Toolchain, temp: Path):
        self.info, self.cfg, self.temp = info, cfg, temp
        self.source = SourceSizes(info.path, chain)
        self._next_log = cfg.output.check_saving_after_percent
        self._broken = False

    def __call__(self) -> str | None:
        try:
            return self._check()
        except Exception:  # noqa: BLE001 - never let a bookkeeping bug end a long encode
            if not self._broken:
                log.warning("  early saving check failed; the encode carries on", exc_info=True)
                self._broken = True
            return None

    def _check(self) -> str | None:
        sizes = self.source.sizes
        if self._broken or sizes is None:
            return None
        frames, chunks = finished_chunks(self.temp)
        if frames and abs(len(sizes) - frames) > max(10, frames // 50):
            # Not the stream Av1an is encoding: a cover image listed first, say.
            log.warning("  the source has %d video packets for %d frames; "
                        "not checking the saving early", len(sizes), frames)
            self._broken = True
            return None
        projection = project(self.info, self.cfg, frames, chunks, sizes)
        after = self.cfg.output.check_saving_after_percent
        if projection is None or projection.done_percent < after:
            return None
        if projection.done_percent >= self._next_log:
            self._next_log = projection.done_percent + self.LOG_EVERY
            log.info("  %.0f%% of the frames encoded, at %.2fx the source's bytes: "
                     "heading for %s", projection.done_percent, projection.ratio,
                     describe_saving(projection.saving_percent))
        reason = shortfall(self.cfg, self.info.size_bytes, projection.output_bytes)
        if reason is None:
            return None
        return f"stopped at {projection.done_percent:.0f}% of the frames: heading for {reason}"

    def close(self) -> None:
        self.source.close()
