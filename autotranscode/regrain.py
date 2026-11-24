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

The film is read once, taken apart in one pass into its video track and the rest
(every other track, the chapters and tags), and put together again from those.
With staging on (`[staging]`, as for `run`), the source is copied too, dropped as
soon as the grain is final, and the new film is put together locally and written
back once checked: the library's disks see one read of each file at the start
and one write at the end, instead of an hour of reading and seeking.

Never beside a run: it would compete for the big cores, and the two share the
work directory. It holds the work directory as a run does, so neither starts
while the other works.
"""

from __future__ import annotations

import json
import os
import shutil
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

STAGED_NEW = ".regrain.out.mkv"   # ".out.mkv": `clean` knows it as half-written
# The new film is put together locally only with this much room beyond its size.
NEW_ROOM = 1.1

# In the work directory, kept until the film is done so a stopped regrain does
# not redo them: the film taken apart (its video track, and the rest), the note
# saying it was taken apart whole and from which film, and the scene cuts.
VIDEO = "regrain-video.mkv"
REST = "regrain-rest.mkv"
VIDEO_NOTE = "regrain-video.json"
SCENES = "regrain-scenes.json"


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
    # Its dispositions (default, forced, ...), each set again by hand in a remux:
    # ffmpeg marks the first of several subtitles default when none is, unless
    # told every track's.
    flags: tuple[str, ...] = ()


def tracks(chain: Toolchain, path: Path) -> list[Track]:
    data = json.loads(run([chain.ffprobe, "-v", "error", "-show_entries",
                           "stream=index,codec_type,codec_name:stream_disposition"
                           ":stream_tags", "-of", "json", str(path)]).stdout or "{}")
    return [Track(int(s["index"]), s.get("codec_type", ""), s.get("codec_name", ""),
                  bool((s.get("disposition") or {}).get("attached_pic")),
                  tuple(sorted((key.lower(), value) for key, value
                               in (s.get("tags") or {}).items()
                               if key.lower().split("-")[0] in KEPT_TAGS)),
                  tuple(sorted(name for name, on in (s.get("disposition") or {}).items()
                               if on)))
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


def split_command(chain: Toolchain, output: Path, index: int, video: Path,
                  rest: Path | None) -> list[str]:
    """In one read of `output`: its film track alone into `video`, and every other
    track with the chapters and tags into `rest` (None: there is no other track).
    The timestamps as they are (-copyts)."""
    argv = [chain.ffmpeg, "-hide_banner", "-v", "error", "-y", "-copyts", "-i", str(output),
            "-map", f"0:{index}", "-c", "copy", "-f", "matroska", str(video)]
    if rest is not None:
        argv += ["-map", "0", "-map", f"-0:{index}", "-c", "copy", "-map_metadata", "0",
                 "-map_chapters", "0", "-f", "matroska", str(rest)]
    return argv


def remux_command(chain: Toolchain, rest: Path | None, found: list[Track], film: Track,
                  video: Path, base: Path, aspect: str, part: Path) -> list[str]:
    """The film again from its parts: `video` (the track with the new grain) for
    its film track, which keeps the tags of `base` (the track taken out), and every
    other track from `rest` in its place, with the chapters, tags and attachments,
    and the times as they were (-copyts: the video may start after the audio).
    Every track's dispositions are set as `found` has them.

    `found` is the film's tracks; `rest` holds all but the film track, in the
    same order. Every track's metadata is mapped by hand: one -map_metadata for a
    stream turns off ffmpeg's own copying for all of them, and a first version
    that mapped only the film track's shipped Film A with no track
    languages, its commentary untitled."""
    argv = [chain.ffmpeg, "-hide_banner", "-v", "error", "-y", "-copyts",
            "-i", str(video), "-i", str(base)]
    if rest is not None:
        argv += ["-i", str(rest)]
    in_rest = {track.index: number
               for number, track in enumerate(t for t in found if t.index != film.index)}
    for track in found:
        argv += ["-map", "0:0" if track.index == film.index else f"2:{in_rest[track.index]}"]
    position = found.index(film)
    whole = "2" if rest is not None else "1"
    argv += ["-c", "copy", "-map_metadata", whole, "-map_chapters", whole]
    for number, track in enumerate(found):
        argv += [f"-map_metadata:s:{number}",
                 "1:s:0" if track.index == film.index else f"2:s:{in_rest[track.index]}",
                 f"-disposition:{number}", "+".join(track.flags) or "0"]
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


@dataclass(frozen=True, slots=True)
class Layout:
    """What a file's head says of it: read without reading the rest."""
    duration: float
    starts: tuple[tuple[str, str], ...]      # each audio and video track's start
    chapters: tuple[tuple[str, str, str], ...]   # start, end, title


def layout(chain: Toolchain, path: Path) -> Layout:
    data = json.loads(run([chain.ffprobe, "-v", "error", "-show_entries",
                           "format=duration:stream=codec_type,start_time"
                           ":stream_disposition=attached_pic", "-show_chapters",
                           "-of", "json", str(path)]).stdout or "{}")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    starts = tuple((s.get("codec_type", ""), s.get("start_time", ""))
                   for s in data.get("streams", [])
                   if s.get("codec_type") in {"audio", "video"}
                   and not (s.get("disposition") or {}).get("attached_pic"))
    chapters = tuple((c.get("start_time", ""), c.get("end_time", ""),
                      (c.get("tags") or {}).get("title", ""))
                     for c in data.get("chapters", []))
    return Layout(duration, starts, chapters)


def check(chain: Toolchain, old: Path, new: Path, old_video: Path | None = None) -> None:
    """Raise ValueError unless `new` is `old` with the film track's grain changed
    (and the scene list kept in it, which may be new): the same tracks with the
    same tags, the same video packets, each track starting when it did, the same
    chapters, and the same length. `old_video`, the film track taken out of `old`,
    stands in for it when counting the packets, so `old` is read only at its head."""
    before, after = (_without_scene_list(tracks(chain, path)) for path in (old, new))
    if [(t.kind, t.codec) for t in before] != [(t.kind, t.codec) for t in after]:
        raise ValueError(f"the tracks changed: {[(t.kind, t.codec) for t in before]} -> "
                         f"{[(t.kind, t.codec) for t in after]}")
    if [t.tags for t in before] != [t.tags for t in after]:
        changed = [t.index for t, u in zip(before, after, strict=True) if t.tags != u.tags]
        raise ValueError(f"the tags of track(s) {changed} changed")
    if [t.flags for t in before] != [t.flags for t in after]:
        changed = [t.index for t, u in zip(before, after, strict=True) if t.flags != u.flags]
        raise ValueError(f"the dispositions of track(s) {changed} changed")
    film = film_track(before)
    old_packets = (_video_packets(chain, old_video, 0) if old_video is not None
                   else _video_packets(chain, old, film.index))
    new_packets = _video_packets(chain, new, film.index)
    if old_packets != new_packets:
        raise ValueError(f"the video's frames changed: {old_packets[0]} from {old_packets[1]} "
                         f"-> {new_packets[0]} from {new_packets[1]}")
    was, now = layout(chain, old), layout(chain, new)
    if was.starts != now.starts:
        raise ValueError(f"the tracks start at other times: {was.starts} -> {now.starts}")
    if was.chapters != now.chapters:
        raise ValueError(f"the chapters changed: {len(was.chapters)} -> {len(now.chapters)}")
    longer = abs(now.duration - was.duration)
    if longer > DURATION_TOLERANCE:
        raise ValueError(f"the length changed by {longer:.2f}s")


def find_scenes(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> list[int] | None:
    """The source's scene cuts: by av-scenechange (cuts.detect),
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


def kept_text(chain: Toolchain, output: Path) -> str | None:
    """The scene list the film `output` keeps about itself, as text, if any."""
    return cuts.attachment(chain, output)


def _current(text: str | None, method: str) -> bool:
    found = cuts.parse(text, None) if text is not None else None
    return bool(method) and found is not None and found.grain == method


def done_already(chain: Toolchain, cfg: Config, output: Path) -> bool:
    """Whether the film's grain was made by the method regrain uses now."""
    return output.is_file() and _current(kept_text(chain, output),
                                         pipeline.grain_method(cfg, chain))


def _film_stamp(output: Path, index: int) -> dict[str, int]:
    film = output.stat()
    return {"size": film.st_size, "mtime_ns": film.st_mtime_ns, "track": index}


def take_apart(chain: Toolchain, output: Path, found: list[Track], film: Track,
               work: Path) -> tuple[Path, Path | None]:
    """The film `output` in two, in the work directory, in one read of it: its
    film track, and the rest (None when it has no other track). Those an earlier
    regrain of this same film took apart whole are used again."""
    video, rest, note = work / VIDEO, work / REST, work / VIDEO_NOTE
    others = len(found) > 1
    stamp = _film_stamp(output, film.index)
    try:
        if (video.is_file() and (rest.is_file() or not others)
                and json.loads(note.read_text(encoding="utf-8")) == stamp):
            log.info("  the film as an earlier regrain took it apart: its video track, %s",
                     human_size(video.stat().st_size))
            return video, rest if others else None
    except (OSError, ValueError):
        pass
    note.unlink(missing_ok=True)
    log.info("  taking the film apart: its video track and the rest")
    run(split_command(chain, output, film.index, video, rest if others else None))
    note.write_text(json.dumps(stamp), encoding="utf-8")
    return video, rest if others else None


def scene_starts(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path,
                 frames: int) -> list[int] | None:
    """find_scenes, or the cuts an earlier regrain of this film found."""
    saved = work / SCENES
    try:
        known = cuts.parse(saved.read_text(encoding="utf-8"), frames)
    except OSError:
        known = None
    if known is not None:
        log.info("  scene cuts found by an earlier regrain: %d scenes", len(known.starts))
        return known.starts
    starts = find_scenes(read, chain, cfg, work)
    if starts:
        saved.write_text(cuts.attachment_text(starts, frames), encoding="utf-8")
    return starts


def put_back(new: Path, output: Path) -> None:
    """Write the new film `new` over `output`: copied beside it, then renamed, so
    `output` is the old film or the new one, never half of either."""
    part = output.with_name(output.name + PART_SUFFIX)
    size = new.stat().st_size
    log.info("  writing the new film back, %s", human_size(size))
    try:
        shutil.copyfile(new, part)
        if part.stat().st_size != size:
            raise OSError(f"{part}: {part.stat().st_size} bytes written of {size}")
        os.replace(part, output)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _new_film_path(output: Path, source: Path, cfg: Config, local: bool) -> Path:
    """Where the new film is put together: locally when staging (and there is room
    for it), else beside the old film."""
    beside = output.with_name(output.name + PART_SUFFIX)
    if not local:
        return beside
    staged = pipeline._staged_path(source, cfg, STAGED_NEW)
    try:
        free = shutil.disk_usage(staged.parent).free
    except OSError:
        return beside
    if free < NEW_ROOM * output.stat().st_size:
        log.warning("  putting the new film together beside the old one: %s free at %s",
                    human_size(free), staged.parent)
        return beside
    return staged


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
    # Read from the film's head alone: deciding to leave a film reads nothing more.
    text = kept_text(chain, output)
    if not force and _current(text, method):
        return "skipped: its grain is the current method's already"
    info = probe(source, chain)
    running = work / pipeline.RUNNING_FILE
    running.write_text(str(os.getpid()), encoding="ascii")
    staged_source: Path | None = None
    table: Path | None = None
    try:
        if cfg.staging.enabled:
            staged_source = pipeline.stage_source(info, cfg)
        read = replace(info, path=staged_source) if staged_source is not None else info
        found = tracks(chain, output)
        film = film_track(found)
        _, aspect = video_details(chain, output, film.index)
        video, rest = take_apart(chain, output, found, film, work)
        frames = len(scenegrain.read_stream(chain, video).stamps)
        known = cuts.parse(text, frames) if text is not None else None
        starts = known.starts if known is not None else None
        if starts is not None:
            log.info("  scene list kept in the film: %d scenes", len(starts))
        else:
            starts = scene_starts(read, chain, cfg, work, frames)
        new_video, table = pipeline.film_grain(read, video, starts, chain, cfg, work)
        if table is None:
            stripped = work / "regrain-plain.mkv"
            run([chain.grav1synth or "grav1synth", "remove", str(video), "-o",
                 str(stripped), "-y"])
            new_video = stripped
        # The grain is final: the source is not read again.
        _drop_staged(staged_source, cfg)
        part = _new_film_path(output, source, cfg, local=staged_source is not None)
        try:
            run(remux_command(chain, rest, found, film, new_video, video, aspect, part))
            if starts:
                # Always written, replacing what the remux copied: it now says what
                # made the grain, so a regrain started again leaves this film.
                cuts.attach(chain, part, starts, frames, work, method)
            check(chain, output, part, old_video=video)
            if part.parent == output.parent:
                os.replace(part, output)
            else:
                put_back(part, output)
        finally:
            part.unlink(missing_ok=True)
    except KeyboardInterrupt:
        # Kept for the rerun, so it need not read it off the library's disks again.
        if staged_source is not None and staged_source.is_file():
            log.info("  keeping the staged source so a rerun need not read it again")
        raise
    except BaseException:
        _drop_staged(staged_source, cfg)
        raise
    finally:
        running.unlink(missing_ok=True)
    size = output.stat().st_size
    ledger.record(source, replace(target.record, output_bytes=size))
    pipeline._cleanup(work, cfg, table)
    return "regrained" if table is not None else "no grain to put back; grain removed"


def _drop_staged(staged_source: Path | None, cfg: Config) -> None:
    if staged_source is not None and not cfg.staging.keep:
        staged_source.unlink(missing_ok=True)


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
            if not force and done_already(chain, cfg, target.output):
                state += "; its grain is current, left as it is"
            print(f"{target.output}\n    source: {target.source} [{state}]")
        print(f"{len(found)} film(s); nothing changed (dry run)")
        return 0
    if cfg.grain.estimator != "svtfg" or cfg.grain.mode == "never":
        log.error("regrain makes the grain of grain.estimator = \"svtfg\"; this config has "
                  "estimator %r, mode %r", cfg.grain.estimator, cfg.grain.mode)
        return 2
    try:
        # A `kill` or a closed SSH session stops it as Ctrl-C does: the film as it
        # was, the children killed. A second signal ends it outright.
        with pipeline.stop_signals(), pipeline.only_run(cfg):
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
