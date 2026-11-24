"""Discovery and capability probing of the external binaries we drive.

Everything this orchestrator does is delegated to a real tool. This module is the
single place that answers "is it installed, which version, and what can it do".
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
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
    # "0.2.0+autotranscode.1" from the patched build; "" from any other, which has
    # no --version. See versions.py.
    grav1synth_version: str = ""
    mkvmerge_version: str = ""
    vapoursynth_version: str = ""
    # The L-SMASH-Works commit bootstrap recorded beside the plugin, or "".
    lsmash_commit: str = ""
    # SvtFgEssential, the grain estimator of grain.estimator = "svtfg", and the
    # SVT-AV1-Essential commit bootstrap recorded beside it (SVTFG_COMMIT_FILE), or "".
    svtfg: str | None = None
    svtfg_commit: str = ""

    # Reported by `doctor` to identify the ffmpeg build; nothing here consumes it.
    # See "Why there is no hardware acceleration" in the README before wiring it up.
    hwaccels: set[str] = field(default_factory=set)
    # Rockchip MPP decoders ("h264_rkmpp", "av1_rkmpp"...). verify decodes with
    # them where they give the same frames as software; see verify.decode_args.
    rkmpp_decoders: set[str] = field(default_factory=set)
    # Every decoder this ffmpeg has ("libdav1d", "hevc"...).
    decoders: set[str] = field(default_factory=set)
    # The fast cores, as a taskset list ("4,5,6,7" on an RK3588), when the CPU
    # has some much slower ones. Empty otherwise. See _big_cores, verify.ffmpeg_argv.
    big_cores: str = ""
    filters: set[str] = field(default_factory=set)
    encoders: set[str] = field(default_factory=set)
    # Long options SvtAv1EncApp actually accepts. Flags come and go between
    # releases, so we ask the binary rather than assume the build has them.
    svt_options: set[str] = field(default_factory=set)
    vspipe: str | None = None
    # Namespaces of the VapourSynth plugins that actually load ("lsmas", "ffms2").
    vapoursynth_plugins: set[str] = field(default_factory=set)
    # Where each of them was loaded from, where VapourSynth says.
    vapoursynth_plugin_paths: dict[str, str] = field(default_factory=dict)

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


# A core counts as big when it has at least this share of the fastest core's
# capacity. An RK3588's A55s have about 0.4 of an A76, and pinning to the A76s
# made verify 2.8x faster. A Core Ultra 7 258V's E-cores have 0.66 of its P-cores,
# close enough that leaving them out would cost more than it saves. A share
# rather than equality, too, because Intel reports its favoured cores a little
# higher (1024 against 1005 on that chip): equality picked two P-cores of four.
BIG_CORE_SHARE = 0.5


def _big_cores(cpus: Path = Path("/sys/devices/system/cpu")) -> str:
    """The cores worth pinning to as "a,b,c", or "" when every core is.

    Those with at least BIG_CORE_SHARE of the highest capacity; "" when that is
    all of them. Only cores this process may run on: taskset refuses any other,
    and a container or service limited to some cores would fail every measurement.
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
    if not capacity:
        return ""
    floor = BIG_CORE_SHARE * max(capacity.values())
    big = [cpu for cpu in sorted(capacity) if capacity[cpu] >= floor]
    return "" if len(big) == len(capacity) else ",".join(map(str, big))


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
    chain.decoders = {
        parts[1]
        for line in _capture([ffmpeg, "-hide_banner", "-decoders"]).splitlines()
        if len(parts := line.split()) > 2 and parts[0][:1] in {"V", "A", "S"}
        and parts[1] != "="
    }
    chain.rkmpp_decoders = {
        name for name in chain.decoders if name.endswith("_rkmpp")
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

    if chain.grav1synth and (
        match := re.search(r"grav1synth\s+v?(\d\S*)", _capture([chain.grav1synth, "--version"]))
    ):
        chain.grav1synth_version = match.group(1)
    if chain.mkvmerge and (
        match := re.search(r"mkvmerge\s+v?(\d\S*)", _capture([chain.mkvmerge, "--version"]))
    ):
        chain.mkvmerge_version = match.group(1)

    chain.svtfg = _find_svtfg(cfg.paths.svtfg, chain)
    if chain.svtfg:
        chain.svtfg_commit = read_svtfg_commit(chain.svtfg)
    chain.vspipe = _find_vspipe(chain)
    if chain.vspipe:
        _probe_vapoursynth(chain)
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
    def used(value: float | Mapping[str, float], off: float = 0) -> bool:
        return any(v > off for v in (value.values() if isinstance(value, Mapping) else [value]))

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

# Loads nothing; reports VapourSynth's release and which plugins it found and
# could load, with where from.
_PLUGIN_PROBE = """\
import sys
from vapoursynth import core
version = getattr(core, "core_version", None)
sys.stderr.write("release: %s\\n"
                 % (version.release_major if version else core.version_number()))
for plugin in core.plugins():
    sys.stderr.write("plugin: %s %s\\n"
                     % (plugin.namespace, getattr(plugin, "plugin_path", "") or ""))
core.std.BlankClip(length=1).set_output()
"""

# Written by scripts/bootstrap.sh beside the L-SMASH-Works plugin, which carries
# no version of its own: the commit it was built from.
LSMASH_COMMIT_FILE = "LSMASHSource.commit"


# Written by scripts/bootstrap.sh beside SvtFgEssential: the SVT-AV1-Essential
# commit it was built from. The tool's --version names only the tool (1.0.0),
# which stays put while the estimator in the codec library changes.
SVTFG_COMMIT_FILE = "SvtFgEssential.commit"


def _find_svtfg(configured: str, chain: Toolchain) -> str | None:
    """SvtFgEssential as configured, else beside our SvtAv1EncApp or in
    ~/.local/bin, where bootstrap puts it: a systemd unit's PATH leaves that out."""
    if found := _which(configured):
        return found
    if "/" in configured:
        return None
    places = [Path.home() / ".local" / "bin"]
    if chain.svtav1encapp:
        places.insert(0, Path(chain.svtav1encapp).parent)
    return next((str(p / configured) for p in places if (p / configured).is_file()), None)


def read_svtfg_commit(binary: str) -> str:
    try:
        return (Path(binary).parent / SVTFG_COMMIT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _find_vspipe(chain: Toolchain) -> str | None:
    """vspipe on PATH, or installed beside our SvtAv1EncApp: a systemd unit's PATH
    leaves out ~/.local/bin, where the bootstrap script puts both."""
    if found := shutil.which("vspipe"):
        return found
    places = [Path.home() / ".local" / "bin"]
    if chain.svtav1encapp:
        places.insert(0, Path(chain.svtav1encapp).parent)
    return next((str(p / "vspipe") for p in places if (p / "vspipe").is_file()), None)


def _probe_vapoursynth(chain: Toolchain) -> None:
    assert chain.vspipe is not None
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "plugins.vpy"
        script.write_text(_PLUGIN_PROBE, encoding="utf-8")
        output = _capture([chain.vspipe, "--info", str(script)], chain.bin_dirs)
    read_vapoursynth_probe(chain, output)


def read_vapoursynth_probe(chain: Toolchain, output: str) -> None:
    """Fill in what _PLUGIN_PROBE reported."""
    if match := re.search(r"^release: (\d+)$", output, re.MULTILINE):
        chain.vapoursynth_version = match.group(1)
    for line in re.findall(r"^plugin: (.*)$", output, re.MULTILINE):
        namespace, _, path = line.partition(" ")
        chain.vapoursynth_plugins.add(namespace)
        if path:
            chain.vapoursynth_plugin_paths[namespace] = path
    if plugin := chain.vapoursynth_plugin_paths.get("lsmas"):
        try:
            recorded = (Path(plugin).parent / LSMASH_COMMIT_FILE).read_text(encoding="utf-8")
        except OSError:
            recorded = ""
        chain.lsmash_commit = recorded.strip()


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
    if cfg.grain.mode != "never" and cfg.grain.estimator == "svtfg" and chain.svtfg is None:
        problems.append(
            "SvtFgEssential not found but grain.estimator = 'svtfg' "
            "(scripts/bootstrap.sh svtfg builds it, or set paths.svtfg)"
        )
    if chain.svtav1encapp is not None:
        problems += _missing_svt_options(chain, cfg)
    if cfg.dolby_vision.profile5 == "tonemap-sdr" and not chain.has_dovi_tonemap:
        problems.append(
            "dolby_vision.profile5='tonemap-sdr' needs an ffmpeg with the tonemapx "
            "filter (jellyfin-ffmpeg has it); set it back to 'skip' otherwise"
        )
    method = cfg.encode.chunk_method
    plugin = VAPOURSYNTH_CHUNK_METHODS.get(method)
    if plugin and plugin not in chain.vapoursynth_plugins:
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
        f"grav1synth  {chain.grav1synth or 'MISSING'}  ({chain.grav1synth_version or '-'})",
        f"svtfg       {chain.svtfg or 'MISSING'}  "
        f"({chain.svtfg_commit[:12] or 'commit not recorded'})",
        f"mkvmerge    {chain.mkvmerge or 'MISSING'}  ({chain.mkvmerge_version or '-'})",
        f"vapoursynth {chain.vspipe or 'no'}  "
        f"(R{chain.vapoursynth_version or '?'}; "
        f"plugins: {' '.join(sorted(chain.vapoursynth_plugins)) or 'none'})",
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
        f"(usable decoders, for verify and grain; needs {MPP_DEVICE})",
        f"big cores   {chain.big_cores or 'all'}  (verify's filter passes run there)",
    ]
    return "\n".join(lines)
