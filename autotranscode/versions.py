"""The toolchain this autotranscode was tested with, against the one installed.

toolchain.toml names one version of every tool; scripts/bootstrap.sh installs
from the same file. A tool can pass `doctor` on capability alone -- an Av1an that
runs, an SVT-AV1 that has every option -- and still not be the build the
settings were judged on. This module is what tells the two apart.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .tools import Toolchain, ToolError
from .util import log

MANIFEST = Path(__file__).with_name("toolchain.toml")
ACCEPT = {"exact", "same-major", "at-least", "untracked"}
# What each status says in `doctor`; the ones not "ok" or "missing" fail a pin.
FAILING = {"older", "newer", "different", "unknown"}

# What it means when a tool will not say its version, beyond "cannot tell".
UNKNOWN_HINT = {
    "grav1synth": "unpatched, or patched before the patch gave it a --version; an "
                  "unpatched build corrupts some files",
    "lsmash": "built before bootstrap recorded the commit",
    "svtfg": "built before bootstrap recorded the commit",
    "scenechange": "installed before bootstrap recorded the version",
}


@dataclass(frozen=True, slots=True)
class Pin:
    name: str
    version: str
    accept: str
    enforce: bool
    stage: str


@dataclass(frozen=True, slots=True)
class Check:
    pin: Pin
    # None: the tool is not installed. "": installed, version unknown.
    installed: str | None
    status: str

    @property
    def failed(self) -> bool:
        return self.status in FAILING

    def problem(self) -> str:
        """What is wrong and the command that puts it right."""
        pin = self.pin
        if self.status == "unknown":
            what = f"{pin.name}: cannot tell which version is installed"
            if hint := UNKNOWN_HINT.get(pin.name):
                what += f" ({hint})"
        else:
            wanted = {"exact": pin.version, "same-major": f"{_major(pin.version)}.x, "
                      f"{pin.version} or newer", "at-least": f"{pin.version} or newer"}
            what = f"{pin.name} {self.installed} is {self.status}; tested with " \
                   f"{wanted.get(pin.accept, pin.version)}"
        return f"{what}. Install the tested one: scripts/bootstrap.sh {pin.stage}"


def load(path: Path = MANIFEST) -> dict[str, Pin]:
    return parse(path.read_text(encoding="utf-8"), str(path))


def parse(text: str, where: str = "toolchain.toml") -> dict[str, Pin]:
    """The pins in a manifest's text: this one's, or another version's (`update`)."""
    pins: dict[str, Pin] = {}
    for name, entry in tomllib.loads(text).items():
        pin = Pin(name=name, version=str(entry["version"]), accept=entry["accept"],
                  enforce=bool(entry["enforce"]), stage=entry["stage"])
        if pin.accept not in ACCEPT:
            raise ValueError(f"{where}: [{name}] accept must be one of {sorted(ACCEPT)}")
        pins[name] = pin
    return pins


def numbers(version: str) -> tuple[int, ...]:
    """The release numbers: "8.1.3-Jellyfin" -> (8, 1, 3), "R72" -> (72,)."""
    match = re.search(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def _build(version: str) -> str:
    """Build metadata, after "+": what marks a patched build of a release."""
    return version.partition("+")[2]


def _major(version: str) -> int:
    return (numbers(version) or (0,))[0]


def _is_commit(version: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{7,40}", version) is not None


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Equal lengths, so 0.6 and 0.6.0 compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def compare(pin: Pin, installed: str | None) -> str:
    """ok, older, newer, different, unknown or missing."""
    if installed is None:
        return "missing"
    if pin.accept == "untracked":
        return "ok"
    if not installed:
        return "unknown"
    if _is_commit(pin.version):
        same = installed.startswith(pin.version) or pin.version.startswith(installed)
        return "ok" if same and _is_commit(installed) else "different"
    have, want = _padded(numbers(installed), numbers(pin.version))
    if not any(have):
        return "unknown"
    if pin.accept == "exact":
        if have != want:
            return "older" if have < want else "newer"
        return "ok" if _build(installed) == _build(pin.version) else "different"
    if have < want:
        return "older"
    if pin.accept == "same-major" and have[0] != want[0]:
        return "newer"
    return "ok"


def installed_versions(chain: Toolchain) -> dict[str, str | None]:
    """What each tool says it is: None when absent, "" when it will not say."""
    def found(path: str | None, version: str) -> str | None:
        return None if path is None else version

    return {
        "ffmpeg": chain.ffmpeg_version,
        "svtav1": found(chain.svtav1encapp, chain.svtav1_version),
        "av1an": found(chain.av1an, chain.av1an_version),
        "grav1synth": found(chain.grav1synth, chain.grav1synth_version),
        "vapoursynth": found(chain.vspipe, chain.vapoursynth_version),
        "lsmash": chain.lsmash_commit if "lsmas" in chain.vapoursynth_plugins else None,
        "svtfg": found(chain.svtfg, chain.svtfg_commit),
        "scenechange": found(chain.scenechange, chain.scenechange_version),
        "mkvmerge": found(chain.mkvmerge, chain.mkvmerge_version),
    }


def check(chain: Toolchain, pins: dict[str, Pin] | None = None) -> list[Check]:
    pins = load() if pins is None else pins
    have = installed_versions(chain)
    return [Check(pin, have[name], compare(pin, have[name]))
            for name, pin in pins.items() if name in have]


def describe(checks: list[Check]) -> str:
    rows = [("", "installed", "tested", "accepts", "")]
    for item in checks:
        pin = item.pin
        installed = "-" if item.installed is None else (item.installed or "?")
        status = item.status if item.status == "ok" else item.status.upper()
        if item.failed and not pin.enforce:
            status += " (not enforced)"
        tested = pin.version[:12] if _is_commit(pin.version) else pin.version
        rows.append((pin.name, installed[:24], tested, pin.accept, status))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(
        ("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths, strict=True))
         + "  " + row[4]).rstrip()
        for row in rows
    )


def fixes(checks: list[Check], *, missing: bool = False) -> str:
    """One bootstrap command for every failing pin (and, with `missing`, every tool
    not installed), in the order bootstrap runs them."""
    order = ["deps", "ffmpeg", "svtav1", "vapoursynth", "lsmash", "av1an", "grav1synth",
             "svtfg", "scenechange"]
    stages = {item.pin.stage for item in checks
              if item.failed or (missing and item.status == "missing")}
    return " ".join(stage for stage in order if stage in stages)


def require(chain: Toolchain, cfg: Config, checks: list[Check] | None = None) -> None:
    """Refuse untested versions of the tools that decide the output.

    With tools.versions = "warn" they are logged instead, for trying out a newer
    build on purpose.
    """
    checks = check(chain) if checks is None else checks
    problems = [item.problem() for item in checks if item.failed and item.pin.enforce]
    if not problems:
        return
    if cfg.tools.versions == "warn":
        for problem in problems:
            log.warning("untested toolchain: %s", problem)
        return
    raise ToolError(
        "untested toolchain (set tools.versions = \"warn\" to run anyway):\n  - "
        + "\n  - ".join(problems)
    )
