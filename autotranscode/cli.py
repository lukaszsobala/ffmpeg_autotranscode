"""Command line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
import sys
from pathlib import Path

from . import (
    __version__,
    clean,
    config,
    encode,
    estimate,
    grain,
    pipeline,
    probe,
    sample,
    tools,
    verify,
    watch,
)
from .state import Guess, Ledger
from .util import (
    CommandError,
    child_locale,
    human_duration,
    human_size,
    log,
    setup_logging,
    unusable_locale_vars,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotranscode",
        description="Transcode a media library to AV1 with source-accurate film grain, "
                    "using Av1an, SVT-AV1, grav1synth and jellyfin-ffmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path,
                        help="path to a TOML config file "
                             "(default: ~/.config/autotranscode.toml if it exists)")
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="repeat for debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    parser.add_argument("--log-file", type=Path, help="also write logs here")

    # Common overrides, so routine runs need no config file edits.
    parser.add_argument("--profile", choices=["auto", *sorted(config.PROFILES)],
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

    estimate_cmd = sub.add_parser(
        "estimate",
        help="encode a few short excerpts of each file and rank what is worth transcoding",
    )
    estimate_cmd.add_argument("inputs", nargs="+", type=Path)
    estimate_cmd.add_argument("--samples", type=int,
                              help="excerpts per file (default: 3-6, by runtime)")
    estimate_cmd.add_argument("--seconds", type=int, help="seconds per excerpt")
    estimate_cmd.add_argument("--no-grain-probe", action="store_true",
                              help="skip the grain check (it only runs with "
                                   "grain.denoise_before_encode on)")

    sample_cmd = sub.add_parser(
        "sample",
        help="encode a few random minutes of a few random files per resolution band, "
             "to check the settings on more than the films they were tuned on",
    )
    sample_cmd.add_argument("inputs", nargs="+", type=Path)
    sample_cmd.add_argument("-n", "--per-band", type=int, default=3,
                            help="files to sample per band (default: 3)")
    sample_cmd.add_argument("--band", action="append", choices=list(sample.BAND_NAMES.values()),
                            help="only this band; repeatable (default: every band)")
    sample_cmd.add_argument("--minutes", type=float, default=4.0,
                            help="length of each excerpt (default: 4)")
    sample_cmd.add_argument("--skip-start", type=float, default=10.0, metavar="MINUTES",
                            help="never sample the first this many minutes (default: 10; "
                                 f"at most {100 * sample.MAX_SKIP_SHARE:.0f}%% of a file)")
    sample_cmd.add_argument("--skip-end", type=float, default=10.0, metavar="MINUTES",
                            help="never sample the last this many minutes (default: 10; "
                                 f"at most {100 * sample.MAX_SKIP_SHARE:.0f}%% of a file)")
    sample_cmd.add_argument("--seed", type=int, help="repeat an earlier draw")
    sample_cmd.add_argument("--out", type=Path,
                            help="where the samples and report.md go; rerun into the same "
                                 "directory to resume (default: <work_dir>/samples/<time>)")
    sample_cmd.add_argument("--pick-only", action="store_true",
                            help="choose the samples and stop, to see them before encoding")
    sample_cmd.add_argument("--no-verify", action="store_true",
                            help="skip the scores and snapshots")

    verify_cmd = sub.add_parser(
        "verify",
        help="score an encode against its source where the eye looks: flat areas and grain",
    )
    verify_cmd.add_argument("reference", type=Path, help="the source")
    verify_cmd.add_argument("encoded", type=Path, help="the encode of it")
    verify_cmd.add_argument("--snapshots", type=Path,
                            help="write the worst frames of both files here as PNGs")
    verify_cmd.add_argument("--no-grain", action="store_true",
                            help="skip the grain retention pass (it decodes both files again)")

    clean_cmd = sub.add_parser(
        "clean",
        help="remove work directories, staged copies and partial outputs no run will use "
             "again; resumable work is kept",
    )
    clean_cmd.add_argument("inputs", nargs="*", type=Path,
                           help="the library, to match work directories that do not name "
                                "their source and to find outputs in a mirrored "
                                "output.directory")
    clean_cmd.add_argument("-n", "--dry-run", action="store_true",
                           help="list what would be removed and kept, remove nothing")
    clean_cmd.add_argument("--unknown", action="store_true",
                           help="also remove work whose source cannot be told")
    clean_cmd.add_argument("--all", action="store_true", dest="everything",
                           help="also remove resumable work: every film starts again")

    sub.add_parser("doctor", help="check that the required tools are present and capable")
    sub.add_parser("status", help="summarise the state ledger")

    return parser


def apply_overrides(cfg: config.Config, args: argparse.Namespace) -> None:
    if args.profile:
        profile = config.resolve_profile(args.profile)
        config._merge_profile(cfg, profile)
        cfg.machine.profile = profile
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
    print(f"profile     {cfg.machine.profile}")
    print(tools.describe(chain))
    print()
    try:
        tools.require(chain, cfg)
    except tools.ToolError as exc:
        print(exc)
        print("\nSee scripts/bootstrap.sh for a working install sequence.")
        return 1
    print("All prerequisites satisfied.")
    if not chain.has_grain_pipeline:
        print("Note: grain pipeline unavailable; encoding will proceed without it.")
    if broken := unusable_locale_vars():
        # Not fatal, and deliberately not an error: runs are unaffected because
        # every child gets LC_ALL forced. Say so anyway, because by-hand commands
        # in this shell will abort and the message they give blames nothing useful.
        print("\nNote: this shell names locales the machine has not generated:")
        for variable in broken:
            print(f"  {variable}")
        print(f"Transcodes are unaffected -- every tool is run with LC_ALL={child_locale()}.")
        print("Running these tools by hand in this shell aborts with")
        print('  locale::facet::_S_create_c_locale name not valid')
        print("Usually SSH importing the client's LC_* through AcceptEnv. Either unset")
        print("them in your shell, stop the client sending them, or generate the")
        print("locales with: sudo dpkg-reconfigure locales")
    return 0


def cmd_clean(args: argparse.Namespace, cfg: config.Config, chain: tools.Toolchain) -> int:
    sources = pipeline.discover(args.inputs, cfg) if args.inputs else []
    entries = clean.plan(cfg, chain, Ledger(cfg.state_path), sources,
                         pipeline.library_roots(args.inputs),
                         unknown=args.unknown, everything=args.everything)
    if not entries:
        print("nothing to clean")
        return 0
    print(clean.report(entries))
    if args.dry_run:
        print("dry run: nothing removed")
        return 0
    print(f"removed {human_size(clean.remove(entries))}")
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

    print(f"\nCurrent threshold: grain.threshold_db = {cfg.grain.threshold_db}")
    if grain.needs_probe(cfg):
        print("Files below it take the grain route. Adjust the threshold so it lands\n"
              "between your visibly clean and visibly grainy titles.")
    else:
        print("With these settings the score decides nothing: every film gets a grain\n"
              "table, and calibration after the encode decides how much to keep. It only\n"
              "matters with grain.estimator = \"grav1synth\" or grain.denoise_before_encode.")
    return 0


def cmd_verify(args: argparse.Namespace, cfg: config.Config, chain: tools.Toolchain) -> int:
    try:
        info = probe.probe(args.reference, chain)
        work = cfg.work_dir / "verify"
        result = verify.compare(chain, args.reference, args.encoded, work,
                                grain=not args.no_grain, frames=info.frames)
    except (CommandError, OSError, ValueError) as exc:
        log.error("cannot verify: %s", str(exc)[:300])
        return 1
    print(result.describe())
    print(f"worst frames: {', '.join(map(str, result.worst))}")
    if args.snapshots:
        pq = info.color_transfer == "smpte2084"
        verify.snapshots(chain, args.reference, result.worst, args.snapshots, "source", pq=pq)
        verify.snapshots(chain, args.encoded, result.worst, args.snapshots, "encode", pq=pq)
        print(f"snapshots in {args.snapshots}")
    return 0


def cmd_estimate(inputs: list[Path], cfg: config.Config, chain: tools.Toolchain) -> int:
    """Sample each file, then rank by what a full encode would buy.

    Ordered by bytes saved per hour of encoding rather than by bytes saved: when
    a 4K feature can take many hours, the question is not which file is
    biggest but which hour of encoding is best spent.

    Each result is kept in the ledger. `run` works through a library in the same
    order and skips the files heading below the minimum savings.
    """
    sources = pipeline.discover(inputs, cfg)
    if not sources:
        log.error("no media files found")
        return 1

    roots = pipeline.library_roots(inputs)
    work = cfg.work_dir / "estimate"
    ledger = Ledger(cfg.state_path)
    key = pipeline.size_key(cfg, chain)
    results: list[estimate.Estimate] = []
    skipped = 0
    for index, source in enumerate(sources, start=1):
        log.info("[%d/%d] %s", index, len(sources), source.name)
        try:
            info = probe.probe(source, chain)
            # The same rules `run` applies, so an estimate over a library does not
            # spend minutes sampling files that would never be transcoded.
            pipeline.check_skip(info, pipeline.output_path(info, cfg, roots), cfg)
            result = estimate.estimate(info, chain, cfg, work)
        except pipeline.SkipFile as exc:
            log.info("  skip: %s", exc)
            skipped += 1
            continue
        except (CommandError, OSError, ValueError) as exc:
            log.warning("  cannot estimate: %s", str(exc)[:200])
            continue
        if not result.ok:
            log.warning("  no usable samples; skipping")
            continue
        results.append(result)
        ledger.record_guess(source, Guess(
            source=str(source), source_bytes=info.size_bytes,
            projected_bytes=result.projected_bytes,
            encode_seconds=result.projected_encode_seconds, crf=result.crf, settings=key,
            estimated_at=dt.datetime.now().isoformat(timespec="seconds"),
        ))
        log.info("  %s -> %s (%+.0f%%), %.1f fps, about %s to encode",
                 human_size(info.size_bytes), human_size(result.projected_bytes),
                 -result.saving_percent, result.fps,
                 human_duration(result.projected_encode_seconds))

    if not results:
        log.error("nothing could be estimated")
        return 1

    results.sort(key=lambda r: r.saved_per_hour, reverse=True)
    print(f"\n{'file':<40} {'size':>10} {'estimate':>10} {'saving':>8} "
          f"{'encode':>9} {'saved/h':>9}  crf")
    print("-" * 94)
    for result in results:
        name = result.info.path.name
        name = name if len(name) <= 40 else name[:37] + "..."
        print(f"{name:<40} {human_size(result.info.size_bytes):>10} "
              f"{human_size(result.projected_bytes):>10} {result.saving_percent:>7.0f}% "
              f"{human_duration(result.projected_encode_seconds):>9} "
              f"{human_size(int(result.saved_per_hour)):>9}  {encode.crf_text(result.crf)}"
              f"{' +grain' if result.denoise else ''}")

    saved = sum(r.saved_bytes for r in results)
    hours = sum(r.projected_encode_seconds for r in results) / 3600
    short = [r for r in results
             if watch.shortfall(cfg, r.info.size_bytes, r.projected_bytes) is not None]
    if skipped:
        print(f"\n{skipped} file(s) skipped by the same rules `run` uses.")
    if short:
        print(f"{len(short)} file(s) fall short of the minimum saving; `run` will skip them.")
    print(f"\n{len(results)} file(s): {human_size(sum(r.info.size_bytes for r in results))} "
          f"-> {human_size(sum(r.projected_bytes for r in results))}, "
          f"saving {human_size(saved)} for about {hours:.0f}h of encoding.")
    print("Sampled, not encoded: good for ranking, a few percent out on any one file.")
    return 0


def cmd_plan(inputs: list[Path], cfg: config.Config, chain: tools.Toolchain) -> int:
    sources = pipeline.discover(inputs, cfg)
    if not sources:
        log.error("no media files found")
        return 1
    roots = pipeline.library_roots(inputs)
    ledger = Ledger(cfg.state_path)
    sources, _ = pipeline.by_estimate(sources, ledger, cfg, chain)
    keys = pipeline.size_keys(cfg, chain)

    for source in sources:
        print(f"\n=== {source}")
        try:
            info = probe.probe(source, chain)
        except (CommandError, ValueError) as exc:
            print(f"  cannot probe: {exc}")
            continue
        print(f"  {info.describe()}, {human_size(info.size_bytes)}")
        out = pipeline.output_path(info, cfg, roots)
        try:
            pipeline.check_skip(info, out, cfg)
        except pipeline.SkipFile as exc:
            print(f"  SKIP: {exc}")
            continue
        if guess := ledger.guess(source, keys):
            if short := watch.shortfall(cfg, guess.source_bytes, guess.projected_bytes):
                print(f"  SKIP: estimated to come out {short}")
                continue
            print(f"  estimate: {human_size(guess.projected_bytes)}, "
                  f"{human_size(int(guess.saved_per_hour))} saved per hour")
        print(f"  output: {out}")
        argv = encode.build_command(info, Path("<encoded.mkv>"), chain, cfg,
                                    Path("<temp>"), "<denoise-if-grainy>",
                                    Path("<scenes.json>"))
        # The LC_ALL prefix is not decoration: it is what a real run sets, and
        # without it this command aborts in any shell holding a locale the machine
        # has not generated. Printing it makes the line safe to paste.
        print(f"  encode: LC_ALL={child_locale()} {shlex.join(argv)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(0 if args.quiet else args.verbose, args.log_file)

    chosen = args.config or config.default_path()
    try:
        cfg = config.load(chosen)
        apply_overrides(cfg, args)
    except config.ConfigError as exc:
        log.error("config error: %s", exc)
        return 2
    log.debug("config: %s", chosen or "built-in defaults (no file found)")

    try:
        chain = tools.detect(cfg)
    except tools.ToolError as exc:
        log.error("%s", exc)
        return 2

    if args.command == "doctor":
        return cmd_doctor(cfg, chain)

    if args.command == "clean":
        return cmd_clean(args, cfg, chain)

    if args.command == "status":
        print(Ledger(cfg.state_path).summary())
        return 0

    if args.command == "measure":
        return cmd_measure(args.inputs, cfg, chain)

    if args.command == "verify":
        return cmd_verify(args, cfg, chain)

    if args.command == "estimate":
        if args.samples is not None:
            cfg.estimate.samples = args.samples
        if args.seconds is not None:
            cfg.estimate.sample_seconds = args.seconds
        if args.no_grain_probe:
            cfg.estimate.grain_probe = False
        return cmd_estimate(args.inputs, cfg, chain)

    if args.command == "sample":
        if args.per_band < 1 or args.minutes <= 0 or min(args.skip_start, args.skip_end) < 0:
            log.error("--per-band and --minutes must be positive, the skips not negative")
            return 2
        if not args.pick_only:
            try:
                tools.require(chain, cfg)
            except tools.ToolError as exc:
                log.error("%s", exc)
                return 2
        out = args.out or cfg.work_dir / "samples" / f"{dt.datetime.now():%Y%m%d-%H%M}"
        bands = args.band or [sample.band_name(edge) for edge in sample.band_edges()]
        try:
            return sample.run_samples(
                args.inputs, chain, cfg, out.expanduser().resolve(),
                per_band=args.per_band, bands=bands, minutes=args.minutes,
                skip_start=args.skip_start, skip_end=args.skip_end, seed=args.seed,
                score=not args.no_verify, pick_only=args.pick_only)
        except KeyboardInterrupt:
            log.warning("interrupted; rerun with --out %s to resume", out)
            return 130

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

    curved = cfg.encode.crf_per_doubling or cfg.encode.crf_drop_at_720
    crf = f"{cfg.encode.crf} at 4K, less below" if curved else str(cfg.encode.crf)
    log.info("%d file(s) queued, profile=%s preset=%d crf=%s grain=%s",
             len(sources), cfg.machine.profile, cfg.encode.preset, crf, cfg.grain.mode)

    ledger = Ledger(cfg.state_path)
    sources, estimated = pipeline.by_estimate(sources, ledger, cfg, chain)
    if estimated:
        log.info("%d of them estimated: those go first, most saved per hour of encoding "
                 "at the top", estimated)
    try:
        failures = pipeline.process_all(sources, chain, cfg, ledger, dry_run=args.dry_run,
                                        roots=pipeline.library_roots(args.inputs))
    except KeyboardInterrupt:
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
