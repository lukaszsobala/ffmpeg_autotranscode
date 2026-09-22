# autotranscode

Transcodes a media library to AV1 with **source-accurate film grain**, by orchestrating
tools that already do the hard parts well:

| Tool | What it does here |
|---|---|
| [Av1an](https://github.com/rust-av/Av1an) | Scene detection, chunking, parallel workers, resume, optional target-quality search |
| [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) 4.x | The encoder (`SvtAv1EncApp`) |
| [grav1synth](https://github.com/rust-av/grav1synth) | Derives a film grain table from the source and writes it into the AV1 bitstream |
| [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) 8 | Probing, denoising, muxing, and `tonemapx` — the only CPU path from Dolby Vision profile 5 to SDR |

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

## Why there is no hardware acceleration

Worth stating plainly, because the hardware looks like it should help and does not.

**The RK3588 cannot encode AV1.** Its video engine encodes H.264, HEVC and MJPEG;
AV1 is decode-only. The ten-hour part of this job therefore has no hardware path on
this silicon, through any software. SVT-AV1 on the A76 cores is the only option.

That leaves decode, which the hardware does superbly — 60s of 1080p H.264 on a
ROCK 5B:

| | wall | CPU time | throughput |
|---|---|---|---|
| CPU, all cores | 3.45s | 20.1s | 417 fps |
| CPU, one thread | 12.5s | 12.8s | 115 fps |
| `rkmpp` | 1.65s | 0.37s | 876 fps |

It is not worth reaching for. At preset 4 the encoder spends 151 ms per frame
against 8.7 ms for single-threaded CPU decode, so decode is about 6% of the work,
and Av1an offers no way to claim it: the string `hwaccel` does not appear anywhere
in av1an-core 0.5.2. The only ffmpeg passes this project runs itself are the grain
probe and table build, which decode about eight seconds of video per film.

A `machine.hwaccel_decode` option used to exist here. It was never read by
anything, and its removal changed no behaviour. `doctor` still lists the build's
decoders, because that is a useful way to confirm you have a Rockchip-enabled
ffmpeg — not because they are used.

jellyfin-ffmpeg still earns its place: `tonemapx` is in Jellyfin's fork and not
upstream, and it is what makes Dolby Vision profile 5 transcodable at all here.

## Install

On a Rockchip RK3588 board, the bootstrap script does everything:

```bash
./scripts/bootstrap-rk3588.sh          # or: ./scripts/bootstrap-rk3588.sh ffmpeg svtav1
```

Elsewhere you need `jellyfin-ffmpeg`, `SvtAv1EncApp`, `av1an`, `grav1synth` and
`mkvtoolnix` on `PATH`. Then:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # or: uv venv .venv && uv pip install -e .
.venv/bin/autotranscode doctor                        # says what is missing and how to get it
```

There is nothing to install *into* the venv beyond this package: Python 3.11+ and
no dependencies at all, because everything heavy is an external binary. The venv
buys you the `autotranscode` command rather than `python3 -m autotranscode`, which
works just as well from the repository root with no install at all.

If the tools live somewhere not on `PATH` — `~/.cargo/bin` and `~/.local/bin`
usually are not — set `paths.*` in the config to absolute paths rather than
fighting `PATH`. Av1an is handed those locations too, so it runs the same binaries
`doctor` reported on.

### If a tool aborts with `_S_create_c_locale name not valid`

That is a C++ tool, usually mkvmerge, refusing a locale the machine has never
generated. Transcodes are immune — every child process is given a locale verified
to exist — but the same command typed by hand will abort. `doctor` names the
offending variables. The usual cause is `AcceptEnv LANG LC_*` in `sshd_config`
importing the locales of whatever machine you are connecting *from*, so unsetting
them on the client is the tidiest fix; `sudo dpkg-reconfigure locales` on the board
works too.

### Why Av1an's `ffmpeg` is a wrapper script

Av1an cuts each chunk with `ffmpeg -vf select=between(n,a,b) -f yuv4mpegpipe -`
and no `-fps_mode`, so ffmpeg pads the selection to constant frame rate. On a
23.976 fps Matroska source that duplicates a frame, and Av1an rejects the chunk
with `FRAME MISMATCH: chunk 1: 238/237` — three times, then the whole file fails.
The encode therefore runs with `<work_dir>/bin/ffmpeg` first on Av1an's PATH: a
few lines of shell that add `-fps_mode passthrough` to y4m pipes and hand
everything else to the real ffmpeg unchanged. The `Failed to read y4m frame
delimeter` line printed alongside is not an error; it is SVT-AV1 reaching the end
of its input while it is still encoding the frames it has read ahead.

## Use

```bash
cp config.example.toml ~/.config/autotranscode.toml

autotranscode doctor
autotranscode measure /media/films        # calibrate the grain threshold first
autotranscode plan    /media/films        # see the commands without running them
autotranscode run     /media/films
```

`~/.config/autotranscode.toml` is picked up automatically (`$XDG_CONFIG_HOME` is
honoured). `-c` overrides it; with neither, the built-in defaults apply.

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

## Stopping and resuming

A single film can take half a day, so stopping one is a normal thing to do rather
than an emergency. Three independent layers:

- **Within a file**: Av1an keeps completed chunks under `work_dir`, so an
  interrupted encode restarts from the chunk it was on. The chunk in flight is the
  only work lost -- seconds of video, not hours.
- **The staged copy**: kept when a run does not finish, so resuming does not read
  the source off the library disk a second time.
- **Across a library**: a JSON ledger records every finished file. Re-running skips
  them. A source that changed size is treated as new and reprocessed.

### How to stop

| | |
|---|---|
| `Ctrl-C`, or `kill <pid>` | Stop and checkpoint. Exit code 130; nothing is written to the ledger, so a rerun picks the file up again. |
| `Ctrl-C` twice | Give up on a tool that is slow to wind down. Chunks already finished are still kept. |
| `kill -STOP -<pgid>` / `kill -CONT -<pgid>` | Freeze and thaw in place, losing nothing at all -- the encode keeps its memory and carries on mid-chunk. Use this for a pause measured in minutes. |
| Power cut | Same as Ctrl-C, minus the chunk in flight, with one caveat below. |

To resume, run the same command again. It must be the *same* command: the resume
digest follows the source path, and Av1an will happily reuse chunks encoded at a
different preset or CRF without saying so. Changing settings means deleting that
file's directory under `work_dir` first.

Stopping is never mistaken for finishing. The output is muxed under a `.part` name
and renamed into place only once it is complete, so a half-written file can never
be read as "already transcoded" on the next run.

The one weak spot is a power cut, not a signal. Av1an rewrites `done.json` in place
after each chunk (`File::create` then `write_all`, with no rename), so losing power
inside that write leaves a file it later refuses to parse, and the film restarts
from zero. The window is a fraction of a millisecond per chunk against hours of
encoding, and the cost is one film, so this is worth knowing rather than worth
engineering around. A UPS closes it entirely.

## Staging

An encode reads its source many times over many hours: scene detection walks it,
every chunk decodes part of it, the grain probe pulls samples, the mux takes the
audio and subtitles. If the library lives on a NAS or a spinning disk, that is hours
of constant activity and the drive never gets to idle.

Turning on staging copies each file to local storage once and works from the copy:

```toml
[staging]
enabled = true
directory = ""          # empty => <work_dir>/staging
min_free_multiple = 1.5 # free space needed: the copy, plus room for the output
```

Both directions are staged, because a destination folder on the same disk keeps it
in use just as reading does:

- **Reads** come from the local copy: scene detection, every chunk, the grain probe,
  and the mux's audio and subtitles.
- **The write** goes to local storage too. The finished file moves to its
  destination only after it passes the `output.min_saving_percent` check, so an
  encode that gets rejected never touches that disk at all.
- **Unchanged**: the output path, the ledger entry, and the digest that makes Av1an
  resumable all still follow the real source, so switching staging on or off never
  invalidates half-finished work.

The net effect is that the library disk is read once at the start and written once at
the end, and is free to spin down for the hours in between.

It is only an optimisation, so it never fails a file. If the directory is unusable,
there is not enough free space, or the copy itself fails, it logs why and works in
place instead. A run interrupted mid-encode leaves its copy behind on purpose; the
next run checks the size still matches and reuses it.

Leave it off when the library already lives on the same fast storage as
`paths.work_dir` — it would just be an extra copy.

## Dolby Vision

A Dolby Vision title is an ordinary HEVC (or AV1) video plus an RPU carrying
per-shot metadata. The RPU cannot come with us: Av1an's chunked pipeline does not
carry it through, and it would not survive the re-encode in any case.

What matters is the **base layer** underneath it, and for most profiles that is a
complete picture on its own:

| Profile | Base layer | Handling |
|---|---|---|
| 7 (UHD Blu-ray, dual layer) | HDR10 | encoded; RPU and enhancement layer dropped |
| 8.1 | HDR10 | encoded; RPU dropped |
| 8.2 | SDR | encoded; RPU dropped |
| 8.4 | HLG | encoded; RPU dropped |
| 10.x | per compatibility id | encoded, or skipped when it signals none |
| 5 | **none** — IPT-PQ-C2 | skipped by default |

This is the same trade Jellyfin makes when it transcodes these files on the fly, and
the classification follows Jellyfin's own (`MediaStream.GetVideoColorRange`),
including its sanity check: a file claiming HDR10 compatibility while tagged
something else is out of spec, so the colour tags are believed rather than the
claim.

Profile 5 is the exception — without the RPU it decodes to badly skewed colour, so
it is skipped. Setting `dolby_vision.profile5 = "tonemap-sdr"` will instead reshape
it with ffmpeg's `tonemapx` filter, which reads the RPU on the CPU. That filter only
writes BT.709, so it turns an HDR master into SDR; it is opt-in for that reason.
(libplacebo could reach HDR10, but needs a Vulkan device the RK3588's Mali GPU does
not provide.)

Set `dolby_vision.mode = "skip"` to leave every DV title alone.

**HDR10 static metadata** — mastering display colour volume and MaxCLL/MaxFALL — is
read from the source and passed to the encoder, so what a DV title loses is the
per-shot dynamic metadata, not the whole HDR grade.

## Quality scales with resolution

CRF is a quality target per *coded pixel*, and a pixel is not a fixed amount of
screen: at 576p each one is magnified several times more than at 2160p. Hold CRF
constant across a mixed library and the small files come out looking the worst,
which is the usual reason people conclude AV1 is weak at low resolution.

`encode.crf` is therefore the value for a 1080p source, and `encode.crf_by_width`
shifts it per band. With the default ladder and the rk3588 profile's anchor of 30:

| source | CRF |
|---|---|
| 4K | 32 |
| 1080p | 30 |
| 720p | 27 |
| 576p and below | 24 |

`--crf` still moves everything at once, because it sets the anchor the ladder
shifts from. Bands are keyed by **width**: a scope film is cropped to its picture,
so a 4K print can be 3840x1600 or 3836x2072, and keying on height would file it as
1080p and under-encode it. Empty the table to go back to one CRF for everything.

## What it refuses to do

- **Dolby Vision profile 5**, unless you opt into SDR conversion (above).
- Files already in AV1, clips shorter than `library.min_duration_sec`, and any result
  that saves less than `output.min_saving_percent` (the output is deleted and the file
  is marked skipped).
- **Sources already below `library.min_bits_per_pixel`** (bitrate / (width x height x
  fps)). These are close to what AV1 itself would spend, so an encode costs hours and
  saves little — and `min_saving_percent` only finds that out afterwards, having spent
  the time. The default of 0.03 is deliberately low, so it rejects the hopeless rather
  than the merely unattractive: a 4K HDR remux measures ~0.15, a 1080p web release
  ~0.08, a good 4K AV1 encode ~0.01. A source whose bitrate cannot be read is never
  skipped on this rule.
- `output.replace_source` deletes originals and is **off** by default.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Stdlib-only, so it runs on a bare board with no extra installs.

## Configuration

See [config.example.toml](config.example.toml); every option is commented there.
