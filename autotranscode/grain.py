"""Film grain: measure it honestly, then reproduce it with AV1 grain synthesis.

The approach, and why:

AV1 can carry a *film grain table* describing the noise, strip the noise before
encoding, and re-synthesise it in the decoder. Grain is nearly incompressible, so
removing it before the encoder sees it is typically a large bitrate win while the
result still looks like the source.

To do that well you need a table derived from the actual source, which is what
`grav1synth diff` produces: it compares the source with a denoised copy of itself
and solves for the grain parameters. That beats guessing an ISO value.

Two constraints shape the implementation:

1. `diff` needs both files decodable and frame-aligned, and lossless intermediates
   are huge, so by default we analyse a handful of short samples rather than the
   whole film, then flatten the result into one table covering the whole runtime.
2. Av1an splits the video into chunks that each restart at timestamp zero, so a
   time-varying grain table cannot be handed to the encoder mid-pipeline without
   the segments landing on the wrong frames. We therefore apply grain to the
   *finished* stream with `grav1synth apply`, which rewrites the grain headers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, human_size, log, run

# Fixed reference denoiser used only for *measurement*, so scores are comparable
# across files. The strength actually used for encoding is chosen from the score.
#
# The thresholds here matter, so they were measured rather than guessed. Candidate
# probe filters were run against the same clip at four noise levels; most are not
# monotonic, because a denoiser with a low spatial threshold treats strong noise as
# detail and preserves it, so heavy grain can score *higher* than light grain:
#
#   probe filter           clean   light   medium   heavy    monotonic
#   hqdn3d=1.5:1.5:6:6     59.89   45.37   45.67    46.68    no
#   hqdn3d=4:3:6:6         57.65   42.72   43.06    44.79    no
#   hqdn3d=8:6:12:12       50.75   41.01   36.77    36.33    yes
#   gblur=sigma=0.8        35.30   34.90   33.88    32.58    yes, but no clean gap
#   atadenoise             64.79   47.62   49.68    51.00    no
#   removegrain=1          52.60   50.72   47.66    45.06    yes, weak clean gap
#
# hqdn3d=8:6:12:12 is monotonic *and* leaves a ~10 dB gap between clean and grainy,
# which is what a detection threshold needs.
PROBE_DENOISE = "hqdn3d=8:6:12:12"

# (exclusive upper bound on mean probe PSNR in dB, hqdn3d parameters for the real
# encode). Lower PSNR means the probe denoiser removed more, i.e. more grain.
#
# CALIBRATION WARNING: these bounds were derived from synthetic test patterns, which
# are cleaner and sharper than real footage. Real film loses some PSNR to genuine
# detail being softened, so absolute scores skew lower and these defaults will
# over-trigger. Run `autotranscode measure <files>` across a representative slice of
# your own library and set grain.threshold_db from what you see there.
DENOISE_LADDER: tuple[tuple[float, str], ...] = (
    (37.0, "hqdn3d=2.0:1.5:8:8"),   # heavy grain: 16mm, pushed stock, heavy digital noise
    (41.0, "hqdn3d=1.4:1.0:6:6"),   # clear 35mm grain
    (44.0, "hqdn3d=0.9:0.7:4:4"),   # light grain
)
LIGHTEST_DENOISE = "hqdn3d=0.6:0.5:3:3"

GRAIN_TABLE_HEADER = "filmgrn1"
WHOLE_VIDEO_END = 9_223_372_036_854_775_807


@dataclass(slots=True)
class GrainReport:
    """Outcome of measuring a source against its denoised self."""

    mean_psnr: float
    per_sample_psnr: list[float]
    sample_times: list[float]
    grainy: bool
    denoise_filter: str

    def describe(self) -> str:
        verdict = "grain route" if self.grainy else "no denoise"
        return (
            f"grain probe: mean PSNR {self.mean_psnr:.2f} dB over "
            f"{len(self.per_sample_psnr)} samples -> {verdict} ({self.denoise_filter or 'none'})"
        )


def _sample_times(duration: float, count: int, sample_seconds: int) -> list[float]:
    """Evenly spread sample points, avoiding logos at the head and credits at the tail."""
    usable_start = duration * 0.05
    usable_end = max(usable_start, duration * 0.95 - sample_seconds)
    if usable_end <= usable_start or count < 1:
        return [max(0.0, duration / 2 - sample_seconds / 2)]
    if count == 1:
        return [(usable_start + usable_end) / 2]
    step = (usable_end - usable_start) / (count - 1)
    return [usable_start + step * i for i in range(count)]


def _parse_psnr_log(path: Path) -> float:
    """Mean of the per-frame psnr_avg column written by ffmpeg's psnr filter."""
    values: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := re.search(r"psnr_avg:([0-9.]+|inf)", line):
            raw = match.group(1)
            # 'inf' means the denoiser changed nothing at all: a perfectly clean frame.
            values.append(100.0 if raw == "inf" else float(raw))
    return sum(values) / len(values) if values else 0.0


def _denoise_for(psnr: float, cfg: Config) -> str:
    if cfg.grain.denoise_strength != "auto":
        return cfg.grain.denoise_strength
    if cfg.grain.denoise_filter != "hqdn3d":
        # Caller supplied a different filter family but left strength on auto;
        # we have no calibrated ladder for it, so use it bare.
        return cfg.grain.denoise_filter
    for threshold, params in DENOISE_LADDER:
        if psnr < threshold:
            return params
    return LIGHTEST_DENOISE


def measure(info: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> GrainReport:
    """Measure how much noise a reference denoiser removes, sampled across the file.

    This replaces guesswork with a number in dB that you can log, compare and tune.
    """
    times = _sample_times(info.duration, cfg.grain.samples, cfg.grain.sample_seconds)
    scores: list[float] = []

    for index, start in enumerate(times):
        stats = work / f"psnr_{index:02d}.log"
        argv = [
            chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", str(cfg.grain.sample_seconds),
            "-i", str(info.path),
            "-an", "-sn", "-dn",
            "-filter_complex",
            f"[0:v]split=2[ref][pre];[pre]{PROBE_DENOISE}[den];"
            f"[den][ref]psnr=stats_file={stats.as_posix()}",
            "-f", "null", "-",
        ]
        try:
            run(argv)
        except CommandError as exc:
            log.warning("grain probe failed at %.1fs: %s", start, exc.stderr.strip()[:200])
            continue
        if stats.is_file():
            scores.append(_parse_psnr_log(stats))

    if not scores:
        log.warning("grain probe produced no samples; assuming clean source")
        return GrainReport(100.0, [], times, grainy=False, denoise_filter="")

    mean = sum(scores) / len(scores)

    if cfg.grain.mode == "always":
        grainy = True
    elif cfg.grain.mode == "never":
        grainy = False
    else:
        grainy = mean < cfg.grain.threshold_db

    return GrainReport(
        mean_psnr=mean,
        per_sample_psnr=scores,
        sample_times=times,
        grainy=grainy,
        denoise_filter=_denoise_for(mean, cfg) if grainy else "",
    )


# --- grain table generation -------------------------------------------------


def _extract_lossless(
    info: MediaInfo, chain: Toolchain, cfg: Config, times: list[float], work: Path
) -> Path:
    """Concatenate short lossless excerpts of the source into one analysis clip.

    Lossless matters: any compression artefact in the intermediate would be solved
    for as if it were grain.
    """
    parts: list[Path] = []
    for index, start in enumerate(times):
        part = work / f"src_{index:02d}.mkv"
        run([
            chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", str(cfg.grain.sample_seconds),
            "-i", str(info.path),
            "-an", "-sn", "-dn", "-map", "0:v:0",
            "-c:v", "ffv1", "-level", "3", "-g", "1",
            str(part),
        ])
        parts.append(part)

    if len(parts) == 1:
        return parts[0]

    listing = work / "samples.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    joined = work / "src_samples.mkv"
    run([
        chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(joined),
    ])
    for part in parts:
        part.unlink(missing_ok=True)
    return joined


def _denoise_lossless(source: Path, chain: Toolchain, denoise: str, work: Path) -> Path:
    output = work / "den_samples.mkv"
    run([
        chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
        "-i", str(source),
        "-an", "-sn", "-dn",
        "-vf", denoise,
        "-c:v", "ffv1", "-level", "3", "-g", "1",
        str(output),
    ])
    return output


def flatten_table(raw: str) -> str:
    """Collapse a multi-segment grain table into one segment covering the whole video.

    Segments produced from sampled excerpts carry timestamps from the excerpt
    timeline, which are meaningless once applied to the full film. We keep the
    segment with median grain strength (robust against one unusually dark or
    bright sample) and stretch it over the entire runtime.

    Only the `E` header line is rewritten; every parameter line is copied through
    verbatim, so we never have to reinterpret the encoder's solved coefficients.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != GRAIN_TABLE_HEADER:
        raise ValueError("not a film grain table (missing filmgrn1 header)")

    segments: list[list[str]] = []
    for line in lines[1:]:
        if line.lstrip().startswith("E "):
            segments.append([line])
        elif segments and line.strip():
            segments[-1].append(line)

    if not segments:
        raise ValueError("film grain table contains no segments")

    def strength(segment: list[str]) -> float:
        """Sum the luma scaling points; a bigger sum means stronger grain."""
        for line in segment:
            stripped = line.strip()
            if stripped.startswith("sY "):
                numbers = stripped.split()[1:]
                if not numbers:
                    return 0.0
                count = int(numbers[0])
                coords = [int(n) for n in numbers[1 : 1 + count * 2]]
                return float(sum(coords[1::2]))  # scaling values sit at odd offsets
        return 0.0

    chosen = sorted(segments, key=strength)[len(segments) // 2]

    e_fields = chosen[0].strip().split()
    # E <start> <end> <apply_grain> <random_seed> <update_parameters>
    if len(e_fields) < 6:
        raise ValueError(f"malformed E line: {chosen[0]!r}")
    e_fields[1] = "0"
    e_fields[2] = str(WHOLE_VIDEO_END)

    body = [" ".join(e_fields), *[line.rstrip() for line in chosen[1:]]]
    return "\n".join([GRAIN_TABLE_HEADER, *body]) + "\n"


def build_table(
    info: MediaInfo,
    chain: Toolchain,
    cfg: Config,
    report: GrainReport,
    work: Path,
) -> Path | None:
    """Derive a film grain table from the source. Returns None if it could not be made."""
    if not chain.grav1synth:
        log.warning("grav1synth unavailable; skipping grain table")
        return None

    if cfg.grain.analysis == "full":
        times = [0.0]
        seconds = int(info.duration)
        log.info("grain analysis: full file (this needs a lot of scratch space)")
        sample_cfg_seconds = seconds
    else:
        times = _sample_times(info.duration, cfg.grain.samples, cfg.grain.sample_seconds)
        sample_cfg_seconds = cfg.grain.sample_seconds
        log.info(
            "grain analysis: %d samples x %ds at %s",
            len(times), sample_cfg_seconds,
            ", ".join(f"{t / 60:.0f}m" for t in times),
        )

    original_seconds = cfg.grain.sample_seconds
    cfg.grain.sample_seconds = sample_cfg_seconds
    try:
        source_clip = _extract_lossless(info, chain, cfg, times, work)
    finally:
        cfg.grain.sample_seconds = original_seconds

    log.debug("lossless analysis clip: %s", human_size(source_clip.stat().st_size))
    denoised_clip = _denoise_lossless(source_clip, chain, report.denoise_filter, work)

    raw_table = work / "grain_raw.tbl"
    try:
        run([chain.grav1synth, "diff", str(source_clip), str(denoised_clip),
             "-o", str(raw_table)])
    except CommandError as exc:
        log.warning("grav1synth diff failed: %s", exc.stderr.strip()[:300])
        return None
    finally:
        source_clip.unlink(missing_ok=True)
        denoised_clip.unlink(missing_ok=True)

    if not raw_table.is_file() or not raw_table.stat().st_size:
        log.warning("grav1synth diff produced no table")
        return None

    final_table = work / "grain.tbl"
    try:
        final_table.write_text(
            flatten_table(raw_table.read_text(encoding="utf-8")), encoding="utf-8"
        )
    except ValueError as exc:
        log.warning("could not flatten grain table: %s", exc)
        return None

    log.info("grain table ready: %s", final_table)
    return final_table


def apply_table(
    video: Path, table: Path, chain: Toolchain, output: Path, chroma: bool
) -> Path:
    """Rewrite the grain headers of a finished AV1 stream from a grain table."""
    argv = [chain.grav1synth, "apply", str(video), "-o", str(output), "-g", str(table),
            "--replace"]
    if chroma:
        argv.append("--chroma")
    run(argv)
    return output
