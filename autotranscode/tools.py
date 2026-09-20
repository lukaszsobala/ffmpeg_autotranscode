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
from .util import CommandError, log, run

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
    grav1synth: str | None
    mkvmerge: str | None

    ffmpeg_version: str = ""
    svtav1_version: str = ""
    av1an_version: str = ""

    hwaccels: set[str] = field(default_factory=set)
    filters: set[str] = field(default_factory=set)
    encoders: set[str] = field(default_factory=set)
    has_vapoursynth: bool = False

    # ---- capability helpers -------------------------------------------------

    @property
    def has_libvmaf(self) -> bool:
        return "libvmaf" in self.filters

    @property
    def has_grain_pipeline(self) -> bool:
        return self.av1an is not None and self.grav1synth is not None

    def hwaccel_args(self, requested: str) -> list[str]:
        """FFmpeg flags for hardware-accelerated *decode*.

        Decode only, deliberately: no Rockchip SoC can encode AV1, and even on Intel
        the hardware AV1 encoder is well below SVT-AV1 at these bitrates.
        """
        if requested == "none":
            return []
        if requested == "auto":
            for candidate in ("rkmpp", "vaapi", "qsv"):
                if candidate in self.hwaccels:
                    requested = candidate
                    break
            else:
                return []
        if requested not in self.hwaccels:
            log.warning("hwaccel %r not available in this ffmpeg; decoding on CPU", requested)
            return []
        if requested == "rkmpp":
            # Download frames back to system memory: our filters are all CPU-side.
            return ["-init_hw_device", "rkmpp=rk", "-hwaccel", "rkmpp",
                    "-hwaccel_output_format", "drm_prime"]
        if requested == "vaapi":
            return ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
        if requested == "qsv":
            return ["-hwaccel", "qsv"]
        return []


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

    if (svt := shutil.which("SvtAv1EncApp")) and (
        match := re.search(r"SVT-AV1.*?v?(\d+\.\d+\.\d+)", _capture([svt, "--version"]))
    ):
        chain.svtav1_version = match.group(1)

    chain.has_vapoursynth = shutil.which("vspipe") is not None
    return chain


def require(chain: Toolchain, cfg: Config) -> None:
    """Fail early and specifically, rather than deep inside a 10-hour encode."""
    problems: list[str] = []

    if chain.av1an is None:
        problems.append("av1an not found (cargo install av1an, or see scripts/bootstrap-rk3588.sh)")
    if shutil.which("SvtAv1EncApp") is None:
        # Av1an drives the encoder binary directly, not through FFmpeg. This also
        # matters for grain: --fgs-table is documented as available only through the
        # SvtAv1Enc interface, so it is unreachable via ffmpeg -svtav1-params.
        problems.append(
            "SvtAv1EncApp not found on PATH; Av1an needs the standalone encoder binary "
            "(having libsvtav1 inside ffmpeg is not sufficient)"
        )
    if cfg.encode.concat == "mkvmerge" and chain.mkvmerge is None:
        problems.append("mkvmerge not found (apt install mkvtoolnix) or set encode.concat='ffmpeg'")
    if cfg.grain.mode != "never" and chain.grav1synth is None:
        problems.append(
            "grav1synth not found but grain.mode != 'never' "
            "(cargo install grav1synth, or set grain.mode='never')"
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
        f"grav1synth  {chain.grav1synth or 'MISSING'}",
        f"mkvmerge    {chain.mkvmerge or 'MISSING'}",
        f"SVT-AV1     {chain.svtav1_version or 'unknown'}",
        f"vapoursynth {'yes' if chain.has_vapoursynth else 'no'}",
        f"libvmaf     {'yes' if chain.has_libvmaf else 'no'}",
        f"hwaccels    {', '.join(sorted(chain.hwaccels)) or 'none'}",
    ]
    return "\n".join(lines)
