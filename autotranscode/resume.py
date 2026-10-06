"""Whether the chunks Av1an finished on an earlier run still belong to this encode.

Av1an resumes from whatever chunk list it finds in its temp directory. It checks
neither the source nor the settings, so on its own it would:

- finish a source replaced since (a better release under the same name) with the
  old one's chunks, cut at the old one's frames;
- carry on with the encoder flags the film started with -- they are kept with each
  chunk -- while taking the filters and pixel format from the new command line;
- fail on every run once the file it was reading is gone: its script names the
  staged copy, not the library file.

So each encode leaves a fingerprint beside the chunks: the source's size and
duration, the file Av1an reads, and the command line flags that shape the
picture. A changed source or a lost file starts the film again. Changed settings
are the operator's call (`library.on_changed_settings`): start again, finish the
film as it began, or leave it for now.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .probe import MediaInfo
from .tools import VAPOURSYNTH_CHUNK_METHODS
from .util import log

FINGERPRINT = "encode.json"
# The Av1an flags that shape the picture. Workers, paths and the like do not.
FLAGS = {"-v": "encoder", "--pix-format": "pixel format", "-m": "chunk method",
         "-f": "filters"}
# Chunk methods whose chunks read the source itself, not split files Av1an made.
READS_SOURCE = {*VAPOURSYNTH_CHUNK_METHODS, "select"}

Ask = Callable[[str, list[str], int, int], str]


def flags(argv: list[str]) -> dict[str, str]:
    """The FLAGS' values in an Av1an command line; "" for one it does not have."""
    return {flag: argv[argv.index(flag) + 1] if flag in argv else "" for flag in FLAGS}


def with_flags(argv: list[str], values: dict[str, str]) -> list[str]:
    """`argv` with the FLAGS set to `values`, a flag with no value left out."""
    argv = list(argv)
    for flag in FLAGS:
        value = values.get(flag, "")
        if flag in argv:
            at = argv.index(flag)
            if value:
                argv[at + 1] = value
            else:
                del argv[at:at + 2]
        elif value:
            argv += [flag, value]
    return argv


def changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """What differs between two sets of FLAGS, one line each, encoder flags singly."""
    lines: list[str] = []
    for flag, label in FLAGS.items():
        old, new = before.get(flag, ""), after.get(flag, "")
        if old == new:
            continue
        if flag == "-v":
            was, now = _options(old), _options(new)
            for name in dict.fromkeys([*was, *now]):
                if was.get(name) != now.get(name):
                    lines.append(f"{name}: {was.get(name, '(not set)')} -> "
                                 f"{now.get(name, '(not set)')}")
        else:
            lines.append(f"{label}: {old or '(none)'} -> {new or '(none)'}")
    return lines


def _options(params: str) -> dict[str, str]:
    """SvtAv1EncApp's flags as {--name: value}. A value may be negative: -1."""
    tokens = params.split()
    options: dict[str, str] = {}
    for at, token in enumerate(tokens):
        if token.startswith("--"):
            following = tokens[at + 1] if at + 1 < len(tokens) else ""
            options[token] = "" if following.startswith("--") else following
    return options


def check(
    argv: list[str], info: MediaInfo, read: MediaInfo, cfg: Config, work: Path,
    *, ask: Ask | None = None,
) -> tuple[list[str], str | None]:
    """Make Av1an's temp directory fit this encode, and say how to run it.

    Returns the command line to run -- `argv`, or `argv` with the flags the film
    started with -- and None; or a reason to leave the file for now. `ask` puts
    the choice to the operator; it defaults to a prompt when there is a terminal.
    """
    temp = work / "av1an"
    saved = work / FINGERPRINT
    now = {"source": [info.size_bytes, info.duration], "read": str(read.path),
           "flags": flags(argv)}
    earlier = fingerprint(work)

    if not (temp / "chunks.json").is_file():
        # Nothing cut yet, so nothing to keep. A scene list is kept apart.
        _save(saved, now)
        return argv, None
    if earlier is None:
        # Chunks from before fingerprints were kept: nothing to compare them with.
        log.info("  resuming chunks from an earlier version; taking them as made with "
                 "these settings")
        _save(saved, now)
        return argv, None

    if earlier.get("source") != now["source"]:
        log.warning("  the source has changed since its encode started; starting it again")
        return _restart(argv, temp, saved, now, keep_index=False)
    before = dict(earlier.get("flags") or {})
    was_read = str(earlier.get("read") or "")
    if (was_read and was_read != now["read"] and not Path(was_read).exists()
            and before.get("-m", now["flags"]["-m"]) in READS_SOURCE):
        log.warning("  the encode was reading %s, which is gone; starting it again", was_read)
        return _restart(argv, temp, saved, now, keep_index=False)

    # Av1an goes on reading the file it started with: its own script names it.
    now["read"] = was_read or now["read"]
    differences = changes(before, now["flags"])
    if not differences:
        _save(saved, now)
        return argv, None
    if earlier.get("continuing") == now["flags"]:
        log.info("  finishing with the settings it started with, as chosen before")
        return with_flags(argv, before), None

    done, total = _progress(temp)
    if not done:
        log.info("  settings changed before any chunk finished; starting with the new ones")
        return _restart(argv, temp, saved, now, keep_index=True)

    choice = cfg.library.on_changed_settings
    if choice == "ask":
        if ask is None and not sys.stdin.isatty():
            log.warning("  the settings changed since this file's encode started, %d of %d "
                        "chunks ago (%s). No terminal to ask on: leaving it for now. Set "
                        "library.on_changed_settings to restart, continue or skip to decide "
                        "without asking.", done, total, "; ".join(differences))
            return argv, "settings changed mid-encode; waiting for a decision"
        choice = (ask or prompt)(info.path.name, differences, done, total)

    if choice == "restart":
        log.info("  starting again with the new settings")
        return _restart(argv, temp, saved, now, keep_index=True)
    if choice == "continue":
        log.info("  finishing with the settings it started with; the new ones apply to "
                 "the next files")
        # Remembered, so that a stop and a rerun with the same settings do not ask again.
        _save(saved, {**earlier, "read": now["read"], "continuing": now["flags"]})
        return with_flags(argv, before), None
    log.info("  leaving it for now; its finished chunks are kept")
    return argv, "settings changed mid-encode; left for now"


def fingerprint(work: Path) -> dict | None:
    """The fingerprint the last encode in `work` left, if any."""
    try:
        saved = json.loads((work / FINGERPRINT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) else None


def _restart(argv: list[str], temp: Path, saved: Path, now: dict[str, object],
             *, keep_index: bool) -> tuple[list[str], str | None]:
    discard(temp, keep_index=keep_index)
    _save(saved, now)
    return argv, None


def discard(temp: Path, *, keep_index: bool) -> None:
    """Empty Av1an's temp directory, keeping the source's index if asked: indexing a
    film reads all of it again."""
    for entry in list(temp.iterdir()) if temp.is_dir() else []:
        if keep_index and entry.name == "split" and entry.is_dir():
            for inner in entry.iterdir():
                if not inner.name.startswith("cache."):
                    _remove(inner)
        else:
            _remove(entry)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _save(path: Path, fingerprint: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(fingerprint), encoding="utf-8")
    part.replace(path)


def _progress(temp: Path) -> tuple[int, int]:
    """How many of the film's chunks are finished, and how many there are."""
    try:
        done = len(json.loads((temp / "done.json").read_text(encoding="utf-8"))["done"])
    except (OSError, ValueError, KeyError, TypeError):
        done = 0
    try:
        total = len(json.loads((temp / "chunks.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        total = 0
    return done, total


def prompt(name: str, differences: list[str], done: int, total: int) -> str:
    """Ask on the terminal: restart, continue or skip."""
    lines = [f"{name}: the settings changed since its encode started "
             f"({done} of {total} chunks done):",
             *(f"    {line}" for line in differences),
             "  [r]estart with the new settings, discarding the finished chunks",
             "  [c]ontinue with the settings it started with; the new ones apply to the "
             "next files",
             "  [s]kip it for now, keeping the chunks"]
    print("\n".join(lines), file=sys.stderr, flush=True)
    while True:
        try:
            answer = input("Restart, continue or skip? [r/c/s] ").strip().lower()
        except EOFError:
            return "skip"
        for choice in ("restart", "continue", "skip"):
            if answer and choice.startswith(answer):
                return choice
