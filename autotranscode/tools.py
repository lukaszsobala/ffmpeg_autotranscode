"""Discovery and capability probing of the external binaries we drive.

Everything this orchestrator does is delegated to a real tool. This module is the
single place that answers "is it installed, which version, and what can it do".
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import CommandError, run

# Preferred locations for jellyfin-ffmpeg, in order. Falls back to PATH.
JELLYFIN_FFMPEG_DIRS = (
    "/usr/lib/jellyfin-ffmpeg",
    "/usr/local/lib/jellyfin-ffmpeg",
    "/opt/jellyfin-ffmpeg",
)


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
    has_vapoursynth: bool = False

    # ---- capability helpers -------------------------------------------------

    @property
    def bin_dirs(self) -> list[str]:
        """Directories holding the binaries we resolved, for a child process's PATH.

        Av1an spawns SvtAv1EncApp, ffmpeg and mkvmerge by bare name, so a tool found
        through the config rather than on PATH is invisible to it. `doctor` passing
        while the encode dies with "Encoder SvtAv1EncApp not found" is exactly the
        gap this closes.
        """
        found = (self.ffmpeg, self.ffprobe, self.svtav1encapp, self.mkvmerge)
        return list(dict.fromkeys(str(Path(path).parent) for path in found if path))

    @property
    def has_libvmaf(self) -> bool:
        return "libvmaf" in self.filters

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
    """Prefer jellyfin-ffmpeg: it ships libvmaf, rkmpp/rkrga and a current SVT-AV1."""
    if configured not in {"ffmpeg", "ffprobe"}:
        return _which(configured)
    for directory in JELLYFIN_FFMPEG_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(f"jellyfin-{name}")
    return found or shutil.which(name)


def _capture(argv: list[str]) -> str:
    try:
        result = run(argv, check=False)
    except (OSError, CommandError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _big_cores() -> str:
    """The highest-capacity cores as "a,b,c", or "" when all cores are alike."""
    capacity: dict[int, int] = {}
    for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpu_capacity"):
        try:
            capacity[int(path.parent.name[3:])] = int(path.read_text())
        except (OSError, ValueError):
            continue
    if len(set(capacity.values())) < 2:
        return ""
    top = max(capacity.values())
    return ",".join(str(cpu) for cpu in sorted(capacity) if capacity[cpu] == top)


def detect(cfg: Config) -> Toolchain:
    ffmpeg = _find_ffmpeg(cfg.paths.ffmpeg, "ffmpeg")
    ffprobe = _find_ffmpeg(cfg.paths.ffprobe, "ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolError(
            "ffmpeg/ffprobe not found. Install jellyfin-ffmpeg (see scripts/bootstrap-rk3588.sh) "
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
    }
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

    chain.has_vapoursynth = shutil.which("vspipe") is not None
    chain.big_cores = _big_cores() if shutil.which("taskset") else ""
    return chain


def require(chain: Toolchain, cfg: Config) -> None:
    """Fail early and specifically, rather than deep inside a 10-hour encode."""
    problems: list[str] = []

    if chain.av1an is None:
        problems.append("av1an not found (cargo install av1an, or see scripts/bootstrap-rk3588.sh)")
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
        problems.append("mkvmerge not found (apt install mkvtoolnix) or set encode.concat='ffmpeg'")
    if cfg.grain.mode != "never" and chain.grav1synth is None:
        problems.append(
            "grav1synth not found but grain.mode != 'never' "
            "(cargo install grav1synth, or set grain.mode='never')"
        )
    if cfg.dolby_vision.profile5 == "tonemap-sdr" and not chain.has_dovi_tonemap:
        problems.append(
            "dolby_vision.profile5='tonemap-sdr' needs an ffmpeg with the tonemapx "
            "filter (jellyfin-ffmpeg has it); set it back to 'skip' otherwise"
        )
    if cfg.target_quality.enabled and cfg.target_quality.metric == "vmaf" and not chain.has_libvmaf:
        problems.append(
            "target_quality.metric='vmaf' needs an ffmpeg with libvmaf; "
            "jellyfin-ffmpeg has it, the Ubuntu build does not"
        )
    if cfg.encode.chunk_method in {"lsmash", "ffms2", "bestsource"} and not chain.has_vapoursynth:
        problems.append(
            f"encode.chunk_method='{cfg.encode.chunk_method}' needs VapourSynth; "
            "use 'hybrid' for an FFmpeg-only setup"
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
        f"vapoursynth {'yes' if chain.has_vapoursynth else 'no'}",
        f"libvmaf     {'yes' if chain.has_libvmaf else 'no'}",
        f"hdr10 meta  {'yes' if chain.has_hdr10_metadata else 'no'}  "
        "(--mastering-display / --content-light)",
        f"ac-bias     {'yes' if '--ac-bias' in chain.svt_options else 'no'}",
        f"dovi p5     {'yes' if chain.has_dovi_tonemap else 'no'}  "
        "(tonemapx, for profile 5 -> SDR)",
        f"hwaccels    {', '.join(sorted(chain.hwaccels)) or 'none'}  "
        "(this build's decoders; unused -- the encode is CPU-only)",
    ]
    return "\n".join(lines)
