"""Command line interface."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import shlex
import subprocess
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
    regrain,
    sample,
    tools,
    update,
    verify,
    versions,
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
    parser.add_argument("--deinterlace", choices=["off", "auto", "tff", "bff"],
                        help="deinterlace: auto for files flagged interlaced, tff or bff "
                             "to force it with that field order")

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
                                "output.directory; and `sample --out` directories outside "
                                "the work directory")
    clean_cmd.add_argument("-n", "--dry-run", action="store_true",
                           help="list what would be removed and kept, remove nothing")
    clean_cmd.add_argument("--unknown", action="store_true",
                           help="also remove work whose source cannot be told")
    clean_cmd.add_argument("--all", action="store_true", dest="everything",
                           help="also remove resumable work: every film starts again")
    clean_cmd.add_argument("--samples", action="store_true",
                           help="also remove `sample`'s results: reports, excerpts, encodes "
                                "and snapshots")

    regrain_cmd = sub.add_parser(
        "regrain",
        help="give films already encoded the grain a run makes now, estimated and "
             "calibrated scene by scene: no new encode, each file replaced in place",
    )
    regrain_cmd.add_argument("inputs", nargs="*", type=Path,
                             help="outputs, or folders of them (default: every finished "
                                  "film in the ledger)")
    regrain_cmd.add_argument("-n", "--dry-run", action="store_true",
                             help="list the films and their sources, change nothing")
    regrain_cmd.add_argument("--force", action="store_true",
                             help="also films whose grain the current method made already")

    sub.add_parser("doctor", help="check that the required tools are present and capable")

    update_cmd = sub.add_parser(
        "update",
        help="say whether there is a newer autotranscode and which tools it updates, "
             "and queue it to install once the film in progress is finished",
    )
    update_cmd.add_argument("--check", action="store_true",
                            help="only say whether there is an update (exit code 1 if so)")
    update_cmd.add_argument("-y", "--yes", action="store_true",
                            help="queue it (or install it, with nothing to finish) "
                                 "without asking")
    update_cmd.add_argument("--cancel", action="store_true", help="unqueue a queued update")
    sub.add_parser("status", help="summarise the state ledger")

    return parser


def load_config(args: argparse.Namespace) -> config.Config:
    """The config file, with the flags on top: defaults, profile, file, flags."""
    cfg = config.load(args.config or config.default_path(), profile=args.profile)
    apply_overrides(cfg, args)
    return cfg


def apply_overrides(cfg: config.Config, args: argparse.Namespace) -> None:
    """The flags other than --profile, which `config.load` applies before the file."""
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
    if args.deinterlace is not None:
        cfg.encode.deinterlace = args.deinterlace
    config.validate(cfg)


def require(chain: tools.Toolchain, cfg: config.Config) -> None:
    """Everything a transcode needs: the tools, their capabilities, their versions."""
    tools.require(chain, cfg)
    versions.require(chain, cfg)


def cmd_doctor(cfg: config.Config, chain: tools.Toolchain) -> int:
    print(f"profile     {cfg.machine.profile}")
    print(tools.describe(chain))
    checks = versions.check(chain)
    print(f"\nautotranscode {__version__} was tested with:")
    print(versions.describe(checks))
    print()
    failed = 0
    try:
        tools.require(chain, cfg)
    except tools.ToolError as exc:
        print(exc)
        failed = 1
    try:
        versions.require(chain, cfg, checks)
    except tools.ToolError as exc:
        print(exc)
        failed = 1
    if stages := versions.fixes(checks):
        print(f"\nTo install the tested versions: ./scripts/bootstrap.sh {stages}")
    if failed:
        print("\nSee scripts/bootstrap.sh for a working install sequence.")
        return 1
    if any(item.failed and item.pin.enforce for item in checks):
        print("All prerequisites present, at versions not tested "
              "(tools.versions = \"warn\").")
    else:
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
    sample_sets = [path for path in args.inputs if (path / clean.PICKS).is_file()]
    library = [path for path in args.inputs if path not in sample_sets]
    sources = pipeline.discover(library, cfg) if library else []
    entries = clean.plan(cfg, chain, Ledger(cfg.state_path), sources,
                         pipeline.library_roots(library),
                         unknown=args.unknown, everything=args.everything,
                         sample_sets=sample_sets, samples=args.samples)
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
        # The deinterlacing a run with these settings gives the encode.
        deinterlaced = encode.deinterlace_filter(info, cfg)
        result = verify.compare(chain, args.reference, args.encoded, work,
                                grain=not args.no_grain, frames=info.frames,
                                reference_filter=deinterlaced)
    except (CommandError, OSError, ValueError) as exc:
        log.error("cannot verify: %s", str(exc)[:300])
        return 1
    print(result.describe())
    print(f"worst frames: {', '.join(map(str, result.worst))}")
    if args.snapshots:
        pq = info.color_transfer == "smpte2084"
        verify.snapshots(chain, args.reference, result.worst, args.snapshots, "source", pq=pq,
                         prefilter=deinterlaced)
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
    ledger = Ledger(cfg.state_path)
    sources, _ = pipeline.leave_out_outputs(pipeline.discover(inputs, cfg), ledger)
    if not sources:
        log.error("no media files found")
        return 1

    roots = pipeline.library_roots(inputs)
    work = cfg.work_dir / "estimate"
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
    ledger = Ledger(cfg.state_path)
    sources, _ = pipeline.leave_out_outputs(pipeline.discover(inputs, cfg), ledger)
    if not sources:
        log.error("no media files found")
        return 1
    roots = pipeline.library_roots(inputs)
    sources, _ = pipeline.by_estimate(sources, ledger, cfg, chain,
                                      pipeline.under_way(sources, cfg))
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
        cfg = load_config(args)
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

    if args.command == "update":
        # The new code checks the new toolchain, in a process of its own.
        doctor = [sys.executable, "-m", "autotranscode",
                  *(["-c", str(args.config)] if args.config else []), "doctor"]
        return update.main(cfg, chain, check_only=args.check, yes=args.yes, cancel=args.cancel,
                           doctor=lambda: subprocess.run(doctor, check=False).returncode)

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
                require(chain, cfg)
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

    if args.command == "regrain":
        if not args.dry_run:
            try:
                require(chain, cfg)
            except tools.ToolError as exc:
                log.error("%s", exc)
                return 2
        try:
            return regrain.main(cfg, chain, args.inputs, dry_run=args.dry_run,
                                force=args.force)
        except KeyboardInterrupt:
            return 130

    # run
    try:
        require(chain, cfg)
    except tools.ToolError as exc:
        if args.dry_run or not update.queued(cfg):
            log.error("%s", exc)
            log.error("run 'autotranscode doctor' for details")
            return 2
        # Nothing can be encoded with these tools, so there is no film to finish
        # first: install the queued update now. It is unqueued as it starts, so a
        # failing one is not tried again by the command it starts again.
        log.warning("%s", exc)
        try:
            with pipeline.only_run(cfg):
                update.install_queued(cfg, chain)
        except pipeline.AlreadyRunning as running:
            log.error("%s; not starting a second one", running)
            return 1
        return 2

    # A dry run writes nothing, so it may look ahead while a run is going.
    try:
        with contextlib.nullcontext() if args.dry_run else pipeline.only_run(cfg):
            try:
                return _run(args, cfg, chain)
            except update.InstallQueued:
                # Still holding the work directory; exec starts this command again.
                update.install_queued(cfg, chain)
                return 0
    except pipeline.AlreadyRunning as exc:
        log.error("%s; not starting a second one", exc)
        return 1


def _run(args: argparse.Namespace, cfg: config.Config, chain: tools.Toolchain) -> int:
    ledger = Ledger(cfg.state_path)
    sources, outputs = pipeline.leave_out_outputs(pipeline.discover(args.inputs, cfg), ledger)
    if outputs:
        log.info("%d output(s) of earlier runs found with the sources, left out", outputs)
    if not sources:
        log.error("no media files found")
        return 1

    curved = cfg.encode.crf_per_doubling or cfg.encode.crf_drop_at_720
    crf = f"{cfg.encode.crf} at 4K, less below" if curved else str(cfg.encode.crf)
    log.info("%d file(s) queued, profile=%s preset=%d crf=%s grain=%s",
             len(sources), cfg.machine.profile, cfg.encode.preset, crf, cfg.grain.mode)

    if not args.dry_run and (note := update.queued_note(cfg)):
        log.info("%s", note)
    started = pipeline.under_way(sources, cfg)
    sources, estimated = pipeline.by_estimate(sources, ledger, cfg, chain, started)
    if started:
        log.info("%d of them started on an earlier run: those go first", len(started))
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
