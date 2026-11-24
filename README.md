# autotranscode

Transcodes a film library to AV1 with SVT-AV1, and puts the source's film grain
back with AV1 grain synthesis, estimated frame by frame and checked against the
source scene by scene. It drives tools that already do the hard parts well:

| Tool | What it does here |
|---|---|
| [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) 4.x | The encoder |
| [Av1an](https://github.com/rust-av/Av1an) | Scene detection, chunking, parallel workers, resume |
| [SvtFgEssential](https://github.com/nekotrix/SVT-AV1-Essential) | Estimates the film's grain, frame by frame (SVT-AV1-Essential's standalone estimator) |
| [grav1synth](https://github.com/rust-av/grav1synth) | Writes the grain table into the finished AV1 stream |
| [av-scenechange](https://github.com/rust-av/av-scenechange) | Finds scene cuts for `regrain` (the detector Av1an uses) |
| [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) 8 | Probing, decoding, muxing, and `tonemapx` for Dolby Vision profile 5 |

This repository is the policy layer: what to encode and with which settings, how
grainy a film is, which audio to keep, what to do when a step fails, and how to
stop and resume. [TECHNICAL.md](TECHNICAL.md) explains how it works and how the
settings were arrived at.

Film grain is drawn by the player. dav1d (mpv, VLC, desktop browsers) draws it;
a hardware AV1 decoder may not, and then shows the flatter picture the grain
was put back to hide.

## Install

On Debian or Ubuntu (x86-64 or arm64) and on macOS with
[Homebrew](https://brew.sh), the bootstrap script installs everything:

```bash
./scripts/bootstrap.sh          # or some stages: ./scripts/bootstrap.sh ffmpeg svtav1
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/autotranscode doctor  # what is installed, what is missing, how to fix it
```

| | Debian / Ubuntu | macOS |
|---|---|---|
| jellyfin-ffmpeg 8 | Jellyfin's apt repository, or its portable build | its portable build |
| SVT-AV1, VapourSynth, L-SMASH-Works | built from source into `~/.local` | SVT-AV1 and L-SMASH-Works built; VapourSynth from Homebrew |
| Av1an, grav1synth (patched), av-scenechange | `cargo install` | Av1an from Homebrew; the others `cargo install` |
| SvtFgEssential | built from SVT-AV1-Essential's source | the same |
| mkvtoolnix, Python 3.11+ | apt | Homebrew |

Every version comes from [autotranscode/toolchain.toml](autotranscode/toolchain.toml):
one release, one tested toolchain. `run` refuses tools that differ from it
(`[tools] versions = "warn"` runs anyway), and `autotranscode update` moves both
forward together. On macOS, Homebrew's ffmpeg puts its own SVT-AV1 on `PATH`;
set `paths.svtav1encapp` to the pinned build.

On another Linux distribution, install the build tools, mkvtoolnix and Python
3.11+ yourself; the script's other stages use no package manager. The package
has no Python dependencies; `python3 -m autotranscode` works from the repository
root without installing it.

Any ffmpeg 7.1 or newer can do the encodes. jellyfin-ffmpeg adds `tonemapx`
(Dolby Vision profile 5 to SDR) and Rockchip's hardware decoders. Tools that
are not on `PATH` go in `[paths]`.

If a tool aborts with `_S_create_c_locale name not valid`, your SSH client has
sent locales the machine does not have; `doctor` names them.

## Docker

The image holds the same toolchain, built by the same script: arm64 (tested on
an RK3588; also for Apple silicon, in Docker's Linux VM) and amd64 (tested on an
Intel Core Ultra 7).

```bash
cp .env.example .env           # CONFIG_DIR, DATA_DIR, LIBRARY, TZ, UID/GID
cp config.example.toml "$CONFIG_DIR/autotranscode.toml"   # paths inside the container
docker compose build           # tens of minutes
docker compose run --rm autotranscode doctor
docker compose run --rm -d --name transcode autotranscode --log-file /data/logs/run.log run "$LIBRARY"
docker stop transcode          # a checkpoint, like Ctrl-C; run again to resume
```

| Inside | From `.env` | Holds |
|---|---|---|
| `/config` | `CONFIG_DIR` | `autotranscode.toml` |
| `/data` | `DATA_DIR` | the container's home: work directory, state, staging, logs |
| the library's own path | `LIBRARY` | your films, at the same path as on the host |

Leave `[paths]` tool entries unset in the container. For hardware decoding, add
`-f compose.yaml -f compose.vaapi.yaml` (Intel or AMD GPU) or
`-f compose.yaml -f compose.rk3588.yaml` (RK3588) to each command. Without a
terminal, the log gets a progress line an hour instead of a progress bar.

## Use

```bash
cp config.example.toml ~/.config/autotranscode.toml   # optional; defaults apply without it
autotranscode doctor
autotranscode plan /media/films       # the decisions and commands, without encoding
autotranscode run  /media/films       # files, folders, or a .txt list of paths
```

| Command | What it does |
|---|---|
| `doctor` | Check the tools against the tested toolchain, and say how to fix what differs |
| `plan` | Show each file's decisions and the exact Av1an command |
| `run` | Transcode (`--dry-run` probes without encoding or recording anything) |
| `status` | Done, failed and skipped files, and the space saved |
| `estimate` | Encode short excerpts of each file and rank the library by space saved per hour of encoding; `run` then goes in that order |
| `verify SOURCE ENCODED` | Score an encode where the eye looks: low-pass PSNR, grain retained, snapshots of the worst frames |
| `sample` | Encode a few random minutes of a few files per resolution, to check the settings on the rest of a library |
| `regrain` | Give finished films the current grain without encoding them again |
| `update` | Say whether a newer release exists and which tools it changes; queue it |
| `clean` | Remove work no run will use again (`--dry-run` lists it) |

`--profile`, `--preset`, `--crf`, `-w/--workers`, `-o/--output-dir`,
`--grain {auto,always,never}` and `--deinterlace {off,auto,tff,bff}` override
the config for one run. `machine.profile = "auto"` picks `rk3588` on that board
and `generic` elsewhere; `workstation` is for many fast cores.

### Film grain

Nothing to set up. After the encode, SvtFgEssential estimates the grain of
every frame of the source, several scenes at a time, one process per core.
Calibration then measures the grain in the result against the source's, scene
by scene, and scales each scene's grain to match: the estimate supplies the
grain's texture, and the source decides how much of it goes back
([TECHNICAL.md](TECHNICAL.md#the-whole-film-estimate) has the measurements). A
clean source ships without grain (`grain.mode = "auto"`); `"always"` keeps a
little, `"never"` skips it.

AV1 carries grain as parameters in the frame headers, so `autotranscode regrain`
gives films already encoded the current grain by rewriting those headers. It
needs each film's source, checks the new file (tracks, packets, tags,
chapters, length) before it replaces the old one, and leaves films whose grain
is already current, so a regrain of a whole library can be stopped and started
again. Every film keeps its scene cuts inside it, as a small attachment, so a
later regrain does not need to find them again.

### Checking quality

Do not judge an encode by plain PSNR
([why](TECHNICAL.md#why-psnr-is-not-trusted)). `verify` reports low-pass PSNR,
which leaves the grain out (watch its 5th percentile), how much of the source's
grain the encode has (an encode with none still reads around 40%, so read it as
"floor = none, ~100% = like the source"), and snapshots of the worst frames of
both files.

```bash
autotranscode verify SOURCE ENCODED --snapshots ./snaps
autotranscode sample /media/films -n 3 --minutes 4    # reruns finish the same draw
```

`sample` draws `n` files per resolution band, skips openings and end credits,
encodes a few minutes of each with the live config, and writes a `report.md`
with bitrates, savings, scores and snapshots side by side.

`estimate`'s ranking is good for ordering a library; its numbers are rough.
Files it predicts will miss the minimum saving are skipped without encoding.

## Stopping and resuming

| | |
|---|---|
| `Ctrl-C`, `kill <pid>`, a closed SSH session | Stop and checkpoint (exit code 130). Run the same command again to resume. |
| `Ctrl-C` twice | Give up on a tool that is slow to stop. Finished chunks are still kept. |
| `kill -STOP` / `-CONT` on Av1an's process group | Pause in place without losing anything (`ps -o pgid= -C av1an`). |
| Power cut | As Ctrl-C, less the chunk in flight. |

Finished chunks, scene detection, the grain estimate and the staged copy are
kept, so a stop loses seconds of encoding. If a film's encoder settings changed
since it started, you are asked whether to start again, finish with the old
settings, or leave it; without a terminal, `library.on_changed_settings`
decides. A changed source starts again.

A second `run` on the same work directory refuses to start. Use a different
`paths.work_dir` for each run.

### Updating

```bash
autotranscode update            # is there a newer release? queue it
autotranscode update --check    # only say (exit code 1 when there is one)
```

A run cannot pause halfway through a film, so an update is queued. The run
finishes its current film, installs the update, rebuilds the tools that
changed, and carries on. With nothing running, `update` installs straight away.
A checkout with changes of its own is left alone. jellyfin-ffmpeg and mkvmerge
come from the system's package manager, not from `update`. In Docker, update the
checkout and rebuild the image.

### Cleaning up

```bash
autotranscode clean --dry-run /media/films   # what would go, what stays, and why
autotranscode clean /media/films
```

It removes work directories, staged copies and half-written outputs that no run
will use again. It keeps work that can be resumed (`--all` removes it too) and
never touches the film a run is working on.

## Staging

If the library is on a NAS or a spinning disk, staging copies each source to
local storage once, works from the copy, and writes the result back once it has
passed the saving checks:

```toml
[staging]
enabled = true
directory = ""          # empty: <work_dir>/staging
min_free_multiple = 1.5 # free space needed, as a multiple of the source
```

`regrain` stages too: one read of the source and of the film, and one write.
At most the source and the film's video track twice are on local storage at
once. If there is not enough room, or the copy fails, the file is processed in
place.

## Hardware decoding

The encode runs on the CPU. The passes autotranscode runs itself (scene cuts,
the grain estimate and calibration, `verify`) decode on hardware where that
gives exactly software's frames: Rockchip's decoders, NVIDIA (NVDEC), Intel and
AMD (VA-API), and Apple (VideoToolbox). Each film is checked before its decoder
is used: a stretch is decoded both ways and compared frame by frame, and a film
that differs is decoded in software. `doctor` lists the decoders it will try;
`machine.hardware_decode = "off"` turns this off.

## Dolby Vision

The Dolby Vision metadata is dropped and the base layer encoded, as Jellyfin
does when it transcodes; HDR10 metadata is kept. Profiles 7 and 8.x are
encoded, and 10.x when it has a base layer. Profile 5 has none and is skipped, unless
`dolby_vision.profile5 = "tonemap-sdr"` converts it to SDR.
`dolby_vision.mode = "skip"` leaves every Dolby Vision file alone.

## Interlaced sources

Encoded as they are by default, with a warning, because no player deinterlaces
AV1. `encode.deinterlace = "auto"` deinterlaces files flagged interlaced (bwdif,
one frame per frame). For a file flagged wrongly, name the field order
(`--deinterlace tff` or `bff`); `ffmpeg -i FILE -vf idet -f null -` counts it.

## What it skips

- Files already in AV1, and files shorter than `library.min_duration_sec`.
- Results that save less than `output.min_saving_percent` (10%) or
  `output.min_saving_mib` (100 MiB). Once `output.check_saving_after_percent`
  of the frames are encoded, an encode heading for less stops early. A file that
  missed is not tried again until the settings that decide size change.
- Sources already below `library.min_bits_per_pixel` (0.03 by default).
- Files whose tracks the mux would refuse: the mux is tried on 5 seconds first.
- Outputs that already exist: nothing is overwritten. A file that appears under
  the output's name during the encode is kept, and the encode saved beside it.
- Originals are deleted only with `output.replace_source = true`.

## Configuration

[config.example.toml](config.example.toml) lists every option, with comments.
`~/.config/autotranscode.toml` is read automatically; `-c` names another file.

## Development

```bash
python3 -m unittest discover -s tests     # stdlib only
scripts/release.sh X.Y.Z --push           # test, tag, push; see TECHNICAL.md
```

## License

0BSD, see [LICENSE](LICENSE). The exception is
`scripts/grav1synth-0.2.0-grain-flag.patch`, a change to grav1synth, which is
MIT-licensed by its authors.
