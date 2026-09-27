"""Encode random minutes of random films, a few per resolution band, for the eye.

Every setting in the resolution table was tuned on one film per band, so each
row is only as good as that film was typical. This checks them against the rest
of a library: pick a few titles at random from each band, cut a few minutes from
a random point in each, run the real pipeline over them, and put the excerpt,
its encode, the scores and snapshots of the worst frames side by side.

Random, but not naive:

- Files are drawn folder by folder, so a series of 80 episodes cannot fill a
  band before a film in a folder of its own is looked at twice.
- Openings and end credits are skipped, since they are not what anyone tunes for.
- A file is only a candidate if `run` would transcode it, by the same rules.
- The picks are written down before anything is encoded, so a rerun into the
  same directory resumes the same samples instead of drawing new ones.

The excerpts are cut by stream copy, video only, so each is bit for bit the
source and its size compares directly with the encode's.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
import shutil
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import encode, pipeline, probe, verify
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, human_duration, human_size, log, run

# Names for the lower edge of each width band, as the README's table calls them.
BAND_NAMES = {3000: "4K", 1700: "1080p", 1100: "720p", 0: "SD"}

# Never skip more than this share of a file at either end: a 22-minute episode
# asked to skip ten minutes each way would otherwise have nothing left.
MAX_SKIP_SHARE = 0.15


@dataclass(slots=True)
class Pick:
    """One sample: which file, from where, and what it was when picked."""

    id: str
    band: str
    source: str
    start: float
    length: float
    width: int
    height: int
    codec: str
    hdr: bool
    duration: float


@dataclass(slots=True)
class Result:
    """What encoding one pick produced, kept beside it as result.json."""

    id: str
    ok: bool = False
    error: str = ""
    crf: int = 0
    svt: list[str] = field(default_factory=list)
    source_bytes: int = 0
    encoded_bytes: int = 0
    seconds_long: float = 0.0     # the excerpt's own length, keyframe-aligned
    encode_seconds: float = 0.0   # the whole pipeline: grain work, encode and mux
    frames: int = 0
    lowpass_mean: float | None = None
    lowpass_p5: float | None = None
    lowpass_min: float | None = None
    grain_retention: float | None = None
    worst: list[int] = field(default_factory=list)

    @property
    def saving(self) -> float:
        return 100.0 * (1 - self.encoded_bytes / self.source_bytes) if self.source_bytes else 0.0

    def kbps(self, size: int) -> float:
        return size * 8 / self.seconds_long / 1000 if self.seconds_long else 0.0



def band_edges(cfg: Config) -> list[int]:
    """Lower edges of the bands, widest first: the CRF ladder's, or the defaults'."""
    keys = cfg.encode.crf_by_width or {str(edge): 0 for edge in BAND_NAMES}
    return sorted((int(key) for key in keys), reverse=True)


def band_name(edge: int) -> str:
    return BAND_NAMES.get(edge, f"{edge}px+")


def band_of(info: MediaInfo, cfg: Config) -> str:
    """The band `info` is encoded as, named: the same rule the settings use."""
    width = encode.band_width(info)
    edges = band_edges(cfg)
    return band_name(next((edge for edge in edges if width >= edge), edges[-1]))


def choose_start(
    duration: float, length: float, skip_start: float, skip_end: float, rng: random.Random,
) -> float | None:
    """A random start leaving `skip_start` and `skip_end` alone, or None if it cannot fit.

    Both skips are capped at MAX_SKIP_SHARE of the runtime, so a short episode
    still yields a sample from its middle rather than being passed over.
    """
    head = min(skip_start, duration * MAX_SKIP_SHARE)
    tail = min(skip_end, duration * MAX_SKIP_SHARE)
    latest = duration - tail - length
    if latest < head:
        return None
    return rng.uniform(head, latest)


def draw_order(sources: list[Path], rng: random.Random) -> list[Path]:
    """Sources in random order, spread across folders.

    One file from every folder in turn, the folders in random order, then a
    second from each, and so on. A band is filled from as many different
    folders as the library can offer before any folder gives two.
    """
    folders: dict[Path, list[Path]] = defaultdict(list)
    for path in sources:
        folders[path.parent].append(path)
    for files in folders.values():
        rng.shuffle(files)
    order = list(folders)
    rng.shuffle(order)
    deepest = max((len(files) for files in folders.values()), default=0)
    return [folders[folder][depth] for depth in range(deepest)
            for folder in order if depth < len(folders[folder])]


def pick(
    sources: list[Path], chain: Toolchain, cfg: Config, *, per_band: int, bands: list[str],
    length: float, skip_start: float, skip_end: float, rng: random.Random,
) -> list[Pick]:
    """Probe files in random order until every wanted band has `per_band` picks.

    Only as many files are probed as it takes, so a large library is not
    probed end to end to find a handful of samples.
    """
    wanted = {name: per_band for name in bands}
    passed_over: dict[str, int] = defaultdict(int)
    picks: list[Pick] = []
    for number, path in enumerate(draw_order(sources, rng), start=1):
        if not any(wanted.values()):
            break
        if number % 25 == 0:
            log.info("  probed %d of %d files; still wanted: %s", number, len(sources),
                     ", ".join(f"{n} {band}" for band, n in wanted.items() if n) or "none")
        try:
            info = probe.probe(path, chain)
        except (CommandError, OSError, ValueError) as exc:
            log.debug("cannot probe %s: %s", path, exc)
            continue
        band = band_of(info, cfg)
        if not wanted.get(band):
            continue
        try:
            # A path that cannot exist, so only the rules about the file itself
            # apply, not whether the library already holds its transcode.
            pipeline.check_skip(info, Path("/nonexistent/sample.mkv"), cfg)
        except pipeline.SkipFile as exc:
            log.debug("not a candidate, %s: %s", path.name, exc)
            passed_over[band] += 1
            continue
        start = choose_start(info.duration, length, skip_start, skip_end, rng)
        if start is None:
            log.debug("too short to sample: %s", path.name)
            passed_over[band] += 1
            continue
        index = per_band - wanted[band] + 1
        wanted[band] -= 1
        picks.append(Pick(
            id=f"{band}-{index}", band=band, source=str(path), start=round(start, 3),
            length=length, width=info.width, height=info.height, codec=info.codec,
            hdr=info.is_hdr, duration=info.duration,
        ))
        log.info("  %s: %s at %s", picks[-1].id, path.name, human_duration(start))
    for band, missing in wanted.items():
        if missing:
            log.warning("  only %d of %d %s sample(s): %d more %s file(s) found, but `run` "
                        "would skip them or they are too short (-vv says which)",
                        per_band - missing, per_band, band, passed_over[band], band)
    order = {name: position for position, name in enumerate(bands)}
    return sorted(picks, key=lambda p: (order[p.band], p.id))


def cut(chain: Toolchain, pick_: Pick, out: Path) -> None:
    """The picked minutes of the source, stream-copied: the source's own bits.

    The cut starts at the keyframe at or before `start`, so it may begin a few
    seconds early; the result records the excerpt's real length.
    """
    partial = out.with_name(out.name + ".part")
    run([chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-ss", f"{pick_.start:.3f}", "-i", pick_.source, "-t", f"{pick_.length:.3f}",
         "-map", "0:v:0", "-c", "copy", "-f", "matroska", str(partial)])
    partial.replace(out)


def sample_config(cfg: Config, directory: Path, work: Path) -> Config:
    """The live settings, with output and bookkeeping pointed at the sample."""
    local = copy.deepcopy(cfg)
    local.output.directory = str(directory)
    local.output.min_saving_percent = -1e9   # keep every result, however it turns out
    local.output.replace_source = False
    local.library.min_duration_sec = 0.0      # the excerpt is short by design
    local.library.min_bits_per_pixel = 0.0    # the whole file already passed this
    local.staging.enabled = False
    local.paths.work_dir = str(work)
    return local


def encode_pick(
    pick_: Pick, chain: Toolchain, cfg: Config, root: Path, *, score: bool,
) -> Result:
    directory = root / pick_.id
    directory.mkdir(parents=True, exist_ok=True)
    saved = directory / "result.json"
    previous = Result(**json.loads(saved.read_text(encoding="utf-8"))) \
        if saved.is_file() else None
    if previous and previous.ok and (previous.lowpass_mean is not None or not score):
        return previous

    result = Result(pick_.id)
    stem = Path(pick_.source).stem
    excerpt = directory / f"{stem}.{int(pick_.start)}s.mkv"
    try:
        if not excerpt.is_file():
            cut(chain, pick_, excerpt)
        info = probe.probe(excerpt, chain)
        local = sample_config(cfg, directory, root / "work")
        result.crf = encode.effective_crf(info, local)
        result.svt = encode.svt_params(info, local, chain)
        result.source_bytes = excerpt.stat().st_size
        result.seconds_long = info.duration
        result.frames = info.frames or round(info.duration * info.fps)

        encoded = pipeline.output_path(info, local)
        if encoded.is_file():   # encoded by an earlier run that stopped before scoring
            result.encode_seconds = previous.encode_seconds if previous else 0.0
        else:
            result.encode_seconds = pipeline.process_one(excerpt, chain, local).seconds
        result.encoded_bytes = encoded.stat().st_size
        result.ok = True
        saved.write_text(json.dumps(asdict(result), indent=1), encoding="utf-8")

        if score:
            _score(result, chain, excerpt, encoded, directory, root / "work",
                   pq=info.color_transfer == "smpte2084")
    except pipeline.SkipFile as exc:
        result.ok, result.error = False, f"skipped: {exc}"
    except (CommandError, OSError, ValueError) as exc:
        result.ok, result.error = False, str(exc).splitlines()[0][:300] if str(exc) else repr(exc)
    saved.write_text(json.dumps(asdict(result), indent=1), encoding="utf-8")
    return result


def _score(result: Result, chain: Toolchain, excerpt: Path, encoded: Path, directory: Path,
           work: Path, *, pq: bool) -> None:
    """Scores and snapshots. A failure here leaves the encode standing: it is noted,
    and a rerun measures again without encoding again."""
    try:
        comparison = verify.compare(chain, excerpt, encoded, work / f"verify-{result.id}")
        if comparison.lowpass:
            result.lowpass_mean = comparison.lowpass_mean
            result.lowpass_p5 = comparison.lowpass_p5
            result.lowpass_min = min(comparison.lowpass)
        result.grain_retention = comparison.grain_retention
        result.worst = comparison.worst
        snaps = directory / "snapshots"
        verify.snapshots(chain, excerpt, result.worst, snaps, "source", pq=pq)
        verify.snapshots(chain, encoded, result.worst, snaps, "encode", pq=pq)
    except (CommandError, OSError, ValueError) as exc:
        result.error = f"not scored: {str(exc).splitlines()[0][:200] if str(exc) else exc!r}"
        log.warning("  %s", result.error)


def _db(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def write_report(root: Path, header: list[str], picks: list[Pick],
                 results: dict[str, Result], status: str) -> None:
    lines = [
        "# Library samples", "",
        f"**Status: {status}**", "",
        *header, "",
        "Each sample directory holds the excerpt (the source's own bits, video only),",
        "its encode, and `snapshots/` of the three frames the encode handles worst,",
        "from both files. Low-pass PSNR is luma after a sigma-2 blur, so grain does",
        "not count and blocking does; its 5th percentile and minimum are where",
        "visible damage shows. Grain is the source's fine texture that survives,",
        "with synthesis on: about 40% is none, 100% is all.", "",
        "| sample | film | source | from | source kbps | AV1 kbps | saving | CRF "
        "| low-pass mean | 5th pct | min | grain | took |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in picks:
        film = Path(p.source).stem
        film = film if len(film) <= 40 else film[:37] + "..."
        what = f"{p.width}x{p.height} {p.codec}{' HDR' if p.hdr else ''}"
        r = results.get(p.id)
        if r is None:
            lines.append(f"| {p.id} | {film} | {what} | {human_duration(p.start)} "
                         "| waiting | | | | | | | | |")
            continue
        if not r.ok:
            lines.append(f"| {p.id} | {film} | {what} | {human_duration(p.start)} "
                         f"| failed: {r.error[:80]} | | | | | | | | |")
            continue
        grain = f"{100 * r.grain_retention:.0f}%" if r.grain_retention is not None else "-"
        lines.append(
            f"| {p.id} | {film} | {what} | {human_duration(p.start)} "
            f"| {r.kbps(r.source_bytes):,.0f} | {r.kbps(r.encoded_bytes):,.0f} "
            f"| {r.saving:.0f}% | {r.crf} | {_db(r.lowpass_mean)} | {_db(r.lowpass_p5)} "
            f"| {_db(r.lowpass_min)} | {grain} | {human_duration(r.encode_seconds)} |")

    by_band: dict[str, list[Result]] = defaultdict(list)
    for p in picks:
        if (r := results.get(p.id)) and r.ok:
            by_band[p.band].append(r)
    if by_band:
        lines += ["", "| band | samples | source | AV1 | saving | worst low-pass min |",
                  "|---|---:|---:|---:|---:|---:|"]
        for band, done in by_band.items():
            source = sum(r.source_bytes for r in done)
            encoded = sum(r.encoded_bytes for r in done)
            mins = [r.lowpass_min for r in done if r.lowpass_min is not None]
            lines.append(f"| {band} | {len(done)} | {human_size(source)} "
                         f"| {human_size(encoded)} | {100 * (1 - encoded / source):.0f}% "
                         f"| {_db(min(mins)) if mins else '-'} |")
        lines += ["", "Savings are video to video, so a full file saves a little less",
                  "once its audio and subtitles are counted."]

    failed = [r for r in results.values() if not r.ok]
    if failed:
        lines += ["", "Failed samples keep their error in `result.json`; a rerun retries them."]
    lines.append("")
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_samples(
    inputs: list[Path], chain: Toolchain, cfg: Config, root: Path, *,
    per_band: int, bands: list[str], minutes: float, skip_start: float, skip_end: float,
    seed: int | None, score: bool, pick_only: bool,
) -> int:
    root.mkdir(parents=True, exist_ok=True)
    picks_file = root / "picks.json"

    if picks_file.is_file():
        stored = json.loads(picks_file.read_text(encoding="utf-8"))
        picks = [Pick(**entry) for entry in stored["picks"]]
        seed = stored.get("seed")
        log.info("reusing the %d picks already in %s", len(picks), picks_file)
    else:
        sources = pipeline.discover(inputs, cfg)
        if not sources:
            log.error("no media files found")
            return 1
        if seed is None:
            seed = random.SystemRandom().randrange(1_000_000)
        rng = random.Random(seed)
        log.info("%d file(s) found; drawing %d per band from %s (seed %d)",
                 len(sources), per_band, ", ".join(bands), seed)
        picks = pick(sources, chain, cfg, per_band=per_band, bands=bands,
                     length=minutes * 60, skip_start=skip_start * 60,
                     skip_end=skip_end * 60, rng=rng)
        if not picks:
            log.error("nothing to sample")
            return 1
        picks_file.write_text(json.dumps({
            "seed": seed, "minutes": minutes, "skip_start": skip_start, "skip_end": skip_end,
            "picked": dt.datetime.now().isoformat(timespec="seconds"),
            "picks": [asdict(p) for p in picks],
        }, indent=1), encoding="utf-8")

    header = [
        f"{len(picks)} sample(s) of {picks[0].length / 60:g} minutes, seed {seed}. "
        f"Preset {cfg.encode.preset}, grain mode {cfg.grain.mode}, the live config's "
        "settings per band.",
    ]
    results: dict[str, Result] = {}
    for p in picks:
        saved = root / p.id / "result.json"
        if saved.is_file():
            results[p.id] = Result(**json.loads(saved.read_text(encoding="utf-8")))
    write_report(root, header, picks, results, "picked" if pick_only else "starting")
    if pick_only:
        print(f"{len(picks)} pick(s) in {picks_file}; run again without --pick-only to encode")
        return 0

    for number, p in enumerate(picks, start=1):
        log.info("[%d/%d] %s: %s from %s", number, len(picks), p.id, Path(p.source).name,
                 human_duration(p.start))
        write_report(root, header, picks, results, f"sample {number}/{len(picks)} ({p.id})")
        results[p.id] = encode_pick(p, chain, cfg, root, score=score)
        r = results[p.id]
        if r.ok:
            log.info("  %s -> %s (%.0f%% smaller)", human_size(r.source_bytes),
                     human_size(r.encoded_bytes), r.saving)
        else:
            log.warning("  failed: %s", r.error)

    failed = sum(not r.ok for r in results.values())
    status = f"finished {dt.datetime.now():%Y-%m-%d %H:%M}" + (
        f", {failed} failed" if failed else "")
    write_report(root, header, picks, results, status)
    if not failed:
        shutil.rmtree(root / "work", ignore_errors=True)   # Av1an chunks; the outputs stay
    done = [r for r in results.values() if r.ok]
    if done:
        log.info("%d sample(s) done, median saving %.0f%%; report: %s", len(done),
                 statistics.median(r.saving for r in done), root / "report.md")
    return 1 if failed else 0
