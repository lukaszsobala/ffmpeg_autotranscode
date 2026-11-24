"""The film's scene cuts: found on the video engine, and kept inside the film.

`regrain` needs the cuts of a film encoded long ago: SvtFgEssential starts afresh
at each, and calibration goes scene by scene. Av1an's scene list is gone with
the work directory by then, and detecting the scenes again with Av1an took 22
of Film A's 79 minutes: it decodes in software, and its detector
(av-scenechange) barely uses a second thread.

So `detect` runs av-scenechange's own command-line tool -- the crate Av1an
calls, with the options Av1an gives it -- on frames decoded by the video engine
where that is bit-exact (verify.decode_args), five minutes of the film a part,
one part per big core. On Film A it found Av1an's cuts frame for frame
in every part tried, at 250 frames/s against Av1an's 120.

And the cuts are kept inside the film, as a Matroska attachment
(ATTACHMENT, a few kilobytes of JSON), so a later regrain needs no detection at
all: `run` attaches Av1an's list to every film it encodes, and `regrain`
attaches the cuts it found. With them goes what made the film's grain, so a
regrain stopped halfway, and started again, leaves the films it finished.
Nothing plays an attachment; players and Jellyfin use them only for subtitle
fonts.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import encode, essential, verify
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import Children, CommandError, child_env, digest, human_duration, log, run

# The attachment the cuts travel in.
ATTACHMENT = "autotranscode-scenes.json"
ATTACHMENT_TYPE = "application/json"

# Each part covers this much of the film and starts a scene of its own, as the
# parts of scenes.detect do.
PART_SECONDS = 300


# --- detection -------------------------------------------------------------------


def downscale_filter(read: MediaInfo, cfg: Config) -> str:
    """Av1an's downscale before detection (encode.sc_downscale_height): VapourSynth's
    Bicubic, whose defaults are b = c = 1/3, to an even width."""
    height = cfg.encode.sc_downscale_height
    if not height or read.height <= height:
        return ""
    width = round(read.width * height / read.height) // 2 * 2
    return f"scale={width}:{height}:flags=bicubic:param0=1/3:param1=1/3"


def detect_command(chain: Toolchain, cfg: Config) -> list[str]:
    assert chain.scenechange is not None
    speed = "1" if cfg.encode.sc_method == "fast" else "0"
    return [chain.scenechange, "--min-scenecut", str(cfg.encode.min_scene_len),
            "--speed", speed, "-"]


def plan(frames: int, length: int) -> list[essential.Part]:
    """Parts of `length` frames, the last running to the end (up to two lengths)."""
    if frames <= 0:
        return []
    count = max(1, frames // max(1, length))
    starts = [number * length for number in range(count)]
    return [essential.Part(start, starts[number + 1] if number + 1 < count else frames, (0,))
            for number, start in enumerate(starts)]


def key(read: MediaInfo, chain: Toolchain, cfg: Config, frames: int) -> str:
    enc = cfg.encode
    return digest({"cuts": [chain.scenechange_version, enc.sc_method, enc.min_scene_len,
                            enc.sc_downscale_height, PART_SECONDS],
                   "source": [read.size_bytes, read.duration, frames]})


def _part_path(folder: Path, part: essential.Part) -> Path:
    return folder / f"{part.start:07d}-{part.end:07d}.json"


def _read_part(path: Path, frames: int) -> list[int] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data["frame_count"]) != frames:
            return None
        return [int(cut) for cut in data["scene_changes"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _detect_part(chain: Toolchain, cfg: Config, read: MediaInfo, times: Sequence[float],
                 origin: float, part: essential.Part, prefilter: str, folder: Path,
                 cores: queue.Queue[str], children: Children) -> None:
    done = _part_path(folder, part)
    decode = essential.decode_command(chain, read.path, times, origin, part, prefilter,
                                      essential.feed_format(read))
    nice = ["nice", "-n", str(cfg.machine.nice)] if cfg.machine.nice > 0 else []
    env = child_env(chain.bin_dirs)
    core = cores.get()
    try:
        if children.stopping.is_set():
            return
        # One core each: the detector is nearly single-threaded, and left to the
        # scheduler it runs on an RK3588's little cores at a third of the speed.
        pin = ["taskset", "-c", core] if core else []
        detect = [*nice, *pin, *detect_command(chain, cfg)]
        with subprocess.Popen(decode, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) as source:
            with subprocess.Popen(detect, stdin=source.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env) as detector:
                children.add(source, detector)
                assert source.stdout is not None
                source.stdout.close()
                out, err = detector.communicate()
            decode_err = source.stderr.read() if source.stderr else b""
        children.discard(source, detector)
    finally:
        cores.put(core)
    if children.stopping.is_set():
        return
    if detector.returncode or source.returncode:
        failed, argv, detail = ((detector, detect, err) if detector.returncode
                                else (source, decode, decode_err))
        raise CommandError(argv, failed.returncode,
                           detail.decode(errors="replace").strip()[-400:])
    written = done.with_suffix(".new")
    written.write_bytes(out)
    if _read_part(written, part.frames) is None:
        written.unlink(missing_ok=True)
        raise essential.Mismatch(f"frames {part.start}-{part.end}: the detector saw a different "
                                 "number of frames")
    os.replace(written, done)


def detect(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> list[int]:
    """The scene cuts of the source `read`, frame 0 first, in the frames the encode
    has. Parts are kept as they finish, so a stop loses only those in flight.

    Raises CommandError, or essential.Mismatch when the frames cannot be counted
    one for one; the caller can fall back to Av1an.
    """
    times = encode.frame_times(chain, read.path)
    if not times:
        raise essential.Mismatch(f"{read.path.name} has no video frames")
    frames = len(times)
    parts = plan(frames, max(1, round(read.fps * PART_SECONDS)))
    folder = work / f"cuts-{key(read, chain, cfg, frames)}"
    for old in work.glob("cuts-*"):
        if old != folder:
            shutil.rmtree(old, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    # Longest first: the last part runs to the end, up to twice as long as the
    # rest, and started last it would finish alone.
    todo = sorted((part for part in parts
                   if _read_part(_part_path(folder, part), part.frames) is None),
                  key=lambda part: -part.frames)

    big = [cpu for cpu in chain.big_cores.split(",") if cpu]
    count = len(big) or os.cpu_count() or 1
    cores: queue.Queue[str] = queue.Queue()
    for number in range(count):
        cores.put(big[number] if big else "")
    origin = essential.start_time(chain, read.path)
    prefilter = downscale_filter(read, cfg)
    engine = any(arg.endswith("_rkmpp")
                 for arg in verify.decode_args(chain, read.path, grain=True))
    log.info("  detecting scenes (av-scenechange%s): %d frames in %d parts, %d at a time%s",
             ", decoded on the video engine" if engine else "", frames, len(parts), count,
             f" ({len(parts) - len(todo)} done before)" if len(todo) < len(parts) else "")

    started = time.monotonic()
    children = Children()
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(_detect_part, chain, cfg, read, times, origin, part,
                               prefilter, folder, cores, children)
                   for part in todo]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except BaseException:
            children.stop(pool)
            raise

    found: set[int] = set()
    for part in parts:
        cuts = _read_part(_part_path(folder, part), part.frames)
        assert cuts is not None
        found.update(part.start + cut for cut in cuts)
    found.add(0)
    shutil.rmtree(folder, ignore_errors=True)
    elapsed = time.monotonic() - started
    if todo:
        detected = sum(part.frames for part in todo)
        log.info("  %d scenes in %s (%.0f frames/s)", len(found), human_duration(elapsed),
                 detected / elapsed if elapsed else 0.0)
    return sorted(cut for cut in found if cut < frames)


# --- kept inside the film --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Kept:
    """What a film keeps about itself in its attachment."""
    starts: list[int]   # its scene cuts, frame 0 first
    # What made its grain (pipeline.grain_method), "" if not recorded: regrain
    # leaves a film whose grain the current method already made.
    grain: str = ""


def attachment_text(starts: Sequence[int], frames: int, grain: str = "") -> str:
    data: dict[str, object] = {"frames": frames, "scene_starts": list(starts)}
    if grain:
        data["grain"] = grain
    return json.dumps(data, separators=(",", ":"))


def parse(text: str, frames: int | None) -> Kept | None:
    """What an attachment's text keeps, if its scene starts fit a video of `frames`
    frames: from 0, rising, all inside it. None otherwise. With `frames` None, the
    frames it says it was made for stand in: enough to read what made the grain
    without counting the film's frames, which reads the whole file."""
    try:
        data = json.loads(text)
        made_for = int(data["frames"])
        if frames is not None and made_for != frames:
            return None
        starts = [int(start) for start in data["scene_starts"]]
        grain = str(data.get("grain") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not starts or starts[0] != 0 or starts[-1] >= made_for:
        return None
    if any(after <= before for before, after in itertools.pairwise(starts)):
        return None
    return Kept(starts, grain)


def attachment_index(chain: Toolchain, path: Path) -> int | None:
    """The stream index of the film's scene list, if it has one."""
    data = json.loads(run([chain.ffprobe, "-v", "error", "-show_entries",
                           "stream=index,codec_type:stream_tags=filename", "-of", "json",
                           str(path)]).stdout or "{}")
    for stream in data.get("streams", []):
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        if stream.get("codec_type") == "attachment" and tags.get("filename") == ATTACHMENT:
            return int(stream["index"])
    return None


def attachment(chain: Toolchain, path: Path) -> str | None:
    """The text of `path`'s scene list, if it has one: read from the file's head,
    without reading the rest. Dumped into a directory of its own, so that a dry
    run, which has no work directory, can read it too."""
    index = attachment_index(chain, path)
    if index is None:
        return None
    with tempfile.TemporaryDirectory(prefix="autotranscode-") as folder:
        dumped = Path(folder) / ATTACHMENT
        try:
            run([chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
                 f"-dump_attachment:{index}", str(dumped), "-i", str(path),
                 "-map", "0:v:0", "-frames:v", "0", "-f", "null", "-"])
            return dumped.read_text(encoding="utf-8")
        except (CommandError, OSError):
            return None


def attached(chain: Toolchain, path: Path, frames: int) -> Kept | None:
    """What `path` keeps about itself, when it has an attachment that fits its `frames`."""
    text = attachment(chain, path)
    return parse(text, frames) if text is not None else None


def mkvpropedit(chain: Toolchain) -> str | None:
    """mkvpropedit, from the same MKVToolNix as mkvmerge when there is one."""
    if chain.mkvmerge:
        beside = Path(chain.mkvmerge).with_name("mkvpropedit")
        if beside.is_file():
            return str(beside)
    return shutil.which("mkvpropedit")


def attach(chain: Toolchain, path: Path, starts: Sequence[int], frames: int,
           work: Path, grain: str = "") -> bool:
    """Keep `starts`, and what made the grain, inside the Matroska file `path`, in
    place of any list there.

    Written in place by mkvpropedit, in a moment. False, with a note, when there
    is no MKVToolNix to do it: the film is fine without, and a regrain then
    detects its scenes.
    """
    tool = mkvpropedit(chain)
    if tool is None:
        log.info("  the scene list is not kept in the film: mkvpropedit (MKVToolNix) "
                 "not found")
        return False
    data = work / ATTACHMENT
    data.write_text(attachment_text(starts, frames, grain), encoding="utf-8")
    try:
        argv = [tool, "-q", str(path)]
        if attachment_index(chain, path) is not None:
            argv += ["--delete-attachment", f"name:{ATTACHMENT}"]
        run([*argv, "--attachment-name", ATTACHMENT, "--attachment-mime-type",
             ATTACHMENT_TYPE, "--add-attachment", str(data)])
    finally:
        data.unlink(missing_ok=True)
    return True
