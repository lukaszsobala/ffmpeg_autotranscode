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

Grain is close to random, so it is close to incompressible. At any bitrate worth
having, SVT-AV1 simply discards it: on a grainy 4K film, CRF 32, 27 and 22 all left
the same none of the source's grain, at 1x, 1.4x and 1.9x the bits. What goes with
it matters more than the grain itself. Grain was dithering the smooth gradients —
fog, skies, dark walls — and without it they collapse into flat blocks one 10-bit
code value apart. No CRF fixed that, and putting grain back only half did: the
synthesised grain is too faint to hide steps that size. What removed them was
SVT-AV1's variance boost, which spends bits on exactly those flat blocks. On by
default for 4K at strength 2, with its PQ curve for HDR10, at CRF 30: 1.4x the bits
of plain CRF 22, still an 85% saving on the test film. Lower resolutions need
different fixes; see [Quality scales with resolution](#quality-scales-with-resolution).

AV1 can carry a compact description of grain in the bitstream and re-synthesise it
at playback, so it costs almost nothing: grain added to the test film's encode
changed its size by under 1%. The work is in describing grain that looks like the
source's, and that took three attempts to get right:

1. **`grav1synth diff`** against a denoised copy of the source. Its tables drew a
   visibly repeating, quilted texture, whatever denoiser fed it — their AR filters
   had a DC gain of 0.9 to 1.95, so the grain template filled with blobs that recur
   block after block. Still available as `grain.estimator = "grav1synth"`.
2. **SVT-AV1's own estimator, in the encode** (`--film-grain`). It looked right —
   AR gains of 0.60-0.77 across 95 frames — but doubled the encode time.
3. **SVT-AV1's estimator on a few seconds of samples**, applied afterwards. The
   default. The samples are encoded with `--film-grain`, ffmpeg's decoder reads back
   every frame's parameters, the most typical frame's become the table (its medoid:
   one atypical frame drew faint stripes), and grav1synth writes it into the
   finished stream. About five minutes per film, whatever its length.

The table is then **calibrated**: the grain in the result is measured against the
source's, plane by plane, and the table rescaled to match — in seconds, since only
the stream headers are rewritten. SVT's colour grain came out twice as strong as the
source's and showed as speckle until this step existed.

The picture is **not** denoised before encoding. That was meant to save the bits the
encoder would spend on grain, but SVT-AV1 spends none on it anyway: the denoised
encode was 101% the size of the plain one, and the denoiser's temporal smoothing
cost detail the encoder would have kept. `grain.denoise_before_encode` turns it back
on.

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
   ├── grain probe ..................... is there grain worth putting back?
   │
   ├── grain table (if grainy)
   │      ├── extract N short LOSSLESS excerpts
   │      ├── SvtAv1EncApp --film-grain  estimate grain on those alone
   │      └── medoid frame ............. one table for the whole runtime
   │
   ├── av1an -e svt-av1
   │      └── scene-split, parallel, resumable → video-only AV1
   │
   ├── grav1synth apply -g grain.tbl ... write grain into the bitstream
   │      └── calibrate ................ measure against the source, rescale, rewrite
   │
   └── ffmpeg mux ...................... + audio, subtitles, chapters, metadata
          └── output.av1.mkv
```

Only the short analysis excerpts ever touch the disk as lossless files.

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
anything, and its removal changed no behaviour.

**`verify` is the exception.** It decodes a whole film and its encode several
times over, so it decodes H.264 sources with `h264_rkmpp`, which matched
software on every frame of two films. AV1 stays on the CPU. `av1_rkmpp` never
applies film grain, and it decoded a 1202-wide encode wrongly: 14,487 of 14,584
frames differed from dav1d's, at 20-28 dB. A 1920-wide encode matched exactly.
Decoding was never the bottleneck anyway. The blur and PSNR filters are, and the
kernel keeps their threads on the A55 cores. Pinned to the A76s with a thread
each, 3,000 SD frames took 52 s instead of 144 s. Hardware decoding contributed
only about 8% of that.

Both AV1 findings matter for playback. **A player that decodes AV1 on the RK3588's
video engine shows none of the synthesised grain.** That puts back the flat,
grain-free look the grain was added to hide. At some frame sizes it may show
wrong pictures too. Judge these encodes in a player that decodes AV1 in software
(dav1d: mpv, VLC, most desktop browsers).

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
autotranscode plan    /media/films        # see the commands without running them
autotranscode run     /media/films
```

`~/.config/autotranscode.toml` is picked up automatically (`$XDG_CONFIG_HOME` is
honoured). `-c` overrides it; with neither, the built-in defaults apply.

`run` accepts files, directories, or a `.txt` file listing one path per line.

### Deciding which films get grain

Nothing to set up by default. Every film gets a grain estimate, and calibration
decides how much to put back — measuring how much fine texture the source has that
the encode lost. A clean source measures near zero and ships without grain
(`grain.mode = "auto"`); `"always"` keeps a minimum, `"never"` skips the lot.

The older detector is still there for the configurations that need its denoiser —
`grain.estimator = "grav1synth"` or `grain.denoise_before_encode` — and only then
does `grain.threshold_db` matter. `measure` prints its score for each file, the mean
PSNR between the file and a reference-denoised copy (lower means more grain):

```
file                                              PSNR dB  grainy  denoise
------------------------------------------------------------------------------
Old Grainy Classic (1962).mkv                       36.41  True    hqdn3d=2.0:1.5:8:8
Modern Scope Film (2017).mkv                        41.02  True    hqdn3d=1.4:1.0:6:6
Some Clean Film (2019).mkv                          50.44  False   -
```

Treat it with suspicion. On the one real film measured end to end, a visibly
grainy 4K HDR title, it scored 46.0 dB and called the film clean against the
default 44 threshold: hqdn3d leaves structure that survives any encode, so the score
barely moves with grain. If you rely on it, set the threshold from titles you have
looked at, not from the default.

### Commands

| Command | Purpose |
|---|---|
| `doctor` | Check tools and capabilities; explain what is missing |
| `measure` | The old grain detector's scores (only for `grain.estimator = "grav1synth"`) |
| `estimate` | Encode a few short excerpts per file and rank a library by what is worth transcoding |
| `verify` | Score an encode against its source where the eye looks: XPSNR, grain retained, worst-frame snapshots |
| `plan` | Show per-file decisions and the exact Av1an command, without encoding |
| `run` | Transcode (`--dry-run` probes and measures but does not encode) |
| `status` | Summarise the ledger: done / failed / skipped, space reclaimed |

Common overrides work without editing config: `--profile`, `--preset`, `--crf`,
`-w/--workers`, `-o/--output-dir`, `--grain {auto,always,never}`.

### Deciding what to transcode first

A 4K feature costs the better part of a day on this board, so picking titles by
eye is expensive guesswork. `estimate` samples instead:

```bash
autotranscode estimate /media/films        # a file, a directory or a .txt list
```

For each file it cuts several short excerpts spread across the runtime, encodes
them with the settings a real run would use — same CRF ladder, same SVT
parameters, same denoise decision — and compares them against the bytes the
source spends on exactly those moments, summed from packet sizes rather than
taken from an average bitrate.

```
file                                 size   estimate   saving    encode   saved/h  crf
--------------------------------------------------------------------------------------
Some Film                       764.6 MiB   48.1 MiB      94%    34m21s   1.2 GiB  32
```

The ranking is by **saved/h**, bytes saved per hour of encoding, not by bytes
saved: the useful question is not which file is biggest but which hour of
encoding is best spent. `+grain` after the CRF means the grain route was chosen,
which is where the largest savings usually come from.

Costs roughly a minute per file, mostly the excerpt encodes. `--samples` and
`--seconds` trade accuracy for time, and `--no-grain-probe` skips the grain check
— faster, and wrong on exactly the films where grain matters most.

**It is an extrapolation.** Checked against a full encode of the same 4K HDR
source, six 10-second excerpts projected 48.1 MiB against an actual 52.7 MiB —
9% under, or **93.7% saving predicted against 93.1% achieved**. A handful of
excerpts cannot know that the third act is one long dark scene, and the encoder
sees each excerpt cold, without the lookahead a full run gives it.

The time column is the sampled encode rate over the runtime, and it is
*optimistic*: it counts neither scene detection, nor the grain table, nor the
mux. On the same file it said 34m against an actual 51m. That understates every
file by roughly the same factor, so the ranking survives it.

Rank a library with this; do not quote its byte counts.

### Checking quality: do not trust PSNR

A 4K HDR encode at CRF 32 scored 50.8 dB luma PSNR — "visually lossless" by the
usual reading — and was visibly blocky in flat areas, with its grain gone. Two
things fooled PSNR, and both are worth knowing before trusting any number:

- **Grain swamps the error.** An encoder that strips grain leaves an error the
  size of the grain at every pixel. The block steps and banding it leaves behind
  are one or two 10-bit code values tall — invisible in that sum, obvious to the
  eye across a flat sky. XPSNR, which weights flat regions up, fares no better:
  it reads the source's grain as texture and forgives errors there.
- **ffmpeg pairs frames by timestamp**, and Matroska rounds timestamps to whole
  milliseconds. Two muxers can round the same frame to 41 and 42 ms, and a frame
  one millisecond late is compared with its predecessor. On the test film that
  produced frames scored at 10 dB that match at 43.7 once paired by index.

```bash
autotranscode verify SOURCE ENCODED --snapshots /tmp/snaps
```

pairs frames by index and reports:

- **Low-pass PSNR** — the structural measure. Both files get a slight blur
  (Gaussian, sigma 2), which removes the 1-2 pixel grain and keeps anything 8 px
  and up, then PSNR at 10-bit. What remains is blocking, banding and lost detail,
  with the grain factored out. Watch the **5th percentile**: visible damage lives
  in the bad frames. On the test film, CRF 32 → 27 → 22 moved it 51.2 → 52.4 → 53.6
  dB, in step with what the eye saw at the worst frame.
- **Grain retained** — what a 3x3 median removes from the encode, as a fraction of
  what it removes from the source, with synthesised grain *on*. A median keeps
  step edges, so block edges do not count as grain. The measure has a floor: the
  median also removes fine edges and text, which survive, so an encode with no
  grain left reads well above zero (38% on the test film, at every CRF from 32 to
  22). Read it as "floor = none, ~100% = source-like".
- **XPSNR**, for reference only, for the reasons above. It is 8-bit because
  ffmpeg's `xpsnr` is broken for high bit depth here (a 10-bit file against itself
  scores 9.98 dB, in ffmpeg 7.1 and jellyfin-ffmpeg 8.1 alike).
- **Snapshots** of the worst frames of both files, tonemapped identically. A metric
  earns trust only by agreeing with your eye on its worst frames; crop a flat area
  and stretch its contrast to see what the numbers mean.

Both PSNRs are measured with synthesised film grain switched off in the decoder,
so they score the encode underneath; random synthetic grain never matches the
source pixel for pixel and would otherwise penalise the very fix for lost grain.

To find settings that fix a bad encode rather than just confirm it,
`scripts/quality-sweep.sh SOURCE ENCODED` finds the encode's worst stretches —
for structure, and separately for grain loss — cuts them from the source by
stream copy, runs the real pipeline over them with several candidate settings,
and verifies and snapshots each. It runs as a user systemd unit, so it survives
closing the editor and logging out; progress is in the `report.md` it rewrites
after every step, with every per-frame number in `results.json`. Add
`-- --excerpt DIR/excerpt.mkv --variant 'NAME CRF GRAIN [SVT...]'` for another
round on the same footage, or `-- --rescore` to re-measure one after the
measurements change.

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

`encode.crf` is therefore the value for a 4K source, and `encode.crf_by_width`
shifts it per band. Two other settings change by band too, because what fixed
blocking at one resolution lost at another:

| source | CRF | variance boost | dark bias |
|---|---|---|---|
| 4K | 30 | 2 | off |
| 1080p | 37 | off | 100 |
| 720p | 27 | off | off |
| 576p and below | 24 | off | off |

- **Variance boost** spends more bits on flat areas. It was the only thing that
  cleared blocking in fog and on flat paper in a grainy 4K HDR film. Below 4K it
  lost at every resolution tested: the same size bought a worse picture.
- **Dark bias** (`encode.luminance_qp_bias`, SVT-AV1's `--luminance-qp-bias`)
  lowers the QP of dark frames. A grainy 1080p film smeared its shadows into
  blocks in a dark scene at CRF 32, and neither more bits nor variance boost fixed
  it. Bias 100 at CRF 37 did, for 1.32x the size of CRF 32 without the bias. Bias
  25 or 50, at the lower CRFs needed to match it in that scene, cost more. The
  bias works on whole frames, so a dim film pays for it on most of them. **1080p's
  CRF 37 depends on it**: drop the bias and bring 1080p back to about 32.

Both settings take either one number for every source or a table keyed by width
like the CRF ladder.

`--crf` still moves everything at once, because it sets the anchor the ladder
shifts from. Bands are keyed by **width**: a scope film is cropped to its picture,
so a 4K print can be 3840x1600 or 3836x2072, and keying on height would file it as
1080p and under-encode it. Empty the CRF table to go back to one CRF for everything.

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
