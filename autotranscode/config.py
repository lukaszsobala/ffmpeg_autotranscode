"""Configuration: TOML file -> validated dataclasses, with machine profiles.

Every knob has a default here, so a missing or partial config file still works.
Machine profiles only change *defaults*; anything set explicitly in the file wins.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# Profile-specific default overrides. Chosen for what the hardware can actually do:
# RK3588 has no AV1 encoder at all (decode only), so SVT-AV1 runs on 4xA76 + 4xA55.
PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    "rk3588": {
        # One worker, not two. The limit is memory, not cores: two SVT-AV1
        # instances on a 4K 10-bit source push a 16 GB board into swap. SVT-AV1
        # already spreads one 4K encode across the cores, so a second worker
        # buys nothing: one managed 3.6-4.3 fps, two 1.4-1.9 fps each.
        "machine": {"workers": 1},
        # Measured on a ROCK 5B, 1080p, 150 frames, these settings: preset 8 = 30.6 fps,
        # 6 = 13.7, 4 = 6.6, 3 = 4.4, 2 = 2.6. Bitstream against preset 8: -17.3% at 6,
        # -29.0% at 4, -30.4% at 3, -34.9% at 2. Preset 4 takes 83% of the total
        # available gain at 2.6x less time than preset 2, and preset 3 is dominated --
        # it costs 1.5x preset 4 for 1.9%, because the only tools it adds are the
        # self-guided restoration filter and the full PD0 path. Preset 2 is the next
        # real step (4x4 blocks and compound inter prediction), at ~22h per 2h film.
        # crf 30 is the 4K anchor: with variance boost on, it was the highest that
        # kept a grainy 4K HDR film's fog and flat paper free of blocks (34 and 38
        # brought them back). The resolution curve lowers it for smaller sources.
        "encode": {"preset": 4, "crf": 30, "chunk_method": "auto"},
    },
    "generic": {},
    "workstation": {
        "machine": {"workers": 0},
        "encode": {"preset": 4, "crf": 28, "chunk_method": "auto"},
    },
}


CHUNK_METHODS = {"auto", "hybrid", "lsmash", "ffms2", "bestsource", "select", "segment"}

# Hardware decoders for the passes autotranscode runs itself (scene cuts, the
# grain estimate and calibration, verify), in the order they are tried: Rockchip's
# video engine, NVIDIA's NVDEC, Intel's Quick Sync, VA-API (AMD, and Intel again:
# it copies frames back more slowly than QSV does), Apple's VideoToolbox. Av1an
# and the encoder decode in software whatever this says.
HARDWARE_DECODERS = ("rkmpp", "cuda", "qsv", "vaapi", "videotoolbox")


@dataclass(slots=True)
class Paths:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    av1an: str = "av1an"
    svtav1encapp: str = "SvtAv1EncApp"
    grav1synth: str = "grav1synth"
    # SVT-AV1-Essential's grain estimator, for grain.estimator = "svtfg". Found
    # beside SvtAv1EncApp or in ~/.local/bin when not on PATH.
    svtfg: str = "SvtFgEssential"
    # av-scenechange's command-line tool, which `regrain` finds scene cuts with.
    # Found beside Av1an or in ~/.cargo/bin when not on PATH.
    scenechange: str = "av-scenechange"
    mkvmerge: str = "mkvmerge"
    work_dir: str = "~/.cache/autotranscode"
    state_file: str = "~/.cache/autotranscode/state.json"


@dataclass(slots=True)
class Machine:
    # "auto" picks rk3588 on an RK3588 board and generic anywhere else.
    profile: str = "auto"
    workers: int = 0  # 0 => by cores and available memory; see encode.workers
    nice: int = 10
    # "auto" tries each of HARDWARE_DECODERS the machine has, and uses one for a
    # film only once it gave software's frames exactly on a stretch of it; "off"
    # decodes in software; or one of HARDWARE_DECODERS, tried alone.
    hardware_decode: str = "auto"


@dataclass(slots=True)
class Encode:
    # Every setting below was judged at preset 4; see the rk3588 profile for how
    # the presets compare.
    preset: int = 4
    # The CRF for a 4K source, and the most any source gets. Smaller sources get
    # less along the curve below, which is relative to this, so this one number
    # still moves the whole library's quality.
    crf: int = 30
    # CRF is not resolution-neutral: a coded pixel at 576p covers far more of the
    # screen than one at 2160p, so the same CRF looks progressively worse the
    # smaller the source. The curve: crf_drop_at_720 below `crf` at 720 wide,
    # rising crf_per_doubling with each doubling of width, never above `crf` and
    # never more than crf_max_drop below it, in the encoder's 0.25 steps. At the
    # defaults that is
    #
    #   crf = clamp(24 + 3.5 * log2(width / 720), 23, 30)
    #
    # SD 24, 720p 27, 1080p 29, and 30 from about 2360 wide up. Those are the
    # points found by eye: 4K blocked in fog above 30; 1080p was blocky in a dark
    # scene at 32 and held at 30, with 29 as margin; 720p at 27 and SD at 24
    # looked like their sources (higher was not tried). The three lower points lie
    # on a log line; 4K sits below it, which is what the cap is for. 1080p once
    # sat at 37 on the strength of the dark bias below, which held a dark scene
    # together but not detail in motion: rain vanished and faces smeared on a
    # second film. See ac_bias for what fixed it. Both at 0 is one CRF for all.
    #
    # Width, not height: a scope film is cropped to its picture, so a 4K print is
    # 3840x1600 or even 3836x2072, and height would place it at 1080p and
    # under-encode it. Cropping takes the bars off the top and bottom, so width is
    # what stays put. A picture narrower than 16:9 goes by its height instead;
    # see encode.band_width.
    crf_per_doubling: float = 3.5
    crf_drop_at_720: float = 6.0
    crf_max_drop: float = 7.0
    pix_format: str = "yuv420p10le"
    tune: int = 0  # 0=VQ (subjective), 2=SSIM
    # 0 = off, 1-4 = on at that strength. Either one number, or a table keyed by
    # width, taking the value of the band a source falls in: "3000" is 4K, "1700"
    # 1080p, "1100" 720p, "0" everything below.
    #
    # On at 4K only. The only setting that removed the flat 64px blocks in fog and
    # on paper on the grainy 4K test film: CRF 18 without it was as blocky as
    # CRF 22, and synthesised grain is too faint to hide steps of 1-2 code values.
    # Strength 2 at CRF 30 cost 1.4x the bits of plain CRF 22 (85% saving instead
    # of 89%) and was judged clean by eye. Below 4K it lost at every resolution
    # tested: the same size bought a lower score, and a grainy 1080p film came out
    # 1.5x the size of its source.
    variance_boost_strength: int | dict[str, int] = field(
        default_factory=lambda: {"3000": 2, "0": 0}
    )
    # Which variance boost curve: 0 default, 1 low-medium contrast, 2 tuned for
    # PQ. "auto" takes 2 for PQ (HDR10) output and 0 otherwise.
    variance_boost_curve: str | int = "auto"   # auto, or 0, 1 or 2 written either way
    # 0-100: lower the QP of dark frames, in proportion to how dark they are.
    # One number or a width table, as above. It works per frame, not per block,
    # so a dim film pays for it on most of its frames.
    #
    # On at 1080p only. A grainy 8-bit 1080p film smeared its shadows into blocks
    # in a dark red scene at CRF 32, and neither more bits (CRF 30), variance boost
    # nor chroma offsets fixed it; 100 did. With ac-bias and the weak temporal
    # filter below it still takes the full 100 at CRF 30: that scene was judged
    # blocky with 50 at CRF 30, and with 100 at CRF 32.
    luminance_qp_bias: int | dict[str, int] = field(
        default_factory=lambda: {"3000": 0, "1700": 100, "1100": 0}
    )
    # 0-8: bias rate-distortion toward keeping high-frequency detail. One number
    # or a width table, as above. Temporal filter strength: SVT-AV1's 0-4, or -1
    # to leave it at the encoder's own default (3 in SVT-AV1 4.2 and 4.3).
    #
    # Both on at 4K and 1080p, together. Variance boost left a grainy 4K film's fog
    # blotched: the grain smeared into patches tens of pixels across, some of them
    # tinted. Temporal filtering averages the grain of neighbouring frames into
    # exactly those lumps, and ac-bias keeps the encoder from flattening what is
    # left. Filter strength set the worst fog frame (low-pass 54.6 dB at the
    # default, 55.6 at 1, 55.9 off), but off looked worse in motion than 1, and
    # neither CRF 26 nor stronger variance boost came close. 1.17x the bits of
    # the whole film without them (82.5% saving instead of 85%); better in 148 of
    # its 159 three-second windows and never worse by more than 0.2 dB.
    #
    # At 1080p the default filter wiped out rain and the encoder smeared a face
    # at CRF 37; strength 1 and ac-bias 1 brought the rain back, CRF 30 the face.
    # With the dark bias, the worst stretches of a grainy 1080p film came out
    # 2.5x the bits of CRF 37, gaining 3.7 dB of low-pass 5th percentile and
    # keeping 90% of the grain instead of 72%.
    ac_bias: float | dict[str, float] = field(
        default_factory=lambda: {"1700": 1.0, "0": 0.0}
    )
    tf_strength: int | dict[str, int] = field(
        default_factory=lambda: {"1700": 1, "0": -1}
    )
    keyint_sec: float = 10.0
    chunk_method: str = "auto"  # lsmash when its VapourSynth plugin loads, else hybrid
    concat: str = "mkvmerge"
    sc_method: str = "standard"
    # Scene detection on frames no taller than this; 0 for full size. On a 4K
    # excerpt 1080 found the same 43 cuts 20% sooner. Lower saves little at 4K,
    # where decoding is the floor, and added cuts to a 1080p excerpt.
    sc_downscale_height: int = 1080
    extra_split_sec: int = 10
    min_scene_len: int = 24
    extra_svt_params: list[str] = field(default_factory=list)
    # off: encode the picture as it is. auto: deinterlace what ffprobe flags as
    # interlaced, in its field order. tff / bff: deinterlace every file, top or
    # bottom field first, for a run on files whose flags are wrong; see
    # encode.deinterlace_filter.
    deinterlace: str = "off"


@dataclass(slots=True)
class Estimate:
    """Sampling settings for `estimate`, which ranks a library by what it would save."""

    samples: int = 0        # 0 => scale with runtime, between min and max below
    min_samples: int = 3
    max_samples: int = 6
    sample_seconds: int = 10
    # With grain.denoise_before_encode on, probe grain first, so that a grainy film
    # is sampled denoised, as the real run would encode it. Without that denoise the
    # grain route changes nothing about size, and the probe does not run.
    grain_probe: bool = True


@dataclass(slots=True)
class Grain:
    mode: str = "auto"  # auto | always | never
    analysis: str = "sampled"  # sampled | full
    # Excerpts for the grain table and its calibration, spread over the film. The
    # same everywhere: they cost minutes against an encode of hours, and more of
    # them give the table more scenes to be typical of and calibration more of the
    # film to match.
    samples: int = 10
    sample_seconds: int = 3
    denoise_filter: str = "hqdn3d"  # hqdn3d | nlmeans | atadenoise
    denoise_strength: str = "auto"  # "auto" or an explicit filter argument string
    # Mean PSNR (dB) between source and its denoised self, measured on samples.
    # Lower => the denoiser removed more => more grain. Below this the old detector
    # takes the grain route; it only runs for the grav1synth estimator or a denoise
    # before the encode (see grain.needs_probe). Otherwise calibration decides.
    threshold_db: float = 44.0
    keep_table: bool = True
    # Denoise the picture before encoding when a grain table will put grain back.
    # Off, because measured it bought nothing and cost detail: SVT-AV1 discards
    # 4K grain by itself at any CRF from 22 to 32, so the denoised encode was no
    # smaller (101% of the plain one), while the denoiser's temporal smoothing cost
    # 1.3 dB of low-pass PSNR -- detail the encoder would otherwise have kept.
    denoise_before_encode: bool = False
    # Measure the applied grain against the source's and rescale the table to
    # match. grav1synth's tables came out about 40% too strong on the test film.
    # Cheap: the table is rewritten into the finished stream in seconds, with no
    # re-encode, and the measurement reads a few seconds of video.
    calibrate: bool = True
    # Who estimates the grain.
    # "svtfg": SVT-AV1-Essential's SvtFgEssential on the whole film, scene by
    # scene, one table entry per frame, then calibrated scene by scene. On To Live
    # and Die in L.A. its grain had the source's texture (lag-1 correlation +0.36
    # against the source's +0.33) and brightness profile; "svt"'s had neither.
    # "svt": SVT-AV1's own estimator on the grain samples alone, one table for the
    # whole film: its AR filter makes grain finer than film's (lag-1 -0.08).
    # "grav1synth": `grav1synth diff` against a denoised copy, whose tables drew a
    # visibly repeating, quilted texture whatever denoiser fed it.
    # Every table is applied after the encode, with grav1synth.
    estimator: str = "svtfg"  # svtfg | svt | grav1synth
    svt_level: int = 10  # --film-grain for the "svt" estimate, 1-50
    estimate_preset: int = 8
    # SvtFgEssential processes for "svtfg", each on a run of whole scenes: its
    # automatic analysis is single-threaded, and resets at every scene cut, so
    # the parts give the same table as one pass. 0: one per big core (all cores
    # on a CPU without big and little ones).
    svtfg_jobs: int = 0
    # Per-scene calibration ("svtfg"): about this many frames measured at 1080p,
    # fewer in proportion at higher resolutions, in windows of calibration_window
    # frames at the encode's keyframes, so each window decodes only itself. The
    # measure (a 3x3 median) runs at about 15 frames/s at 1080p on an RK3588, so
    # 4000 frames for each of the three streams take about 13 minutes.
    calibration_frames: int = 4000
    calibration_window: int = 4


@dataclass(slots=True)
class Staging:
    """Copy each source to local storage before reading it.

    An encode reads its source many times over many hours -- scene detection, every
    chunk, the grain samples, the final mux. Pointed at a NAS or a spinning disk
    that is constant activity, and the drive never gets to idle. Copying the file
    once and working from the copy turns that into a single sequential read.

    Off by default: it costs disk space equal to the largest source, and it is
    pointless when the library already lives on the same fast storage as work_dir.
    """

    enabled: bool = False
    directory: str = ""  # empty => <work_dir>/staging
    # Require this multiple of the source size to be free before copying, so
    # staging cannot fill the disk the encode also needs for its chunks.
    min_free_multiple: float = 1.5
    keep: bool = False  # leave the copy behind afterwards (debugging)


@dataclass(slots=True)
class DolbyVision:
    """What to do with Dolby Vision sources.

    The RPU cannot survive: AV1 carriage aside, Av1an's chunked pipeline drops it.
    What survives is the base layer, and for every profile except 5 that is a
    complete HDR10, HLG or SDR picture -- the same thing Jellyfin serves when it
    transcodes these titles on the fly. Encoding it is a real option; refusing the
    whole file was not.
    """

    mode: str = "base-layer"  # base-layer | skip
    # Profile 5 has no viewable base layer. tonemapx can reshape it using the RPU,
    # but only down to SDR BT.709, so it is opt-in rather than automatic.
    profile5: str = "skip"  # skip | tonemap-sdr
    tonemap_algorithm: str = "bt2390"
    tonemap_desat: float = 0.0
    tonemap_peak: float = 0.0


@dataclass(slots=True)
class Audio:
    mode: str = "smart"  # copy | opus | smart
    opus_kbps_per_channel: int = 64
    passthrough_codecs: list[str] = field(
        default_factory=lambda: ["opus", "aac", "ac3", "eac3", "flac", "mp3"]
    )
    max_channels: int = 0  # 0 => keep source layout


@dataclass(slots=True)
class Subtitles:
    mode: str = "copy"  # copy | none | text-only


@dataclass(slots=True)
class Output:
    suffix: str = ".av1"   # always Matroska: it holds every track the mux carries over
    directory: str = ""  # empty => write next to the source
    min_saving_percent: float = 10.0
    # And at least this much smaller in absolute terms, in MiB. A 10% saving on a
    # 600 MiB episode is 60 MiB: not worth a generation of loss or the hours.
    min_saving_mib: float = 100.0
    # Once this share of the frames is encoded, project the final size from the
    # chunks so far and stop when it misses either minimum. 0 never checks.
    check_saving_after_percent: float = 10.0
    replace_source: bool = False


@dataclass(slots=True)
class Library:
    extensions: list[str] = field(
        default_factory=lambda: [".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov", ".webm", ".wmv"]
    )
    skip_codecs: list[str] = field(default_factory=lambda: ["av1"])
    min_duration_sec: float = 60.0
    # Bits per pixel per second: bitrate / (width * height * fps). A source already
    # below this is close to what AV1 would spend anyway, so an encode would cost
    # hours to save little -- and `min_saving_percent` only notices that afterwards.
    # Deliberately low: it rejects the hopeless, not the merely unattractive.
    # 0 disables the check. For scale, a 4K HDR remux is ~0.15, a 1080p web
    # release ~0.08, and a good 4K AV1 encode ~0.01.
    min_bits_per_pixel: float = 0.03
    recursive: bool = True
    # A file whose encode was stopped and whose encoder settings have changed since:
    # "ask" on the terminal (without one, leave it for now), "restart" with the new
    # settings, "continue" with the ones it started with, or "skip" it for now.
    on_changed_settings: str = "ask"


@dataclass(slots=True)
class Tools:
    # "enforce": `run` and `sample` refuse a tool whose version toolchain.toml does
    # not accept, where it says to enforce it. "warn": log it and run anyway, for
    # trying a newer build on purpose.
    versions: str = "enforce"


@dataclass(slots=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    machine: Machine = field(default_factory=Machine)
    encode: Encode = field(default_factory=Encode)
    estimate: Estimate = field(default_factory=Estimate)
    grain: Grain = field(default_factory=Grain)
    staging: Staging = field(default_factory=Staging)
    dolby_vision: DolbyVision = field(default_factory=DolbyVision)
    audio: Audio = field(default_factory=Audio)
    subtitles: Subtitles = field(default_factory=Subtitles)
    output: Output = field(default_factory=Output)
    library: Library = field(default_factory=Library)
    tools: Tools = field(default_factory=Tools)

    @property
    def work_dir(self) -> Path:
        return Path(self.paths.work_dir).expanduser()

    @property
    def state_path(self) -> Path:
        return Path(self.paths.state_file).expanduser()

    @property
    def staging_dir(self) -> Path:
        directory = self.staging.directory
        return Path(directory).expanduser() if directory else self.work_dir / "staging"


class ConfigError(ValueError):
    pass


# What each annotation accepts. The module defers annotations, so a field's type
# is its source text: "int", "list[str]", "int | dict[str, int]".
_KINDS: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (int, float), "bool": (bool,),
}
_WANTED = {
    "str": "a string", "int": "a whole number", "float": "a number", "bool": "true or false",
    "list[str]": "a list of strings", "str | int": "a string or a whole number",
    "int | dict[str, int]": "a whole number or a table of them by width",
    "float | dict[str, float]": "a number or a table of them by width",
}


def _is_kind(value: Any, kind: str) -> bool:
    if kind.startswith("list[") and kind.endswith("]"):
        return isinstance(value, list) and all(_is_kind(item, kind[5:-1]) for item in value)
    if kind.startswith("dict["):
        return isinstance(value, dict)   # its entries are _check_by_width's to judge
    if kind != "bool" and isinstance(value, bool):
        return False   # TOML's true is not the number 1
    return isinstance(value, _KINDS[kind])


def _check_type(value: Any, annotation: str, name: str) -> None:
    """Refuse a value its option cannot use, naming both.

    Unchecked, a string preset ended in a TypeError traceback, and a string of
    extensions (".mkv") was iterated letter by letter: the library came up empty
    with only "no media files found".
    """
    if not any(_is_kind(value, kind.strip()) for kind in annotation.split("|")):
        wanted = _WANTED.get(annotation, annotation)
        raise ConfigError(f"{name} must be {wanted}; got {value!r}")


def _apply_section(obj: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown option '{key}' in [{where}]")
        # tomllib gives ints for whole numbers; widen to float where the field wants
        # one. The annotation is a string here, not the type: this module defers them.
        expected = str(known[key].type)
        _check_type(value, expected, f"{where}.{key}")
        if expected == "float" and isinstance(value, int):
            value = float(value)
        setattr(obj, key, value)


# Where the SoC is named: the kernel's own copy, then the host's copy as the
# Rockchip compose file mounts it. Docker hides /sys/firmware, which is where
# /proc/device-tree points, and will not mount anything under /proc.
DEVICE_TREE_COMPATIBLE = (
    Path("/proc/device-tree/compatible"),
    Path("/run/host/device-tree/compatible"),
)


def detect_profile() -> str:
    """The profile for this machine: rk3588 when the device tree names that SoC.

    Boards describe their SoC in the device tree ("radxa,rock-5b\0rockchip,rk3588");
    a PC or a Mac has no device tree at all and gets the generic profile.
    """
    for path in DEVICE_TREE_COMPATIBLE:
        try:
            compatible = path.read_bytes()
        except OSError:
            continue
        return "rk3588" if b"rockchip,rk3588" in compatible.split(b"\0") else "generic"
    return "generic"


def resolve_profile(profile: str) -> str:
    return detect_profile() if profile == "auto" else profile


def _merge_profile(cfg: Config, profile: str) -> None:
    overrides = PROFILES.get(profile)
    if overrides is None:
        raise ConfigError(f"unknown machine profile '{profile}'; known: {sorted(PROFILES)}")
    for section, values in overrides.items():
        _apply_section(getattr(cfg, section), values, section)


def default_path() -> Path | None:
    """Where the config lives when -c is not given, or None if there is none.

    Worth having because the failure mode without it is quiet: forgetting the flag
    does not error, it silently falls back to built-in defaults that name no tool
    paths, and the run then dies reporting the tools as missing.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    path = root / "autotranscode.toml"
    return path if path.is_file() else None


def load(path: Path | None, profile: str | None = None) -> Config:
    """Load config. Profile defaults are applied first, then the file overrides them.

    `profile` (the --profile flag) takes the place of the file's profile, not of
    its settings: applied after the file, it overwrote the file's preset and crf.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    cfg = Config()

    profile = resolve_profile(
        profile or raw.get("machine", {}).get("profile", cfg.machine.profile))
    _merge_profile(cfg, profile)
    cfg.machine.profile = profile

    for section, values in raw.items():
        if not hasattr(cfg, section):
            raise ConfigError(f"unknown config section [{section}]")
        target = getattr(cfg, section)
        if not is_dataclass(target):
            raise ConfigError(f"[{section}] is not a config section")
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        _apply_section(target, values, section)
    cfg.machine.profile = profile   # the file's "auto" is resolved, not kept

    validate(cfg)
    return cfg


def _check_by_width(
    value: Any, name: str, low: float, high: float, span: str, *, decimal: bool = False,
) -> None:
    """A setting that is one number, or a table of numbers keyed by width.

    Whole numbers only, unless `decimal`: then 1 and 1.0 are both accepted.
    """
    kinds = (int, float) if decimal else (int,)
    table = value if isinstance(value, dict) else {"0": value}
    if not table:
        raise ConfigError(f"{name} needs at least one band; use 0 to turn it off")
    for width, number in table.items():
        if not str(width).isdigit():
            raise ConfigError(f"{name} keys must be widths; got '{width}'")
        if isinstance(number, bool) or not isinstance(number, kinds) \
                or not low <= number <= high:
            raise ConfigError(f"{name} must be {span}; got {number!r}")


def validate(cfg: Config) -> None:
    if cfg.machine.hardware_decode not in {"auto", "off", *HARDWARE_DECODERS}:
        raise ConfigError("machine.hardware_decode must be auto, off or one of "
                          + ", ".join(HARDWARE_DECODERS))
    if not -1 <= cfg.encode.preset <= 13:
        raise ConfigError("encode.preset must be between -1 and 13")
    if not 1 <= cfg.encode.crf <= 70:
        raise ConfigError("encode.crf must be between 1 and 70 (SVT-AV1 range)")
    for name in ("crf_per_doubling", "crf_drop_at_720", "crf_max_drop"):
        if getattr(cfg.encode, name) < 0:
            raise ConfigError(f"encode.{name} must be 0 or more")
    if cfg.encode.crf - cfg.encode.crf_max_drop < 1:
        raise ConfigError(
            f"encode.crf_max_drop lets CRF fall below 1 "
            f"(encode.crf {cfg.encode.crf} - {cfg.encode.crf_max_drop:g})"
        )
    _check_by_width(cfg.encode.variance_boost_strength, "encode.variance_boost_strength",
                    0, 4, "0 (off) to 4")
    _check_by_width(cfg.encode.luminance_qp_bias, "encode.luminance_qp_bias",
                    0, 100, "0 (off) to 100")
    _check_by_width(cfg.encode.ac_bias, "encode.ac_bias", 0, 8, "0 (off) to 8", decimal=True)
    _check_by_width(cfg.encode.tf_strength, "encode.tf_strength",
                    -1, 4, "-1 (encoder default) or 0 to 4")
    if cfg.encode.deinterlace not in {"off", "auto", "tff", "bff"}:
        raise ConfigError("encode.deinterlace must be off, auto, tff or bff")
    if cfg.grain.estimator not in {"svtfg", "svt", "grav1synth"}:
        raise ConfigError("grain.estimator must be svtfg, svt or grav1synth")
    if cfg.grain.svtfg_jobs < 0:
        raise ConfigError("grain.svtfg_jobs cannot be negative (0 picks by cores)")
    if cfg.grain.calibration_frames < 100:
        raise ConfigError("grain.calibration_frames must be at least 100")
    if not 1 <= cfg.grain.calibration_window <= 48:
        raise ConfigError("grain.calibration_window must be between 1 and 48 frames")
    if not 1 <= cfg.grain.svt_level <= 50:
        raise ConfigError("grain.svt_level must be between 1 and 50")
    if str(cfg.encode.variance_boost_curve) not in {"auto", "0", "1", "2"}:
        raise ConfigError("encode.variance_boost_curve must be auto, 0, 1 or 2")
    if cfg.library.min_bits_per_pixel < 0:
        raise ConfigError("library.min_bits_per_pixel cannot be negative")
    if cfg.library.on_changed_settings not in {"ask", "restart", "continue", "skip"}:
        raise ConfigError("library.on_changed_settings must be ask, restart, continue or skip")
    if cfg.tools.versions not in {"enforce", "warn"}:
        raise ConfigError("tools.versions must be enforce or warn")
    if cfg.grain.mode not in {"auto", "always", "never"}:
        raise ConfigError("grain.mode must be auto, always or never")
    if cfg.grain.analysis not in {"sampled", "full"}:
        raise ConfigError("grain.analysis must be sampled or full")
    if cfg.staging.min_free_multiple < 1.0:
        raise ConfigError("staging.min_free_multiple must be at least 1.0")
    if cfg.dolby_vision.mode not in {"base-layer", "skip"}:
        raise ConfigError("dolby_vision.mode must be base-layer or skip")
    if cfg.dolby_vision.profile5 not in {"skip", "tonemap-sdr"}:
        raise ConfigError("dolby_vision.profile5 must be skip or tonemap-sdr")
    if cfg.audio.mode not in {"copy", "opus", "smart"}:
        raise ConfigError("audio.mode must be copy, opus or smart")
    if cfg.subtitles.mode not in {"copy", "none", "text-only"}:
        raise ConfigError("subtitles.mode must be copy, none or text-only")
    if cfg.encode.chunk_method not in CHUNK_METHODS:
        raise ConfigError(f"encode.chunk_method must be one of {', '.join(sorted(CHUNK_METHODS))}")
    if cfg.encode.sc_downscale_height < 0:
        raise ConfigError("encode.sc_downscale_height cannot be negative (0 turns it off)")
    for name, value, least in (
            ("machine.workers", cfg.machine.workers, 0),
            ("estimate.samples", cfg.estimate.samples, 0),
            ("estimate.min_samples", cfg.estimate.min_samples, 1),
            ("estimate.sample_seconds", cfg.estimate.sample_seconds, 1),
            ("grain.samples", cfg.grain.samples, 1),
            ("grain.sample_seconds", cfg.grain.sample_seconds, 1),
            ("audio.max_channels", cfg.audio.max_channels, 0),
            # 0 does not fail the mux, but the size projection counts the track as free.
            ("audio.opus_kbps_per_channel", cfg.audio.opus_kbps_per_channel, 1)):
        if value < least:
            raise ConfigError(f"{name} must be at least {least}; got {value}")
    if cfg.estimate.max_samples < cfg.estimate.min_samples:
        raise ConfigError("estimate.max_samples cannot be below estimate.min_samples")
    if not 0 <= cfg.output.check_saving_after_percent <= 100:
        raise ConfigError("output.check_saving_after_percent must be between 0 and 100")
