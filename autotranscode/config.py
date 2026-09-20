"""Configuration: TOML file -> validated dataclasses, with machine profiles.

Every knob has a default here, so a missing or partial config file still works.
Machine profiles only change *defaults*; anything set explicitly in the file wins.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# Profile-specific default overrides. Chosen for what the hardware can actually do:
# RK3588 has no AV1 encoder at all (decode only), so SVT-AV1 runs on 4xA76 + 4xA55.
PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    "rk3588": {
        "machine": {"workers": 2, "hwaccel_decode": "rkmpp"},
        # preset 8 is the realistic floor on A76 cores; 6 roughly halves throughput again.
        "encode": {"preset": 8, "crf": 32, "chunk_method": "hybrid"},
        "grain": {"samples": 4, "sample_seconds": 2},
        "target_quality": {"enabled": False},
    },
    "generic": {},
    "workstation": {
        "machine": {"workers": 0, "hwaccel_decode": "auto"},
        "encode": {"preset": 4, "crf": 28, "chunk_method": "hybrid"},
        "grain": {"samples": 6, "sample_seconds": 3},
    },
}


@dataclass(slots=True)
class Paths:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    av1an: str = "av1an"
    grav1synth: str = "grav1synth"
    mkvmerge: str = "mkvmerge"
    work_dir: str = "~/.cache/autotranscode"
    state_file: str = "~/.cache/autotranscode/state.json"


@dataclass(slots=True)
class Machine:
    profile: str = "generic"
    workers: int = 0  # 0 => let Av1an decide
    hwaccel_decode: str = "auto"  # auto | rkmpp | vaapi | qsv | none
    nice: int = 10


@dataclass(slots=True)
class Encode:
    preset: int = 6
    crf: int = 30
    pix_format: str = "yuv420p10le"
    tune: int = 0  # 0=VQ (subjective), 2=SSIM
    variance_boost_strength: int = 2
    ac_bias: float = 0.0
    keyint_sec: float = 10.0
    chunk_method: str = "hybrid"  # hybrid works without VapourSynth
    concat: str = "mkvmerge"
    sc_method: str = "standard"
    extra_split_sec: int = 10
    min_scene_len: int = 24
    extra_svt_params: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TargetQuality:
    enabled: bool = False
    metric: str = "vmaf"
    score: float = 94.0
    min_q: int = 18
    max_q: int = 48
    probes: int = 4
    probing_speed: str = "veryfast"


@dataclass(slots=True)
class Grain:
    mode: str = "auto"  # auto | always | never
    analysis: str = "sampled"  # sampled | full
    samples: int = 6
    sample_seconds: int = 3
    denoise_filter: str = "hqdn3d"  # hqdn3d | nlmeans | atadenoise
    denoise_strength: str = "auto"  # "auto" or an explicit filter argument string
    # Mean PSNR (dB) between source and its denoised self, measured on samples.
    # Lower => the denoiser removed more => more grain. Below this we take the grain route.
    threshold_db: float = 44.0
    chroma: bool = False
    keep_table: bool = True


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
    suffix: str = ".av1"
    container: str = "mkv"
    directory: str = ""  # empty => write next to the source
    min_saving_percent: float = 10.0
    replace_source: bool = False


@dataclass(slots=True)
class Library:
    extensions: list[str] = field(
        default_factory=lambda: [".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov", ".webm", ".wmv"]
    )
    skip_codecs: list[str] = field(default_factory=lambda: ["av1"])
    min_duration_sec: float = 60.0
    recursive: bool = True


@dataclass(slots=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    machine: Machine = field(default_factory=Machine)
    encode: Encode = field(default_factory=Encode)
    target_quality: TargetQuality = field(default_factory=TargetQuality)
    grain: Grain = field(default_factory=Grain)
    audio: Audio = field(default_factory=Audio)
    subtitles: Subtitles = field(default_factory=Subtitles)
    output: Output = field(default_factory=Output)
    library: Library = field(default_factory=Library)

    @property
    def work_dir(self) -> Path:
        return Path(self.paths.work_dir).expanduser()

    @property
    def state_path(self) -> Path:
        return Path(self.paths.state_file).expanduser()


class ConfigError(ValueError):
    pass


def _apply_section(obj: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown option '{key}' in [{where}]")
        expected = known[key].type
        # tomllib gives ints for whole numbers; widen to float where the field wants one.
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        setattr(obj, key, value)


def _merge_profile(cfg: Config, profile: str) -> None:
    overrides = PROFILES.get(profile)
    if overrides is None:
        raise ConfigError(f"unknown machine profile '{profile}'; known: {sorted(PROFILES)}")
    for section, values in overrides.items():
        _apply_section(getattr(cfg, section), values, section)


def load(path: Path | None) -> Config:
    """Load config. Profile defaults are applied first, then the file overrides them."""
    raw: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    cfg = Config()

    profile = raw.get("machine", {}).get("profile", cfg.machine.profile)
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

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not -1 <= cfg.encode.preset <= 13:
        raise ConfigError("encode.preset must be between -1 and 13")
    if not 1 <= cfg.encode.crf <= 70:
        raise ConfigError("encode.crf must be between 1 and 70 (SVT-AV1 range)")
    if cfg.grain.mode not in {"auto", "always", "never"}:
        raise ConfigError("grain.mode must be auto, always or never")
    if cfg.grain.analysis not in {"sampled", "full"}:
        raise ConfigError("grain.analysis must be sampled or full")
    if cfg.audio.mode not in {"copy", "opus", "smart"}:
        raise ConfigError("audio.mode must be copy, opus or smart")
    if cfg.subtitles.mode not in {"copy", "none", "text-only"}:
        raise ConfigError("subtitles.mode must be copy, none or text-only")
    if cfg.output.container not in {"mkv", "webm"}:
        raise ConfigError("output.container must be mkv or webm (AV1 + full subtitle support)")
    if cfg.encode.concat == "mkvmerge" and cfg.output.container != "mkv":
        raise ConfigError("encode.concat=mkvmerge can only produce mkv")
