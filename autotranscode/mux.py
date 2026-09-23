"""Assemble the final file: encoded video plus the source's audio, subs and chapters."""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .probe import MediaInfo, Stream
from .tools import Toolchain
from .util import log, run

# Subtitle codecs that are plain text; the rest are bitmap (PGS, VobSub).
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Formats where re-encoding to Opus is a clear win: lossless or legacy-bloated.
WASTEFUL_AUDIO = {"truehd", "dts", "pcm_s16le", "pcm_s24le", "pcm_bluray", "mlp", "wmapro"}


def _opus_bitrate(stream: Stream, cfg: Config) -> int:
    channels = stream.channels or 2
    if cfg.audio.max_channels:
        channels = min(channels, cfg.audio.max_channels)
    return channels * cfg.audio.opus_kbps_per_channel


def _audio_args(info: MediaInfo, cfg: Config) -> list[str]:
    """Decide per-track whether to copy or re-encode, and say why in the log."""
    args: list[str] = []
    for position, stream in enumerate(info.audio):
        if cfg.audio.mode == "copy":
            decision = "copy"
        elif cfg.audio.mode == "opus":
            decision = "opus"
        else:  # smart
            if stream.codec in WASTEFUL_AUDIO:
                decision = "opus"
            elif stream.codec in cfg.audio.passthrough_codecs:
                decision = "copy"
            else:
                decision = "opus"

        if decision == "copy":
            args += [f"-c:a:{position}", "copy"]
            log.debug("audio %d (%s, %dch): copy", position, stream.codec, stream.channels)
        else:
            bitrate = _opus_bitrate(stream, cfg)
            args += [f"-c:a:{position}", "libopus", f"-b:a:{position}", f"{bitrate}k"]
            channels = stream.channels or 2
            if cfg.audio.max_channels and channels > cfg.audio.max_channels:
                channels = cfg.audio.max_channels
                args += [f"-ac:a:{position}", str(channels)]
            if channels > 2:
                # Required for libopus to encode surround layouts correctly.
                args += [f"-mapping_family:a:{position}", "1"]
            log.debug(
                "audio %d (%s, %dch): -> opus %dk", position, stream.codec, stream.channels, bitrate
            )
    return args


def _subtitle_maps(info: MediaInfo, cfg: Config) -> tuple[list[str], list[str]]:
    if cfg.subtitles.mode == "none" or not info.subtitles:
        return [], []
    if cfg.subtitles.mode == "copy":
        return ["-map", "1:s?"], ["-c:s", "copy"]

    maps: list[str] = []
    for position, stream in enumerate(info.subtitles):
        if stream.codec in TEXT_SUBTITLE_CODECS:
            maps += ["-map", f"1:s:{position}"]
    return (maps, ["-c:s", "copy"]) if maps else ([], [])


# output.container -> ffmpeg muxer name.
MUXERS = {"mkv": "matroska", "webm": "webm"}


def build_command(
    video: Path, info: MediaInfo, output: Path, chain: Toolchain, cfg: Config
) -> list[str]:
    """Mux the encoded video (input 0) with everything else from the source (input 1)."""
    sub_maps, sub_codec = _subtitle_maps(info, cfg)
    argv = [
        chain.ffmpeg, "-hide_banner", "-v", "warning", "-y",
        "-i", str(video),
        "-i", str(info.path),
        "-map", "0:v:0",
    ]
    if info.audio:
        argv += ["-map", "1:a?"]
    argv += sub_maps
    argv += ["-c:v", "copy"]
    argv += _audio_args(info, cfg)
    argv += sub_codec
    argv += [
        "-map_chapters", "1",
        "-map_metadata", "1",
        # Keep track names and languages, which Jellyfin relies on for selection.
        "-disposition:v:0", "default",
        # Named, not inferred: the pipeline writes to "<name>.part" and renames it
        # into place, and ffmpeg cannot pick a muxer from a .part extension.
        "-f", MUXERS[cfg.output.container],
        str(output),
    ]
    return argv


def mux(
    video: Path, info: MediaInfo, output: Path, chain: Toolchain, cfg: Config,
    dry_run: bool = False,
) -> Path:
    run(build_command(video, info, output, chain, cfg), dry_run=dry_run)
    return output
