"""The film's scene list, kept and detected so that a stop costs little of it.

Av1an cuts a film into chunks at its scene changes, and finding them means
decoding the whole film once: at 4K on an RK3588 that takes longer than a day.
Av1an keeps nothing of it until the whole film is done, and resumes only once it
has written its chunk list after that, so a stop anywhere before cost every hour
spent so far. Two things fix that:

- The list lives beside the grain table, out of Av1an's temp directory, and Av1an
  is given it with `--scenes`, which it reads whenever the file exists.
- With a VapourSynth chunk method, the list is made a few minutes of the film at a
  time: Av1an's own detector, run with `--sc-only` on a script that serves one
  part, and each part kept as it finishes. A stop then loses the part in flight.
  Each part starts a scene of its own, which costs a keyframe every few minutes.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import encode
from .config import Config
from .probe import MediaInfo
from .tools import Toolchain
from .util import CommandError, digest, log, run_watched

# How much of the film each part of the scene detection covers. A stop loses at
# most one part's detection; each part adds one keyframe.
PART_SECONDS = 300

# The index Av1an makes in its temp directory for each VapourSynth chunk method,
# under split/ (av1an-core 0.5.2, generate_loadscript_text). The parts load the
# source the same way and share it, so the film is indexed once.
INDEX_NAMES = {"lsmash": "cache.lwi", "ffms2": "cache.ffindex", "bestsource": "cache.bsindex"}


def key(read: MediaInfo, chain: Toolchain, cfg: Config) -> str:
    """A digest of everything the scene list depends on."""
    enc = cfg.encode
    return digest({
        "scenes": [encode.chunk_method(cfg, chain), enc.sc_method, enc.min_scene_len,
                   enc.extra_split_sec, enc.sc_downscale_height],
        "source": [read.size_bytes, read.duration],
    })


def whole_list(path: Path) -> bool:
    """Whether `path` holds a scene list Av1an can read: there, and not cut short."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["frames"]) > 0 and bool(data.get("split_scenes") or data.get("scenes"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def prepare(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> Path:
    """Where Av1an is to keep the film's scene list, with any list it can reuse there.

    The list is named by a digest of what it depends on, so a changed setting or
    source detects the scenes afresh, and lists and parts made for other settings
    are removed.

    An encode that will resume -- done.json and chunks.json both there -- reads the
    scene list for the frame count only, its chunks having been cut already. So
    the list they were cut by is kept under the current name whatever settings
    made it. Av1an's own scenes.json, from runs before this, is kept either way:
    it was made for this work directory, with the settings it still holds.
    """
    scenes = work / f"scenes-{key(read, chain, cfg)}.json"
    temp = work / "av1an"
    legacy = temp / "scenes.json"
    earlier = [path for path in (*sorted(work.glob("scenes-*.json")), legacy)
               if path != scenes and path.is_file()]
    if not whole_list(scenes):
        scenes.unlink(missing_ok=True)
        resuming = (temp / "done.json").is_file() and (temp / "chunks.json").is_file()
        for old in earlier:
            if (resuming or old == legacy) and whole_list(old):
                os.replace(old, scenes)
                break
    for old in earlier:
        old.unlink(missing_ok=True)
    for old in work.glob("scenes-*.parts"):
        if old != parts_dir(scenes):
            shutil.rmtree(old, ignore_errors=True)
    if scenes.is_file():
        log.info("  scene list kept from an earlier run")
    return scenes


def parts_dir(scenes: Path) -> Path:
    return scenes.with_suffix(".parts")


def detect(read: MediaInfo, chain: Toolchain, cfg: Config, work: Path, scenes: Path) -> None:
    """Make the scene list a part at a time, keeping each part as it is done.

    Does nothing when the list is already there, when the chunk method reads the
    source through ffmpeg (there is no frame-exact way to start a part there), or
    when the film is too short to be worth it: Av1an then detects the scenes in
    one go, as it always did. A part that fails leaves it to Av1an as well.
    """
    method = encode.chunk_method(cfg, chain)
    if whole_list(scenes) or method not in INDEX_NAMES or not chain.av1an:
        return
    length = round(read.fps * PART_SECONDS)
    frames = read.frames or round(read.fps * (read.video_duration or read.duration))
    if length <= 0 or frames < 2 * length:
        return
    # The last part runs to the end of the film, however many frames the decoder
    # finds there, so the estimate only decides how many parts there are.
    starts = [index * length for index in range(frames // length)]
    folder = parts_dir(scenes)
    folder.mkdir(parents=True, exist_ok=True)
    index_file = (work / "av1an" / "split" / INDEX_NAMES[method]).absolute()
    index_file.parent.mkdir(parents=True, exist_ok=True)

    kept = sum(whole_list(part_path(folder, number)) for number in range(len(starts)))
    log.info("  detecting scenes in %d parts of %d minutes, each kept as it finishes%s",
             len(starts), PART_SECONDS // 60, f" ({kept} done before)" if kept else "")
    parts: list[dict[str, Any]] = []
    try:
        for number, start in enumerate(starts):
            end = starts[number + 1] if number + 1 < len(starts) else None
            part = part_path(folder, number)
            if not whole_list(part):
                log.info("  scene detection, part %d of %d", number + 1, len(starts))
                _detect_part(read, chain, cfg, method, index_file, start, end, part)
            parts.append(json.loads(part.read_text(encoding="utf-8")))
        merged = merge(parts, starts)
    except (CommandError, OSError, ValueError) as exc:
        log.warning("  scene detection in parts failed (%s); Av1an detects the scenes itself",
                    str(exc).splitlines()[0] if str(exc) else exc)
        return
    written = scenes.with_name(scenes.name + ".part")
    written.write_text(json.dumps(merged), encoding="utf-8")
    os.replace(written, scenes)
    shutil.rmtree(folder, ignore_errors=True)
    log.info("  scene list complete: %d scenes, %d chunks",
             len(merged["scenes"]), len(merged["split_scenes"]))


def part_path(folder: Path, number: int) -> Path:
    return folder / f"{number:04d}.json"


def _detect_part(
    read: MediaInfo, chain: Toolchain, cfg: Config, method: str, index_file: Path,
    start: int, end: int | None, part: Path,
) -> None:
    """Run Av1an's scene detection over frames [start, end) and keep its list as `part`."""
    stem = part.name.removesuffix(".json")
    script = part.with_name(f"{stem}.vpy")
    script.write_text(source_script(read.path.absolute(), method, index_file, start, end),
                      encoding="utf-8")
    # Av1an reads a scene list that is already there instead of detecting, and
    # writes its own in place without a rename: so a fresh name, renamed once whole.
    written = part.with_name(f"{stem}.new.json")
    written.unlink(missing_ok=True)
    temp = part.with_name(f"{stem}.av1an")
    shutil.rmtree(temp, ignore_errors=True)
    argv = encode.scene_command(chain, cfg, method, script, written, temp)
    if cfg.machine.nice > 0:
        argv = ["nice", "-n", str(cfg.machine.nice), *argv]
    run_watched(argv, lambda: None, extra_path=chain.bin_dirs)
    if not whole_list(written):
        raise ValueError(f"Av1an wrote no scene list for frames {start} on")
    frames = int(json.loads(written.read_text(encoding="utf-8"))["frames"])
    if end is not None and frames != end - start:
        raise ValueError(f"frames {start}-{end} came out as {frames} frames")
    os.replace(written, part)
    script.unlink(missing_ok=True)
    shutil.rmtree(temp, ignore_errors=True)


def source_script(source: Path, method: str, index_file: Path, start: int,
                  end: int | None) -> str:
    """A VapourSynth script serving frames [start, end) of `source`, or to its end.

    Loaded as Av1an's own script loads it (av1an-core 0.5.2, loadscript.vpy), so
    the frames, and the index, are the ones the encode will use.
    """
    src, idx = repr(str(source)), repr(str(index_file))
    if method == "lsmash":
        load = f"video = core.lsmas.LWLibavSource({src}, cachefile={idx})\n"
    elif method == "ffms2":
        load = f"video = core.ffms2.Source({src}, cachefile={idx})\n"
    else:
        load = ("try:\n"
                f"    video = core.bs.VideoSource({src}, cachepath={idx}, cachemode=4)\n"
                "except Exception:\n"
                f"    video = core.bs.VideoSource({src}, cachepath={idx})\n")
    return ("# Written by autotranscode: one part of the film, for scene detection.\n"
            "from vapoursynth import core\n"
            f"{load}"
            f"video[{start}:{'' if end is None else end}].set_output()\n")


def merge(parts: list[dict[str, Any]], starts: list[int]) -> dict[str, Any]:
    """One scene list from the parts' lists, each starting where the last ended."""
    total = 0
    merged: dict[str, Any] = {"frames": 0, "scenes": [], "split_scenes": []}
    for data, start in zip(parts, starts, strict=True):
        if start != total:
            raise ValueError(f"the scene list's part from frame {start} follows {total} frames")
        for name in ("scenes", "split_scenes"):
            for scene in data.get(name) or data.get("scenes") or []:
                merged[name].append({**scene, "start_frame": int(scene["start_frame"]) + total,
                                     "end_frame": int(scene["end_frame"]) + total})
        total += int(data["frames"])
    merged["frames"] = total
    return merged
