"""Discovery and capability probing of the external binaries we drive.

Everything this orchestrator does is delegated to a real tool. This module is the
single place that answers "is it installed, which version, and what can it do".
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import CommandError, run

# Preferred locations for jellyfin-ffmpeg, in order. Falls back to PATH.
JELLYFIN_FFMPEG_DIRS = (
    "/usr/lib/jellyfin-ffmpeg",
    "/usr/local/lib/jellyfin-ffmpeg",
    "/opt/jellyfin-ffmpeg",
    # Where scripts/bootstrap.sh unpacks the portable build: macOS, and Linux
    # distributions Jellyfin's apt repository does not cover.
    str(Path.home() / ".local/lib/jellyfin-ffmpeg"),
)

# The Rockchip video engine's device node; see _rockchip_video_engine.
MPP_DEVICE = "/dev/mpp_service"


class ToolError(RuntimeError):
    pass


@dataclass(slots=True)
class Toolchain:
    ffmpeg: str
    ffprobe: str
    av1an: str | None
    svtav1encapp: str | None
    grav1synth: str | None
    mkvmerge: str | None

    ffmpeg_version: str = ""
    svtav1_version: str = ""
    av1an_version: str = ""

    # Reported by `doctor` to identify the ffmpeg build; nothing here consumes it.
    # See "Why there is no hardware acceleration" in the README before wiring it up.
    hwaccels: set[str] = field(default_factory=set)
    # Rockchip MPP decoders ("h264_rkmpp", "av1_rkmpp"...). verify decodes with
    # them where they give the same frames as software; see verify.decode_args.
    rkmpp_decoders: set[str] = field(default_factory=set)
    # The fastest cores, as a taskset list ("4-7" on an RK3588), when the CPU
    # mixes big and little ones. Empty on a uniform CPU. See verify.ffmpeg_argv.
    big_cores: str = ""
    filters: set[str] = field(default_factory=set)
    encoders: set[str] = field(default_factory=set)
    # Long options SvtAv1EncApp actually accepts. Flags come and go between
    # releases, so we ask the binary rather than assume the build has them.
    svt_options: set[str] = field(default_factory=set)
    vspipe: str | None = None
    # Namespaces of the VapourSynth plugins that actually load ("lsmas", "ffms2").
    vapoursynth_plugins: set[str] = field(default_factory=set)

    # ---- capability helpers -------------------------------------------------

    @property
    def bin_dirs(self) -> list[str]:
        """Directories holding the binaries we resolved, for a child process's PATH.

        Av1an spawns SvtAv1EncApp, ffmpeg and mkvmerge by bare name, so a tool found
        through the config rather than on PATH is invisible to it. `doctor` passing
        while the encode dies with "Encoder SvtAv1EncApp not found" is exactly the
        gap this closes.
        """
        found = (self.ffmpeg, self.ffprobe, self.svtav1encapp, self.mkvmerge, self.vspipe)
        return list(dict.fromkeys(str(Path(path).parent) for path in found if path))

    @property
    def has_vapoursynth(self) -> bool:
        return self.vspipe is not None

    @property
    def has_grain_pipeline(self) -> bool:
        return self.av1an is not None and self.grav1synth is not None

    @property
    def has_hdr10_metadata(self) -> bool:
        """Whether the encoder can be told the mastering display and light levels."""
        return {"--mastering-display", "--content-light"} <= self.svt_options

    @property
    def has_dovi_tonemap(self) -> bool:
        """tonemapx reshapes Dolby Vision profile 5 on the CPU; libplacebo needs a GPU."""
        return "tonemapx" in self.filters


def _which(candidate: str) -> str | None:
    if "/" in candidate:
        path = Path(candidate).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(candidate)


def _find_ffmpeg(configured: str, name: str) -> str | None:
    """Prefer jellyfin-ffmpeg: it ships tonemapx, rkmpp/rkrga and a current SVT-AV1."""
    if configured not in {"ffmpeg", "ffprobe"}:
        return _which(configured)
    for directory in JELLYFIN_FFMPEG_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(f"jellyfin-{name}")
    return found or shutil.which(name)


def _capture(argv: list[str], extra_path: list[str] | None = None) -> str:
    try:
        result = run(argv, check=False, extra_path=extra_path or ())
    except (OSError, CommandError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _rockchip_video_engine() -> bool:
    """Whether the rkmpp decoders can open the video engine, not just exist.

    jellyfin-ffmpeg's arm64 build lists them on every ARM machine: another board,
    a container the device was not passed to, a Mac's Linux VM. There they fail on
    the first frame, and verify would fail with them.
    """
    return os.access(MPP_DEVICE, os.R_OK | os.W_OK)


def _big_cores(cpus: Path = Path("/sys/devices/system/cpu")) -> str:
    """The highest-capacity cores as "a,b,c", or "" when all cores are alike.

    Only cores this process may run on: taskset refuses any other, and a
    container or service limited to some cores would fail every measurement.
    """
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        allowed = None
    capacity: dict[int, int] = {}
    for path in cpus.glob("cpu[0-9]*/cpu_capacity"):
        try:
            capacity[int(path.parent.name[3:])] = int(path.read_text())
        except (OSError, ValueError):
            continue
    if allowed is not None:
        capacity = {cpu: value for cpu, value in capacity.items() if cpu in allowed}
    if len(set(capacity.values())) < 2:
        return ""
    top = max(capacity.values())
    return ",".join(str(cpu) for cpu in sorted(capacity) if capacity[cpu] == top)


def detect(cfg: Config) -> Toolchain:
    ffmpeg = _find_ffmpeg(cfg.paths.ffmpeg, "ffmpeg")
    ffprobe = _find_ffmpeg(cfg.paths.ffprobe, "ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolError(
            "ffmpeg/ffprobe not found. Install jellyfin-ffmpeg (see scripts/bootstrap.sh) "
            "or set paths.ffmpeg / paths.ffprobe in the config."
        )

    chain = Toolchain(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        av1an=_which(cfg.paths.av1an),
        svtav1encapp=_which(cfg.paths.svtav1encapp),
        grav1synth=_which(cfg.paths.grav1synth),
        mkvmerge=_which(cfg.paths.mkvmerge),
    )

    banner = _capture([ffmpeg, "-hide_banner", "-version"])
    if match := re.search(r"ffmpeg version (\S+)", banner):
        chain.ffmpeg_version = match.group(1)

    chain.hwaccels = {
        line.strip()
        for line in _capture([ffmpeg, "-hide_banner", "-hwaccels"]).splitlines()[1:]
        if line.strip()
    }
    chain.rkmpp_decoders = {
        parts[1]
        for line in _capture([ffmpeg, "-hide_banner", "-decoders"]).splitlines()
        if len(parts := line.split()) > 2 and parts[1].endswith("_rkmpp")
    } if _rockchip_video_engine() else set()
    chain.filters = {
        parts[1]
        for line in _capture([ffmpeg, "-hide_banner", "-filters"]).splitlines()
        if len(parts := line.split()) > 2
    }
    chain.encoders = {
        parts[1]
        for line in _capture([ffmpeg, "-hide_banner", "-encoders"]).splitlines()
        if len(parts := line.split()) > 2
    }

    if chain.av1an and (
        match := re.search(r"[Aa]v1an\s+v?(\S+)", _capture([chain.av1an, "--version"]))
    ):
        chain.av1an_version = match.group(1)

    if svt := chain.svtav1encapp:
        if match := re.search(r"SVT-AV1.*?v?(\d+\.\d+\.\d+)", _capture([svt, "--version"])):
            chain.svtav1_version = match.group(1)
        chain.svt_options = set(re.findall(r"--[a-z0-9][a-z0-9-]+", _capture([svt, "--help"])))

    chain.vspipe = _find_vspipe(chain)
    if chain.vspipe:
        chain.vapoursynth_plugins = _vapoursynth_plugins(chain)
    chain.big_cores = _big_cores() if shutil.which("taskset") else ""
    return chain


def _missing_svt_options(chain: Toolchain, cfg: Config) -> list[str]:
    """Encoder options the configured quality depends on, absent from this encoder.

    These are not extras to skip quietly. The CRF curve was set with them on:
    1080p at CRF 29 needs the dark bias for dark scenes, and ac-bias and a weak
    temporal filter for detail in motion; 4K at CRF 30 needs variance boost and
    the same two, or its fog comes out blotchy. An older SvtAv1EncApp --
    distribution packages lag well behind -- would give exactly those encodes,
    with nothing to say so.
    """
    def used(value: float | dict[str, float], off: float = 0) -> bool:
        return any(v > off for v in (value.values() if isinstance(value, dict) else [value]))

    problems: list[str] = []
    upgrade = (f"SvtAv1EncApp {chain.svtav1_version or '(unknown version)'} lacks it; "
               "install SVT-AV1 3.1 or newer (scripts/bootstrap.sh svtav1)")
    if used(cfg.encode.luminance_qp_bias) and "--luminance-qp-bias" not in chain.svt_options:
        problems.append(
            f"encode.luminance_qp_bias needs --luminance-qp-bias. {upgrade}, or set it "
            "to 0 and expect dark 1080p scenes to block"
        )
    if used(cfg.encode.variance_boost_strength) \
            and "--enable-variance-boost" not in chain.svt_options:
        problems.append(
            f"encode.variance_boost_strength needs --enable-variance-boost. {upgrade}"
        )
    if used(cfg.encode.ac_bias) and "--ac-bias" not in chain.svt_options:
        problems.append(f"encode.ac_bias needs --ac-bias. {upgrade}")
    if used(cfg.encode.tf_strength, off=-1) and "--tf-strength" not in chain.svt_options:
        problems.append(f"encode.tf_strength needs --tf-strength. {upgrade}, or set it to -1")
    return problems


# Av1an's VapourSynth chunk methods, and the plugin namespace each needs.
VAPOURSYNTH_CHUNK_METHODS = {"lsmash": "lsmas", "ffms2": "ffms2", "bestsource": "bs"}

# Loads nothing; reports which plugins VapourSynth found and could load.
_PLUGIN_PROBE = """\
import sys
from vapoursynth import core
sys.stderr.write("plugins: " + " ".join(p.namespace for p in core.plugins()) + "\\n")
core.std.BlankClip(length=1).set_output()
"""


def _find_vspipe(chain: Toolchain) -> str | None:
    """vspipe on PATH, or installed beside our SvtAv1EncApp: a systemd unit's PATH
    leaves out ~/.local/bin, where the bootstrap script puts both."""
    if found := shutil.which("vspipe"):
        return found
    places = [Path.home() / ".local" / "bin"]
    if chain.svtav1encapp:
        places.insert(0, Path(chain.svtav1encapp).parent)
    return next((str(p / "vspipe") for p in places if (p / "vspipe").is_file()), None)


def _vapoursynth_plugins(chain: Toolchain) -> set[str]:
    assert chain.vspipe is not None
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "plugins.vpy"
        script.write_text(_PLUGIN_PROBE, encoding="utf-8")
        output = _capture([chain.vspipe, "--info", str(script)], chain.bin_dirs)
    match = re.search(r"^plugins: (.*)$", output, re.MULTILINE)
    return set(match.group(1).split()) if match else set()


def require(chain: Toolchain, cfg: Config) -> None:
    """Fail early and specifically, rather than deep inside a 10-hour encode."""
    problems: list[str] = []

    if chain.av1an is None:
        problems.append("av1an not found (see scripts/bootstrap.sh)")
    if chain.svtav1encapp is None:
        # Av1an drives the encoder binary directly, not through FFmpeg. This also
        # matters for grain: --fgs-table is documented as available only through the
        # SvtAv1Enc interface, so it is unreachable via ffmpeg -svtav1-params.
        problems.append(
            "SvtAv1EncApp not found; Av1an needs the standalone encoder binary "
            "(having libsvtav1 inside ffmpeg is not sufficient). Put it on PATH or "
            "set paths.svtav1encapp in the config."
        )
    if cfg.encode.concat == "mkvmerge" and chain.mkvmerge is None:
        problems.append("mkvmerge not found (install mkvtoolnix) or set encode.concat='ffmpeg'")
    if cfg.grain.mode != "never" and chain.grav1synth is None:
        problems.append(
            "grav1synth not found but grain.mode != 'never' "
            "(cargo install grav1synth, or set grain.mode='never')"
        )
    if chain.svtav1encapp is not None:
        problems += _missing_svt_options(chain, cfg)
    if cfg.dolby_vision.profile5 == "tonemap-sdr" and not chain.has_dovi_tonemap:
        problems.append(
            "dolby_vision.profile5='tonemap-sdr' needs an ffmpeg with the tonemapx "
            "filter (jellyfin-ffmpeg has it); set it back to 'skip' otherwise"
        )
    method = cfg.encode.chunk_method
    if (plugin := VAPOURSYNTH_CHUNK_METHODS.get(method)) and plugin not in chain.vapoursynth_plugins:
        where = "VapourSynth" if chain.has_vapoursynth else "VapourSynth (vspipe not found)"
        problems.append(
            f"encode.chunk_method='{method}' needs {where} with the '{plugin}' plugin"
            + (" (scripts/bootstrap.sh lsmash builds it)" if method == "lsmash" else "")
            + "; 'auto' falls back to 'hybrid' without it"
        )

    if problems:
        raise ToolError("missing prerequisites:\n  - " + "\n  - ".join(problems))


def describe(chain: Toolchain) -> str:
    lines = [
        f"ffmpeg      {chain.ffmpeg}  ({chain.ffmpeg_version or 'unknown version'})",
        f"ffprobe     {chain.ffprobe}",
        f"av1an       {chain.av1an or 'MISSING'}  ({chain.av1an_version or '-'})",
        f"svtav1      {chain.svtav1encapp or 'MISSING'}  ({chain.svtav1_version or '-'})",
        f"grav1synth  {chain.grav1synth or 'MISSING'}",
        f"mkvmerge    {chain.mkvmerge or 'MISSING'}",
        f"vapoursynth {chain.vspipe or 'no'}  "
        f"(plugins: {' '.join(sorted(chain.vapoursynth_plugins)) or 'none'})",
        f"hdr10 meta  {'yes' if chain.has_hdr10_metadata else 'no'}  "
        "(--mastering-display / --content-light)",
        f"ac-bias     {'yes' if '--ac-bias' in chain.svt_options else 'no'}  (for 4K and 1080p)",
        f"tf strength {'yes' if '--tf-strength' in chain.svt_options else 'no'}  "
        "(--tf-strength, for 4K and 1080p)",
        f"dark bias   {'yes' if '--luminance-qp-bias' in chain.svt_options else 'no'}  "
        "(--luminance-qp-bias, for 1080p)",
        f"dovi p5     {'yes' if chain.has_dovi_tonemap else 'no'}  "
        "(tonemapx, for profile 5 -> SDR)",
        f"hwaccels    {', '.join(sorted(chain.hwaccels)) or 'none'}  "
        "(this build's decoders; unused -- the encode is CPU-only)",
        f"rkmpp       {', '.join(sorted(chain.rkmpp_decoders)) or 'none'}  "
        f"(usable decoders, for verify; needs {MPP_DEVICE})",
        f"big cores   {chain.big_cores or 'none'}  (verify's filter passes run there)",
    ]
    return "\n".join(lines)
