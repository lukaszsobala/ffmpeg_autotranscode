"""Measure what an encode did to the picture, in ways that match what people see.

PSNR is the wrong tool for this job, and was shown to be: a 4K HDR encode that
scored 50.8 dB luma PSNR was visibly blocky in flat areas and had lost its grain.
PSNR averages squared error over every pixel, so a smooth gradient broken into
blocks costs almost nothing -- the steps are a code value or two high -- while
the eye goes straight to it. On PQ material the effect is worse, because most of
the picture sits in a narrow range of dark code values.

Three measurements replace it:

XPSNR (luma)
    PSNR weighted by local activity: an error in a flat patch counts far more
    than the same error in texture, which is how visibility actually works. It
    is what flagged the blocking that PSNR missed.

    ffmpeg's xpsnr filter is broken for high bit depth on this machine: a 10-bit
    source compared with *itself* scores 9.98 dB rather than infinity, in both
    ffmpeg 7.1 and jellyfin-ffmpeg 8.1, and in 16-bit too. Both inputs are
    therefore converted to 8-bit first, identically. That hides errors below a
    quarter of an 8-bit step, which is far below anything visible, and it is the
    difference between a metric and a random number.

Grain retention
    How much pixel-scale texture survived: what a 3x3 median filter removes from
    the encode, as a fraction of what it removes from the source. 100% is the
    source's grain intact. Measured on the decoded picture *with* synthesised
    grain applied, because that is what a viewer gets.

    A median, not the grain probe's hqdn3d, because a median keeps step edges
    and removes isolated pixel noise -- so block edges, which are artefacts, do
    not get counted as surviving grain. Measured on the encode that looked
    grainless, at three points in the film:

        filter            retained
        hqdn3d=8:6:12:12  92%  99%  97%   (says nothing was lost)
        median=radius=1   36%  43%  46%   (matches what the eye saw)

Snapshots
    The frames the metric likes least, tonemapped the same way for every file so
    they can be flipped between in an image viewer. A metric only earns trust by
    agreeing with the eye on its worst frames.

A full-reference metric punishes grain synthesis: synthetic grain is random, so
it never matches the source's grain pixel for pixel. XPSNR is therefore measured
with synthesis turned *off* in the decoder, which scores the encode underneath,
and grain retention is measured with it on.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .tools import Toolchain
from .util import CommandError, log, run

# Separates grain from structure for the retention measure; see the docstring.
GRAIN_FILTER = "median=radius=1"

# Treat 'inf' (a frame identical to its reference) as this, so means stay finite.
IDENTICAL_DB = 100.0

# Removes pixel-scale grain and keeps anything 8 px or larger -- block steps,
# banding contours, lost detail. See lowpass_psnr().
LOWPASS = "gblur=sigma=2"

# Pair frames by decode order rather than by timestamp; see xpsnr().
ALIGN = "settb=1/1000,setpts=N"

# Scores XPSNR as 8-bit; see the module docstring for why.
XPSNR_FORMAT = "yuv420p"

# Tonemapping for snapshots of PQ sources, applied identically to every file so
# that the comparison is fair. Only the viewing is SDR; nothing is measured on it.
PQ_TO_SDR = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=rgb24"
)


@dataclass(slots=True)
class Comparison:
    """How `distorted` compares with `reference`, frame for frame."""

    frames: int
    xpsnr: list[float]
    lowpass: list[float] = field(default_factory=list)
    grain_retention: float | None = None
    grain_by_frame: list[float] = field(default_factory=list)
    worst: list[int] = field(default_factory=list)

    @property
    def xpsnr_mean(self) -> float:
        return statistics.fmean(self.xpsnr) if self.xpsnr else 0.0

    @property
    def xpsnr_p5(self) -> float:
        """The 5th percentile: how bad the bad frames are, without one outlier."""
        return percentile(self.xpsnr, 5)

    @property
    def xpsnr_min(self) -> float:
        return min(self.xpsnr) if self.xpsnr else 0.0

    @property
    def lowpass_mean(self) -> float:
        return statistics.fmean(self.lowpass) if self.lowpass else 0.0

    @property
    def lowpass_p5(self) -> float:
        return percentile(self.lowpass, 5)

    def describe(self) -> str:
        grain = (f", grain retained {100 * self.grain_retention:.0f}%"
                 if self.grain_retention is not None else "")
        lowpass = (f", low-pass PSNR mean {self.lowpass_mean:.2f} dB, "
                   f"5th pct {self.lowpass_p5:.2f}" if self.lowpass else "")
        return (f"{self.frames} frames: XPSNR mean {self.xpsnr_mean:.2f} dB, "
                f"5th pct {self.xpsnr_p5:.2f}, worst {self.xpsnr_min:.2f}{lowpass}{grain}")


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def parse_xpsnr_log(text: str) -> list[float]:
    """Per-frame luma XPSNR from the filter's stats file, in frame order."""
    scores: list[float] = []
    for match in re.finditer(r"XPSNR y:\s*([0-9.]+|inf)", text):
        raw = match.group(1)
        scores.append(IDENTICAL_DB if raw == "inf" else float(raw))
    return scores


def parse_psnr_log(text: str) -> list[float]:
    """Per-frame luma PSNR from the psnr filter's stats file."""
    return [IDENTICAL_DB if raw == "inf" else float(raw)
            for raw in re.findall(r"psnr_y:([0-9.]+|inf)", text)]


def parse_mse_log(text: str, plane: str = "y") -> list[float]:
    """Per-frame MSE of one plane (y, u or v) from the psnr filter's stats file."""
    return [float(value) for value in re.findall(rf"mse_{plane}:([0-9.]+)", text)]


def worst_frames(scores: list[float], count: int, spacing: int) -> list[int]:
    """The `count` lowest-scoring frames, at least `spacing` frames apart.

    Adjacent frames of one bad shot say the same thing; spreading the picks out
    shows whether a problem is one scene or the whole film.
    """
    picked: list[int] = []
    for index in sorted(range(len(scores)), key=scores.__getitem__):
        if all(abs(index - other) >= spacing for other in picked):
            picked.append(index)
            if len(picked) == count:
                break
    return sorted(picked)


def worst_windows(
    scores: list[float], window: int, count: int, margin: int = 0,
    avoid: list[int] | None = None,
) -> list[int]:
    """Start frames of the `count` lowest-mean windows of `window` frames.

    Windows never overlap each other or any start in `avoid`, and `margin`
    frames at each end are ignored, where logos and credits would otherwise win.
    """
    taken = list(avoid or [])
    last = len(scores) - window - margin
    if window <= 0 or last < margin:
        return [0] if scores else []
    prefix = [0.0]
    for value in scores:
        prefix.append(prefix[-1] + value)
    means = {start: (prefix[start + window] - prefix[start]) / window
             for start in range(margin, last + 1)}
    picked: list[int] = []
    for start in sorted(means, key=means.__getitem__):
        if all(abs(start - other) >= window for other in picked + taken):
            picked.append(start)
            if len(picked) == count:
                break
    return sorted(picked)


def xpsnr(chain: Toolchain, reference: Path, distorted: Path, stats: Path) -> list[float]:
    """Per-frame luma XPSNR of `distorted` against `reference`.

    Synthesised film grain is not applied to `distorted` (see module docstring):
    `-export_side_data film_grain` tells the AV1 decoder to hand the grain
    parameters over as side data instead of adding the grain to the picture.

    Frames are paired by index, not by timestamp. ffmpeg's comparison filters
    pair by timestamp, and Matroska stores whole milliseconds: two muxers can
    round a 23.976 fps frame to 41 and 42 ms, and a frame one millisecond late
    gets compared with its predecessor. That scored an encode 10-25 dB on frames
    that match at 43.7 dB once paired properly -- and it inflates or deflates
    plain PSNR the same way. Index pairing is right because the pipeline's
    output is frame-exact; `ALIGN` renumbers both inputs 0, 1, 2...
    """
    argv = [
        chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(reference),
        "-export_side_data", "film_grain", "-i", str(distorted),
        "-lavfi",
        f"[0:v]{ALIGN},format={XPSNR_FORMAT}[ref];"
        f"[1:v]{ALIGN},format={XPSNR_FORMAT}[dist];"
        f"[ref][dist]xpsnr=stats_file={stats.as_posix()}",
        "-f", "null", "-",
    ]
    run(argv)
    return parse_xpsnr_log(stats.read_text(encoding="utf-8", errors="replace"))


def lowpass_psnr(chain: Toolchain, reference: Path, distorted: Path, stats: Path) -> list[float]:
    """Per-frame luma PSNR of both files after a slight blur, at 10-bit.

    Separates the two ways a grainy film gets damaged. An encoder that strips
    grain leaves an error the size of the grain at every pixel, and in any
    full-band metric that error swamps the block steps and banding contours
    left behind -- which are one or two 10-bit code values tall, but span flat
    areas where the eye finds them at once. XPSNR fares worse still: it reads
    the source's grain as texture and discounts errors there, so a grainy sky
    counts as busy and its blocking is forgiven.

    A Gaussian of sigma 2 removes most of the 1-2 pixel grain from both files
    and keeps structures of 8 pixels and up. What remains of the error is
    structural damage with the grain factored out; grain retention measures the
    grain separately. Run at 10-bit, since the steps in question are below one
    8-bit code value, and paired by index for the reason given in xpsnr().
    """
    argv = [
        chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(reference),
        "-export_side_data", "film_grain", "-i", str(distorted),
        "-lavfi",
        f"[0:v]{ALIGN},format=yuv420p10le,{LOWPASS}[ref];"
        f"[1:v]{ALIGN},format=yuv420p10le,{LOWPASS}[dist];"
        f"[ref][dist]psnr=stats_file={stats.as_posix()}",
        "-f", "null", "-",
    ]
    run(argv)
    return parse_psnr_log(stats.read_text(encoding="utf-8", errors="replace"))


def noise_mse(chain: Toolchain, path: Path, stats: Path) -> list[float]:
    """Per-frame luma MSE between `path` and its median-filtered self: its grain."""
    argv = [
        chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(path),
        "-an", "-sn", "-dn",
        "-filter_complex",
        f"[0:v]split=2[ref][pre];[pre]{GRAIN_FILTER}[den];"
        f"[den][ref]psnr=stats_file={stats.as_posix()}",
        "-f", "null", "-",
    ]
    run(argv)
    return parse_mse_log(stats.read_text(encoding="utf-8", errors="replace"))


def retention(source: list[float], encoded: list[float]) -> float:
    """Grain amplitude kept overall: the ratio of RMS noise, 0.5 = half as strong."""
    count = min(len(source), len(encoded))
    total = sum(source[:count])
    return (sum(encoded[:count]) / total) ** 0.5 if total else 0.0


def retention_by_frame(source: list[float], encoded: list[float]) -> list[float]:
    """The same ratio frame by frame, so the scenes that lost most can be found.

    A frame with no measurable grain in the source says nothing about loss and
    scores 1.0, so that flat black frames do not look like the worst offenders.
    """
    return [(e / s) ** 0.5 if s > 0 else 1.0 for s, e in zip(source, encoded, strict=False)]


def grain_retention(
    chain: Toolchain, reference: Path, distorted: Path, work: Path,
) -> tuple[float, list[float]]:
    """Grain amplitude kept by `distorted`, overall and per frame.

    Pixel-scale detail (hair, fabric weave) counts too; losing that is a loss
    the eye sees as well.
    """
    source = noise_mse(chain, reference, work / "noise_reference.log")
    encoded = noise_mse(chain, distorted, work / "noise_distorted.log")
    return retention(source, encoded), retention_by_frame(source, encoded)


def snapshots(
    chain: Toolchain, path: Path, frames: list[int], out_dir: Path, label: str,
    *, pq: bool,
) -> list[Path]:
    """Write `frames` of `path` as PNGs named <label>_f<frame>.png.

    Frames are chosen by decode index with select, not by timestamp: seeking by
    time can land one frame apart in two files, and a comparison of different
    frames is worse than none.
    """
    if not frames:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    chosen = "+".join(f"eq(n\\,{frame})" for frame in sorted(frames))
    view = PQ_TO_SDR if pq else "format=rgb24"
    pattern = out_dir / f".{label}_%03d.png"
    run([
        chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y",
        "-i", str(path),
        "-vf", f"select='{chosen}',{view}",
        "-fps_mode", "passthrough",
        str(pattern),
    ])
    written: list[Path] = []
    for position, frame in enumerate(sorted(frames), start=1):
        produced = out_dir / f".{label}_{position:03d}.png"
        final = out_dir / f"{label}_f{frame:05d}.png"
        if produced.is_file():
            produced.replace(final)
            written.append(final)
    return written


def compare(
    chain: Toolchain, reference: Path, distorted: Path, work: Path,
    *, grain: bool = True, worst: int = 3,
) -> Comparison:
    """Everything above, for one pair of files."""
    work.mkdir(parents=True, exist_ok=True)
    scores = xpsnr(chain, reference, distorted, work / "xpsnr.log")
    if not scores:
        raise CommandError(["xpsnr"], 1, "XPSNR produced no scores")
    result = Comparison(frames=len(scores), xpsnr=scores)
    try:
        result.lowpass = lowpass_psnr(chain, reference, distorted, work / "lowpass.log")
    except CommandError as exc:
        log.warning("low-pass PSNR not measured: %s", exc.stderr.strip()[:200])
    # Worst frames by the structural measure when there is one: XPSNR is blind to
    # blocking under grain (see lowpass_psnr), so its worst frames are the wrong ones.
    ranked = result.lowpass or scores
    result.worst = worst_frames(ranked, worst, spacing=max(24, len(ranked) // 20))
    if grain:
        try:
            result.grain_retention, result.grain_by_frame = grain_retention(
                chain, reference, distorted, work)
        except CommandError as exc:
            log.warning("grain retention not measured: %s", exc.stderr.strip()[:200])
    return result
