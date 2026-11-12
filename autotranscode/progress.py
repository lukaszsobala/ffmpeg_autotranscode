"""Av1an's progress bar, redrawn to fit the terminal.

Av1an draws one line of some 120 columns before its bar gets any: time taken,
chunks, percent, frames, speed, time left, bitrate and size. On a narrower
terminal the line wraps, and each redraw, which clears the lines it believes it
drew, falls out of step with what is on the screen, until the bar is not to be
seen at all. Its layout is fixed. So Av1an is given a pseudo-terminal wide enough
for its line, the line is read back, and drawn here to the terminal's width: on
one line when it fits, on two when it does not, the least needed figures dropped
first. The film's name goes on a line of its own above it: what Av1an itself says
names only the staged copy, and the line that named the film has long scrolled
away by the time anyone looks. Anything else Av1an writes is passed through above
both, as are this program's own log lines while it runs, save one harmless
warning about audio (AUDIO_WARNING).

Only when the terminal is one: written to a file, Av1an draws no bar anyway.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TextIO

# The pseudo-terminal Av1an draws on: wide enough that its line never wraps.
COLUMNS, ROWS = 300, 50
# Redraws at most this often; Av1an's own come ten times a second.
REDRAW_SECONDS = 0.25

# Av1an's bar, the same in 0.5.2 and 0.6.0 (progress_bar.rs, INDICATIF_PROGRESS_TEMPLATE),
# colours removed:
#   00:12:01 [12/431 Chunks] ▐████    ▌  42% 9312/212784 (1.71 fps, eta 35h,
#                                           4521.3 Kbps, est. 12.3 GiB)
# and while scene detection does not yet know the length (INDICATIF_SC_SPINNER_TEMPLATE):
#   00:00:53 [   ]  91 frames (1.71 fps)
# The bar's cells include the partial blocks "▉▊▋▌▍▎▏", so its own closing "▌" is
# only the one followed by the percentage.
BAR = re.compile(
    r"(?P<elapsed>\d+:\d\d:\d\d) +(?:\[(?P<done>\d+)/(?P<chunks>\d+) Chunks\] *)?"
    r"▐.*?▌ +(?P<percent>\d+)% +(?P<pos>\d+)/(?P<length>\d+) +"
    r"\((?P<rate>[^,()]+), eta (?P<eta>[^,()]+)"
    r"(?:, (?P<kbps>[\d.]+) Kbps, est\. (?P<size>[^,()]+))?\)")
SPINNER = re.compile(r"(?P<elapsed>\d+:\d\d:\d\d) +\[[^\]]*\] +(?P<pos>\d+) frames "
                     r"\((?P<rate>[^()]+)\)")
# Where one draw of a line ends: a line end, a return, or a cursor movement or
# erase. Colour codes do not end one.
BREAK = re.compile(r"\r\n|\r|\n|\x1b\[[0-9;?]*[@-ln-~]")
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[@-~]|\x1b[@-_]")
# A draw of the bar that BAR does not read, in a layout some other Av1an has. Dropped:
# passed through, every draw of it would pile up above the bar as one long line.
STRAY = re.compile(r"\d+:\d\d:\d\d +.*▐.*▌")
# Av1an copies the source's audio aside whenever it has any, -a -an or not
# (av1an-core 0.5.2 and 0.6.0, ffmpeg.rs encode_audio). With the audio left out, a source
# without subtitles leaves ffmpeg nothing to write, and Av1an says so in a dozen
# lines of Rust debug output before carrying on. Nothing is lost: the mux takes
# audio and subtitles from the source. So the warning and its dump are dropped,
# up to the "Params:" line that ends them.
AUDIO_WARNING = re.compile(r"\bFFmpeg failed to encode audio!$")
AUDIO_DUMP = re.compile(r'\s*(?:Output \{|status: \w+\(|\w+\(|\d+,|\),?|\}|'
                        r'(?:stdout|stderr): ".*",)$')
AUDIO_PARAMS = re.compile(r"\s*Params: ")

PARTIAL_BLOCKS = " ▏▎▍▌▋▊▉"

# What the bars drawn now are about; see `showing`.
_title = ""


@contextlib.contextmanager
def showing(title: str) -> Iterator[None]:
    """Name `title` above every bar drawn inside the block."""
    global _title
    before, _title = _title, title
    try:
        yield
    finally:
        _title = before


@dataclass(slots=True)
class Status:
    """What Av1an's bar last said."""

    elapsed: str
    pos: int
    rate: str
    percent: int | None = None
    length: int | None = None
    eta: str | None = None
    chunks: tuple[int, int] | None = None
    kbps: float | None = None
    size: str | None = None


def parse(text: str) -> tuple[Status | None, str]:
    """The last bar in `text`, colours removed, and what is left of `text` without it."""
    status = None
    for pattern in (BAR, SPINNER):
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        found = matches[-1]
        if pattern is BAR:
            status = Status(
                elapsed=found["elapsed"], pos=int(found["pos"]), rate=found["rate"].strip(),
                percent=int(found["percent"]), length=int(found["length"]),
                eta=found["eta"].strip(),
                chunks=(int(found["done"]), int(found["chunks"])) if found["done"] else None,
                kbps=float(found["kbps"]) if found["kbps"] else None,
                size=found["size"].strip() if found["size"] else None)
        else:
            status = Status(elapsed=found["elapsed"], pos=int(found["pos"]),
                            rate=found["rate"].strip())
        text = pattern.sub("", text)
        break
    return status, text.strip()


def bar(fraction: float, width: int) -> str:
    """A bar `width` cells wide, filled to `fraction` in eighths of a cell."""
    eighths = round(max(0.0, min(fraction, 1.0)) * width * 8)
    full, part = divmod(eighths, 8)
    drawn = "█" * full + (PARTIAL_BLOCKS[part] if part and full < width else "")
    return drawn.ljust(width)


def render(status: Status, width: int) -> list[str]:
    """The status as lines no wider than `width - 1` (a full line would leave the
    cursor past the edge, which terminals disagree on): one line when the bar and
    every figure fit, else the bar on the first and the figures that fit on the
    second."""
    width = max(width - 1, 20)
    if status.percent is None:
        return [_fit([f"scenes: {status.pos:,} frames", status.rate, status.elapsed], width)]

    head = f"{status.percent:3d}%"
    tail = f"eta {status.eta}" if status.eta else ""
    figures = []
    if status.chunks:
        figures.append(f"{status.chunks[0]}/{status.chunks[1]} chunks")
    figures.append(status.rate)
    if status.size:
        figures.append(f"est. {status.size}")
    if status.kbps is not None:
        figures.append(f"{status.kbps:,.0f} kb/s")
    if not status.chunks:   # scene detection: frames are all there is to count
        figures.append(f"{status.pos:,}/{status.length:,} frames" if status.length
                       else f"{status.pos:,} frames")
    figures.append(status.elapsed)
    fraction = status.percent / 100 if not status.length else status.pos / status.length

    details = "  ".join(figures)
    fixed = len(head) + len(tail) + len(details) + 8   # spaces and the bar's ends
    if width - fixed >= 10:
        cells = min(width - fixed, 40)
        return [f"{head} ▐{bar(fraction, cells)}▌ {tail}  {details}"]
    cells = max(width - len(head) - len(tail) - 5, 1)
    return [f"{head} ▐{bar(fraction, cells)}▌ {tail}"[:width], _fit(figures, width)]


def heading(title: str, width: int) -> str:
    """`title` cut short, if need be, to the width `render` keeps to."""
    width = max(width - 1, 20)
    return title if len(title) <= width else title[:width - 1].rstrip() + "…"


def _fit(figures: list[str], width: int) -> str:
    """As many of `figures`, from the first, as fit in `width`."""
    line = ""
    for figure in figures:
        joined = f"{line}  {figure}" if line else figure
        if len(joined) > width:
            break
        line = joined
    return line or figures[0][:width]


def terminal_width(stream: TextIO) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return 80


class Screen:
    """The bar at the bottom of the terminal, under the title if there is one, with
    whatever else is written going above them. Safe to call from any thread."""

    def __init__(self, out: TextIO, width: TextIO | int | None = None, title: str = ""):
        self.out = out
        self._width = width
        self.title = title   # drawn on a line above the bar
        self.lock = threading.RLock()
        self.status: Status | None = None
        self._drawn = 0           # lines of the bar on the screen
        self._last_draw = 0.0
        # Set once the command is being stopped: what it says then is dropped.
        self.muted = False
        self._finished = False    # and nothing at all is written after `finish`

    def width(self) -> int:
        if isinstance(self._width, int):
            return self._width
        return terminal_width(self._width or self.out)

    def update(self, status: Status, *, now: bool = False) -> None:
        with self.lock:
            self.status = status
            if now or time.monotonic() - self._last_draw >= REDRAW_SECONDS:
                self._redraw()

    def write_above(self, text: str) -> None:
        """Write `text`, whole lines, above the bar."""
        with self.lock:
            if self._finished:
                return
            self._clear()
            self.out.write(text)
            self._redraw()

    def clear(self) -> None:
        with self.lock:
            self._clear()
            self.out.flush()

    def mute(self) -> None:
        with self.lock:
            self.muted = True

    def finish(self) -> None:
        """Leave the last bar on the screen, the cursor below it, and draw no more."""
        with self.lock:
            if self._finished:
                return
            self.muted = self._finished = True
            if self.status is not None:
                self._redraw()
            if self._drawn:
                self.out.write("\n")
                self.out.flush()
            self._drawn = 0

    def _clear(self) -> None:
        if self._drawn:
            self.out.write("\r\x1b[2K" + "\x1b[1A\x1b[2K" * (self._drawn - 1))
            self._drawn = 0

    def _redraw(self) -> None:
        self._clear()
        if self.status is not None:
            width = self.width()
            lines = render(self.status, width)
            if self.title:
                lines.insert(0, heading(self.title, width))
            self.out.write("\n".join(lines))
            self._drawn = len(lines)
            self._last_draw = time.monotonic()
        self.out.flush()


class Feed:
    """Av1an's terminal output, taken apart: its bar goes to the screen's bar,
    every other line above it."""

    def __init__(self, screen: Screen):
        self.screen = screen
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""   # output not yet ended by a break
        self._text = ""      # a line's text, gathered across cursor movements
        self._in_audio_warning = False

    def feed(self, data: bytes) -> None:
        self._pending += self._decoder.decode(data)
        start = 0
        for brk in BREAK.finditer(self._pending):
            self._segment(self._pending[start:brk.start()], newline="\n" in brk.group())
            start = brk.end()
        self._pending = self._pending[start:]

    def end(self) -> None:
        self._pending += self._decoder.decode(b"", final=True)
        self._segment(self._pending, newline=True)
        self._pending = ""

    def _segment(self, raw: str, *, newline: bool) -> None:
        if self.screen.muted:
            return
        status, rest = parse(ESCAPE.sub("", raw))
        if status is not None:
            self.screen.update(status)
        elif STRAY.search(rest):
            return
        if rest:
            self._text = f"{self._text} {rest}" if self._text else rest
        if newline and self._text:
            self._line(self._text)
            self._text = ""

    def _line(self, text: str) -> None:
        if self._in_audio_warning:
            if AUDIO_PARAMS.match(text):
                self._in_audio_warning = False
                return
            if AUDIO_DUMP.match(text):
                return
            # Not the dump as Av1an 0.5.2 and 0.6.0 print it: show the rest.
            self._in_audio_warning = False
        elif AUDIO_WARNING.search(text):
            self._in_audio_warning = True
            return
        self.screen.write_above(text + "\n")


def usable(stream: TextIO) -> bool:
    """Whether to redraw Av1an's bar: on a terminal, where pseudo-terminals exist."""
    try:
        return os.name == "posix" and stream.isatty()
    except (AttributeError, ValueError):
        return False


class Relay:
    """A pseudo-terminal for Av1an to draw on, read in a thread of its own and
    redrawn on `out`. While it runs, log lines written to `out` go above the bar.

    Give the child `child_fd` as its stdout and stderr, then call `started()`;
    `stop()` before stopping it, and `close()` once it has exited.
    """

    def __init__(self, out: TextIO | None = None):
        import fcntl
        import pty
        import struct
        import termios

        self.out = out or sys.stderr
        self.screen = Screen(self.out, title=_title)
        self.master, self.child_fd = pty.openpty()
        fcntl.ioctl(self.child_fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._handlers: list[tuple[logging.StreamHandler, TextIO]] = []

    def started(self) -> None:
        """The child has the pseudo-terminal: start reading it."""
        os.close(self.child_fd)
        self.child_fd = -1
        self._divert_logging()
        self._thread.start()

    def stop(self) -> None:
        """The command is being stopped: drop what it and its children say while
        they die (broken pipes, "Terminated", encoder errors), one line each."""
        self.screen.mute()

    def close(self, wait: float = 5.0) -> None:
        if self.screen.muted:
            wait = min(wait, 0.5)   # nothing more of it is shown
        try:
            if self.child_fd >= 0:
                os.close(self.child_fd)
                self.child_fd = -1
            if self._thread.is_alive():
                # Whatever the child started may hold the terminal open after it.
                self._thread.join(wait)
            with contextlib.suppress(OSError):
                os.close(self.master)
            if self._thread.ident is not None:
                self._thread.join(1.0)
        finally:
            self.screen.finish()
            for handler, stream in self._handlers:
                handler.setStream(stream)
            self._handlers.clear()

    def _read(self) -> None:
        feed = Feed(self.screen)
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:   # EIO: the child's end is closed
                break
            if not data:
                break
            feed.feed(data)
        feed.end()

    def _divert_logging(self) -> None:
        """Have the log handlers writing to `out` write above the bar instead."""
        above = _Above(self.screen)
        for logger in (logging.getLogger(), logging.getLogger("autotranscode")):
            for handler in logger.handlers:
                if (type(handler) is logging.StreamHandler
                        and getattr(handler, "stream", None) is self.out):
                    handler.setStream(above)
                    self._handlers.append((handler, self.out))


class _Above:
    """A stream for a log handler: each line it writes goes above the bar."""

    def __init__(self, screen: Screen):
        self.screen = screen
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            text, self._buffer = self._buffer, ""
            self.screen.write_above(text if text.endswith("\n") else text + "\n")
