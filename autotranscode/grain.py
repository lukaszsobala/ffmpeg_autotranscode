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

import json
import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import encode
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, child_env, human_size, log, run
from .verify import GRAIN_FILTER, parse_mse_log

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


def sample_times(duration: float, count: int, sample_seconds: int) -> list[float]:
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
    times = sample_times(info.duration, cfg.grain.samples, cfg.grain.sample_seconds)
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


# See _extract_lossless: grav1synth needs rows that are a multiple of 32 bytes.
ANALYSIS_CROP = "crop=trunc(iw/32)*32:ih:0:0"


def _extract_lossless(
    info: MediaInfo, chain: Toolchain, cfg: Config, times: list[float], work: Path
) -> Path:
    """Concatenate short lossless excerpts of the source into one analysis clip.

    Lossless matters: any compression artefact in the intermediate would be solved
    for as if it were grain.

    The width is cropped to a multiple of 32. grav1synth reads each row with a
    stride padded to 32 bytes and then rejects a frame whose size differs from
    width x height -- "data length mismatch, expected 15896384, found 15912960"
    on a 3836-wide 10-bit source. That rules out 720-wide DVDs, 854, and cropped
    releases such as 1916 or 3836, measured failing at both bit depths: 32 bytes
    is 16 samples at 10-bit and 32 at 8-bit, so a multiple of 32 suits both.
    Grain parameters are statistics of the noise, so the columns lost (at most
    31) cannot change them.
    """
    parts: list[Path] = []
    for index, start in enumerate(times):
        part = work / f"src_{index:02d}.mkv"
        run([
            chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", str(cfg.grain.sample_seconds),
            "-i", str(info.path),
            "-an", "-sn", "-dn", "-map", "0:v:0",
            "-vf", ANALYSIS_CROP,
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


def frame_grain_params(chain: Toolchain, path: Path) -> list[dict]:
    """Every frame's AV1 film grain parameters, as ffmpeg's decoder reports them."""
    result = run([chain.ffprobe, "-v", "error", "-export_side_data", "film_grain",
                  "-select_streams", "v:0", "-show_frames", "-of", "json", str(path)])
    frames: list[dict] = []
    for frame in json.loads(result.stdout or "{}").get("frames", []):
        for side in frame.get("side_data_list") or []:
            if side.get("side_data_type") == "Film grain parameters" and side.get("type") == "av1":
                frames.append(side)
    return frames


def estimate_with_svt(info: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> Path | None:
    """A grain table from SVT-AV1's own estimate, made on the grain samples alone.

    The samples are encoded with --film-grain, which makes SVT estimate the grain
    of every frame; ffmpeg reads those parameters back, and the most typical
    frame's become the table. Only a few seconds are encoded, so the estimate costs
    minutes where --film-grain on the whole film doubles a day-long encode.
    """
    if not chain.svtav1encapp:
        log.warning("SvtAv1EncApp unavailable; skipping grain table")
        return None
    times = sample_times(info.duration, cfg.grain.samples, cfg.grain.sample_seconds)
    log.info("grain estimate (SVT-AV1): %d samples x %ds", len(times), cfg.grain.sample_seconds)
    clip = _extract_lossless(info, chain, cfg, times, work)
    ivf = work / "grain_estimate.ivf"
    params = encode.svt_params(info, cfg, chain)
    if "--preset" in params:
        at = params.index("--preset")
        params = params[:at] + params[at + 2:]
    decode = [chain.ffmpeg, "-hide_banner", "-v", "error", "-i", str(clip),
              "-fps_mode", "passthrough", "-pix_fmt", encode.pix_format(info, cfg),
              "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
    compress = [chain.svtav1encapp, "-i", "stdin", "-b", str(ivf), "--progress", "0",
                "--preset", str(cfg.grain.estimate_preset), *params,
                "--film-grain", str(cfg.grain.svt_level), "--film-grain-denoise", "0"]
    env = child_env(chain.bin_dirs)
    try:
        with subprocess.Popen(decode, stdout=subprocess.PIPE, env=env) as source:
            encoder = subprocess.run(compress, stdin=source.stdout, capture_output=True,
                                     text=True, errors="replace", env=env)
        if encoder.returncode != 0:
            log.warning("SVT grain estimate failed: %s", encoder.stderr.strip()[-300:])
            return None
        frames = frame_grain_params(chain, ivf)
    except (CommandError, OSError) as exc:
        log.warning("SVT grain estimate failed: %s", str(exc)[:300])
        return None
    finally:
        clip.unlink(missing_ok=True)
    ivf.unlink(missing_ok=True)
    if not frames:
        log.warning("SVT found no grain in the samples; no table")
        return None
    table = work / "grain.tbl"
    table.write_text(table_from_params(medoid(frames)), encoding="utf-8")
    log.info("grain table ready: %s (from %d frames)", table, len(frames))
    return table


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
    if cfg.grain.estimator == "svt":
        return estimate_with_svt(info, chain, cfg, work)

    if cfg.grain.analysis == "full":
        times = [0.0]
        seconds = int(info.duration)
        log.info("grain analysis: full file (this needs a lot of scratch space)")
        sample_cfg_seconds = seconds
    else:
        times = sample_times(info.duration, cfg.grain.samples, cfg.grain.sample_seconds)
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


def _numbers(value: object) -> list[int]:
    """ffprobe writes parameter lists as space-separated strings."""
    return [int(v) for v in str(value).split()] if value not in (None, "") else []


def _ar_real(params: dict) -> list[float]:
    """Every AR coefficient of a frame in real units, so frames coded with
    different ar_coeff_shift values can be compared."""
    scale = 2 ** int(params["ar_coeff_shift"])
    components = params.get("components", [])
    coeffs = _numbers(components[0].get("ar_coeffs_y")) if components else []
    for component in components[1:]:
        coeffs += _numbers(component.get("ar_coeffs_uv"))
    return [c / scale for c in coeffs]


def medoid(frames: list[dict]) -> dict:
    """The frame whose grain texture is the most typical of all of them.

    An encoder estimates grain afresh per frame, and one-off estimates can carry
    a directional quirk -- a single SVT-AV1 frame picked by median strength drew
    faint vertical stripes. The per-coefficient median is typical but is not a
    parameter set any frame used; the frame closest to it is both.
    """
    vectors = [_ar_real(f) for f in frames]
    width = min(len(v) for v in vectors)
    centre = [statistics.median(v[i] for v in vectors) for i in range(width)]
    distances = [sum((v[i] - centre[i]) ** 2 for i in range(width)) for v in vectors]
    return frames[distances.index(min(distances))]


def table_from_params(params: dict) -> str:
    """A one-segment film grain table, covering the whole video, from the
    parameters ffmpeg's AV1 decoder reports for one frame.

    Written from the decoder's view rather than taken from `grav1synth inspect`,
    which writes scaling_shift as 8 whatever the stream says: on SVT-AV1's 11
    that makes the grain eight times too strong. ffmpeg stores the chroma
    multipliers less their spec offsets (128, 128, 256), which the table format
    does not, hence the additions.
    """
    components = params.get("components", [])
    luma = components[0] if components else {}
    chroma = components[1:3] if len(components) >= 3 else [{}, {}]

    def mults(c: dict) -> list[int]:
        return [int(c.get("uv_mult", 0)) + 128, int(c.get("uv_mult_luma", 0)) + 128,
                int(c.get("uv_offset", 0)) + 256]

    def points(values: object, scalings: object) -> list[str]:
        pairs = list(zip(_numbers(values), _numbers(scalings), strict=False))
        return [str(len(pairs)), *(str(n) for pair in pairs for n in pair)]

    header = [params["ar_coeff_lag"], params["ar_coeff_shift"], params["grain_scale_shift"],
              params["scaling_shift"], params["chroma_scaling_from_luma"],
              params["overlap_flag"], *mults(chroma[0]), *mults(chroma[1])]
    lines = [
        GRAIN_TABLE_HEADER,
        f"E 0 {WHOLE_VIDEO_END} 1 {int(params.get('seed', 0))} 1",
        "\tp " + " ".join(str(v) for v in header),
        "\tsY " + " ".join(points(luma.get("y_points_value"), luma.get("y_points_scaling"))),
        "\tsCb " + " ".join(points(chroma[0].get("uv_points_value"),
                                   chroma[0].get("uv_points_scaling"))),
        "\tsCr " + " ".join(points(chroma[1].get("uv_points_value"),
                                   chroma[1].get("uv_points_scaling"))),
        "\tcY " + " ".join(map(str, _numbers(luma.get("ar_coeffs_y")))),
        "\tcCb " + " ".join(map(str, _numbers(chroma[0].get("ar_coeffs_uv")))),
        "\tcCr " + " ".join(map(str, _numbers(chroma[1].get("ar_coeffs_uv")))),
    ]
    return "\n".join(lines) + "\n"


def scale_table(raw: str, luma: float, cb: float | None = None, cr: float | None = None) -> str:
    """The same grain table with its strength multiplied, per plane.

    AV1 grain synthesis adds noise * scaling(pixel value), so scaling the points of
    the piecewise scaling functions -- sY, sCb, sCr, written as a count followed by
    (value, scaling) pairs -- scales the grain's amplitude and nothing else: its
    texture, set by the AR coefficients, is untouched. `cb` and `cr` default to
    `luma`. Scalings are 8-bit, so they are clamped to 0-255.
    """
    factors = {"sY": luma, "sCb": luma if cb is None else cb, "sCr": luma if cr is None else cr}
    out: list[str] = []
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] in factors and len(fields) > 1:
            count = int(fields[1])
            points = [int(v) for v in fields[2:2 + 2 * count]]
            for index in range(1, len(points), 2):
                points[index] = max(0, min(255, round(points[index] * factors[fields[0]])))
            indent = line[: len(line) - len(line.lstrip())]
            line = indent + " ".join([fields[0], str(count), *map(str, points)])
        out.append(line)
    return "\n".join(out) + ("\n" if raw.endswith("\n") else "")


PLANES = ("y", "u", "v")


def window_grain(
    path: Path, chain: Toolchain, times: list[float], seconds: float, work: Path,
    *, synthesis: bool = True,
) -> tuple[float, float, float]:
    """Mean grain energy of `path` over the given windows, per plane (Y, U, V).

    The same measure as verify's grain retention: what a 3x3 median removes. With
    `synthesis` off, the AV1 decoder hands film grain parameters over as side data
    instead of adding the grain, which measures the encode underneath.
    """
    values: dict[str, list[float]] = {plane: [] for plane in PLANES}
    for index, start in enumerate(times):
        stats = work / f"grain_{path.stem}_{index}_{int(synthesis)}.log"
        run([
            chain.ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-y",
            *([] if synthesis else ["-export_side_data", "film_grain"]),
            "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path),
            "-an", "-sn", "-dn",
            "-filter_complex",
            f"[0:v]split=2[ref][pre];[pre]{GRAIN_FILTER}[den];"
            f"[den][ref]psnr=stats_file={stats.as_posix()}",
            "-f", "null", "-",
        ])
        text = stats.read_text(encoding="utf-8", errors="replace")
        for plane in PLANES:
            values[plane] += parse_mse_log(text, plane)
    y, u, v = (statistics.fmean(values[plane]) if values[plane] else 0.0 for plane in PLANES)
    return y, u, v


def strength_factor(source: float, underneath: float, with_grain: float) -> float | None:
    """How much to scale a grain table so the result matches the source's grain.

    Energies add: the encode keeps some fine texture of its own (`underneath`), and
    synthesised grain adds (factor * amplitude) squared on top. Matching the source
    means factor^2 * (with_grain - underneath) = source - underneath. None when the
    measurements cannot support an answer -- no synthesised grain to scale, or an
    encode that already carries more texture than the source.
    """
    synthesised = with_grain - underneath
    wanted = source - underneath
    if synthesised <= 0 or wanted <= 0:
        return None
    return (wanted / synthesised) ** 0.5


def calibrate_strength(
    source: Path, grainy: Path, chain: Toolchain, cfg: Config, work: Path, duration: float,
) -> tuple[float | None, float | None, float | None]:
    """Per-plane factors that make `grainy`'s synthesised grain match `source`'s.

    Chroma is calibrated on its own: grav1synth's colour grain was far stronger,
    relative to its luma grain, than the source's, and showed as coloured speckle.
    """
    times = sample_times(duration, cfg.grain.samples, cfg.grain.sample_seconds)
    seconds = float(cfg.grain.sample_seconds)
    try:
        wanted = window_grain(source, chain, times, seconds, work)
        underneath = window_grain(grainy, chain, times, seconds, work, synthesis=False)
        with_grain = window_grain(grainy, chain, times, seconds, work, synthesis=True)
    except CommandError as exc:
        log.warning("grain calibration failed: %s", exc.stderr.strip()[:200])
        return None, None, None
    y, u, v = (strength_factor(w, n, g) for w, n, g in zip(wanted, underneath, with_grain,
                                                           strict=True))
    return y, u, v


def apply_table(video: Path, table: Path, chain: Toolchain, output: Path) -> Path:
    """Rewrite the grain headers of a finished AV1 stream from a grain table.

    grav1synth 0.2.0's `apply` takes only the input, -o, -g and -y; there is no
    --replace and no --chroma. Whether chroma planes get grain is decided by the
    table, which `diff` fills in from the source (the sCb/sCr lines), not by a flag
    here.
    """
    argv = [chain.grav1synth, "apply", str(video), "-o", str(output), "-g", str(table), "-y"]
    run(argv)
    return output
