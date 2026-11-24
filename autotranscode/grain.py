"""Film grain: estimate the source's, put it back with AV1 grain synthesis, and
check the result against the source.

Grain is close to incompressible, and SVT-AV1 discards it at any bitrate worth
having. AV1 can instead describe it in the bitstream, as a film grain table the
decoder synthesises from, at almost no cost. The work is describing grain that
looks like the source's. With the default estimator, "svtfg", that takes three
steps after the encode:

1. **Estimate** (essential.py). SvtFgEssential, SVT-AV1-Essential's standalone
   grain estimator, runs over every frame of the source and writes a table entry
   per frame. The estimate is entirely the tool's; autotranscode only splits the
   film at scene cuts so that several copies run at once, and joins their tables.
   It gets what calibration cannot fix: the grain's texture (how neighbouring
   pixels correlate) and how its strength follows brightness. SVT-AV1's own
   estimator, "svt", got neither (TECHNICAL.md, "The whole-film estimate").
2. **Time and write** (retime, apply_table). Av1an encodes in chunks that each
   restart at timestamp zero, so the table goes into the finished stream: retimed
   by its packets, then written into the frame headers by grav1synth. Nothing is
   re-encoded.
3. **Calibrate** (scenegrain.py, strength_factor). The grain in the result is
   measured against the source's, plane by plane and scene by scene, and each
   scene's entries are scaled to match. An estimate made from the source alone
   cannot know how much fine texture this encode keeps by itself, and its
   strength needed anything from x0.74 to x1.23 in luma, x0.63 to x1.53 in
   chroma, across six films, and differed again from scene to scene. Calibration
   is also the grain detector: a source with nothing to put back measures near
   zero (decide_strength).

The rest of this module serves the other estimators, kept as options: "svt"
(estimate_with_svt: SVT-AV1 on a few seconds of samples, one table for the film)
and "grav1synth" (grav1synth diff against a denoised copy, with the hqdn3d probe
in `measure` choosing the denoiser), and the table handling all of them share.
"""

from __future__ import annotations

import itertools
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
from .verify import decode_args, first, grain_graph, parse_mse_log

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

    def describe(self, denoising: bool = True) -> str:
        """One log line. `denoising` says whether the filter will actually be used;
        naming a denoiser that is never applied only misleads."""
        verdict = "grain route" if self.grainy else "no grain route"
        detail = f" ({self.denoise_filter or 'none'})" if denoising else ""
        return (
            f"grain probe: mean PSNR {self.mean_psnr:.2f} dB over "
            f"{len(self.per_sample_psnr)} samples -> {verdict}{detail}"
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
            f"[0:v]{first(encode.deinterlace_filter(info, cfg))}"
            f"split=2[ref][pre];[pre]{PROBE_DENOISE}[den];"
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
            "-vf", first(encode.deinterlace_filter(info, cfg)) + ANALYSIS_CROP,
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
        with subprocess.Popen(decode, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              env=env) as source:
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
    report: GrainReport | None,
    work: Path,
) -> Path | None:
    """Derive a film grain table from the source. Returns None if it could not be made.

    `report` is the grain probe's, needed only by the grav1synth estimator, which
    diffs the source against the denoiser the probe chose.
    """
    if not chain.grav1synth:
        log.warning("grav1synth unavailable; skipping grain table")
        return None
    if cfg.grain.estimator in {"svt", "svtfg"}:
        # "svtfg" makes its table after the encode (pipeline.film_grain); here,
        # only ever as the fallback from samples.
        return estimate_with_svt(info, chain, cfg, work)
    if report is None:
        raise ValueError("the grav1synth estimator needs the grain probe's report")

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


# --- tables with an entry per frame -------------------------------------------
#
# grav1synth (through av1-grain 0.4.1) reads a table as segments "E start end
# apply seed update" followed, when apply is 1, by every parameter line; it
# ignores `update` and skips a segment with apply 0, so frames there get no grain.
# Each frame takes the FIRST segment with start <= t <= end, both ends inclusive,
# where t is the packet's timestamp in ms times 10,000 -- Matroska's
# millisecond-rounded times, not frame number / rate. A table timed by frame
# number (SvtFgEssential's: entry n from n x 417083 at 23.976 fps) therefore gives
# about half the frames the entry before theirs. `retime` times each entry by
# the packets of the stream it is applied to instead.


@dataclass(frozen=True, slots=True)
class Entry:
    """One frame's grain, as a table segment says it without its times."""

    apply: bool
    seed: int
    # The parameter lines, stripped: "p ...", "sY ...", ... ("" when not applied).
    body: tuple[str, ...]

    def same_grain(self, other: Entry) -> bool:
        return self.apply == other.apply and self.body == other.body


def parse_entries(raw: str) -> list[Entry]:
    """A table's segments in order, times dropped. A segment that says it reuses
    the parameters before it (update 0) and gives none gets those parameters."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != GRAIN_TABLE_HEADER:
        raise ValueError("not a film grain table (missing filmgrn1 header)")
    entries: list[Entry] = []
    head: list[str] | None = None
    body: list[str] = []

    def close() -> None:
        if head is None:
            return
        if len(head) < 6:
            raise ValueError(f"malformed E line: {' '.join(head)!r}")
        apply = head[3] != "0"
        kept = tuple(body)
        if apply and not kept and entries:
            kept = entries[-1].body
        if apply and not kept:
            raise ValueError("a grain table segment applies grain but gives no parameters")
        entries.append(Entry(apply, int(head[4]) & 0xFFFF, kept if apply else ()))

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("E "):
            close()
            head, body = stripped.split(), []
        elif head is not None:
            body.append(" ".join(stripped.split()))
    close()
    return entries


def scale_entry(entry: Entry, luma: float, cb: float, cr: float) -> Entry:
    """The entry with its strength multiplied, per plane (see scale_table)."""
    if not entry.apply:
        return entry
    scaled = scale_table("\n".join([GRAIN_TABLE_HEADER, *entry.body]), luma, cb, cr)
    return replace_body(entry, tuple(scaled.splitlines()[1:]))


def replace_body(entry: Entry, body: tuple[str, ...]) -> Entry:
    return Entry(entry.apply, entry.seed, body)


def frame_table(entries: list[Entry]) -> str:
    """The entries as a table timed by frame number: segment n is frame n. For
    keeping a per-frame estimate; `retime` makes the table a stream gets."""
    out = [GRAIN_TABLE_HEADER]
    for number, entry in enumerate(entries):
        out.append(f"E {number} {number} {int(entry.apply)} {entry.seed} {int(entry.apply)}")
        out += [f"\t{line}" for line in entry.body]
    return "\n".join(out) + "\n"


def retime(entries: list[Entry], stamps: list[int]) -> str:
    """A table for a stream whose frames have packet timestamps `stamps` (in
    grav1synth's units, ms x 10,000, in display order), one entry per frame.

    Each run of frames with the same grain becomes one segment, from its first
    frame's timestamp to just before the next run's: so every frame falls in its
    own segment and no other, whatever the rounding. Runs keep their first frame's
    seed; grav1synth moves the seed on at every frame it applies a segment to.
    """
    if len(entries) != len(stamps):
        raise ValueError(f"{len(entries)} grain entries for a stream of {len(stamps)} frames")
    if any(later <= earlier for earlier, later in itertools.pairwise(stamps)):
        raise ValueError("the stream's frame timestamps are not increasing")
    out = [GRAIN_TABLE_HEADER]
    start = 0
    for index in range(1, len(entries) + 1):
        if index < len(entries) and entries[index].same_grain(entries[start]):
            continue
        entry = entries[start]
        begin = 0 if start == 0 else stamps[start]
        end = WHOLE_VIDEO_END if index == len(entries) else stamps[index] - 1
        out.append(f"E {begin} {end} {int(entry.apply)} {entry.seed} {int(entry.apply)}")
        out += [f"\t{line}" for line in entry.body]
        start = index
    return "\n".join(out) + "\n"


PLANES = ("y", "u", "v")


def window_grain(
    path: Path, chain: Toolchain, times: list[float], seconds: float, work: Path,
    *, synthesis: bool = True, prefilter: str = "",
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
            *decode_args(chain, path, grain=synthesis),
            "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path),
            "-an", "-sn", "-dn",
            "-filter_complex",
            f"{grain_graph(prefilter)}psnr=stats_file={stats.as_posix()}",
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
    means factor^2 * (with_grain - underneath) = source - underneath.

    0.0 when the encode already has all the source's fine texture: there is no
    grain to put back, which is the right answer for a clean source. None only
    when nothing was synthesised, so there is nothing to scale.
    """
    synthesised = with_grain - underneath
    if synthesised <= 0:
        return None
    wanted = source - underneath
    if wanted <= 0:
        return 0.0
    return (wanted / synthesised) ** 0.5


# Calibration never more than doubles a table's strength: past that, the table is
# wrong in more than strength, and scaling it hides that.
MAX_FACTOR = 2.0

# Below this luma factor the synthesised grain would be a faint fraction of the
# table's -- too little to see, so in "auto" the film ships without any.
NEGLIGIBLE = 0.15


def decide_strength(
    measured: tuple[float | None, float | None, float | None], mode: str,
) -> tuple[bool, float, float, float]:
    """(keep grain?, luma, Cb, Cr factors) from calibration's measurements.

    This is also the grain *detector*. The old one compared the source with an
    hqdn3d-denoised copy and called the grainy test film clean (46 dB against a
    44 dB threshold), so the grain route never ran on it. Calibration measures
    exactly what matters instead -- how much fine texture the source has that the
    encode lost -- and a clean source simply comes out near zero. "always" keeps
    grain regardless, at no less than NEGLIGIBLE.

    A plane that could not be measured keeps the table's strength (1.0).
    """
    luma, cb, cr = (1.0 if f is None else f for f in measured)
    if mode == "auto" and measured[0] is not None and luma < NEGLIGIBLE:
        return False, luma, cb, cr
    luma = max(NEGLIGIBLE, min(MAX_FACTOR, luma))
    return True, luma, min(MAX_FACTOR, cb), min(MAX_FACTOR, cr)


def needs_probe(cfg: Config) -> bool:
    """Whether the hqdn3d grain probe still decides anything.

    With SVT estimating and nothing denoised before the encode, calibration is
    the detector (see decide_strength). The probe's denoiser choice still matters
    to the grav1synth estimator and to a pre-encode denoise.
    """
    return cfg.grain.estimator == "grav1synth" or cfg.grain.denoise_before_encode


def calibrate_strength(
    source: Path, grainy: Path, chain: Toolchain, cfg: Config, work: Path, duration: float,
    source_filter: str = "",
) -> tuple[float | None, float | None, float | None]:
    """Per-plane factors that make `grainy`'s synthesised grain match `source`'s.

    Chroma is calibrated on its own: grav1synth's colour grain was far stronger,
    relative to its luma grain, than the source's, and showed as coloured speckle.
    `source_filter` is the deinterlacing the encode had: combing is pixel-scale
    texture too, and would be asked of the grain.
    """
    times = sample_times(duration, cfg.grain.samples, cfg.grain.sample_seconds)
    seconds = float(cfg.grain.sample_seconds)
    try:
        wanted = window_grain(source, chain, times, seconds, work, prefilter=source_filter)
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
