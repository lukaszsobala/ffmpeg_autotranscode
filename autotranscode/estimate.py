"""Encode a few seconds from a few places and extrapolate the whole file.

A full encode at a slow preset can cost hours, so deciding what to transcode by
transcoding it is no help. This samples instead: cut several short excerpts,
encode them with the settings the real run would use, and compare their size
against the bytes the source spends on the same moments.

Two things make the result trustworthy enough to rank a library by:

1. The comparison is against the source's *actual* bytes in those intervals,
   summed from the packet sizes, not against an average bitrate. Sampling a quiet
   stretch of a film with a bursty bitrate would otherwise flatter the result.
2. The excerpts are encoded with the same CRF curve, the same SVT parameters and
   the same denoise decision as a real run, so the ratio comes from the pipeline
   being estimated rather than a generic one.

It is still an extrapolation. A handful of excerpts cannot know that the third act
is one long dark scene, and the encoder's rate control sees each excerpt cold,
without the lookahead a full run gives it. Treat the numbers as ranking
information, good to a few percent, not as a promise.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import encode, grain
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, child_env, log, run


@dataclass(slots=True)
class Estimate:
    """What sampling `info` suggests a full encode would do."""

    info: MediaInfo
    crf: float
    denoise: str
    sample_seconds: float = 0.0
    source_sampled: int = 0
    encoded_sampled: int = 0
    encode_seconds: float = 0.0
    starts: list[float] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.source_sampled > 0 and self.encoded_sampled > 0

    @property
    def ratio(self) -> float:
        """Encoded size as a fraction of the source's bytes for the same footage."""
        return self.encoded_sampled / self.source_sampled if self.source_sampled else 0.0

    @property
    def source_video_bytes(self) -> int:
        """Bytes the source spends on video, the only part an encode can shrink."""
        if self.info.video_bytes:
            return min(self.info.video_bytes, self.info.size_bytes)
        if self.info.video_bitrate and self.info.duration:
            measured = int(self.info.video_bitrate * self.info.duration / 8)
            return min(measured, self.info.size_bytes)
        return self.info.size_bytes

    @property
    def projected_bytes(self) -> int:
        """Whole-file output size: video scaled by the ratio, everything else kept.

        Audio and subtitles are carried through unchanged here. With
        `audio.mode = "opus"` a real run may also shrink the audio, so this is the
        pessimistic end for those files.
        """
        other = self.info.size_bytes - self.source_video_bytes
        return int(self.source_video_bytes * self.ratio) + other

    @property
    def saved_bytes(self) -> int:
        return self.info.size_bytes - self.projected_bytes

    @property
    def saving_percent(self) -> float:
        if not self.info.size_bytes:
            return 0.0
        return 100.0 * self.saved_bytes / self.info.size_bytes

    @property
    def fps(self) -> float:
        frames = self.sample_seconds * self.info.fps
        return frames / self.encode_seconds if self.encode_seconds else 0.0

    @property
    def projected_encode_seconds(self) -> float:
        """Rough: the sampled rate over the whole runtime.

        Optimistic by a little, because it counts neither scene detection nor the
        grain table nor the mux, and each excerpt starts at a keyframe.
        """
        return self.info.duration * self.info.fps / self.fps if self.fps else 0.0

    @property
    def saved_per_hour(self) -> float:
        """Bytes saved per hour of encoding -- what to sort a library by."""
        hours = self.projected_encode_seconds / 3600
        return self.saved_bytes / hours if hours else 0.0


def sample_count(duration: float, cfg: Config) -> int:
    """How many excerpts to take: more for a feature than for an episode."""
    if cfg.estimate.samples > 0:
        return cfg.estimate.samples
    per_hour = max(1.0, duration / 3600)
    return max(cfg.estimate.min_samples, min(cfg.estimate.max_samples, round(3 * per_hour)))


def _sampled_source_bytes(info: MediaInfo, chain: Toolchain, start: float, length: float) -> int:
    """Video bytes the source actually spends between `start` and `start + length`.

    Summed from packet sizes rather than taken from the average bitrate: bitrate
    varies enormously within a film, and an excerpt must be judged against the real
    cost of that footage or the ratio is meaningless.
    """
    argv = [
        chain.ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", f"{start:.3f}%+{length:.3f}",
        "-show_entries", "packet=size",
        "-of", "csv=p=0",
        str(info.path),
    ]
    try:
        result = run(argv)
    except CommandError as exc:
        log.warning("  could not read source packets at %.0fs: %s", start, exc.stderr[:120])
        return 0
    return sum(int(line) for line in result.stdout.split() if line.strip().isdigit())


def _encode_sample(
    info: MediaInfo, chain: Toolchain, cfg: Config, start: float, length: float,
    crf: float, denoise: str, out: Path,
) -> tuple[int, float]:
    """Encode one excerpt with the real settings. Returns (bytes, wall seconds).

    ffmpeg decodes to a y4m pipe and SvtAv1EncApp reads it, which is what Av1an
    does per chunk. -fps_mode passthrough for the same reason the Av1an wrapper
    needs it: without it a 23.976 fps source gains duplicated frames.
    """
    filters = ",".join(f for f in (encode.deinterlace_filter(info, cfg), denoise) if f)
    decode = [
        chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
        "-i", str(info.path),
        "-an", "-sn", "-dn",
        *(["-vf", filters] if filters else []),
        "-fps_mode", "passthrough",
        "-pix_fmt", encode.pix_format(info, cfg),
        "-strict", "-1", "-f", "yuv4mpegpipe", "-",
    ]
    compress = [
        chain.svtav1encapp, "-i", "stdin", "-b", str(out),
        "--progress", "0",
        *encode.svt_params(info, cfg, chain, crf=crf),
    ]

    log.debug("exec: %s | %s", " ".join(decode), " ".join(compress))
    started = time.monotonic()
    env = child_env(chain.bin_dirs)
    with subprocess.Popen(decode, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env) as src:
        assert src.stdout is not None
        encoder = subprocess.run(
            compress, stdin=src.stdout, capture_output=True, text=True,
            errors="replace", env=env,
        )
        src.stdout.close()
        src.wait()
    elapsed = time.monotonic() - started

    if encoder.returncode != 0 or not out.is_file():
        raise CommandError(compress, encoder.returncode or 1, encoder.stderr)
    return out.stat().st_size, elapsed


def estimate(info: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> Estimate:
    """Sample `info` and report what a full encode would probably do."""
    crf = encode.effective_crf(info, cfg)
    work.mkdir(parents=True, exist_ok=True)   # the grain probe writes its logs here

    # Of the grain route, only a denoise before the encode changes its size: the
    # grain table is written into the finished stream afterwards and adds nothing
    # measurable (101% on the test film). So the probe is only worth its time when
    # that denoise is on, which by default it is not.
    denoise = ""
    if (cfg.grain.denoise_before_encode and cfg.estimate.grain_probe
            and cfg.grain.mode != "never"):
        report = grain.measure(info, chain, cfg, work)
        if report.grainy:
            denoise = report.denoise_filter

    length = float(cfg.estimate.sample_seconds)
    count = sample_count(info.duration, cfg)
    # Never sample more than a quarter of a file; short files would otherwise be
    # "estimated" by encoding most of themselves.
    if info.duration and count * length > info.duration / 4:
        count = max(1, int(info.duration / 4 / length))

    starts = grain.sample_times(info.duration, count, int(length))
    result = Estimate(info=info, crf=crf, denoise=denoise, starts=starts)

    for index, start in enumerate(starts):
        out = work / f"sample_{index:02d}.ivf"
        out.unlink(missing_ok=True)
        source_bytes = _sampled_source_bytes(info, chain, start, length)
        if not source_bytes:
            continue
        try:
            encoded_bytes, elapsed = _encode_sample(
                info, chain, cfg, start, length, crf, denoise, out
            )
        except (CommandError, OSError) as exc:
            log.warning("  sample at %.0fs failed: %s", start, str(exc)[:160])
            continue
        finally:
            out.unlink(missing_ok=True)

        result.source_sampled += source_bytes
        result.encoded_sampled += encoded_bytes
        result.encode_seconds += elapsed
        result.sample_seconds += length

    return result
