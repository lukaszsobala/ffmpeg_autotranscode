#!/usr/bin/env python3
"""Find encoder settings that fix what the eye sees, measured rather than guessed.

Built after a CRF 32 encode scored 50.8 dB PSNR yet was visibly blocky in flat
areas and had lost its grain. Two stages:

1. Verify the existing encode against its source with `autotranscode.verify`,
   frame by frame, to find where it is worst. Snapshots of those frames let the
   eye confirm the metric before anything is concluded from it.
2. Cut those stretches out of the source (stream copy, so bit-identical), and
   run the real pipeline over them once per candidate setting. Each result is
   verified the same way and snapshotted at the same frames.

Everything goes to one directory with a report.md, rewritten after every step
so a partial run is still readable. Nothing touches the library, the ledger or
the live config: each variant gets its own config file and state file.

Meant to run detached, since it takes an hour or two at 4K:

    systemd-run --user --unit=autotranscode-quality --nice=10 --same-dir \
        .venv/bin/python scripts/quality_sweep.py SOURCE ENCODED --out DIR
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autotranscode import config, probe, tools, verify
from autotranscode.util import human_duration, run


@dataclass(slots=True)
class Variant:
    name: str
    why: str
    crf: int
    grain: str = "never"           # the pipeline's --grain mode
    svt: list[str] = field(default_factory=list)


# Chosen to separate the two complaints -- blocking in flat areas, lost grain --
# and to show whether each fix costs bits or only needs different decisions.
VARIANTS = [
    Variant("A-crf32", "the encode you saw: current settings", 32),
    Variant("B-crf27", "more bits, nothing else", 27),
    Variant("C-crf22", "many more bits: what fidelity costs", 22),
    Variant("D-crf32-grainroute", "our own route: denoise, encode, grav1synth table", 32,
            grain="always"),
    Variant("E-crf32-svtgrain", "SVT's built-in grain synthesis on top", 32,
            svt=["--film-grain", "10", "--film-grain-denoise", "0"]),
    Variant("F-crf32-flat", "decisions for flat and dark areas, same CRF", 32,
            svt=["--enable-qm", "1", "--luminance-qp-bias", "50", "--tf-strength", "0",
                 "--variance-boost-strength", "3"]),
    Variant("G-crf27-flat-grain", "the likely answer: B + F + E together", 27,
            svt=["--enable-qm", "1", "--luminance-qp-bias", "50", "--tf-strength", "0",
                 "--variance-boost-strength", "3", "--film-grain", "10",
                 "--film-grain-denoise", "0"]),
]


def parse_variant(spec: str) -> Variant:
    """'NAME CRF GRAIN [SVT PARAMS...]' -> Variant, for --variant."""
    words = shlex.split(spec)
    if len(words) < 3 or not words[1].isdigit() or words[2] not in {"auto", "always", "never"}:
        raise SystemExit(f"--variant wants 'NAME CRF GRAIN [SVT PARAMS...]', got {spec!r}")
    return Variant(words[0], "given on the command line", int(words[1]), words[2], words[3:])


@dataclass(slots=True)
class Outcome:
    variant: Variant
    ok: bool = False
    error: str = ""
    seconds: float = 0.0
    bytes: int = 0
    kbps: float = 0.0
    result: verify.Comparison | None = None


# --------------------------------------------------------------------------- TOML


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def dump_config(cfg: config.Config) -> str:
    """A complete TOML config, so a variant does not inherit anything by accident."""
    lines: list[str] = []
    for section in dataclasses.fields(cfg):
        values = getattr(cfg, section.name)
        tables: list[tuple[str, dict]] = []
        lines.append(f"[{section.name}]")
        for item in dataclasses.fields(values):
            value = getattr(values, item.name)
            if isinstance(value, dict):
                tables.append((item.name, value))   # sub-tables go after the keys
            else:
                lines.append(f"{item.name} = {_toml_value(value)}")
        for name, table in tables:
            lines.append(f"[{section.name}.{name}]")
            lines += [f'"{key}" = {_toml_value(val)}' for key, val in table.items()]
        lines.append("")
    return "\n".join(lines)


def variant_config(base: config.Config, variant: Variant, root: Path, preset: int | None) -> Path:
    cfg = dataclasses.replace(base)
    for section in dataclasses.fields(cfg):
        setattr(cfg, section.name, dataclasses.replace(getattr(base, section.name)))
    here = root / "runs" / variant.name
    cfg.encode.crf = variant.crf
    cfg.encode.crf_by_width = {}              # the CRF named is the CRF used
    cfg.encode.extra_svt_params = list(variant.svt)
    if preset is not None:
        cfg.encode.preset = preset
    cfg.grain.mode = variant.grain
    cfg.output.directory = str(here)
    cfg.output.min_saving_percent = -1e9      # keep every result, however it turns out
    cfg.output.replace_source = False
    cfg.library.min_duration_sec = 0.0
    cfg.library.min_bits_per_pixel = 0.0
    cfg.staging.enabled = False
    cfg.paths.work_dir = str(root / "work" / variant.name)
    cfg.paths.state_file = str(root / "work" / variant.name / "state.json")
    config.validate(cfg)
    here.mkdir(parents=True, exist_ok=True)
    path = here / "config.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- report


class Report:
    def __init__(self, path: Path, source: Path, encoded: Path) -> None:
        self.path = path
        self.started = dt.datetime.now()
        self.source = source
        self.encoded = encoded
        self.status = "starting"
        self.stage1: verify.Comparison | None = None
        self.windows: list[float] = []
        self.window_seconds = 0.0
        self.window_reasons: dict[float, str] = {}
        self.outcomes: list[Outcome] = []
        self.snapshots: list[int] = []
        self.notes: list[str] = []

    def write(self) -> None:
        out = [
            "# Quality sweep",
            "",
            f"Started {self.started:%Y-%m-%d %H:%M}. **Status: {self.status}**",
            "",
            "## How to read this",
            "",
            "- **XPSNR** is PSNR weighted by local activity: an error in a flat patch counts",
            "  far more than one in texture, which is how visibility works. Plain PSNR scored",
            "  the blocky encode 50.8 dB. Higher is better; compare variants with each other,",
            "  not with PSNR. Measured at 8-bit because ffmpeg's XPSNR is broken for 10-bit",
            "  here (a file against itself scores 9.98 dB), and with synthesised grain *off*,",
            "  so it scores the encode underneath the grain.",
            "- **5th pct** is how bad the bad frames are. Visible blocking lives here, not in",
            "  the mean.",
            "- **Grain** is the source's pixel-scale grain that survives, with synthesis",
            "  *on*: what a 3x3 median removes from the encode, as a fraction of what it",
            "  removes from the source. 100% is untouched. The encode you saw keeps ~40%.",
            "- **Snapshots** are the same frames from every variant, tonemapped identically.",
            "  Flip between them in an image viewer at 100% zoom. The numbers are only worth",
            "  anything if they agree with what you see there.",
            "",
        ]
        if self.stage1:
            s = self.stage1
            grain = (f"{100 * s.grain_retention:.0f}%" if s.grain_retention is not None
                     else "not measured")
            out += [
                "## Stage 1: the existing encode, whole file",
                "",
                f"- {s.frames} frames, XPSNR mean **{s.xpsnr_mean:.2f} dB**, "
                f"5th percentile **{s.xpsnr_p5:.2f}**, worst {s.xpsnr_min:.2f}",
                f"- grain retained: **{grain}**",
                f"- worst frames: {', '.join(map(str, s.worst))} "
                f"(snapshots in `stage1/`)",
                "",
            ]
        if self.windows:
            spans = ", ".join(
                f"{human_duration(t)}+{self.window_seconds:.0f}s"
                + (f" ({self.window_reasons[t]})" if t in self.window_reasons else "")
                for t in self.windows)
            out += [f"Stretches sampled for stage 2 (worst for the reason given): {spans}", ""]
        if self.outcomes:
            out += [
                "## Stage 2: variants over those stretches",
                "",
                "| variant | kbps | size vs A | XPSNR mean | 5th pct | worst | grain | time |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
            base = next((o.bytes for o in self.outcomes if o.ok), 0)
            for o in self.outcomes:
                if not o.ok:
                    out.append(f"| {o.variant.name} | failed: {o.error[:60]} | | | | | | |")
                    continue
                r = o.result
                assert r is not None
                grain = (f"{100 * r.grain_retention:.0f}%" if r.grain_retention is not None
                         else "-")
                size = f"{100 * o.bytes / base:.0f}%" if base else "-"
                out.append(
                    f"| {o.variant.name} | {o.kbps:,.0f} | {size} | {r.xpsnr_mean:.2f} | "
                    f"{r.xpsnr_p5:.2f} | {r.xpsnr_min:.2f} | {grain} | "
                    f"{human_duration(o.seconds)} |"
                )
            out += ["", "What each variant changes:", ""]
            out += [f"- **{o.variant.name}**: {o.variant.why}"
                    + (f" (`{' '.join(o.variant.svt)}`)" if o.variant.svt else "")
                    + (f", grain mode `{o.variant.grain}`" if o.variant.grain != "never" else "")
                    for o in self.outcomes]
            out.append("")
        if self.snapshots:
            out += [
                "## Snapshots",
                "",
                f"Frames {', '.join(map(str, self.snapshots))} of the sampled stretches, from",
                "the source (`source_f*.png`) and every variant, in `snapshots/`.",
                "",
            ]
        if self.notes:
            out += ["## Notes", "", *[f"- {note}" for note in self.notes], ""]
        self.path.write_text("\n".join(out), encoding="utf-8")


# -------------------------------------------------------------------------- steps


def cut_excerpt(
    chain: tools.Toolchain, cfg: config.Config, source: Path, starts: list[float],
    seconds: float, root: Path,
) -> Path:
    """The chosen stretches of the source, stream-copied and joined.

    Stream copy keeps the source bits exactly, so the excerpt is the source, not
    a re-encode of it. Each cut starts at the keyframe at or before its time.
    """
    parts: list[Path] = []
    for index, start in enumerate(starts):
        part = root / f"part{index}.mkv"
        run([chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{seconds:.3f}",
             "-map", "0:v:0", "-c", "copy", str(part)])
        parts.append(part)
    excerpt = root / "excerpt.mkv"
    joined: list[str] = [str(parts[0])]
    for part in parts[1:]:
        joined += ["+", str(part)]
    run([chain.mkvmerge or "mkvmerge", "-q", "-o", str(excerpt), *joined])
    for part in parts:
        part.unlink(missing_ok=True)
    return excerpt


def run_variant(
    base: config.Config, variant: Variant, excerpt: Path, root: Path, preset: int | None,
) -> Outcome:
    outcome = Outcome(variant)
    cfg_path = variant_config(base, variant, root, preset)
    log_path = cfg_path.parent / "run.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.run(
            [sys.executable, "-m", "autotranscode", "-c", str(cfg_path), "run", str(excerpt)],
            stdout=log_file, stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parent.parent,
        )
    outcome.seconds = time.monotonic() - started
    produced = sorted(cfg_path.parent.glob("*.mkv"))
    if proc.returncode != 0 or not produced:
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        outcome.error = f"exit {proc.returncode}: {' / '.join(tail)}"
        return outcome
    output = produced[0]
    outcome.bytes = output.stat().st_size
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("encoded", type=Path, help="an existing encode of SOURCE")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=3, help="stretches to sample")
    parser.add_argument("--seconds", type=float, default=10.0, help="length of each")
    parser.add_argument("--only", nargs="*", help="variant names to run (default: all)")
    parser.add_argument("--excerpt", type=Path,
                        help="reuse the excerpt.mkv of an earlier sweep: skips stage 1 and "
                             "the cutting, for another round of variants on the same footage")
    parser.add_argument("--variant", action="append", default=[], metavar="SPEC",
                        help="an extra variant, as one quoted string: "
                             "'NAME CRF GRAIN [SVT PARAMS...]', e.g. "
                             "'H-crf30-qm 30 never --enable-qm 1'. Repeatable.")
    parser.add_argument("--smoke", action="store_true",
                        help="plumbing check: 2 s stretches, preset 12, no stage 1 metrics")
    args = parser.parse_args()

    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = config.load(config.default_path())
    chain = tools.detect(base)
    report = Report(root / "report.md", args.source, args.encoded)

    try:
        info = probe.probe(args.source, chain)
        pq = info.color_transfer == "smpte2084"
        fps = info.fps or 24.0
        seconds = 2.0 if args.smoke else args.seconds
        window = round(seconds * fps)

        # Stage 1: where is the existing encode worst?
        report.status = "stage 1: scoring the existing encode frame by frame"
        report.write()
        if args.excerpt:
            starts = []
        elif args.smoke:
            starts = [info.duration * f for f in (0.3, 0.6)][: args.windows]
        else:
            stage1 = verify.compare(chain, args.source, args.encoded, root / "stage1")
            report.stage1 = stage1
            report.write()
            verify.snapshots(chain, args.source, stage1.worst, root / "stage1", "source", pq=pq)
            verify.snapshots(chain, args.encoded, stage1.worst, root / "stage1", "encode", pq=pq)
            # Two complaints, so windows for both: the worst stretches for blocking,
            # and the worst for grain loss, which is a different scene more often
            # than not -- this film lost 30% of its grain in one place and 64% in
            # another.
            margin = max(window, len(stage1.xpsnr) // 50)
            blocky = verify.worst_windows(stage1.xpsnr, window, max(1, args.windows - 1), margin)
            grainless = (verify.worst_windows(stage1.grain_by_frame, window, 1, margin, blocky)
                         if stage1.grain_by_frame and args.windows > 1 else [])
            frames = sorted(blocky + grainless)
            report.window_reasons = {f / fps: ("grain" if f in grainless else "blocking")
                                     for f in frames}
            starts = [frame / fps for frame in frames]
        report.windows, report.window_seconds = starts, seconds

        if args.excerpt:
            excerpt = args.excerpt.expanduser().resolve()
            report.notes.append(f"Stage 1 skipped: reusing the excerpt `{excerpt}`.")
        else:
            report.status = "cutting the sampled stretches"
            report.write()
            excerpt = cut_excerpt(chain, base, args.source, starts, seconds, root)

        # Stage 2: each variant through the real pipeline.
        extra = [parse_variant(spec) for spec in args.variant]
        chosen = [v for v in VARIANTS + extra
                  if not args.only or v.name in args.only or v in extra]
        if args.smoke:
            chosen = [v for v in chosen if v.name.startswith(("A-", "D-", "E-"))]
        preset = 12 if args.smoke else None
        excerpt_seconds = probe.probe(excerpt, chain).duration or 1.0
        for number, variant in enumerate(chosen, start=1):
            report.status = f"stage 2: variant {number}/{len(chosen)} ({variant.name})"
            report.write()
            outcome = run_variant(base, variant, excerpt, root, preset)
            if not outcome.error:
                output = sorted((root / "runs" / variant.name).glob("*.mkv"))[0]
                outcome.kbps = outcome.bytes * 8 / excerpt_seconds / 1000
                try:
                    outcome.result = verify.compare(
                        chain, excerpt, output, root / "work" / variant.name / "verify")
                    outcome.ok = True
                except Exception as exc:     # a failed measurement must not end the sweep
                    outcome.error = f"verify: {exc}"
            report.outcomes.append(outcome)
            report.write()

        # Snapshots at the frames the current settings handle worst.
        first = next((o for o in report.outcomes if o.ok and o.result), None)
        if first and first.result:
            report.status = "writing snapshots"
            report.write()
            frames = first.result.worst
            snaps = root / "snapshots"
            verify.snapshots(chain, excerpt, frames, snaps, "source", pq=pq)
            for o in report.outcomes:
                if o.ok:
                    output = sorted((root / "runs" / o.variant.name).glob("*.mkv"))[0]
                    verify.snapshots(chain, output, frames, snaps, o.variant.name, pq=pq)
            report.snapshots = frames

        shutil.rmtree(root / "work", ignore_errors=True)   # Av1an chunks; the outputs stay
        report.status = f"finished {dt.datetime.now():%Y-%m-%d %H:%M}"
        report.write()
        return 0
    except Exception:
        report.status = "FAILED -- see the traceback in Notes"
        report.notes.append("```\n" + traceback.format_exc() + "```")
        report.write()
        raise


if __name__ == "__main__":
    sys.exit(main())
