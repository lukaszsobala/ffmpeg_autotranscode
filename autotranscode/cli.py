"""Command line interface."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import __version__, config, encode, grain, pipeline, probe, tools
from .state import Ledger
from .util import CommandError, human_size, log, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotranscode",
        description="Transcode a media library to AV1 with source-accurate film grain, "
                    "using Av1an, SVT-AV1, grav1synth and jellyfin-ffmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="path to a TOML config file")
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="repeat for debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    parser.add_argument("--log-file", type=Path, help="also write logs here")

    # Common overrides, so routine runs need no config file edits.
    parser.add_argument("--profile", choices=sorted(config.PROFILES),
                        help="machine profile (overrides the config file)")
    parser.add_argument("--preset", type=int, help="SVT-AV1 preset, -1 (slowest) to 13")
    parser.add_argument("--crf", type=int, help="SVT-AV1 CRF, 1-70")
    parser.add_argument("-w", "--workers", type=int, help="Av1an parallel workers")
    parser.add_argument("-o", "--output-dir", type=Path, help="write outputs here")
    parser.add_argument("--grain", choices=["auto", "always", "never"],
                        help="film grain handling mode")

    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="transcode files, directories or a .txt list")
    run_cmd.add_argument("inputs", nargs="+", type=Path)
    run_cmd.add_argument("--dry-run", action="store_true",
                         help="probe and measure grain, but do not encode")

    plan_cmd = sub.add_parser("plan", help="show what would be done, including exact commands")
    plan_cmd.add_argument("inputs", nargs="+", type=Path)

    measure_cmd = sub.add_parser(
        "measure", help="report grain scores only, for calibrating grain.threshold_db"
    )
    measure_cmd.add_argument("inputs", nargs="+", type=Path)

    sub.add_parser("doctor", help="check that the required tools are present and capable")
    sub.add_parser("status", help="summarise the state ledger")

    return parser


def apply_overrides(cfg: config.Config, args: argparse.Namespace) -> None:
    if args.profile:
        config._merge_profile(cfg, args.profile)
        cfg.machine.profile = args.profile
    if args.preset is not None:
        cfg.encode.preset = args.preset
    if args.crf is not None:
        cfg.encode.crf = args.crf
    if args.workers is not None:
        cfg.machine.workers = args.workers
    if args.output_dir is not None:
        cfg.output.directory = str(args.output_dir)
    if args.grain is not None:
        cfg.grain.mode = args.grain
    config.validate(cfg)


def cmd_doctor(cfg: config.Config, chain: tools.Toolchain) -> int:
    print(tools.describe(chain))
    print()
    try:
        tools.require(chain, cfg)
    except tools.ToolError as exc:
        print(exc)
        print("\nSee scripts/bootstrap-rk3588.sh for a working install sequence.")
        return 1
    print("All prerequisites satisfied.")
    if not chain.has_grain_pipeline:
        print("Note: grain pipeline unavailable; encoding will proceed without it.")
    return 0


def cmd_measure(inputs: list[Path], cfg: config.Config, chain: tools.Toolchain) -> int:
    sources = pipeline.discover(inputs, cfg)
    if not sources:
        log.error("no media files found")
        return 1

    print(f"{'file':<48} {'PSNR dB':>8}  {'grainy':<7} denoise")
    print("-" * 96)
    for source in sources:
        try:
            info = probe.probe(source, chain)
            work = cfg.work_dir / "measure"
            work.mkdir(parents=True, exist_ok=True)
            report = grain.measure(info, chain, cfg, work)
        except (CommandError, OSError, ValueError) as exc:
            print(f"{source.name[:48]:<48} {'error':>8}  {str(exc)[:40]}")
            continue
        name = source.name if len(source.name) <= 48 else source.name[:45] + "..."
        print(f"{name:<48} {report.mean_psnr:>8.2f}  "
              f"{report.grainy!s:<7} {report.denoise_filter or '-'}")

    print(
        f"\nCurrent threshold: grain.threshold_db = {cfg.grain.threshold_db}\n"
        "Files below it take the denoise + grain-synthesis route. Adjust the threshold\n"
        "so it lands between your visibly clean and visibly grainy titles."
    )
    return 0


def cmd_plan(inputs: list[Path], cfg: config.Config, chain: tools.Toolchain) -> int:
    sources = pipeline.discover(inputs, cfg)
    if not sources:
        log.error("no media files found")
        return 1

    for source in sources:
        print(f"\n=== {source}")
        try:
            info = probe.probe(source, chain)
        except (CommandError, ValueError) as exc:
            print(f"  cannot probe: {exc}")
            continue
        print(f"  {info.describe()}, {human_size(info.size_bytes)}")
        out = pipeline.output_path(info, cfg)
        try:
            pipeline.check_skip(info, out, cfg)
        except pipeline.SkipFile as exc:
            print(f"  SKIP: {exc}")
            continue
        print(f"  output: {out}")
        argv = encode.build_command(info, Path("<encoded.mkv>"), chain, cfg,
                                    Path("<temp>"), "<denoise-if-grainy>")
        print(f"  encode: {shlex.join(argv)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(0 if args.quiet else args.verbose, args.log_file)

    try:
        cfg = config.load(args.config)
        apply_overrides(cfg, args)
    except config.ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    try:
        chain = tools.detect(cfg)
    except tools.ToolError as exc:
        log.error("%s", exc)
        return 2

    if args.command == "doctor":
        return cmd_doctor(cfg, chain)

    if args.command == "status":
        print(Ledger(cfg.state_path).summary())
        return 0

    if args.command == "measure":
        return cmd_measure(args.inputs, cfg, chain)

    if args.command == "plan":
        return cmd_plan(args.inputs, cfg, chain)

    # run
    try:
        tools.require(chain, cfg)
    except tools.ToolError as exc:
        log.error("%s", exc)
        log.error("run 'autotranscode doctor' for details")
        return 2

    sources = pipeline.discover(args.inputs, cfg)
    if not sources:
        log.error("no media files found")
        return 1

    log.info("%d file(s) queued, profile=%s preset=%d crf=%d grain=%s",
             len(sources), cfg.machine.profile, cfg.encode.preset,
             cfg.encode.crf, cfg.grain.mode)

    ledger = Ledger(cfg.state_path)
    try:
        failures = pipeline.process_all(sources, chain, cfg, ledger, dry_run=args.dry_run)
    except KeyboardInterrupt:
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
