"""ffprobe wrapper producing a typed description of a source file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .tools import Toolchain
from .util import run

# Transfer characteristics that mean "this is HDR"; we must preserve their signalling.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

# Dolby Vision handling follows Jellyfin's classifier
# (MediaBrowser.Model/Entities/MediaStream.cs, GetVideoColorRange), because Jellyfin
# is the thing that will ultimately play these files and it has already settled which
# DV variants have a usable base layer. The RPU itself cannot be carried into AV1
# through Av1an's chunked pipeline, so what we keep is the base layer -- which for
# every profile except 5 (and 10 with compatibility 0) is a complete, correctly
# tagged HDR10, HLG or SDR picture. That is the same trade Jellyfin makes when it
# transcodes these titles on the fly.

# Profiles that carry an RPU at all.
DOVI_PROFILES = {5, 7, 8, 10}

# Codec tags that signal Dolby Vision even when the configuration record is absent.
DOVI_CODEC_TAGS = {"dovi", "dvh1", "dvhe", "dav1"}

# dv_bl_signal_compatibility_id -> what the base layer is on its own, per profile.
# Profile 8 is defined as always having a cross-compatible base layer, so an id of 0
# there is out of spec rather than meaningful; profile 10 (DV in AV1) may legitimately
# signal 0. Anything outside these is malformed, and handled as "invalid".
BL_COMPATIBILITY = {
    8: {1: "hdr10", 2: "sdr", 4: "hlg"},
    10: {0: "none", 1: "hdr10", 2: "sdr", 4: "hlg"},
}

# The colour signalling each usable base layer must actually carry. A file that
# claims HDR10 compatibility but is not tagged for it is out of spec, and we trust
# the tags rather than the claim.
EXPECTED_SIGNALLING = {
    "hdr10": ("bt2020", "smpte2084", "bt2020nc"),
    "hlg": ("bt2020", "arib-std-b67", "bt2020nc"),
}


@dataclass(slots=True)
class DolbyVision:
    """The DOVI configuration record, as ffprobe reports it in stream side data."""

    profile: int = 0
    level: int = 0
    compatibility_id: int = 0
    rpu_present: bool = False
    el_present: bool = False
    bl_present: bool = False

    @property
    def name(self) -> str:
        # "8.1" / "8.2" / "8.4" is the conventional notation; other profiles are bare.
        if not self.profile:
            return "unknown"
        return (
            f"{self.profile}.{self.compatibility_id}"
            if self.profile in {8, 10}
            else str(self.profile)
        )

    def claimed_base_layer(self) -> str:
        """What the signalling says the base layer is, before checking the tags."""
        if self.profile == 5:
            # IPT-PQ-C2. The stream is usually tagged bt2020/smpte2084 anyway, which
            # is a lie: without the RPU it decodes to badly skewed colour.
            return "none"
        if self.profile == 7:
            # Dual layer. Enhancement layer and RPU aside, the base layer is HDR10,
            # whatever the compatibility id says (muxers usually write 6).
            return "hdr10"
        if (by_id := BL_COMPATIBILITY.get(self.profile)) is not None:
            return by_id.get(self.compatibility_id, "invalid")
        return "invalid"

    def describe(self) -> str:
        layers = "dual-layer" if self.el_present else "single-layer"
        return f"DV profile {self.name} ({layers})"


@dataclass(slots=True)
class MasteringDisplay:
    """SMPTE ST 2086 mastering display colour volume: CIE xy primaries and nits."""

    green_x: float
    green_y: float
    blue_x: float
    blue_y: float
    red_x: float
    red_y: float
    white_x: float
    white_y: float
    max_luminance: float
    min_luminance: float

    def svt_argument(self) -> str:
        """The "G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)" string SvtAv1EncApp expects."""
        return (
            f"G({self.green_x:.4f},{self.green_y:.4f})"
            f"B({self.blue_x:.4f},{self.blue_y:.4f})"
            f"R({self.red_x:.4f},{self.red_y:.4f})"
            f"WP({self.white_x:.4f},{self.white_y:.4f})"
            f"L({self.max_luminance:.4f},{self.min_luminance:.4f})"
        )


@dataclass(slots=True)
class ContentLight:
    """CEA-861.3 content light level: MaxCLL and MaxFALL, in nits."""

    max_cll: int
    max_fall: int

    def svt_argument(self) -> str:
        return f"{self.max_cll},{self.max_fall}"


@dataclass(slots=True)
class Stream:
    index: int
    codec: str
    codec_type: str
    language: str = "und"
    title: str = ""
    channels: int = 0
    channel_layout: str = ""
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
    video_bytes: int = 0   # exact, from container tags; 0 when unknown
    # Pixel shape, "16:15" for 4:3 PAL, "64:45" for a 16:9 DVD. Empty when the
    # pixels are square. The encode carries none of it, so the mux restores it.
    sample_aspect: str = ""

    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    dolby_vision: DolbyVision | None = None
    mastering_display: MasteringDisplay | None = None
    content_light: ContentLight | None = None

    audio: list[Stream] = field(default_factory=list)
    subtitles: list[Stream] = field(default_factory=list)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    @property
    def has_dolby_vision(self) -> bool:
        return self.dolby_vision is not None

    @property
    def base_layer(self) -> str:
        """What we can actually encode: hdr10 | hlg | sdr | none | invalid.

        For a non-DV source this is just what the colour tags say. For a DV source
        it is the base layer, cross-checked against those tags: a file claiming
        HDR10 compatibility while tagged bt709 is out of spec, and the tags are what
        the decoder will hand us, so they win.
        """
        if self.dolby_vision is None:
            return self._signalled_range()

        claimed = self.dolby_vision.claimed_base_layer()
        if claimed in {"none", "invalid"}:
            return claimed
        expected = EXPECTED_SIGNALLING.get(claimed)
        if expected is not None and (
            self.color_primaries, self.color_transfer, self.color_space
        ) != expected:
            return "invalid"
        return claimed

    def _signalled_range(self) -> str:
        if self.color_transfer == "smpte2084":
            return "hdr10"
        if self.color_transfer == "arib-std-b67":
            return "hlg"
        return "sdr"

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
        dv = f" {self.dolby_vision.describe()}" if self.dolby_vision else ""
        return (
            f"{self.width}x{self.height} {self.codec} {self.bit_depth}-bit "
            f"{self.fps:.3f}fps{hdr}{dv}, {self.duration / 60:.1f} min, "
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


def _tag_int(stream: dict, name: str) -> int:
    """A Matroska statistics tag, e.g. NUMBER_OF_BYTES or BPS.

    mkvmerge writes these per track, and they are exact -- the video tag matches a
    full packet sum to the byte. Worth preferring, because the alternative is
    demuxing the whole file. Language-suffixed spellings (NUMBER_OF_BYTES-eng) are
    equally common, so match on the prefix.
    """
    tags = stream.get("tags") or {}
    for key, value in tags.items():
        if key.upper() == name or key.upper().startswith(f"{name}-"):
            return _int(value)
    return 0


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
        channel_layout=raw.get("channel_layout", ""),
        bitrate=_int(raw.get("bit_rate")),
        default=bool(disposition.get("default")),
        forced=bool(disposition.get("forced")),
    )


def _rational(value: object, default: float = 0.0) -> float:
    """ffprobe reports ST 2086 values as rationals, e.g. "34000/50000"."""
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        return default


def _side_data(entries: list[dict], name: str) -> dict | None:
    return next(
        (sd for sd in entries
         if str(sd.get("side_data_type", "")).lower() == name.lower()),
        None,
    )


def _dolby_vision(video: dict) -> DolbyVision | None:
    """Parse the DOVI configuration record, if this stream carries one."""
    record = _side_data(video.get("side_data_list") or [], "DOVI configuration record")
    if record is None:
        # Some muxes carry no configuration record but still tag the codec as DV.
        # Report it so the caller can decline rather than encode unknown colour.
        if str(video.get("codec_tag_string", "")).lower() in DOVI_CODEC_TAGS:
            return DolbyVision(profile=0, compatibility_id=-1)
        return None

    dv = DolbyVision(
        profile=_int(record.get("dv_profile")),
        level=_int(record.get("dv_level")),
        compatibility_id=_int(record.get("dv_bl_signal_compatibility_id")),
        rpu_present=bool(_int(record.get("rpu_present_flag"))),
        el_present=bool(_int(record.get("el_present_flag"))),
        bl_present=bool(_int(record.get("bl_present_flag"))),
    )
    # A record describing neither an RPU nor a base layer is not a DV stream.
    return dv if (dv.rpu_present or dv.profile in DOVI_PROFILES) else None


def _hdr10_metadata(
    path: Path, chain: Toolchain, video: dict
) -> tuple[MasteringDisplay | None, ContentLight | None]:
    """Read ST 2086 and CEA-861.3 metadata, which HEVC carries per frame.

    These do not appear in -show_streams, so this costs one extra ffprobe that
    decodes a single frame. Only PQ sources carry them, so only PQ sources pay.
    """
    if video.get("color_transfer") != "smpte2084":
        return None, None

    result = run([
        chain.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", "%+#1", "-show_frames",
        "-show_entries",
        "frame_side_data=side_data_type,red_x,red_y,green_x,green_y,blue_x,blue_y,"
        "white_point_x,white_point_y,min_luminance,max_luminance,max_content,max_average",
        "-of", "json", str(path),
    ], check=False)
    if result.returncode != 0:
        return None, None

    try:
        frames = json.loads(result.stdout).get("frames") or []
    except json.JSONDecodeError:
        return None, None
    entries = frames[0].get("side_data_list") or [] if frames else []

    mastering = None
    if (raw := _side_data(entries, "Mastering display metadata")) is not None:
        mastering = MasteringDisplay(
            green_x=_rational(raw.get("green_x")), green_y=_rational(raw.get("green_y")),
            blue_x=_rational(raw.get("blue_x")), blue_y=_rational(raw.get("blue_y")),
            red_x=_rational(raw.get("red_x")), red_y=_rational(raw.get("red_y")),
            white_x=_rational(raw.get("white_point_x")),
            white_y=_rational(raw.get("white_point_y")),
            max_luminance=_rational(raw.get("max_luminance")),
            min_luminance=_rational(raw.get("min_luminance")),
        )

    content_light = None
    if (raw := _side_data(entries, "Content light level metadata")) is not None:
        content_light = ContentLight(
            max_cll=_int(raw.get("max_content")),
            max_fall=_int(raw.get("max_average")),
        )
    return mastering, content_light


def _sample_aspect(value: str) -> str:
    """ffprobe's sample_aspect_ratio, or "" for square or unknown pixels."""
    match = re.fullmatch(r"(\d+):(\d+)", value or "")
    if not match or int(match[1]) == 0 or int(match[2]) == 0 or match[1] == match[2]:
        return ""
    return value


def probe(path: Path, chain: Toolchain) -> MediaInfo:
    # -show_streams already carries stream-level side_data_list, which is where the
    # DOVI configuration record lives. Do not ask for the "side_data" section on top
    # of it: that turns on packet side data, so ffprobe demuxes the entire file --
    # many minutes per title on a 4K remux, for information already in hand.
    result = run([
        chain.ffprobe, "-v", "error",
        "-show_format", "-show_streams",
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

    # Exact if the container says so, and Matroska usually does.
    video_bytes = _tag_int(video, "NUMBER_OF_BYTES")

    video_bitrate = _int(video.get("bit_rate")) or _tag_int(video, "BPS")
    if not video_bitrate and video_bytes and duration:
        video_bitrate = int(video_bytes * 8 / duration)
    if not video_bitrate:
        # Containers often omit per-stream bitrate; approximate from total minus a
        # nominal audio allowance. Only used for heuristics, never for encoding.
        total = _int(fmt.get("bit_rate"))
        video_bitrate = max(0, total - 640_000 * max(1, len([
            s for s in streams if s.get("codec_type") == "audio"
        ])))

    dolby_vision = _dolby_vision(video)
    mastering, content_light = _hdr10_metadata(path, chain, video)

    return MediaInfo(
        path=path,
        video_bytes=video_bytes,
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
        sample_aspect=_sample_aspect(video.get("sample_aspect_ratio", "")),
        color_primaries=video.get("color_primaries", ""),
        color_transfer=video.get("color_transfer", ""),
        color_space=video.get("color_space", ""),
        color_range=video.get("color_range", ""),
        dolby_vision=dolby_vision,
        mastering_display=mastering,
        content_light=content_light,
        audio=[_stream(s) for s in streams if s.get("codec_type") == "audio"],
        subtitles=[_stream(s) for s in streams if s.get("codec_type") == "subtitle"],
    )
