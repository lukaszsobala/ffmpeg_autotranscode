"""Build and run the Av1an command line.

Av1an owns the hard parts: scene detection, chunking, parallel workers and resume.
This module's only job is to translate config plus probed source properties into
correct flags for Av1an and SvtAv1EncApp.
"""

from __future__ import annotations

import math
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .config import Config
from .probe import MediaInfo
from .tools import VAPOURSYNTH_CHUNK_METHODS, Toolchain
from .util import available_memory, log, run, run_watched

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


# bwdif's parity for each interlaced field order: the field *shown* first, the
# second letter. "tb" is coded top first but shown bottom first.
SHOWN_FIRST = {"tt": "tff", "bt": "tff", "bb": "bff", "tb": "bff"}


def deinterlace_filter(info: MediaInfo, cfg: Config) -> str:
    """bwdif for a source to be deinterlaced (encode.deinterlace), else "".

    One frame out per frame in: chunks, keyframes, the grain windows and verify
    all count frames, so doubling them (send_field) would break every one.

    Field order and which frames to treat come from the probe and the setting,
    not from frame flags. The flags lie: capture.avi says progressive, and idet
    found 39,019 of its frames bottom field first, 25,128 top first and 33,600
    progressive. And they may not reach the filter at all through Av1an's
    VapourSynth pipe, where bwdif would then take every frame as progressive and
    top field first. Hence deint=all, and tff / bff to force the order.
    """
    mode = cfg.encode.deinterlace
    if mode == "off":
        return ""
    parity = SHOWN_FIRST.get(info.field_order) if mode == "auto" else mode
    if not parity:
        return ""
    return f"bwdif=mode=send_frame:parity={parity}:deint=all"


def effective_color(info: MediaInfo, cfg: Config) -> tuple[str, str, str]:
    """(primaries, transfer, matrix) of the frames the encoder will actually see.

    Normally the source's own tags, but a tonemapped profile 5 arrives as BT.709
    and must be signalled that way or players will stretch it back into HDR.
    """
    if dovi_tonemap_filter(info, cfg):
        return "bt709", "bt709", "bt709"
    return info.color_primaries, info.color_transfer, info.color_space


def effective_crf(info: MediaInfo, cfg: Config) -> float:
    """`encode.crf` lowered for the source's resolution, in 0.25 steps.

    A CRF is a quality target per coded pixel, and a pixel at 576p is magnified
    several times more on the same screen than one at 2160p, so holding CRF
    constant across a library means the small files look the worst. The curve
    corrects for that: `crf_drop_at_720` below `encode.crf` at 720 wide, rising
    `crf_per_doubling` per doubling of width, capped at `encode.crf` and floored
    `crf_max_drop` below it. `encode.crf` remains the one knob that moves
    everything, and 4K is what it means literally.

    Width is `band_width`'s: a scope film arrives cropped to its picture, and
    3836x2072 is a 4K source whose height would call it 1080p. It has the details,
    including 4:3 and non-square-pixel HD.
    """
    enc = cfg.encode
    width = band_width(info)
    if width <= 0:
        return float(enc.crf)
    drop = enc.crf_drop_at_720 - enc.crf_per_doubling * math.log2(width / 720)
    drop = min(enc.crf_max_drop, max(0.0, drop))
    return max(1.0, round((enc.crf - drop) * 4) / 4)


def crf_text(crf: float) -> str:
    """A CRF as SvtAv1EncApp and a reader want it: 29, 25.75."""
    return f"{crf:g}"


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
    info: MediaInfo, cfg: Config, chain: Toolchain, crf: float | None = None
) -> list[str]:
    """Encoder flags passed straight to SvtAv1EncApp (not FFmpeg syntax)."""
    enc = cfg.encode
    params: list[str] = [
        "--preset", str(enc.preset),
        "--crf", crf_text(crf if crf is not None else effective_crf(info, cfg)),
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


def chunk_method(cfg: Config, chain: Toolchain) -> str:
    """The configured chunk method, with "auto" resolved.

    L-SMASH when its plugin loads: it indexes a file in seconds and hands over
    exact frames. hybrid instead has Av1an find keyframes with `ffprobe
    -show_frames`, which decodes the whole film on one core first -- about ten
    hours for a 4K feature on an RK3588 -- and copies the video into split files.
    """
    method = cfg.encode.chunk_method
    if method == "auto":
        return "lsmash" if "lsmas" in chain.vapoursynth_plugins else "hybrid"
    return method


# Av1an's own estimate of a worker's memory, in GB per megapixel of picture:
# SVT-AV1's, plus the chunk method's (av1an-core 0.5.2 and 0.6.0, determine_workers).
SVT_GB_PER_MEGAPIXEL = 1.2
CHUNK_GB_PER_MEGAPIXEL = {"lsmash": 0.3, "ffms2": 0.3, "bestsource": 0.3}
OTHER_CHUNK_GB_PER_MEGAPIXEL = 0.1
# How many cores Av1an counts one SVT-AV1 worker as keeping busy.
CORES_PER_WORKER = 6


def workers(
    info: MediaInfo, cfg: Config, chain: Toolchain,
    *, memory: int | None = None, cores: int | None = None,
) -> int:
    """How many Av1an workers to run: `machine.workers`, or else as many as the
    cores keep busy and the memory free right now can hold.

    Av1an decides the same way but against total memory, as if the machine had
    nothing else to do. On a desktop that is far from true, and with no swap an
    extra 4K worker gets the encode killed rather than slowed. The memory is
    measured once, as the encode starts. `memory` and `cores` default to this
    machine's; with memory unknown, the cores alone decide.
    """
    if cfg.machine.workers > 0:
        return cfg.machine.workers
    if cores is None:
        try:
            cores = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cores = os.cpu_count() or 1
    count = max(1, cores // CORES_PER_WORKER)
    memory = available_memory() if memory is None else memory
    megapixels = info.width * info.height / 1e6
    if memory > 0 and megapixels > 0:
        per_mp = SVT_GB_PER_MEGAPIXEL + CHUNK_GB_PER_MEGAPIXEL.get(
            chunk_method(cfg, chain), OTHER_CHUNK_GB_PER_MEGAPIXEL)
        count = min(count, int(memory / 1e9 / (megapixels * per_mp)))
    return max(1, count)


def build_command(
    info: MediaInfo,
    output: Path,
    chain: Toolchain,
    cfg: Config,
    temp_dir: Path,
    denoise_filter: str = "",
    scenes: Path | None = None,
) -> list[str]:
    """Assemble the full Av1an invocation for one file.

    `scenes` is where Av1an keeps its scene list: read when it is there, written
    once scene detection is done. Outside `temp_dir`, so that it survives a stop
    before Av1an writes its chunk list; see `scenes`.

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
        "-m", (method := chunk_method(cfg, chain)),
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

    argv += ["-w", str(workers(info, cfg, chain))]
    if scenes is not None:
        argv += ["--scenes", str(scenes)]
    if method in VAPOURSYNTH_CHUNK_METHODS:
        # The index goes next to the source by default: into the library.
        argv += ["--cache-mode", "temp"]
    if enc.sc_downscale_height > 0:
        argv += ["--sc-downscale-height", str(enc.sc_downscale_height)]

    # Filters run inside Av1an's chunk pipeline, so no full-size intermediate file
    # is ever written. Deinterlacing comes first, as it needs the fields as they
    # were shot; then tonemapping, so that everything after it works in the
    # output colour space.
    filters = [f for f in (deinterlace_filter(info, cfg), dovi_tonemap_filter(info, cfg),
                           denoise_filter) if f]
    if filters:
        argv += ["-f", f"-vf {','.join(filters)}"]

    return argv


def scene_command(
    chain: Toolchain, cfg: Config, method: str, script: Path, scenes: Path, temp_dir: Path,
) -> list[str]:
    """Av1an's scene detection alone, over the frames `script` serves, into `scenes`.

    The settings that decide the scenes are build_command's, so a film detected in
    parts is cut as it would have been whole, part boundaries aside.
    """
    enc = cfg.encode
    argv: list[str] = [
        chain.av1an or "av1an",
        "-i", str(script),
        # Never written: --sc-only stops once the scenes are, and deletes the temp.
        "-o", str(temp_dir.with_name(temp_dir.name + ".mkv")),
        "-e", "svt-av1",
        "-m", method,
        "--sc-method", enc.sc_method,
        "--extra-split-sec", str(enc.extra_split_sec),
        "--min-scene-len", str(enc.min_scene_len),
        "--temp", str(temp_dir),
        "--scenes", str(scenes),
        "--sc-only",
        "-y",
    ]
    if enc.sc_downscale_height > 0:
        argv += ["--sc-downscale-height", str(enc.sc_downscale_height)]
    return argv


def run_encode(
    argv: list[str], cfg: Config, chain: Toolchain, dry_run: bool = False,
    check: Callable[[], str | None] | None = None, keyframes: Path | None = None,
) -> str | None:
    """Run Av1an with its output streamed to the terminal rather than captured.

    These encodes run for hours; swallowing the progress bar would be unkind.
    `check` is asked every minute whether to stop; the reason it gave is returned.
    `keyframes` is where the hybrid chunk method's keyframe scan is kept between
    runs (FFPROBE_SHIM).
    """
    if cfg.machine.nice > 0:
        argv = ["nice", "-n", str(cfg.machine.nice), *argv]
    log.info("encoding (this is the long part)")
    if dry_run:
        run(argv, dry_run=True)
        return None
    shim_dir = str(write_ffmpeg_shim(cfg.work_dir / "bin", chain.ffmpeg))
    write_ffprobe_shim(cfg.work_dir / "bin", chain.ffprobe)
    env = {KEYFRAMES_VARIABLE: str(keyframes)} if keyframes is not None else {}
    return run_watched(argv, check or (lambda: None), extra_path=[shim_dir, *chain.bin_dirs],
                       extra_env=env, redraw=True)


# More than this between the encode's length and the source video's is not
# Matroska's millisecond rounding: the encode has lost the source's timing.
DRIFT_SECONDS = 0.1


def drifted(expected: float, actual: float) -> bool:
    return expected > 0 and actual > 0 and abs(actual - expected) > DRIFT_SECONDS


def timestamps_v2(times: list[float]) -> str:
    """mkvmerge's timestamp file: one presentation time per frame, in ms."""
    return "# timestamp format v2\n" + "".join(f"{t * 1000:.3f}\n" for t in times)


_FRAME_TIMES: dict[tuple[str, int, int], list[float]] = {}


def frame_times(chain: Toolchain, path: Path) -> list[float]:
    """The video's presentation times in seconds, in display order.

    Remembered while the file is unchanged: listing them reads the whole file
    (two minutes for an 18 GB Blu-ray remux on a USB disk), and a film needs
    its source's times up to three times (encode timing, grain estimate,
    calibration)."""
    stat = path.stat()
    known = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if known in _FRAME_TIMES:
        return list(_FRAME_TIMES[known])
    out = run([chain.ffprobe, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)]).stdout
    lines = [line.strip().rstrip(",") for line in out.splitlines() if line.strip()]
    try:
        times = sorted(float(line) for line in lines)
    except ValueError:
        raise ValueError(f"{path.name} has video packets without timestamps") from None
    _FRAME_TIMES.clear()    # one film at a time; no need to keep the last one's
    _FRAME_TIMES[known] = times
    return list(times)


def _duration(chain: Toolchain, path: Path) -> float:
    out = run([chain.ffprobe, "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _frame_count(chain: Toolchain, path: Path) -> int:
    out = run([chain.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
               str(path)]).stdout.strip()
    return int(out) if out.isdigit() else 0


def keep_source_timing(info: MediaInfo, encoded: Path, chain: Toolchain, work: Path) -> None:
    """Give `encoded` the source's frame times, if the encode lost them.

    Av1an hands the encoder frames and nothing else, and the result plays them
    at one constant rate. A source with a variable frame rate -- a phone's,
    which drops frames in low light -- then runs short against its own audio:
    6,699 frames that span 124 s at an average 54 fps played in 111.6 s at 60,
    and the sound drifted 12 s ahead by the end. Every frame survives, so the
    source's timestamps fit the encode one for one, and mkvmerge writes them in.
    grav1synth and the mux keep them from there.

    Nothing is done for a constant frame rate: the lengths already agree.
    """
    expected = info.video_duration or info.duration
    actual = _duration(chain, encoded)
    if not drifted(expected, actual):
        return
    if not chain.mkvmerge:
        raise ValueError(f"the encode runs {actual:.1f}s against the source's {expected:.1f}s "
                         "and needs mkvmerge to put the source's timing back")
    times = frame_times(chain, info.path)
    frames = _frame_count(chain, encoded)
    if len(times) != frames:
        raise ValueError(f"the encode runs {actual:.1f}s against the source's {expected:.1f}s, "
                         f"and its {frames} frames cannot take the source's "
                         f"{len(times)} timestamps")
    stamps = work / "timestamps.txt"
    stamps.write_text(timestamps_v2(times), encoding="ascii")
    retimed = work / "retimed.mkv"
    run([chain.mkvmerge, "-q", "-o", str(retimed), "--timestamps", f"0:{stamps}", str(encoded)])
    os.replace(retimed, encoded)
    log.info("  variable frame rate: the encode ran %.1fs; given the source's frame times "
             "(%.1fs)", actual, expected)


# Av1an 0.5.2 and 0.6.0 cut each chunk out of a split segment
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


# The hybrid chunk method finds the source's keyframes with `ffprobe -show_frames`,
# which decodes the whole film on one core: about ten hours for a 4K feature on an
# RK3588. Av1an keeps nothing of it until the chunk list is written, after the
# scan and the split that follows, so a stop anywhere before that paid for the
# scan again on every rerun. This keeps its output in the file named by
# $AUTOTRANSCODE_KEYFRAMES and replays it. Written under another name and renamed
# only once the scan succeeds, so a scan cut short is never replayed.
KEYFRAMES_VARIABLE = "AUTOTRANSCODE_KEYFRAMES"
FFPROBE_SHIM = """\
#!/bin/sh
# Written by autotranscode; see FFPROBE_SHIM in autotranscode/encode.py.
real={real}
cache="$AUTOTRANSCODE_KEYFRAMES"
case " $* " in
  *" -show_frames "*" frame=key_frame "*) ;;
  *) exec "$real" "$@" ;;
esac
[ -n "$cache" ] || exec "$real" "$@"
if [ ! -s "$cache" ]; then
  if "$real" "$@" > "$cache.part"; then
    mv -f "$cache.part" "$cache"
  else
    status=$?
    rm -f "$cache.part"
    exit "$status"
  fi
fi
exec cat "$cache"
"""


def write_ffmpeg_shim(directory: Path, real_ffmpeg: str) -> Path:
    """Write an `ffmpeg` wrapper into `directory` and return the directory.

    Put first on Av1an's PATH, it stands in for ffmpeg in every y4m pipe Av1an
    opens and execs the real binary for everything else.
    """
    return _write_shim(directory, "ffmpeg", FFMPEG_SHIM, real_ffmpeg)


def write_ffprobe_shim(directory: Path, real_ffprobe: str) -> Path:
    """Write an `ffprobe` wrapper into `directory` and return the directory.

    Put first on Av1an's PATH, it keeps the keyframe scan (FFPROBE_SHIM) and
    execs the real binary for everything else.
    """
    return _write_shim(directory, "ffprobe", FFPROBE_SHIM, real_ffprobe)


def _write_shim(directory: Path, name: str, text: str, real: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / name
    part = directory / f"{name}.part"
    part.write_text(text.format(real=shlex.quote(str(Path(real).resolve()))))
    part.chmod(0o755)
    os.replace(part, shim)
    return directory
