"""Build and run the Av1an command line.

Av1an owns the hard parts: scene detection, chunking, parallel workers, resume and
target-quality search. This module's only job is to translate config plus probed
source properties into correct flags for Av1an and SvtAv1EncApp.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import log, run, run_watched

# ITU-T H.273 code points, which is what AV1 signals and what SvtAv1EncApp expects.
# ffprobe reports the names on the left.
COLOR_PRIMARIES = {
    "bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12,
}
COLOR_TRANSFER = {
    "bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "linear": 8, "log100": 9, "log316": 10, "iec61966-2-4": 11, "bt1361e": 12,
    "iec61966-2-1": 13, "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16,
    "smpte428": 17, "arib-std-b67": 18,
}
COLOR_MATRIX = {
    "rgb": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "ycgco": 8, "bt2020nc": 9, "bt2020_ncl": 9, "bt2020c": 10, "bt2020_cl": 10,
    "smpte2085": 11, "chroma-derived-nc": 12, "chroma-derived-c": 13, "ictcp": 14,
}

Number = TypeVar("Number", int, float)


def dovi_tonemap_filter(info: MediaInfo, cfg: Config) -> str:
    """The tonemapx filter for a DV source whose base layer is not viewable.

    Only profile 5 (and profile 10 signalling no compatibility) needs this. It is
    the same filter Jellyfin uses on machines without a usable GPU: tonemapx reads
    the RPU itself (apply_dovi is on by default) and reshapes the picture. The
    catch is that tonemapx only writes BT.709 -- there is no CPU path from
    profile 5 to HDR10 -- so this trades HDR away to make the file playable at
    all, which is why it is opt-in.
    """
    if info.base_layer != "none" or cfg.dolby_vision.profile5 != "tonemap-sdr":
        return ""
    dv = cfg.dolby_vision
    return (
        f"tonemapx=tonemap={dv.tonemap_algorithm}"
        f":desat={dv.tonemap_desat}"
        f":peak={dv.tonemap_peak}"
        ":t=bt709:m=bt709:p=bt709"
        ":apply_dovi=1"
        ":format=yuv420p10"
    )


def effective_color(info: MediaInfo, cfg: Config) -> tuple[str, str, str]:
    """(primaries, transfer, matrix) of the frames the encoder will actually see.

    Normally the source's own tags, but a tonemapped profile 5 arrives as BT.709
    and must be signalled that way or players will stretch it back into HDR.
    """
    if dovi_tonemap_filter(info, cfg):
        return "bt709", "bt709", "bt709"
    return info.color_primaries, info.color_transfer, info.color_space


def effective_crf(info: MediaInfo, cfg: Config) -> int:
    """`encode.crf` shifted for the source's resolution.

    A CRF is a quality target per coded pixel, and a pixel at 576p is magnified
    several times more on the same screen than one at 2160p, so holding CRF
    constant across a library means the small files look the worst. The ladder in
    `encode.crf_by_width` corrects for that; `encode.crf` remains the one knob
    that moves everything, and the 4K band is what it means literally.

    Bands are keyed by width because a scope film arrives cropped to its picture:
    3836x2072 is a 4K source whose height would file it as 1080p. `band_width`
    has the details, including 4:3 and non-square-pixel HD.
    """
    ladder = cfg.encode.crf_by_width
    if not ladder:
        return cfg.encode.crf
    return max(1, min(70, cfg.encode.crf + by_width(ladder, info)))


def by_width(value: Number | dict[str, Number], info: MediaInfo) -> Number:
    """A setting's value for this source: itself, or its entry in a width table.

    The table's keys are the smallest width of each band; a source takes the
    widest band its `band_width` reaches, and anything narrower the narrowest.
    """
    if not isinstance(value, dict):
        return value
    width = band_width(info)
    bands = sorted((int(key) for key in value), reverse=True)
    band = next((key for key in bands if width >= key), bands[-1])
    return value[str(band)]


def band_width(info: MediaInfo) -> int:
    """The width a source is filed under: the larger of how wide it is shown and
    how wide a 16:9 picture of its height would be.

    Width alone keeps a cropped scope film in its real band -- 1920x800 is
    1080p -- but drops a narrow one a band: 4:3 HD stored as 1440x1080 would be
    filed as 720p, and 960x720 as SD. The height catches those. Displayed width,
    not stored, because HDV's 1440x1080 is shown 1920 wide on non-square pixels.
    """
    shown = info.width
    if info.sample_aspect:
        num, den = (int(part) for part in info.sample_aspect.split(":"))
        shown = round(info.width * num / den)
    return max(shown, round(info.height * 16 / 9))


def variance_boost_curve(info: MediaInfo, cfg: Config) -> str:
    """The curve SVT-AV1 should judge flatness by: its PQ curve for HDR10 output.

    PQ packs the shadows into few code values, so the default curve under-boosts
    the dark flat areas of an HDR film. Decided on the output, not the source, so
    a profile 5 tonemapped to SDR gets the SDR curve.
    """
    curve = str(cfg.encode.variance_boost_curve)
    if curve != "auto":
        return curve
    return "2" if effective_color(info, cfg)[1] == "smpte2084" else "0"


def svt_params(
    info: MediaInfo, cfg: Config, chain: Toolchain, crf: int | None = None
) -> list[str]:
    """Encoder flags passed straight to SvtAv1EncApp (not FFmpeg syntax)."""
    enc = cfg.encode
    params: list[str] = [
        "--preset", str(enc.preset),
        "--crf", str(crf if crf is not None else effective_crf(info, cfg)),
        "--tune", str(enc.tune),
    ]

    # Variance boost spends extra bits on low-variance blocks: skies, walls, dark
    # gradients, exactly where blocking shows. It is OFF in SVT-AV1 unless asked
    # for, and the strength alone does not turn it on -- this project passed
    # --variance-boost-strength for months without --enable-variance-boost, so the
    # encoder logged "Variance Boost : 0" and the flat areas blocked up.
    if (strength := by_width(enc.variance_boost_strength, info)) > 0:
        if "--enable-variance-boost" in chain.svt_options:
            params += ["--enable-variance-boost", "1"]
        params += ["--variance-boost-strength", str(strength)]
        if "--variance-boost-curve" in chain.svt_options:
            params += ["--variance-boost-curve", variance_boost_curve(info, cfg)]

    # Dark frames get a lower QP. Skipped on an encoder without it rather than
    # failing the run; `doctor` says whether it is there.
    bias = by_width(enc.luminance_qp_bias, info)
    if bias > 0 and "--luminance-qp-bias" in chain.svt_options:
        params += ["--luminance-qp-bias", str(bias)]

    # Biases rate-distortion toward high-frequency error, so texture and grain
    # survive rather than flattening into blotches. With a weaker temporal filter,
    # which would otherwise average the grain of neighbouring frames into lumps.
    # `tools.require` refuses an encoder without them when they are configured.
    if (ac_bias := by_width(enc.ac_bias, info)) > 0 and "--ac-bias" in chain.svt_options:
        params += ["--ac-bias", str(ac_bias)]
    if (tf := by_width(enc.tf_strength, info)) >= 0 and "--tf-strength" in chain.svt_options:
        params += ["--tf-strength", str(tf)]

    if info.fps > 0 and enc.keyint_sec > 0:
        params += ["--keyint", str(round(info.fps * enc.keyint_sec))]

    # Preserve colour signalling. Getting this wrong is how HDR turns grey.
    src_primaries, src_transfer, src_matrix = effective_color(info, cfg)
    if (primaries := COLOR_PRIMARIES.get(src_primaries)) is not None:
        params += ["--color-primaries", str(primaries)]
    if (transfer := COLOR_TRANSFER.get(src_transfer)) is not None:
        params += ["--transfer-characteristics", str(transfer)]
    if (matrix := COLOR_MATRIX.get(src_matrix)) is not None:
        params += ["--matrix-coefficients", str(matrix)]
    if info.color_range in {"tv", "limited"}:
        params += ["--color-range", "0"]
    elif info.color_range in {"pc", "full"}:
        params += ["--color-range", "1"]

    # HDR10 static metadata. Without it a player has no idea what display the
    # grade was made for, and tone-maps against a guess. Only meaningful while the
    # output is still PQ, so a tonemapped profile 5 drops it along with the HDR.
    if src_transfer == "smpte2084":
        if info.mastering_display and "--mastering-display" in chain.svt_options:
            params += ["--mastering-display", info.mastering_display.svt_argument()]
        if info.content_light and "--content-light" in chain.svt_options:
            params += ["--content-light", info.content_light.svt_argument()]

    params += enc.extra_svt_params
    return params


def pix_format(info: MediaInfo, cfg: Config) -> str:
    """Pick the encoding pixel format.

    On "auto" we always choose 10-bit, including for 8-bit sources: AV1 10-bit costs
    very little at these presets and removes the banding that 8-bit encoding
    introduces in gradients. `info` is accepted so this stays the one place to add
    source-dependent rules (4:2:2, 12-bit) later.
    """
    if cfg.encode.pix_format != "auto":
        return cfg.encode.pix_format
    return "yuv420p10le"


def build_command(
    info: MediaInfo,
    output: Path,
    chain: Toolchain,
    cfg: Config,
    temp_dir: Path,
    denoise_filter: str = "",
) -> list[str]:
    """Assemble the full Av1an invocation for one file.

    Tolerates a missing Av1an so that `plan` still shows the command it would run;
    `tools.require` is what actually gates a real encode.
    """
    enc = cfg.encode

    argv: list[str] = [
        chain.av1an or "av1an",
        "-i", str(info.path),
        "-o", str(output),
        "-e", "svt-av1",
        "-v", " ".join(svt_params(info, cfg, chain)),
        "--pix-format", pix_format(info, cfg),
        "-m", enc.chunk_method,
        "-c", enc.concat,
        "--sc-method", enc.sc_method,
        "--extra-split-sec", str(enc.extra_split_sec),
        "--min-scene-len", str(enc.min_scene_len),
        "--temp", str(temp_dir),
        # Audio and subtitles are muxed in later from the source, so that grain
        # application operates on a video-only stream.
        "-a", "-an",
        "--resume",
        "-y",
    ]

    if cfg.machine.workers > 0:
        argv += ["-w", str(cfg.machine.workers)]

    # Filters run inside Av1an's chunk pipeline, so no full-size intermediate file
    # is ever written. Tonemapping comes first: everything after it works in the
    # output colour space.
    filters = [f for f in (dovi_tonemap_filter(info, cfg), denoise_filter) if f]
    if filters:
        argv += ["-f", f"-vf {','.join(filters)}"]

    tq = cfg.target_quality
    if tq.enabled:
        argv += [
            "--target-quality", str(tq.score),
            "--target-metric", tq.metric,
            "--probes", str(tq.probes),
            "--probing-speed", tq.probing_speed,
            "--min-q", str(tq.min_q),
            "--max-q", str(tq.max_q),
        ]

    return argv


def run_encode(
    argv: list[str], cfg: Config, chain: Toolchain, dry_run: bool = False,
    check: Callable[[], str | None] | None = None,
) -> str | None:
    """Run Av1an with its output streamed to the terminal rather than captured.

    These encodes run for hours; swallowing the progress bar would be unkind.
    `check` is asked every minute whether to stop; the reason it gave is returned.
    """
    if cfg.machine.nice > 0:
        argv = ["nice", "-n", str(cfg.machine.nice), *argv]
    log.info("encoding (this is the long part)")
    if dry_run:
        run(argv, dry_run=True)
        return None
    shim_dir = str(write_ffmpeg_shim(cfg.work_dir / "bin", chain.ffmpeg))
    return run_watched(argv, check or (lambda: None), extra_path=[shim_dir, *chain.bin_dirs])


# Av1an 0.5.2 (and master as of 2026-09) cuts each chunk out of a split segment
# with `ffmpeg -vf select=between(n,a,b) -f yuv4mpegpipe -` and no -fps_mode, so
# ffmpeg converts the selection to constant frame rate. On a 23.976 fps Matroska
# source with millisecond timestamps that conversion duplicates a frame: the
# encoder receives 238 frames for a 237-frame chunk, and Av1an rejects the chunk
# with "FRAME MISMATCH" -- three times, then gives up on the whole file.
# Passthrough hands over exactly the decoded frames, which is how Av1an counts.
FFMPEG_SHIM = """\
#!/bin/sh
# Written by autotranscode; see FFMPEG_SHIM in autotranscode/encode.py.
real={real}
case " $* " in
  *" yuv4mpegpipe "*) ;;
  *) exec "$real" "$@" ;;
esac
# Insert "-fps_mode passthrough" just before the output, the last argument.
last=$#
i=0
for arg do
  i=$((i + 1))
  [ "$i" -eq 1 ] && set --
  [ "$i" -eq "$last" ] && set -- "$@" -fps_mode passthrough
  set -- "$@" "$arg"
done
exec "$real" "$@"
"""


def write_ffmpeg_shim(directory: Path, real_ffmpeg: str) -> Path:
    """Write an `ffmpeg` wrapper into `directory` and return the directory.

    Put first on Av1an's PATH, it stands in for ffmpeg in every y4m pipe Av1an
    opens and execs the real binary for everything else.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "ffmpeg"
    part = directory / "ffmpeg.part"
    part.write_text(FFMPEG_SHIM.format(real=shlex.quote(str(Path(real_ffmpeg).resolve()))))
    part.chmod(0o755)
    os.replace(part, shim)
    return directory
