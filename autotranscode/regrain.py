"""`autotranscode regrain`: new grain for films already encoded, without encoding again.

AV1 carries film grain as parameters in its frame headers, which the decoder
turns into grain, so a better grain table needs no new encode: grav1synth
rewrites the headers, replacing whatever grain was there (applying a table over
an earlier one gave, frame for frame, the pictures of applying it to the encode
without grain). So for each finished film: its source is found in the ledger,
its scene cuts read from the film, or found and then kept in it (cuts.py), its
grain estimated and calibrated scene by scene as `run` does now
(pipeline.film_grain), the video track rewritten, and the file remuxed with every
other track as it was. The new file is checked against the old one and then
takes its place.

A film whose grain the current method made (pipeline.grain_method, kept in the
film with its scene cuts) is left as it is, so a regrain stopped on the third
day starts again where it was; --force does such films again.

Never beside a run: it would compete for the big cores, and the two share the
work directory. It holds the work directory as a run does, so neither starts
while the other works.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import cuts, pipeline, progress, scenegrain, scenes
from .config import Config
from .probe import MediaInfo, probe
from .state import Ledger, Record
from .tools import Toolchain
from .util import CommandError, human_duration, human_size, log, run

# Track tags a remux must carry over (a "-eng" style suffix aside).
KEPT_TAGS = {"language", "title", "filename", "mimetype", "handler_name"}

# The new file may run this much longer or shorter than the old one.
DURATION_TOLERANCE = 0.5
PART_SUFFIX = ".regrain.part"


@dataclass(frozen=True, slots=True)
class Target:
    output: Path
    record: Record

    @property
    def source(self) -> Path:
        return Path(self.record.source)


def targets(inputs: list[Path], ledger: Ledger) -> tuple[list[Target], list[Path]]:
    """The finished films among `inputs` (files or folders of outputs; none means
    every finished film), and the inputs that are not autotranscode outputs."""
    done = {}
    for record in ledger.records.values():
        if record.status == "done" and record.output:
            done[Path(record.output).resolve()] = record
    if not inputs:
        return [Target(path, record) for path, record in done.items()], []
    found: dict[Path, Target] = {}
    strangers: list[Path] = []
    for item in inputs:
        item = item.expanduser().resolve()
        if item.is_dir():
            inside = [path for path in done if path.is_relative_to(item)]
            if not inside:
                strangers.append(item)
            for path in inside:
                found[path] = Target(path, done[path])
        elif item in done:
            found[item] = Target(item, done[item])
        else:
            strangers.append(item)
    return sorted(found.values(), key=lambda target: str(target.output)), strangers


@dataclass(frozen=True, slots=True)
class Track:
    index: int
    kind: str           # video, audio, subtitle, attachment, data
    codec: str
    picture: bool       # an attached picture: cover art, not the film
    # The tags a remux must keep: language, title, an attachment's filename and
    # type. Statistics tags (BPS, NUMBER_OF_FRAMES...) are left out: mkvmerge
    # writes them, and a remux by ffmpeg drops them.
    tags: tuple[tuple[str, str], ...] = ()


def tracks(chain: Toolchain, path: Path) -> list[Track]:
    data = json.loads(run([chain.ffprobe, "-v", "error", "-show_entries",
                           "stream=index,codec_type,codec_name:stream_disposition=attached_pic"
                           ":stream_tags", "-of", "json", str(path)]).stdout or "{}")
    return [Track(int(s["index"]), s.get("codec_type", ""), s.get("codec_name", ""),
                  bool((s.get("disposition") or {}).get("attached_pic")),
                  tuple(sorted((key.lower(), value) for key, value
                               in (s.get("tags") or {}).items()
                               if key.lower().split("-")[0] in KEPT_TAGS)))
            for s in data.get("streams", [])]


def film_track(found: list[Track]) -> Track:
    for track in found:
        if track.kind == "video" and not track.picture:
            return track
    raise ValueError("no video track")


def video_details(chain: Toolchain, path: Path, index: int) -> tuple[list[str], str]:
    """The film track's disposition flags and display aspect ratio ("" if square)."""
    data = json.loads(run([chain.ffprobe, "-v", "error", "-select_streams", str(index),
                           "-show_entries", "stream=display_aspect_ratio,sample_aspect_ratio"
                           ":stream_disposition", "-of", "json", str(path)]).stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    flags = [name for name, on in (stream.get("disposition") or {}).items() if on]
    sar = stream.get("sample_aspect_ratio", "")
    dar = stream.get("display_aspect_ratio", "")
    aspect = dar if sar not in {"", "1:1", "0:1", "N/A"} and dar not in {"", "0:1", "N/A"} else ""
    return flags, aspect


def extract_command(chain: Toolchain, output: Path, index: int, video: Path) -> list[str]:
    """The film track alone, its timestamps as they are."""
    return [chain.ffmpeg, "-hide_banner", "-v", "error", "-y", "-copyts", "-i", str(output),
            "-map", f"0:{index}", "-c", "copy", "-f", "matroska", str(video)]


def remux_command(chain: Toolchain, output: Path, found: list[Track], film: Track,
                  video: Path, flags: list[str], aspect: str, part: Path) -> list[str]:
    """`output` again with `video` for its film track: every track in its place,
    with its metadata, the chapters, tags and attachments, and the times as they
    were (-copyts: the video may start after the audio).

    Every track's metadata is mapped by hand: one -map_metadata for a stream
    turns off ffmpeg's own copying for all of them, and a first version that
    mapped only the film track's shipped Film A with no track
    languages, its commentary untitled."""
    argv = [chain.ffmpeg, "-hide_banner", "-v", "error", "-y", "-copyts",
            "-i", str(output), "-i", str(video)]
    for track in found:
        argv += ["-map", "1:0" if track.index == film.index else f"0:{track.index}"]
    position = found.index(film)
    argv += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"]
    for number, track in enumerate(found):
        argv += [f"-map_metadata:s:{number}", f"0:s:{track.index}"]
    argv += [f"-disposition:{position}", "+".join(flags) if flags else "0"]
    if aspect:
        argv += [f"-aspect:{position}", aspect]
    return [*argv, "-f", "matroska", str(part)]


def _without_scene_list(found: list[Track]) -> list[Track]:
    return [track for track in found
            if not (track.kind == "attachment" and ("filename", cuts.ATTACHMENT) in track.tags)]


def _video_packets(chain: Toolchain, path: Path, index: int) -> tuple[int, str]:
    out = run([chain.ffprobe, "-v", "error", "-select_streams", str(index),
               "-show_entries", "packet=pts", "-of", "csv=p=0", str(path)]).stdout.split()
    return len(out), (out[0].rstrip(",") if out else "")


def _duration(chain: Toolchain, path: Path) -> float:
    out = run([chain.ffprobe, "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def check(chain: Toolchain, old: Path, new: Path) -> None:
    """Raise ValueError unless `new` is `old` with the film track's grain changed
    (and the scene list kept in it, which may be new)."""
    before, after = (_without_scene_list(tracks(chain, path)) for path in (old, new))
    if [(t.kind, t.codec) for t in before] != [(t.kind, t.codec) for t in after]:
        raise ValueError(f"the tracks changed: {[(t.kind, t.codec) for t in before]} -> "
                         f"{[(t.kind, t.codec) for t in after]}")
    if [t.tags for t in before] != [t.tags for t in after]:
        changed = [t.index for t, u in zip(before, after, strict=True) if t.tags != u.tags]
        raise ValueError(f"the tags of track(s) {changed} changed")
    film = film_track(before)
    old_packets, new_packets = (_video_packets(chain, path, film.index) for path in (old, new))
    if old_packets != new_packets:
        raise ValueError(f"the video's frames changed: {old_packets[0]} from {old_packets[1]} "
                         f"-> {new_packets[0]} from {new_packets[1]}")
    longer = abs(_duration(chain, new) - _duration(chain, old))
    if longer > DURATION_TOLERANCE:
        raise ValueError(f"the length changed by {longer:.2f}s")


def find_scenes(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> list[int] | None:
    """The source's scene cuts: by av-scenechange on the video engine (cuts.detect),
    else by Av1an as `run` finds them. None when neither finds any; the encode's
    keyframes then stand in."""
    if chain.scenechange:
        try:
            return cuts.detect(read, chain, cfg, work)
        except (CommandError, ValueError) as exc:
            detail = exc.stderr.strip()[-200:] if isinstance(exc, CommandError) else str(exc)
            log.warning("  scene detection by av-scenechange failed (%s); Av1an detects "
                        "them instead", detail)
    else:
        log.info("  av-scenechange not found (scripts/bootstrap.sh scenechange); Av1an "
                 "detects the scenes")
    scene_list = scenes.prepare(read, chain, cfg, work)
    scenes.detect(read, chain, cfg, work, scene_list)
    return scenes.starts(scene_list)


def kept(chain: Toolchain, output: Path, work: Path) -> cuts.Kept | None:
    """What the film `output` keeps about itself (cuts.Kept), if anything."""
    film = film_track(tracks(chain, output))
    frames, _ = _video_packets(chain, output, film.index)
    return cuts.attached(chain, output, frames, work)


def done_already(chain: Toolchain, cfg: Config, output: Path, work: Path) -> bool:
    """Whether the film's grain was made by the method regrain uses now."""
    method = pipeline.grain_method(cfg, chain)
    found = kept(chain, output, work) if output.is_file() else None
    return bool(method) and found is not None and found.grain == method


def regrain_one(target: Target, chain: Toolchain, cfg: Config, ledger: Ledger,
                force: bool = False) -> str:
    """Regrain one film in place; what became of it, for the summary."""
    output, source = target.output, target.source
    if not output.is_file():
        return "skipped: the output is gone"
    if not source.is_file():
        return f"skipped: its source is gone ({source})"
    work = pipeline._work_dir(source, cfg)
    method = pipeline.grain_method(cfg, chain)
    known = kept(chain, output, work)
    if not force and known is not None and known.grain == method:
        return "skipped: its grain is the current method's already"
    info = probe(source, chain)
    running = work / pipeline.RUNNING_FILE
    running.write_text(str(os.getpid()), encoding="ascii")
    part = output.with_name(output.name + PART_SUFFIX)
    table: Path | None = None
    try:
        found = tracks(chain, output)
        film = film_track(found)
        video = work / "regrain-video.mkv"
        run(extract_command(chain, output, film.index, video))
        frames = len(scenegrain.read_stream(chain, video).stamps)
        starts = known.starts if known is not None else None
        if starts is not None:
            log.info("  scene list kept in the film: %d scenes", len(starts))
        else:
            starts = find_scenes(info, chain, cfg, work)
        new_video, table = pipeline.film_grain(info, video, starts, chain, cfg, work)
        if table is None:
            stripped = work / "regrain-plain.mkv"
            run([chain.grav1synth or "grav1synth", "remove", str(video), "-o", str(stripped),
                 "-y"])
            new_video = stripped
        flags, aspect = video_details(chain, output, film.index)
        run(remux_command(chain, output, found, film, new_video, flags, aspect, part))
        if starts:
            # Always written, replacing what the remux copied: it now says what
            # made the grain, so a regrain started again leaves this film.
            cuts.attach(chain, part, starts, frames, work, method)
        check(chain, output, part)
        os.replace(part, output)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        running.unlink(missing_ok=True)
    size = output.stat().st_size
    ledger.record(source, replace(target.record, output_bytes=size))
    pipeline._cleanup(work, cfg, table)
    return "regrained" if table is not None else "no grain to put back; grain removed"


def main(cfg: Config, chain: Toolchain, inputs: list[Path], *, dry_run: bool = False,
         force: bool = False) -> int:
    ledger = Ledger(cfg.state_path)
    found, strangers = targets(inputs, ledger)
    for stranger in strangers:
        log.warning("%s: no finished film of autotranscode's there (the ledger has none)",
                    stranger)
    if not found:
        log.error("nothing to regrain")
        return 1
    if dry_run:
        for target in found:
            state = "ok" if target.source.is_file() else "SOURCE GONE"
            work = cfg.work_dir / pipeline.source_digest(target.source)
            if not force and done_already(chain, cfg, target.output, work):
                state += "; its grain is current, left as it is"
            print(f"{target.output}\n    source: {target.source} [{state}]")
        print(f"{len(found)} film(s); nothing changed (dry run)")
        return 0
    if cfg.grain.estimator != "svtfg" or cfg.grain.mode == "never":
        log.error("regrain makes the grain of grain.estimator = \"svtfg\"; this config has "
                  "estimator %r, mode %r", cfg.grain.estimator, cfg.grain.mode)
        return 2
    try:
        with pipeline.only_run(cfg):
            return _regrain_all(found, chain, cfg, ledger, force)
    except pipeline.AlreadyRunning as exc:
        log.error("%s; regrain once it has finished", exc)
        return 1


def _regrain_all(found: list[Target], chain: Toolchain, cfg: Config, ledger: Ledger,
                 force: bool = False) -> int:
    failures = 0
    for number, target in enumerate(found, 1):
        log.info("[%d/%d] %s", number, len(found), target.output.name)
        started = time.monotonic()
        try:
            with progress.showing(target.output.stem):
                outcome = regrain_one(target, chain, cfg, ledger, force)
        except KeyboardInterrupt:
            log.warning("stopped; the film is as it was, and a rerun carries on from the "
                        "grain estimate's finished parts")
            raise
        except (CommandError, OSError, ValueError) as exc:
            failures += 1
            detail = exc.stderr.strip()[-300:] if isinstance(exc, CommandError) else str(exc)
            log.error("  failed, the film is as it was: %s", detail)
            continue
        if outcome.startswith("skipped"):
            log.info("  %s", outcome)
            continue
        log.info("  %s in %s (%s)", outcome,
                 human_duration(time.monotonic() - started),
                 human_size(target.output.stat().st_size) if target.output.is_file() else "-")
    return 1 if failures else 0
