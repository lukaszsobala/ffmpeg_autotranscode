"""ffprobe wrapper producing a typed description of a source file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .tools import Toolchain
from .util import run

# Transfer characteristics that mean "this is HDR"; we must preserve their signalling.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass(slots=True)
class Stream:
    index: int
    codec: str
    codec_type: str
    language: str = "und"
    title: str = ""
    channels: int = 0
    bitrate: int = 0
    default: bool = False
    forced: bool = False


@dataclass(slots=True)
class MediaInfo:
    path: Path
    size_bytes: int
    duration: float
    container: str

    codec: str
    width: int
    height: int
    pix_fmt: str
    bit_depth: int
    fps: float
    frames: int
    video_bitrate: int

    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    has_dolby_vision: bool = False

    audio: list[Stream] = field(default_factory=list)
    subtitles: list[Stream] = field(default_factory=list)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    @property
    def pixels_per_second(self) -> float:
        return self.width * self.height * self.fps

    @property
    def bits_per_pixel(self) -> float:
        """Rough source efficiency measure; very low values mean re-encoding won't help."""
        if not self.pixels_per_second:
            return 0.0
        return self.video_bitrate / self.pixels_per_second

    def describe(self) -> str:
        hdr = f" HDR({self.color_transfer})" if self.is_hdr else ""
        return (
            f"{self.width}x{self.height} {self.codec} {self.bit_depth}-bit "
            f"{self.fps:.3f}fps{hdr}, {self.duration / 60:.1f} min, "
            f"{len(self.audio)} audio / {len(self.subtitles)} sub"
        )


def _parse_fps(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bit_depth(pix_fmt: str, stream: dict) -> int:
    if raw := stream.get("bits_per_raw_sample"):
        return _int(raw, 8)
    if "p10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt:
        return 10
    if "p12" in pix_fmt or "12le" in pix_fmt:
        return 12
    return 8


def _stream(raw: dict) -> Stream:
    tags = raw.get("tags") or {}
    disposition = raw.get("disposition") or {}
    return Stream(
        index=_int(raw.get("index")),
        codec=raw.get("codec_name", "unknown"),
        codec_type=raw.get("codec_type", "unknown"),
        language=tags.get("language", "und"),
        title=tags.get("title", ""),
        channels=_int(raw.get("channels")),
        bitrate=_int(raw.get("bit_rate")),
        default=bool(disposition.get("default")),
        forced=bool(disposition.get("forced")),
    )


def probe(path: Path, chain: Toolchain) -> MediaInfo:
    result = run([
        chain.ffprobe, "-v", "error",
        "-show_format", "-show_streams", "-show_entries", "side_data",
        "-of", "json", str(path),
    ])
    data = json.loads(result.stdout)

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video = next(
        (s for s in streams
         if s.get("codec_type") == "video"
         and (s.get("disposition") or {}).get("attached_pic", 0) == 0),
        None,
    )
    if video is None:
        raise ValueError(f"no video stream in {path}")

    pix_fmt = video.get("pix_fmt", "yuv420p")
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(video.get("r_frame_rate"))

    frames = _int(video.get("nb_frames"))
    if not frames and duration and fps:
        frames = int(duration * fps)

    video_bitrate = _int(video.get("bit_rate"))
    if not video_bitrate:
        # Containers often omit per-stream bitrate; approximate from total minus a
        # nominal audio allowance. Only used for heuristics, never for encoding.
        total = _int(fmt.get("bit_rate"))
        video_bitrate = max(0, total - 640_000 * max(1, len([
            s for s in streams if s.get("codec_type") == "audio"
        ])))

    side_data = video.get("side_data_list") or []
    has_dv = any("Dolby Vision" in str(sd.get("side_data_type", "")) for sd in side_data)

    return MediaInfo(
        path=path,
        size_bytes=_int(fmt.get("size")) or path.stat().st_size,
        duration=duration,
        container=fmt.get("format_name", ""),
        codec=video.get("codec_name", "unknown"),
        width=_int(video.get("width")),
        height=_int(video.get("height")),
        pix_fmt=pix_fmt,
        bit_depth=_bit_depth(pix_fmt, video),
        fps=fps,
        frames=frames,
        video_bitrate=video_bitrate,
        color_primaries=video.get("color_primaries", ""),
        color_transfer=video.get("color_transfer", ""),
        color_space=video.get("color_space", ""),
        color_range=video.get("color_range", ""),
        has_dolby_vision=has_dv,
        audio=[_stream(s) for s in streams if s.get("codec_type") == "audio"],
        subtitles=[_stream(s) for s in streams if s.get("codec_type") == "subtitle"],
    )
