# autotranscode

Transcodes a media library to AV1 with **source-accurate film grain**, by orchestrating
tools that already do the hard parts well:

| Tool | What it does here |
|---|---|
| [Av1an](https://github.com/rust-av/Av1an) | Scene detection, chunking, parallel workers, resume, optional target-quality search |
| [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) 4.x | The encoder (`SvtAv1EncApp`) |
| [grav1synth](https://github.com/rust-av/grav1synth) | Derives a film grain table from the source and writes it into the AV1 bitstream |
| [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) 8 | Probing, denoising, muxing; brings `libvmaf` and Rockchip `rkmpp`/`rkrga` |

This repository contributes only the policy layer: what to encode, how grainy it is,
which audio to keep, and what to do when a step fails.

## Why film grain gets special treatment

Grain is close to random, so it is close to incompressible: on a grainy film most of
the bitrate goes into reproducing noise. AV1 can avoid that. The encoder strips the
grain, carries a compact description of it in the bitstream, and the decoder
re-synthesises matching grain at playback. The picture still looks like film and the
file gets dramatically smaller.

Doing it *well* means describing the grain that is actually there, rather than
sprinkling on a generic ISO-noise preset. `grav1synth diff` compares the source with
a denoised copy of itself and solves for the real parameters, which is the route this
tool takes.

Two constraints shaped the implementation, and they are worth knowing before you
change anything:

1. **Av1an chunks the video, and each chunk restarts at timestamp zero.** A grain
   table with real timestamps would therefore land on the wrong frames if handed to
   the encoder mid-pipeline. So grain is applied *after* encoding, to the finished
   stream, with `grav1synth apply`.
2. **`--fgs-table` is reachable only through the SvtAv1Enc interface**, not through
   `ffmpeg -svtav1-params`. Anything built on `ffmpeg -c:v libsvtav1` cannot load a
   grain table at all.

## Pipeline

```
source.mkv
   │
   ├── ffprobe ......................... resolution, depth, HDR, streams
   │
   ├── grain probe ..................... mean PSNR vs a reference-denoised copy
   │      └── clean? ................... skip straight to encoding
   │
   ├── grain table (if grainy)
   │      ├── extract N short LOSSLESS excerpts
   │      ├── denoise them
   │      ├── grav1synth diff .......... solve for grain parameters
   │      └── flatten .................. one segment spanning the whole runtime
   │
   ├── av1an -e svt-av1 -f "-vf <denoise>"
   │      └── scene-split, parallel, resumable → video-only AV1
   │
   ├── grav1synth apply -g grain.tbl ... write grain into the bitstream
   │
   └── ffmpeg mux ...................... + audio, subtitles, chapters, metadata
          └── output.av1.mkv
```

Denoising happens *inside* Av1an's chunk pipeline, so no full-size intermediate is
ever written. Only the short analysis excerpts touch the disk as lossless files.

## Install

On a Rockchip RK3588 board, the bootstrap script does everything:

```bash
./scripts/bootstrap-rk3588.sh          # or: ./scripts/bootstrap-rk3588.sh ffmpeg svtav1
```

Elsewhere you need `jellyfin-ffmpeg`, `SvtAv1EncApp`, `av1an`, `grav1synth` and
`mkvtoolnix` on `PATH`. Then:

```bash
pip install -e .          # optional; `python3 -m autotranscode` works without it
autotranscode doctor      # says exactly what is missing and how to get it
```

Python 3.11+ and no Python dependencies at all — everything heavy is an external binary.

## Use

```bash
cp config.example.toml ~/.config/autotranscode.toml

autotranscode -c ~/.config/autotranscode.toml doctor
autotranscode -c ~/.config/autotranscode.toml measure /media/films        # calibrate first
autotranscode -c ~/.config/autotranscode.toml plan    /media/films        # see the commands
autotranscode -c ~/.config/autotranscode.toml run     /media/films
```

`run` accepts files, directories, or a `.txt` file listing one path per line.

### Calibrate the grain threshold before a real run

`measure` prints, for each file, the mean PSNR between it and a reference-denoised
copy. Lower means more grain.

```
file                                              PSNR dB  grainy  denoise
------------------------------------------------------------------------------
Old Grainy Classic (1962).mkv                       36.41  True    hqdn3d=2.0:1.5:8:8
Modern Scope Film (2017).mkv                        41.02  True    hqdn3d=1.4:1.0:6:6
Some Clean Film (2019).mkv                          50.44  False   -
```

Set `grain.threshold_db` so it falls between titles you consider clean and titles you
consider grainy. **The shipped default of 44.0 was derived from synthetic test
patterns and will over-trigger on real footage** — treat calibration as a required
first step, not an optional one.

### Commands

| Command | Purpose |
|---|---|
| `doctor` | Check tools and capabilities; explain what is missing |
| `measure` | Grain scores only, for calibration |
| `plan` | Show per-file decisions and the exact Av1an command, without encoding |
| `run` | Transcode (`--dry-run` probes and measures but does not encode) |
| `status` | Summarise the ledger: done / failed / skipped, space reclaimed |

Common overrides work without editing config: `--profile`, `--preset`, `--crf`,
`-w/--workers`, `-o/--output-dir`, `--grain {auto,always,never}`.

## Resuming

Two independent layers, which matters when a single film takes many hours:

- **Within a file**: Av1an keeps completed chunks, so an interrupted encode resumes
  where it stopped.
- **Across a library**: a JSON ledger records every finished file. Re-running skips
  them. A source that changed size is treated as new and reprocessed.

Both are safe to interrupt with Ctrl-C.

## What it refuses to do

- **Dolby Vision** files are skipped. The DV RPU does not survive Av1an's chunked
  pipeline, and quietly degrading DV to HDR10 is worse than not touching the file.
- Files already in AV1, clips shorter than `library.min_duration_sec`, and any result
  that saves less than `output.min_saving_percent` (the output is deleted and the file
  is marked skipped).
- `output.replace_source` deletes originals and is **off** by default.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Stdlib-only, so it runs on a bare board with no extra installs.

## Configuration

See [config.example.toml](config.example.toml); every option is commented there.
