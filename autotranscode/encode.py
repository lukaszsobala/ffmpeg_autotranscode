"""Build and run the Av1an command line.

Av1an owns the hard parts: scene detection, chunking, parallel workers, resume and
target-quality search. This module's only job is to translate config plus probed
source properties into correct flags for Av1an and SvtAv1EncApp.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import log, run

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


def svt_params(info: MediaInfo, cfg: Config, crf: int | None = None) -> list[str]:
    """Encoder flags passed straight to SvtAv1EncApp (not FFmpeg syntax)."""
    enc = cfg.encode
    params: list[str] = [
        "--preset", str(enc.preset),
        "--crf", str(crf if crf is not None else enc.crf),
        "--tune", str(enc.tune),
        "--variance-boost-strength", str(enc.variance_boost_strength),
    ]

    if enc.ac_bias > 0:
        # Biases rate-distortion toward high-frequency error; helps texture and
        # any grain the denoiser left behind survive the encode.
        params += ["--ac-bias", str(enc.ac_bias)]

    if info.fps > 0 and enc.keyint_sec > 0:
        params += ["--keyint", str(round(info.fps * enc.keyint_sec))]

    # Preserve colour signalling. Getting this wrong is how HDR turns grey.
    if (primaries := COLOR_PRIMARIES.get(info.color_primaries)) is not None:
        params += ["--color-primaries", str(primaries)]
    if (transfer := COLOR_TRANSFER.get(info.color_transfer)) is not None:
        params += ["--transfer-characteristics", str(transfer)]
    if (matrix := COLOR_MATRIX.get(info.color_space)) is not None:
        params += ["--matrix-coefficients", str(matrix)]
    if info.color_range in {"tv", "limited"}:
        params += ["--color-range", "0"]
    elif info.color_range in {"pc", "full"}:
        params += ["--color-range", "1"]

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
    """Assemble the full Av1an invocation for one file."""
    assert chain.av1an, "av1an is required"
    enc = cfg.encode

    argv: list[str] = [
        chain.av1an,
        "-i", str(info.path),
        "-o", str(output),
        "-e", "svt-av1",
        "-v", " ".join(svt_params(info, cfg)),
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

    if denoise_filter:
        # Denoising happens inside Av1an's chunk pipeline, so no full-size
        # intermediate file is ever written.
        argv += ["-f", f"-vf {denoise_filter}"]

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


def run_encode(argv: list[str], cfg: Config, dry_run: bool = False) -> None:
    """Run Av1an with its output streamed to the terminal rather than captured.

    These encodes run for hours; swallowing the progress bar would be unkind.
    """
    if cfg.machine.nice > 0:
        argv = ["nice", "-n", str(cfg.machine.nice), *argv]
    log.info("encoding (this is the long part)")
    run(argv, capture=False, dry_run=dry_run)
