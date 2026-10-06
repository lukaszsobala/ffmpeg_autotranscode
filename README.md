# autotranscode

Transcodes a media library to AV1 with **source-accurate film grain**, by orchestrating
tools that already do the hard parts well:

| Tool | What it does here |
|---|---|
| [Av1an](https://github.com/rust-av/Av1an) | Scene detection, chunking, parallel workers, resume |
| [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) 4.x | The encoder (`SvtAv1EncApp`) |
| [grav1synth](https://github.com/rust-av/grav1synth) | Writes a film grain table into the AV1 bitstream |
| [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) 8 | Probing, muxing, and `tonemapx` for Dolby Vision profile 5 |

This repository contributes only the policy layer: what to encode, how grainy it is,
which audio to keep, and what to do when a step fails. How it works, and why the
settings are what they are, is in [TECHNICAL.md](TECHNICAL.md).

**Watch the results in a software AV1 decoder** (dav1d: mpv, VLC, most desktop
browsers). The RK3588's hardware decoder currently shows none of the film grain,
because of [a bug in Rockchip's MPP library](https://github.com/rockchip-linux/mpp/issues/974);
see [TECHNICAL.md](TECHNICAL.md#film-grain-on-the-rk3588s-av1-decoder).

## Install

The bootstrap script installs everything, on Debian or Ubuntu (arm64, RK3588
boards included, or x86-64) and on macOS with [Homebrew](https://brew.sh) (Apple
silicon or Intel):

```bash
./scripts/bootstrap.sh          # or just some stages: ./scripts/bootstrap.sh ffmpeg svtav1
```

| | Debian / Ubuntu | macOS |
|---|---|---|
| jellyfin-ffmpeg | Jellyfin's apt repository; its portable build where the repository has no packages | its portable build, into `~/.local/lib/jellyfin-ffmpeg` |
| SVT-AV1 4.2.0 | built from source into `~/.local` | built from source into `~/.local` |
| VapourSynth (Av1an will not link without it) | built from source into `~/.local` | Homebrew |
| L-SMASH-Works VapourSynth plugin, for the `lsmash` chunk method | built from source into `~/.local/lib/vapoursynth` | built from source into Homebrew's plugin folder |
| Av1an | `cargo install` | Homebrew |
| grav1synth 0.2.0, patched | built from the crate with `scripts/grav1synth-0.2.0-grain-flag.patch` | the same |
| mkvtoolnix, Python 3.11+ | apt | Homebrew |

SVT-AV1 and grav1synth are pinned and patched for reasons given in
[TECHNICAL.md](TECHNICAL.md#toolchain-choices). `doctor` and `run` refuse an
encoder missing an option the configured quality relies on. On macOS, Homebrew's
ffmpeg brings its own SVT-AV1 onto `PATH`; the script says so, and
`paths.svtav1encapp` should then name the pinned build.

On another Linux distribution, install the build tools, mkvtoolnix and Python
3.11+ yourself; the other stages do not use a package manager. Then:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # or: uv venv .venv && uv pip install -e .
.venv/bin/autotranscode doctor                        # says what is missing and how to get it
```

The package has no Python dependencies. `python3 -m autotranscode` works from the
repository root with no install at all.

`machine.profile = "auto"`, the default, picks the `rk3588` profile on an RK3588
board and `generic` anywhere else. `workstation` is there for many fast cores.

jellyfin-ffmpeg is recommended rather than required. Any ffmpeg 7.1 or newer has
what an encode needs; Jellyfin's adds `tonemapx` (Dolby Vision profile 5 to SDR)
and, on Rockchip, hardware decoding for `verify`.

`chunk_method = "auto"` uses L-SMASH-Works when `doctor` shows the `lsmas` plugin
(`scripts/bootstrap.sh lsmash` builds it). Without it, Av1an decodes the whole film
on one core before the first chunk starts.

If the tools live somewhere not on `PATH` — `~/.cargo/bin` and `~/.local/bin`
usually are not — set `paths.*` in the config to absolute paths. Av1an is handed
those locations too, so it runs the same binaries `doctor` reported on.

### If a tool aborts with `_S_create_c_locale name not valid`

Transcodes are immune, but the same command typed by hand will abort. `doctor`
names the offending variables. The usual cause is `AcceptEnv LANG LC_*` in
`sshd_config` importing the locales of whatever machine you are connecting *from*,
so unsetting them on the client is the tidiest fix; `sudo dpkg-reconfigure locales`
on the machine works too.

## Docker

The image holds everything above, built by the same `bootstrap.sh` stages. It is
the easy way onto a Mac, and an alternative to a native install on Linux.

| Host | Image | Notes |
|---|---|---|
| RK3588 and other arm64 Linux | arm64 | Built and tested on an RK3588. Add `compose.rk3588.yaml` on those boards. |
| Apple silicon Mac | arm64 | Docker runs containers in an arm64 Linux VM. Intel Macs are not a target. |
| x86-64 Linux | amd64 | Built and tested on an Intel Core Ultra 7 258V (Lunar Lake). |

Set up once:

```bash
cp .env.example .env           # fill in CONFIG_DIR, DATA_DIR, LIBRARY, TZ (and UID/GID on Linux)
cp config.example.toml "$CONFIG_DIR/autotranscode.toml"   # then edit, with container paths
docker compose build           # compiles the tools: expect tens of minutes
docker compose run --rm autotranscode doctor
```

The container sees three folders:

| Inside | From `.env` | Holds |
|---|---|---|
| `/config` | `CONFIG_DIR` | `autotranscode.toml` |
| `/data` | `DATA_DIR` | the container's home: work dir, state file, staging, logs, samples |
| the library's own path | `LIBRARY` | your films, at the same path as on the host |

The config is read inside the container, so its paths are container paths. Leave
the tool entries in `[paths]` unset: the image has them on `PATH`. Put `work_dir`,
`state_file` and the staging directory under `/data`, or leave them out: `~` is
`/data` inside, so the defaults land in `/data/.cache/autotranscode`. Library paths
stay as they are. An output directory outside the library needs a mount of its
own; add it in a `compose.override.yaml`, which Compose reads automatically.

Then use it as the native command:

```bash
docker compose run --rm autotranscode sample --minutes 5 -n 1 --out /data/samples "$LIBRARY"
docker compose run --rm -d --name transcode autotranscode --log-file /data/logs/run.log run "$LIBRARY"
docker logs -f transcode
docker stop transcode          # SIGTERM: a checkpoint, as Ctrl-C is natively
```

`docker stop` gives the run two minutes to wind down, and a rerun resumes it.

**On a Mac**, install Docker Desktop, OrbStack or Colima, and set the VM's CPUs
and memory in its settings: a 4K encode runs several SVT-AV1 workers at once, each
with its own memory (`machine.workers` overrides the count). Keep `DATA_DIR` on the
internal SSD; staging, on by default, then reads each film from the library only
once. A library on an external drive under `/Volumes` is shared by default in
Docker Desktop.

**On an RK3588**, add the board's compose file:

```bash
docker compose -f compose.yaml -f compose.rk3588.yaml run --rm autotranscode doctor
```

`doctor` should show `profile rk3588` and the `rkmpp` decoders. Without that file
the container still works, but `verify` decodes in software and the profile must
be set by hand.

## Use

```bash
cp config.example.toml ~/.config/autotranscode.toml

autotranscode doctor
autotranscode plan    /media/films        # see the commands without running them
autotranscode run     /media/films
```

`~/.config/autotranscode.toml` is picked up automatically (`$XDG_CONFIG_HOME` is
honoured). `-c` overrides it; with neither, the built-in defaults apply.

`run` accepts files, directories, or a `.txt` file listing one path per line.

### Commands

| Command | Purpose |
|---|---|
| `doctor` | Check tools and capabilities; explain what is missing |
| `measure` | The old grain detector's scores (only for `grain.estimator = "grav1synth"`) |
| `estimate` | Encode a few short excerpts per file and rank a library by what is worth transcoding; `run` then follows that ranking |
| `verify` | Score an encode against its source where the eye looks: low-pass PSNR, grain retained, worst-frame snapshots |
| `sample` | Encode a few random minutes of a few random files per resolution band, to check the settings on the rest of a library |
| `plan` | Show per-file decisions and the exact Av1an command, without encoding |
| `run` | Transcode (`--dry-run` probes and measures but does not encode) |
| `status` | Summarise the ledger: done / failed / skipped, space reclaimed |

Common overrides work without editing config: `--profile`, `--preset`, `--crf`,
`-w/--workers`, `-o/--output-dir`, `--grain {auto,always,never}`.

### Film grain

Nothing to set up. Every film gets a grain estimate, and calibration decides how
much to put back. A clean source ships without grain (`grain.mode = "auto"`);
`"always"` keeps a minimum, `"never"` skips the lot.

Only with `grain.estimator = "grav1synth"` or `grain.denoise_before_encode` does
the old detector, and so `grain.threshold_db`, come into play. `measure` prints its
score per file (lower means more grain):

```
file                                              PSNR dB  grainy  denoise
------------------------------------------------------------------------------
Old Grainy Classic (1962).mkv                       36.41  True    hqdn3d=2.0:1.5:8:8
Modern Scope Film (2017).mkv                        41.02  True    hqdn3d=1.4:1.0:6:6
Some Clean Film (2019).mkv                          50.44  False   -
```

It is unreliable; if you use it, set the threshold from titles you have looked at,
not from the default.

### Deciding what to transcode first

```bash
autotranscode estimate /media/films        # a file, a directory or a .txt list
```

For each file it encodes several short excerpts with the settings a real run would
use, and compares them against the bytes the source spends on the same moments.

```
file                                 size   estimate   saving    encode   saved/h  crf
--------------------------------------------------------------------------------------
Some Film                       764.6 MiB   48.1 MiB      94%    34m21s   1.2 GiB  32
```

Files are ranked by **saved/h**, bytes saved per hour of encoding.

Each result is kept in the state file, and `run` uses it:

- Estimated files go first, the most saved per hour at the top, and the rest follow
  in their usual order. A run stopped partway has spent its hours where they bought
  the most.
- A file whose estimate misses `output.min_saving_percent` or `output.min_saving_mib`
  is skipped without encoding. Nothing is recorded against it: a later estimate can
  change its mind.
- An estimate counts only while the settings that decide size are the ones it was
  made with (the `encode`, `grain` and `audio` sections and the SVT-AV1 version) and
  the file is unchanged. Change any of them and estimate again; until then those
  files are encoded in their usual order, the early saving check still watching.
  `grain.samples` and `grain.sample_seconds` do not count: they shape the grain
  table, not the size, unless `grain.denoise_before_encode` is on.

`plan` shows the same order and the same skips.

It takes roughly a minute per file. `--samples` and `--seconds` trade accuracy for
time. The grain table adds nothing measurable to a file's size, so the grain check
only runs with `grain.denoise_before_encode` on, where a grainy film is sampled
denoised; `+grain` after the CRF marks those, and `--no-grain-probe` skips the check.

Rank a library with it; do not quote its numbers. On a 4K test film it predicted a
93.7% saving against 93.1% achieved, and an encode time of 34 minutes against an
actual 51: the time leaves out scene detection, the grain table and the mux.

### Checking quality

```bash
autotranscode verify SOURCE ENCODED --snapshots /tmp/snaps
```

Do not judge an encode by plain PSNR; [TECHNICAL.md](TECHNICAL.md#why-psnr-is-not-trusted)
explains why. `verify` reports:

- **Low-pass PSNR** — blocking, banding and lost detail, with the grain factored
  out. Watch the **5th percentile**: visible damage lives in the bad frames.
- **Grain retained** — how much of the source's grain the encode has, with
  synthesised grain on. An encode with no grain left still reads well above zero
  (38% on the test film), so read it as "floor = none, ~100% = source-like".
- **XPSNR**, for reference only.
- **Snapshots** of the worst frames of both files, tonemapped identically. Crop a
  flat area and stretch its contrast to see what the numbers mean.

To find settings that fix a bad encode, `scripts/quality-sweep.sh SOURCE ENCODED`
finds the encode's worst stretches, cuts them from the source, runs the real
pipeline over them with several candidate settings, and verifies and snapshots
each. It runs as a user systemd unit, so it survives logging out; progress is in
the `report.md` it rewrites after every step, with every per-frame number in
`results.json`. Add `-- --excerpt DIR/excerpt.mkv --variant 'NAME CRF GRAIN [SVT...]'`
for another round on the same footage, or `-- --rescore` to re-measure one.

### Checking the settings on the rest of a library

The per-resolution settings were each tuned on one film. `sample` tries them on
others:

```bash
autotranscode sample /media/films -n 3 --minutes 4
scripts/library-samples.sh /media/films -n 3        # the same, detached
```

It draws `n` files per resolution band at random, cuts `--minutes` from a random
point in each, and runs the real pipeline over them with the live config. Each
sample gets a directory holding the excerpt, its encode, and snapshots of the three
frames the encode handles worst, and `report.md` puts them side by side with their
bitrates, savings and low-pass PSNR.

- **Openings and end credits are skipped**: the first and last 10 minutes
  (`--skip-start`, `--skip-end`), but never more than 15% of a file at either end.
- **Folders take turns**, so a long series cannot fill a band on its own.
- **Only files `run` would transcode** are candidates.
- **Reruns resume.** Running again with the same `--out` finishes the same samples
  rather than drawing new ones; `--seed` repeats a draw into a new directory.

`--band 1080p` limits it to one band (repeatable), `--pick-only` shows the draw
without encoding, and `--no-verify` skips the scores and snapshots. The global
`--crf` and `--preset` apply, so the same draw can be encoded again at another
setting into another directory.

## Stopping and resuming

A single film can take half a day, so stopping one is a normal thing to do.
Completed chunks, scene detection (five minutes of film at a time), the grain
table, the staged copy and the list of finished files are all kept, so an
interrupted run loses at most the chunk in flight — seconds of video — or the part
of scene detection it was on.

| | |
|---|---|
| `Ctrl-C`, or `kill <pid>` | Stop and checkpoint. Exit code 130; a rerun picks the file up again. |
| `Ctrl-C` twice | Give up on a tool that is slow to wind down. Chunks already finished are still kept. |
| `kill -STOP -<pgid>` / `kill -CONT -<pgid>` | Freeze and thaw in place, losing nothing at all. Use this for a pause measured in minutes. Av1an runs in a process group of its own, so take the pgid from `ps -o pgid= -C av1an`; under systemd, `systemctl --user kill --signal=STOP <unit>` does it for you. |
| Power cut | Same as Ctrl-C, minus the chunk in flight. Rarely, it costs that film's progress; see [TECHNICAL.md](TECHNICAL.md#resuming-in-detail). |

To resume, run the **same** command again. Av1an will reuse chunks encoded at a
different preset or CRF without saying so, so changing settings means deleting
that file's directory under `work_dir` first.

## Staging

An encode reads its source many times over many hours. If the library lives on a
NAS or a spinning disk, staging lets that drive idle: each file is copied to local
storage once, encoded from there, and the result moved to its destination only
once it passes the `output.min_saving_percent` and `output.min_saving_mib` checks.

```toml
[staging]
enabled = true
directory = ""          # empty => <work_dir>/staging
min_free_multiple = 1.5 # free space needed: the copy, plus room for the output
```

It never fails a file: if the directory is unusable, there is not enough free
space, or the copy fails, it logs why and works in place instead. Leave it off when
the library already lives on the same fast storage as `paths.work_dir`.

## Dolby Vision

The Dolby Vision metadata (RPU) is dropped and the base layer underneath is
encoded, as Jellyfin does when it transcodes these files. HDR10 static metadata is
kept.

| Profile | Base layer | Handling |
|---|---|---|
| 7 (UHD Blu-ray, dual layer) | HDR10 | encoded; RPU and enhancement layer dropped |
| 8.1 | HDR10 | encoded; RPU dropped |
| 8.2 | SDR | encoded; RPU dropped |
| 8.4 | HLG | encoded; RPU dropped |
| 10.x | per compatibility id | encoded, or skipped when it signals none |
| 5 | **none** — IPT-PQ-C2 | skipped by default |

Profile 5 has no usable base layer. `dolby_vision.profile5 = "tonemap-sdr"`
converts it to SDR with `tonemapx` instead of skipping it. Set
`dolby_vision.mode = "skip"` to leave every DV title alone.

## What it refuses to do

- **Dolby Vision profile 5**, unless you opt into SDR conversion (above).
- Files already in AV1, clips shorter than `library.min_duration_sec`, and any result
  that saves less than `output.min_saving_percent` or `output.min_saving_mib`
  (100 MiB by default; the output is deleted and the file is marked skipped).
- **Encodes heading for too little saving.** Once `output.check_saving_after_percent`
  of the frames are encoded (10 by default), the encode stops as soon as the
  projected result misses either minimum. Progress lines in the log show where
  each file is heading.
- **Files that did not save enough before**, while the settings that decide size
  (the `encode`, `grain` and `audio` sections, both minimum savings, the SVT-AV1
  version) are unchanged, grain sampling aside. Change any of them, `--crf`
  included, and the file gets another try.
- **Sources already below `library.min_bits_per_pixel`** (bitrate / (width x height x
  fps)). The default of 0.03 rejects only the hopeless: a 4K HDR remux measures
  ~0.15, a 1080p web release ~0.08, a good 4K AV1 encode ~0.01. A source whose
  bitrate cannot be read is never skipped on this rule.
- `output.replace_source` deletes originals and is **off** by default.
- **Overwriting an output.** A file whose output name is taken is skipped before
  anything runs. A file that takes the name while the encode runs is left alone:
  the encode is kept beside it as `<name>.new.mkv` and the source is reported as
  failed, for you to keep one. With `output.directory` set, the output mirrors the
  folders below the directory given to `run`, so films that share a file name in
  different folders do not collide; files named one by one go to its top.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Stdlib-only, so it runs on a bare board with no extra installs.

## Configuration

See [config.example.toml](config.example.toml); every option is commented there.
