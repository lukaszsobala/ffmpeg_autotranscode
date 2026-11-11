"""Assemble the final file: encoded video plus the source's audio, subs and chapters."""

from __future__ import annotations

import math
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .probe import MediaInfo, Stream
from .tools import Toolchain
from .util import CommandError, child_env, log, run

# Subtitle codecs that are plain text; the rest are bitmap (PGS, VobSub).
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Text subtitles Matroska cannot hold, and what each becomes instead. MP4's own
# text track fails the mux ("Subtitle codec mov_text is not supported") -- after
# the whole encode has been paid for.
MKV_SUBTITLE_CONVERSIONS = {"mov_text": "srt"}

# Formats where re-encoding to Opus is a clear win: lossless or legacy-bloated.
WASTEFUL_AUDIO = {"truehd", "dts", "pcm_s16le", "pcm_s24le", "pcm_bluray", "mlp", "wmapro"}

# Layouts libopus refuses for surround (mapping family 1), and the one each is
# relabelled to. Most DTS, TrueHD and AC-3 5.1 is "5.1(side)", which differs from
# Opus's 5.1 only in calling the surround pair side rather than back speakers.
# jellyfin-ffmpeg patches the encoder to accept it; mainline ffmpeg fails the mux,
# after the whole video encode has been paid for.
OPUS_LAYOUTS = {"5.1(side)": "5.1", "5.0(side)": "5.0", "quad(side)": "quad"}

# How much of the source `rehearse` muxes before the encode.
REHEARSAL_SECONDS = 5
# Progress on stdout, twice a second, for mux_until to stop the rehearsal by.
REHEARSAL_PROGRESS = ("-progress", "pipe:1", "-stats_period", "0.5")


def _opus_bitrate(stream: Stream, cfg: Config) -> int:
    channels = stream.channels or 2
    if cfg.audio.max_channels:
        channels = min(channels, cfg.audio.max_channels)
    return channels * cfg.audio.opus_kbps_per_channel


def audio_decision(stream: Stream, cfg: Config) -> str:
    """"copy" or "opus" for one audio track."""
    if cfg.audio.mode in ("copy", "opus"):
        return cfg.audio.mode
    # smart
    if stream.codec in WASTEFUL_AUDIO:
        return "opus"
    return "copy" if stream.codec in cfg.audio.passthrough_codecs else "opus"


def audio_saving_bytes(info: MediaInfo, cfg: Config) -> int:
    """Roughly what re-encoding audio to Opus will save, for a size projection.

    Counts only the tracks whose bitrate is known, so it errs towards saving less.
    """
    saved = 0.0
    for stream in info.audio:
        if audio_decision(stream, cfg) == "opus" and stream.bitrate:
            saved += max(0, stream.bitrate - _opus_bitrate(stream, cfg) * 1000) * info.duration / 8
    return int(saved)


def _audio_args(info: MediaInfo, cfg: Config) -> list[str]:
    """Decide per-track whether to copy or re-encode, and say why in the log."""
    args: list[str] = []
    for position, stream in enumerate(info.audio):
        decision = audio_decision(stream, cfg)
        if decision == "copy":
            args += [f"-c:a:{position}", "copy"]
            log.debug("audio %d (%s, %dch): copy", position, stream.codec, stream.channels)
        else:
            bitrate = _opus_bitrate(stream, cfg)
            args += [f"-c:a:{position}", "libopus", f"-b:a:{position}", f"{bitrate}k"]
            # ffmpeg copies the track's tags, mkvmerge's statistics with them: an
            # Opus track made from DTS-HD MA kept BPS-eng=3933034, and Jellyfin
            # shows it. An empty value deletes the tag; -map_metadata would take
            # the language and title too.
            for name in stream.statistics_tags:
                args += [f"-metadata:s:a:{position}", f"{name}="]
            channels = stream.channels or 2
            if cfg.audio.max_channels and channels > cfg.audio.max_channels:
                channels = cfg.audio.max_channels
                args += [f"-ac:a:{position}", str(channels)]
            if channels > 2:
                # Required for libopus to encode surround layouts correctly.
                args += [f"-mapping_family:a:{position}", "1"]
                if channels == stream.channels and stream.channel_layout in OPUS_LAYOUTS:
                    # Same channels in the same order, only renamed.
                    layout = OPUS_LAYOUTS[stream.channel_layout]
                    args += [f"-filter:a:{position}", f"channelmap=channel_layout={layout}"]
            log.debug(
                "audio %d (%s, %dch): -> opus %dk", position, stream.codec, stream.channels, bitrate
            )
    return args


def _subtitle_args(info: MediaInfo, cfg: Config) -> tuple[list[str], list[str]]:
    """(-map options, codec options) for the subtitle tracks kept.

    Copied, except for a text format Matroska cannot hold, which is converted.
    """
    if cfg.subtitles.mode == "none":
        return [], []
    kept = [(position, stream) for position, stream in enumerate(info.subtitles)
            if cfg.subtitles.mode == "copy" or stream.codec in TEXT_SUBTITLE_CODECS]
    maps: list[str] = []
    codecs: list[str] = ["-c:s", "copy"] if kept else []
    for out, (position, stream) in enumerate(kept):
        maps += ["-map", f"1:s:{position}"]
        if (target := MKV_SUBTITLE_CONVERSIONS.get(stream.codec)) is not None:
            codecs += [f"-c:s:{out}", target]
    return maps, codecs


def display_aspect(info: MediaInfo) -> str:
    """The picture's display aspect as "W:H" when its pixels are not square."""
    if not info.sample_aspect or not info.width or not info.height:
        return ""
    num, den = (int(part) for part in info.sample_aspect.split(":"))
    width, height = info.width * num, info.height * den
    common = math.gcd(width, height)
    return f"{width // common}:{height // common}"


def build_command(
    video: Path, info: MediaInfo, output: Path, chain: Toolchain, cfg: Config
) -> list[str]:
    """Mux the encoded video (input 0) with everything else from the source (input 1)."""
    sub_maps, sub_codec = _subtitle_args(info, cfg)
    argv = [chain.ffmpeg, "-hide_banner", "-v", "warning", "-y"]
    if info.video_start:
        # ffmpeg shifts each input to start at 0, so a source whose video starts
        # after its audio lost the gap: a test file with video at 0.500 s came out
        # at 0, the sound half a second late. -itsoffset applies after that shift,
        # so it is right whether or not keep_source_timing gave the encode the
        # source's own frame times, which already include the gap.
        argv += ["-itsoffset", f"{info.video_start:.6f}"]
    argv += [
        "-i", str(video),
        "-i", str(info.path),
        "-map", "0:v:0",
    ]
    if info.audio:
        argv += ["-map", "1:a?"]
    argv += sub_maps
    # Attachments: the fonts an ASS subtitle track is styled with. Without them
    # a player falls back to its own font, and the subtitles lose their look.
    argv += ["-map", "1:t?"]
    argv += ["-c:v", "copy"]
    if aspect := display_aspect(info):
        # AV1 has no pixel-shape field for ffmpeg to copy, so without this a 4:3
        # PAL DVD plays at 5:4 and a 16:9 one at nearly 4:3.
        argv += ["-aspect", aspect]
    argv += _audio_args(info, cfg)
    argv += sub_codec
    argv += [
        "-map_chapters", "1",
        # Keep the title and tags. Track names and languages come with each mapped
        # stream, and Jellyfin relies on them for selection.
        "-map_metadata", "1",
        "-disposition:v:0", "default",
        # Named, not inferred: the pipeline writes to "<name>.part" and renames it
        # into place, and ffmpeg cannot pick a muxer from a .part extension.
        "-f", "matroska",
        str(output),
    ]
    return argv


def rehearse(info: MediaInfo, chain: Toolchain, cfg: Config, work: Path) -> None:
    """Run the real mux over the source's first seconds, before anything is encoded.

    A track the mux cannot take -- the mov_text and 5.1(side) failures above --
    otherwise fails the file after the whole encode. A small black FFV1 clip
    stands in for the encode: the video is copied whatever it is, so what is on
    trial is the source's own audio, subtitles, fonts and chapters. Raises
    CommandError as the real mux would.
    """
    stand_in = work / "rehearsal_video.mkv"
    output = work / "rehearsal.mkv"
    try:
        run([chain.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=25:d={REHEARSAL_SECONDS}",
             "-c:v", "ffv1", str(stand_in)])
        argv = build_command(stand_in, info, output, chain, cfg)
        mux_until(argv[:1] + list(REHEARSAL_PROGRESS) + argv[1:], REHEARSAL_SECONDS)
    finally:
        stand_in.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def mux_until(argv: list[str], seconds: float,
              clock: Callable[[], float] = time.monotonic) -> None:
    """Run an ffmpeg that reports progress on stdout and stop it once it has muxed
    `seconds` of output, or written for `seconds`. Raises CommandError if it fails
    before then.

    ffmpeg's own limits do not stop it in time. -t, on the input or the output,
    ends a stream only once that stream passes the mark, so a subtitle track with
    nothing in its first seconds sends the demuxer through the whole file for one:
    197 s over 42 GB for a film with 13 PGS tracks, with -shortest added as well.
    A "q" on a pipe is ignored. Nor does the output time always move: with 21 PGS
    tracks to interleave it stayed N/A for 20 s while 74 MB were written. But
    ffmpeg writes the header only once every encoder has started and the muxer has
    taken every track, which is where these failures happen; so once there is
    output, a few seconds of muxing is all there is to learn.
    """
    log.debug("exec: %s", shlex.join(argv))
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace",
                            env=child_env())
    assert proc.stdout is not None and proc.stderr is not None
    stderr: list[str] = []
    err = proc.stderr   # the assert does not narrow inside a lambda
    drain = threading.Thread(target=lambda: stderr.append(err.read()), daemon=True)
    drain.start()
    reached = False
    writing_since: float | None = None
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if not value.isdigit():
                continue
            if key == "total_size" and int(value) > 0 and writing_since is None:
                writing_since = clock()
            long_enough = key == "out_time_us" and int(value) >= seconds * 1e6
            if long_enough or (writing_since is not None
                               and clock() - writing_since >= seconds):
                reached = True
                proc.kill()
                break
    except BaseException:
        proc.kill()
        raise
    finally:
        returncode = proc.wait()
        proc.stdout.close()
        drain.join()
        proc.stderr.close()
    if not reached and returncode != 0:
        raise CommandError(argv, returncode, "".join(stderr))


def mux(
    video: Path, info: MediaInfo, output: Path, chain: Toolchain, cfg: Config,
    dry_run: bool = False,
) -> Path:
    run(build_command(video, info, output, chain, cfg), dry_run=dry_run)
    return output
