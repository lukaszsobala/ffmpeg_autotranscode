# How autotranscode works, and why

The [README](README.md) covers installing and using autotranscode. This file
explains the design: why the pipeline has this shape, and how its settings were
arrived at. Read it before you change anything.

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
source's, and that took four attempts to get right:

1. **`grav1synth diff`** against a denoised copy of the source. Its tables drew a
   visibly repeating, quilted texture, whatever denoiser fed it — their AR filters
   had a DC gain of 0.9 to 1.95, so the grain template filled with blobs that recur
   block after block. Still available as `grain.estimator = "grav1synth"`.
2. **SVT-AV1's own estimator, in the encode** (`--film-grain`). It looked right —
   AR gains of 0.60-0.77 across 95 frames — but doubled the encode time.
3. **SVT-AV1's estimator on a few seconds of samples**, applied afterwards. Now
   `grain.estimator = "svt"`. The samples are encoded with `--film-grain`, ffmpeg's decoder reads back
   every frame's parameters, the most typical frame's become the table (its medoid:
   one atypical frame drew faint stripes), and grav1synth writes it into the
   finished stream. Minutes per film, whatever its length: five on an RK3588 with
   four 2-second samples, the old setting there. Ten 3-second samples took 1m47s
   for a 1080p clip on a Core Ultra 7 258V. Its grain had the wrong texture.
4. **SvtFgEssential over the whole film**, from
   [SVT-AV1-Essential](https://github.com/nekotrix/SVT-AV1-Essential) 4.3.1, one
   table entry per frame, calibrated scene by scene. The default. See
   [The whole-film estimate](#the-whole-film-estimate).

The table is then **calibrated**: the grain in the result is measured against the
source's, plane by plane, and the table rescaled to match — in seconds, since only
the stream headers are rewritten. SVT's colour grain came out twice as strong as the
source's and showed as speckle until this step existed.

The picture is **not** denoised before encoding. That was meant to save the bits the
encoder would spend on grain, but SVT-AV1 spends none on it anyway: the denoised
encode was 101% the size of the plain one, and the denoiser's temporal smoothing
cost detail the encoder would have kept. `grain.denoise_before_encode` turns it back
on.

Two constraints shaped the implementation:

1. **Av1an chunks the video, and each chunk restarts at timestamp zero.** A grain
   table with real timestamps would therefore land on the wrong frames if handed to
   the encoder mid-pipeline. So grain is applied *after* encoding, to the finished
   stream, with `grav1synth apply`.
2. **`--fgs-table` is reachable only through the SvtAv1Enc interface**, not through
   `ffmpeg -svtav1-params`. Anything built on `ffmpeg -c:v libsvtav1` cannot load a
   grain table at all.

### The whole-film estimate

SVT-AV1's estimate on samples gave grain of the wrong texture. On To Live and
Die in L.A. (35 mm, 1080p), ten 3-second windows were encoded with the
production settings and both tables applied to the same encode. Measured on the
flattest 40% of 16-pixel blocks, against what the encode lost there (source minus
encode, in 10-bit units):

| | factor calibration wanted, Y / Cb / Cr | luma lag-1 correlation, horizontal / vertical | luma σ by brightness, dark to bright (calibrated) |
|---|---|---|---|
| the source's grain | | +0.33 / +0.33 | 7.3, 9.9, 12.5, 12.3, 4.3 |
| SVT-AV1 on samples | 2.26 / 1.17 / 0.88 | -0.08 / -0.04 | 6.2, 6.6, 6.6, 6.6, 6.6 |
| SvtFgEssential | 0.74 / 1.01 / 1.04 | +0.36 / +0.38 | 5.7, 8.9, 13.8, 13.3, 5.3 |

SVT's AR filter had negative coefficients next to each pixel, so its grain was
finer than film's; calibration scales strength and cannot fix texture. Its
strength was the same at every brightness, where film grain peaks in the
midtones; and it came out 2.3 times too weak, past calibration's limit of 2. By
eye, on a white wall and a red sunset sky, it was an even digital speckle, and
SvtFgEssential's the source's coarser clumps.

SvtFgEssential (`--strength -1`) estimates grain from each frame's difference
to a motion-compensated neighbour on flat blocks, fits it per brightness band,
checks the quantised AR filter for resonance and repeating patterns, and
shrinks it until it passes. It re-estimates every 11 frames, blending with what
came before, starts afresh at every keyframe it is given, and writes a table
entry for every frame. Only this tool is taken from Essential: as an encoder it
changes many defaults the CRF and variance-boost settings here were judged with.

**Speed.** Its automatic analysis is single-threaded: 20 frames/s for 1080p on
one Cortex-A76, 25 for 8-bit input, decoding included. Since it keeps nothing
across a keyframe, the film is split at Av1an's scene starts into runs of whole
scenes, one SvtFgEssential per big core, and the runs' entries follow each
other. Equal runs left cores idle at the end: Film A in 14 runs for 4
processes spent the last 8 of its 31 minutes with half of them waiting. So each
run takes half of what is left divided by the processes, down to 600 frames, and
the longest go first, so a long take that cannot be split goes early. In a
simulation of that film's shape (341 scenes, three long takes), 26.4 minutes
against an ideal 26.2, and never above 26.8.

The little cores do not pay here. They already carry the decoders at about 40%
each, and four more SvtFgEssential processes on them made 5 frames/s each while
slowing the four on the big cores from 25 to 21: 104 frames/s in all against
100. Against one pass over
720 frames with the same cuts, the parallel runs' scaling functions differed by
0.2% on average (luma 0.01%): a few frames after each cut, where one pass still
carries a little of the scene before. Each run is decoded from a seek and found
by its frames' timestamps (`-copyts`, then `select` on the first frame's time),
so whatever the seek lands on, its frames are exactly the encode's; on the
Rockchip video engine when that is exact for the codec (see
[Why there is no hardware acceleration](#why-there-is-no-hardware-acceleration)).
Finished runs are kept, so a stop loses only those in flight. If the source's
frames still do not match the encode's one for one, the film falls back to the
estimate from samples.

**Timing the table.** grav1synth (through av1-grain 0.4.1) gives each frame the
first segment with start <= t <= end, both inclusive, where t is the packet's
timestamp read as milliseconds times 10,000. SvtFgEssential times entry n at
n x 417083 for 23.976 fps, but Matroska rounds timestamps to the millisecond:
frame 3 at 125.125 ms is stored as 125, which is before 3 x 417083 = 1,251,250,
so it took frame 2's entry, and so did about half the frames. The table is
therefore retimed by the packets of the stream it goes into: each run of frames
with the same parameters becomes one segment, from its first frame's timestamp
to one before the next run's. That also covers a variable frame rate and a
video track that starts after its audio. Runs are about ten frames long (the
parameters change every 11 frames), which keeps grav1synth's search, linear
for every frame, from growing with the square of the film's length. A run keeps
its first frame's seed; grav1synth moves the seed on at every frame anyway.

**Per-scene calibration.** One factor per film left scenes far apart: in the
ten windows above, SvtFgEssential's luma wanted anything from x0.59 to x1.5. So
calibration measures each scene and scales each scene's entries. The measure,
verify's (a 3x3 median on 10-bit), costs about 0.4 core-seconds a 1080p frame,
about 15 frames/s on an RK3588, and decoding the encode whole would cost more
still, so it reads windows: a few frames just after keyframes of the encode,
which Av1an puts at every chunk start, so the seek decodes nothing extra; the
keyframe itself is left out, since an intra frame keeps more texture than its
neighbours. The source's same frames are found by timestamp, as above. Every
scene gets a window before any gets a second, longest scenes first, up to
`grain.calibration_frames` (4,000 at 1080p, fewer in proportion above). A scene's
factor is pulled towards the film's, in the log domain, by as many frames' worth
as two windows; a scene without a window takes the film's. Whether to keep grain
at all is still decided on the film's measure (`decide_strength`).

**Films encoded before.** Grain is parameters in the frame headers, so
`autotranscode regrain` gives a finished film the new grain without encoding it
again: the video track is taken out, the table estimated and calibrated as a
run would, written in with grav1synth, and the file remuxed with every other
track in its place and the timestamps as they were. Applying a table over an
earlier one gave, frame for frame, the pictures of applying it to the encode
without grain. The new file must have the same tracks, the same video packets
and the same length before it replaces the old one.

**On a whole film.** Film A (1080p, 157,013 frames, 341 scenes) was
regrained on the RK3588 in 1h19m: 22 minutes of scene detection (Av1an's, as a
run makes it; detecting at 540 lines was four times slower, its downscaler
costing more than the detection, for the same cuts), 31 for the estimate (84
frames/s over four processes of 37 MB each), 20 for calibration (856 windows,
every scene), and the rest taking the track out and remuxing. The per-frame
table was 63 MB; retimed, 14,348 segments in 6 MB, which grav1synth wrote in
seconds. Against its old grain, the "svt" estimate's single table, in 20
windows spread over the film, measured as above:

| | luma σ | luma lag-1, horizontal / vertical | luma σ by brightness, dark to bright | luma σ against the source's, by window |
|---|---|---|---|---|
| the source's grain | 6.26 | +0.41 / +0.48 | 4.9, 9.4, 9.7, 8.0, 8.4 | |
| old: "svt", one table | 7.10 | +0.04 / +0.06 | 7.1, 7.3, 7.3, 7.4, 7.5 | x0.79 to x3.43 |
| new: SvtFgEssential, per scene | 6.25 | +0.41 / +0.49 | 4.7, 9.2, 10.4, 8.4, 9.3 | x0.84 to x1.28 |

The new grain's one window outside that range was the end credits, where the
source has almost no texture (σ 1.6): x0.29, where the old was x2.53.

**Finding the cuts again.** Those 22 minutes of scene detection were Av1an
redoing what it did at the encode: its list goes with the work directory. So every
film now keeps its cuts inside it, as a Matroska attachment of a few kilobytes
(`autotranscode-scenes.json`, written in place by mkvpropedit after the mux),
which `regrain` reads if it fits the video's frame count. For the films encoded
before, regrain finds the cuts itself (`cuts.py`) and attaches them. Av1an's
detector is the av-scenechange crate (0.24.1 in Av1an 0.6.0's lock file); the
crate's own command-line tool, given Av1an's options (`--min-scenecut 24
--speed 0`), reads Y4M, so it can be fed by the video engine. Av1an decodes in
software, and the detector itself barely uses a second thread:

| Film A, 1080p H.264 | frames/s |
|---|---|
| Av1an, one five-minute part (all eight cores busy) | 120 |
| Av1an, four parts at once | 139 |
| av-scenechange on h264_rkmpp frames, unpinned (the scheduler keeps it on the A55s) | 56 |
| the same, on one A76 | 94 |
| the same, on all four A76s | 170 |
| four parts at once, one per A76 | 250 |

The cuts were Av1an's frame for frame in all six parts tried (120 cuts). At 4K
Av1an detects on frames shrunk to `encode.sc_downscale_height` (1080) by
VapourSynth's Bicubic, whose defaults are b = c = 1/3; ffmpeg's `scale` with
`flags=bicubic:param0=1/3:param1=1/3` stands in for it. On Film G (2160p
10-bit HEVC), four parts of 3,600 frames: Av1an 8.4 frames/s, av-scenechange on
hevc_rkmpp 58, the same 83 cuts. Unshrunk, it ran at 38 and missed two of them.
Without av-scenechange, regrain falls back to Av1an's detection.

The attachment also says what made the film's grain: `svtfg` and the Essential
commit (`pipeline.grain_method`). `regrain` leaves a film whose grain the current
method made, so a library-wide regrain of several days, stopped, starts again
where it was, and films `run` encoded since are left alone; a new Essential pin
makes every film due again. It is kept in the film rather than in the ledger,
whose older versions dropped an entry with a field they did not know (and so,
saving, lost it); the ledger now leaves such fields out instead.

Film A regrained again with all of that took 1h01m instead of 1h19m:
12 minutes to find the cuts (212 frames/s; the same 341 scenes, every one on a
keyframe of the original encode, so Av1an's own), 26 for the estimate (99
frames/s, its parts shrinking towards the end), 16 for calibration (four windows
at a time), the source's frame times listed once instead of twice, and three
minutes to remux, attach the cuts and check. Its factors came out as before (luma
x1.19). A regrain of a film that carries its cuts skips the detection: about 49
minutes.

### The old grain detector

`measure` scores each file by the mean PSNR between it and a reference-denoised
copy; lower means more grain. Only `grain.estimator = "grav1synth"` and
`grain.denoise_before_encode` still use it. On the one real film measured end to
end, a visibly grainy 4K HDR title, it scored 46.0 dB and called the film clean
against the default 44 threshold: hqdn3d leaves structure that survives any
encode, so the score barely moves with grain.

## Pipeline

```
source.mkv
   │
   ├── ffprobe ......................... resolution, depth, HDR, streams
   │
   ├── mux rehearsal ................... the real mux on the first 5 s
   │
   ├── av1an -e svt-av1
   │      └── scene-split, parallel, resumable → video-only AV1
   │
   ├── SvtFgEssential --strength -1 .... the grain of every frame, a run of whole
   │      │                              scenes per big core
   │      └── retime ................... a segment per run of equal frames, timed
   │                                     by the encode's own packets
   │
   ├── grav1synth apply -g grain.tbl ... write grain into the bitstream
   │      └── calibrate ................ measure windows of every scene against the
   │                                     source, rescale each scene, rewrite
   │
   └── ffmpeg mux ...................... + audio, subtitles, chapters, metadata
          ├── mkvpropedit .............. the scene cuts, kept in the film
          └── output.av1.mkv
```

Only the short analysis excerpts ever touch the disk as lossless files.

The mux comes last, so a track it cannot take -- MP4's mov_text text, or 5.1(side)
audio on a mainline ffmpeg -- used to fail the file after the whole encode. Before
anything else, the mux command runs on the first 5 seconds of the source, with a
small black FFV1 clip in place of the encode: the video is copied whatever it is,
so what is on trial is the source's audio, subtitles, fonts and chapters.
With the mov_text conversion switched off, a test MP4 failed it in 0.14 s with
ffmpeg's own message. All 397 files `run` would pick up in a test library passed:
median 0.65 s, slowest 6.1 s.

ffmpeg's own limits did not end it in time. `-t`, on the input or the output,
ends a stream only once that stream passes the mark, so a PGS subtitle track with
nothing early sent the demuxer through the whole file for one: 197 s on a 42 GB
remux with 13 such tracks, `-shortest` or not. So `mux_until` stops ffmpeg
itself, once 5 s of output is muxed or, as the output time can stay N/A while
the muxer waits to interleave sparse tracks, 5 s after writing begins. The
header is only written once every encoder has started and the muxer has taken
every track, which is where these failures happen.

The encode always starts at 0, and ffmpeg shifts each input of the mux to start
at 0 as well. A source whose video starts after its audio would lose the gap, so
the mux puts it back with `-itsoffset`: a test file with video at 0.500 s came out
at 0 without it. 30 of the same 397 files have such a gap, 22 under 50 ms
and the longest 120 ms.

ffmpeg copies each track's tags, mkvmerge's statistics (`BPS`, `NUMBER_OF_BYTES`,
`NUMBER_OF_FRAMES`, `DURATION`, often with a language suffix) among them. On a
track converted to Opus they describe the track it replaced: Battlefield Earth's
Opus track claimed 3.9 Mb/s and 3.5 GB, the DTS-HD MA figures, and Jellyfin shows
the bitrate. The mux deletes them on every track it re-encodes. Copied tracks keep
theirs, which still hold.

## Quality scales with resolution

CRF is a quality target per *coded pixel*, and a pixel is not a fixed amount of
screen: at 576p each one is magnified several times more than at 2160p. Hold CRF
constant across a mixed library and the small files come out looking the worst,
which is the usual reason people conclude AV1 is weak at low resolution.

`encode.crf` is therefore the value for a 4K source and the most any source
gets; smaller sources get less along a curve over width:

```
crf = clamp(24 + 3.5 * log2(width / 720), 23, 30)     at crf 30, in 0.25 steps
```

The points it passes through were found by eye. 4K blocked in fog above 30.
1080p was blocky in a dark scene at 32 and held at 30; 29 is margin. 720p at 27
and SD at 24 looked like their sources, though nothing higher was tried there.
Those three lie on a log line, 3.4 to 3.6 CRF per doubling of width; 4K sits
below it, which is what the cap at 30 is for. The curve reaches it at about 2360
wide, so 1440p gets 30 too. The settings (`crf_per_doubling`, `crf_drop_at_720`,
`crf_max_drop`) are relative to `encode.crf`.

Other settings change by band, because what fixed blocking at one resolution lost
at another:

| source | CRF | variance boost | dark bias | ac-bias | temporal filter |
|---|---|---|---|---|---|
| 4K | 30 | 2 | off | 1 | 1 (weak) |
| 1080p | 29 | off | 100 | 1 | 1 (weak) |
| 720p | 27 | off | off | off | default |
| 576p and below | 23-25.75 | off | off | off | default |

- **Variance boost** spends more bits on flat areas. It was the only thing that
  cleared blocking in fog and on flat paper in a grainy 4K HDR film. Below 4K it
  lost at every resolution tested: the same size bought a worse picture.
- **Dark bias** (`encode.luminance_qp_bias`, SVT-AV1's `--luminance-qp-bias`)
  lowers the QP of dark frames. A grainy 1080p film smeared its shadows into
  blocks in a dark scene at CRF 32, and neither more bits nor variance boost fixed
  it. Bias 100 at CRF 37 did, for 1.32x the size of CRF 32 without the bias. Bias
  25 or 50, at the lower CRFs needed to match it in that scene, cost more. The
  bias works on whole frames, so a dim film pays for it on most of them. At the
  CRF 1080p has now it still matters: that scene was blocky with bias 50 at CRF
  30, and with 100 at CRF 32.
- **ac-bias and a weak temporal filter** (`encode.ac_bias`, `encode.tf_strength`)
  finished the job variance boost started on the 4K film's fog, which it had
  left blotched: grain smeared into patches tens of pixels across, some of them
  tinted. SVT-AV1's temporal filter averages neighbouring frames' grain into
  exactly those lumps, and ac-bias keeps the encoder from flattening what
  remains. Strength 1 with ac-bias 1 cleared it where CRF 26 and stronger
  variance boost did not; turning the filter off scored higher but looked worse
  in motion. The whole film came out 1.17x the size (82.5% saving instead of
  85%), better in 148 of its 159 three-second windows and never worse by more
  than 0.2 dB. At 1080p the default filter wiped rain out of a second film, and
  CRF 37 smeared faces; strength 1 and ac-bias 1 brought the rain back and CRF 30
  the faces, for 2.5x the bits on its worst stretches. A tf strength of -1 leaves
  the encoder's own default.

These settings take either one number for every source or a table keyed by the
lowest width of each band: 3000 for 4K, 1700 for 1080p, 1100 for 720p, 0 below.

`--crf` still moves everything at once, because the curve is relative to it.
Both the curve and the bands go by **width**: a scope film is cropped to its
picture, so a 4K print can be 3840x1600 or 3836x2072, and going by height would
treat it as 1080p and under-encode it. A picture narrower than 16:9 goes by its
height instead: 4:3 HD at 1440x1080 counts as 1920 wide and 960x720 as 1280.
Non-square pixels count at their shown width, so HDV's 1440x1080 is 1920 wide,
and a DVD counts as 1024 (576 lines) or 853 (480) whatever its shape. Set
`crf_per_doubling` and `crf_drop_at_720` to 0 for one CRF everywhere.

Each row was tuned on one film, so it is only as good as that film was typical;
`autotranscode sample` exists to check it against the rest of a library.

## Why PSNR is not trusted

A 4K HDR encode at CRF 32 scored 50.8 dB luma PSNR — "visually lossless" by the
usual reading — and was visibly blocky in flat areas, with its grain gone. Two
things fooled PSNR:

- **Grain swamps the error.** An encoder that strips grain leaves an error the
  size of the grain at every pixel. The block steps and banding it leaves behind
  are one or two 10-bit code values tall — invisible in that sum, obvious to the
  eye across a flat sky. XPSNR, which weights flat regions up, fares no better:
  it reads the source's grain as texture and forgives errors there.
- **ffmpeg pairs frames by timestamp**, and Matroska rounds timestamps to whole
  milliseconds. Two muxers can round the same frame to 41 and 42 ms, and a frame
  one millisecond late is compared with its predecessor. On the test film that
  produced frames scored at 10 dB that match at 43.7 once paired by index.

So `verify` pairs frames by index, and measures:

- **Low-pass PSNR** — the structural measure. Both files get a slight blur
  (Gaussian, sigma 2), which removes the 1-2 pixel grain and keeps anything 8 px
  and up, then PSNR at 10-bit. What remains is blocking, banding and lost detail,
  with the grain factored out. On the test film, CRF 32 → 27 → 22 moved its 5th
  percentile 51.2 → 52.4 → 53.6 dB, in step with what the eye saw at the worst
  frame.
- **Grain retained** — what a 3x3 median removes from the encode, as a fraction of
  what it removes from the source, with synthesised grain *on*. A median keeps
  step edges, so block edges do not count as grain. The measure has a floor: the
  median also removes fine edges and text, which survive, so an encode with no
  grain left reads well above zero (38% on the test film, at every CRF from 32 to
  22).
- **XPSNR**, for reference only, for the reasons above. It is 8-bit because
  ffmpeg's `xpsnr` is broken for high bit depth (a 10-bit file against itself
  scores 9.98 dB, in ffmpeg 7.1 and jellyfin-ffmpeg 8.1 alike).

Both PSNRs are measured with synthesised film grain switched off in the decoder,
so they score the encode underneath; random synthetic grain never matches the
source pixel for pixel and would otherwise penalise the very fix for lost grain.

## Why there is no hardware acceleration

The hardware looks like it should help and does not.

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
in av1an-core 0.6.0. The only ffmpeg passes this project runs itself are the grain
probe and table build, which decode about eight seconds of video per film.

A `machine.hwaccel_decode` option used to exist here. It was never read by
anything, and its removal changed no behaviour.

**`verify` and the grain estimate are the exception.** `verify` decodes a whole
film and its encode several times over, and the grain estimate the whole film
once more, so they decode H.264 sources with `h264_rkmpp`, which matched
software on every frame of two films, and HEVC with `hevc_rkmpp`, which matched
it on 1,920 frames of four films (8- and 10-bit, 1080p and 4K, HDR10 and Dolby
Vision; 96 frames at five points of each, every one decoded from a seek). One
such decode feeding Y4M runs at about 240 frames/s at 1080p and 65-78 at 4K.

AV1 decodes in software, with dav1d allowed eight frames in flight
(`-max_frame_delay 8`): ffmpeg leaves it at 0, which libdav1d keeps low, and a
1080p 10-bit encode decoded at 43 frames/s on the RK3588's eight cores; with 8,
at 200, the same frames bit for bit, with grain or without. AV1 stays off the
video engine, because
`av1_rkmpp` gets some frame sizes wrong. On jellyfin-ffmpeg 8.1.3-2, compared
with dav1d frame by frame (framemd5) over whole films, all with grain on every
frame:

| encode | frames | differing |
|---|---|---|
| 1920x1080 | 42,509 | 0 |
| 1918x802 | 138,448 | 0 |
| 954x720 | 105,668 | 0 |
| 1202x720 | 152,518 | 151,522 |

On the 1202-wide one, keyframes match exactly; from the next frame the chroma is
wrong, and on most inter frames the luma too, so the error is in the decoded
picture, not the grain. Over its first 2,000 frames, luma PSNR against dav1d had
a median of 16.9 dB and a minimum of 4.9 dB. To the eye, the first frame after a
keyframe has a green band along the top and blocky smears over its lower half;
24 frames on, the title card at 35 s is solid green, and 42 frames after a
keyframe the opening logo is a flat purple gradient. Nothing of the picture
comes back until the next keyframe. The three that match show the
grain is now applied exactly (see below), so only the frame width keeps AV1 off
the video engine.

The cause is in MPP's `hal_av1d_vdpu.c`. The decoder stores each reference frame
as rows of 4x4 tiles over the width padded to a multiple of 8 pixels, but
`vdpu_av1d_setup_tile_bufs` sizes the planes from the unpadded width, rounded up
to a multiple of 128 bits. At 1202 wide and 10 bits a tile row needs
1208 x 4 x 10 / 8 = 6,040 bytes and gets 6,016, so luma runs about 4 KB into
chroma, and chroma about 2 KB into the motion vectors. The frame on screen comes
out right, because the post-processor writes it as it is decoded; only the copy
later frames predict from is damaged, which is why keyframes match and the next
frame does not. With w the width and ALIGN(x, n) rounding x up to a multiple of
n, it breaks whenever ALIGN(w, 8) x 5 > ALIGN(10w, 128) / 2. For even widths
that is w mod 64 in {10, 12, 34, 36, 38, 50}, about one in five. 8-bit video,
and any width that is a multiple of 8, are never affected. The formula picked
out the broken widths exactly: 9 of 26 test clips from 626 to 1912 wide, and of
31 film encodes (first 2,000 frames each) only the one 1202 wide. Padding the width
to 8 before sizing the planes, a one-line change, made MPP's own `mpi_dec_test`
match dav1d on every frame of five broken test clips and of the 1202-wide film,
grain included. Until a jellyfin-ffmpeg carries that fix, AV1 stays on dav1d.
The XPSNR and low-pass passes would stay on dav1d regardless: they need
the grain off, and `av1_rkmpp` has no way to leave it off.

Decoding was never the bottleneck anyway. The blur and PSNR filters are, and the
kernel keeps their threads on the A55 cores. Pinned to the A76s with a thread
each, 3,000 SD frames took 52 s instead of 144 s. Hardware decoding contributed
only about 8% of that. Only cores with less than half the fastest one's
capacity are left out, though: an A55 has about 0.4 of an A76, but an Intel
Core Ultra 7 258V's E-cores have two-thirds of its P-cores, and there `verify`
runs on all eight.

**Why `verify` decodes each file three times.** Its four passes (XPSNR,
low-pass PSNR, grain in the source, grain in the encode) each decode what they
measure, so the source and the encode are decoded three times each. One graph
that splits the source three ways and the encode two ways, with the encode's
grain left to a second pass, decodes the source once and the encode twice. It
gave the same scores on every frame of three pairs, and saved little, because
the filters, not decoding, set the pace:

| pair | four passes | two passes | peak memory |
|---|---|---|---|
| 1080p H.264 on `h264_rkmpp`, 1,645 frames | 297 s | 281 s | 216 → 294 MiB |
| 1080p HEVC HDR, 533 frames | 133 s | 114 s | 320 → 412 MiB |
| 4K HEVC HDR, 409 frames | 281 s | 251 s | 802 → 1,115 MiB |

Folding the fourth pass in too, so that one ffmpeg decoded the encode twice,
hung on jellyfin-ffmpeg 8.1.3-2: with the source on `h264_rkmpp`, every
measurement stopped near frame 200 and ffmpeg sat for two hours on 77 s of CPU.
The same graph with the source decoded in software finished. A 5-14% saving
was not worth a hang nobody could explain, nor a quarter to two-fifths more
memory, so the passes stay separate.

### Film grain on the RK3588's AV1 decoder

The RK3588's AV1 decoder can apply film grain itself; the hardware was never the
problem. Rockchip's MPP library did not tell it to: in `hal_av1d_vdpu.c` the line
that would set `sw_apply_grain` was commented out, so the flag stayed at 0 and
every frame came out without grain. The bug and a one-line fix are in
[rockchip-linux/mpp#974](https://github.com/rockchip-linux/mpp/issues/974).

Rockchip merged the fix on 2026-11-03, as commit `9b56348` on MPP's `develop`
branch ("fix[hal_av1d]: Enable film grain on VDPU output"). jellyfin-ffmpeg links
its own MPP statically, built from nyanmisaka's `jellyfin-mpp-next` branch, and
shipped the fix in 8.1.3-2 (tagged 2026-11-04; MPP commit `3bcf8c98`). With it,
`av1_rkmpp`'s grain matched dav1d's bit for bit on three whole films (the table
above). A player on the system's MPP still needs an MPP release after 1.1.0, or
one built from `develop`; on an older MPP it shows none of the synthesised grain,
which brings back the flat look the grain was added to hide.

The fix does not cure the wrong pictures at some 10-bit frame sizes, above, which
is a separate MPP bug in buffer sizing. Judge these encodes in a player that
decodes AV1 in software (dav1d: mpv, VLC, most desktop browsers), or check the
width first: a multiple of 8 is always safe.

### Why jellyfin-ffmpeg

`tonemapx` is in Jellyfin's fork and not upstream, and it is what makes Dolby
Vision profile 5 transcodable at all here. libplacebo could reach HDR10 from
profile 5, but needs a Vulkan device the RK3588's Mali GPU does not provide.

## Toolchain choices

### One version, one toolchain

[autotranscode/toolchain.toml](autotranscode/toolchain.toml) names the version of
every tool this release of autotranscode was tested with. `scripts/bootstrap.sh`
installs from it, and `doctor` compares what is installed against it, so the
installer and the app cannot drift apart. Changing a pin calls for a release.

Passing on capability is not enough. An SVT-AV1 with every option the settings
use can still be a release whose CRF lands elsewhere, and an Av1an that runs can
still read its flags differently. So each pin says what else passes for it:

| Tool | Accepts | Enforced |
|---|---|---|
| SVT-AV1, Av1an | that version only | yes |
| grav1synth | that version, built with the patch | yes |
| jellyfin-ffmpeg | its major, at that release or newer (Jellyfin's repository keeps only the newest) | yes |
| VapourSynth | that release or newer (macOS takes Homebrew's) | no |
| L-SMASH-Works | that commit | no |
| mkvmerge, zimg | anything | no |

Enforced means `run` and `sample` refuse to start; `doctor` reports all of them,
with the `bootstrap.sh` stages that would put them right. `[tools] versions =
"warn"` runs anyway, for trying a newer build on purpose.

Two of them cannot say what they are. Stock grav1synth has no `--version`, so the
patch adds one that names itself (`0.2.0+autotranscode.1`), which also tells a
patched build from a stock one. The L-SMASH-Works plugin carries no version at all;
bootstrap writes the commit it built into `LSMASHSource.commit` beside it, and
`doctor` finds it through the path VapourSynth loaded the plugin from.

### Releases

A release is an annotated tag `vX.Y.Z` on `main`, on the commit whose
`__version__` is X.Y.Z. `update` reads only those: it fetches the tags, takes the
highest version, reads that tag's `toolchain.toml` to say which tools it rebuilds,
and installs it by fast-forwarding the checkout's branch to the tag. Commits on
`main` after the last release reach no machine until the next one, so `main` can
carry unfinished work.

`scripts/release.sh X.Y.Z` cuts one: from a clean `main` that is not behind
`origin/main`, with X.Y.Z above the last release, it sets `__version__`, runs the
tests (and stops if they fail), commits, and tags, with the tools that changed
since the last release in the tag's message. `--push` pushes `main` and the tag
and, with `gh` installed, makes a GitHub release page from the same text. The tag
is what counts; the page is for people.

### How a queued update is installed

`update` writes `update.queued` into the work directory and nothing else. `run`
notes the films that already had work under way when it began, which go first.
Before each film it checks for the file, and before any film not among those, it
installs the update instead of starting it. So a film is only ever encoded by
one toolchain: the half-done one finishes on the old tools, and only new films
meet the new ones.

To install, the run removes the file (a failed build is not retried before every
film), fast-forwards the checkout, runs the bootstrap stages that the new
`toolchain.toml` and the installed tools disagree on, apt's left out, with their
output in `update.log`, and execs its own command line again (`sys.orig_argv`). It
holds `run.lock` throughout, so nothing else starts meanwhile. Python opens files
close-on-exec, so the lock goes with the old image and the new one takes it, and
the log file is opened for appending, so the log carries on.

### Why SVT-AV1 is pinned

Distributions lag far behind: Debian 13 ships 2.3.0, which lacks the dark bias that
1080p's CRF depends on. The per-resolution settings were judged by eye on 4.0.0.
On the same 1080p excerpts at the same settings, 4.2.0 came out 2% smaller and
0.2 dB lower (within what a CRF step moves), and encoded 5-10% faster on the
RK3588, where 4.1 and 4.2 added NEON kernels.

4.3.0 is the pin now. It changed none of the options these settings pass, nor their
defaults (temporal filter strength is still 3), and fixed a bitrate inversion at
fractional CRFs that only showed above 63. Its release notes claim 5-8% better
efficiency at similar quality and more Neon kernels; the CRF curve has not been
re-judged on it yet, so compare a few `sample` excerpts against 4.2.0 output
before trusting it blind.

### Why grav1synth is patched

0.2.0, the latest release, writes a broken sequence header whenever the header's
last field ends a byte. With the usual flags that is any width from 513 to 1024 at
a height from 257 to 512 -- 640x480, NTSC DVD, 854x480 -- and every such file
failed at the grain step with "Error when reformatting data of a packet".

It also marks every frame's grain `clip_to_restricted_range`, which tells the
decoder to clamp the picture to 16-235 wherever it adds grain. Harmless on a
limited-range film; on a full-range phone clip it crushed the shadows and clipped
the highlights. The patch clips only when the stream is limited range.

A grav1synth from plain `cargo install` still works for limited-range sources at
any other size, but `run` refuses it: it has no `--version`, which the patch adds,
so there is no telling it from the broken build. `tools.versions = "warn"` lets it
through.

### Why chunks come from L-SMASH-Works

To cut a film into chunks, Av1an needs frame-exact access to it. With `hybrid`,
the only method that needs nothing but FFmpeg, it first lists every keyframe with
`ffprobe -show_frames`, which decodes the whole video on a single core: 21 minutes
for a 5-minute 4K excerpt, about ten hours for a 4K feature, before the first chunk
starts. It then copies the video stream into split files. L-SMASH-Works indexes
the same excerpt in 1.4 seconds and seeks to exact frames; the frames it hands
over matched ffmpeg's own decode wherever they were checked. The index goes into
the work directory (Av1an's `--cache-mode temp`), never next to the source.

### Why Av1an's `ffmpeg` is a wrapper script

Av1an cuts each chunk with `ffmpeg -vf select=between(n,a,b) -f yuv4mpegpipe -`
and no `-fps_mode`, so ffmpeg pads the selection to constant frame rate. On a
23.976 fps Matroska source that duplicates a frame, and Av1an rejects the chunk
with `FRAME MISMATCH: chunk 1: 238/237` — three times, then the whole file fails.
The encode therefore runs with `<work_dir>/bin/ffmpeg` first on Av1an's PATH: a
few lines of shell that add `-fps_mode passthrough` to y4m pipes and hand
everything else to the real ffmpeg unchanged. The `Failed to read y4m frame
delimeter` line printed alongside is not an error; it is SVT-AV1 reaching the end
of its input while it is still encoding the frames it has read ahead. This only
concerns the `hybrid` chunk method; with `lsmash` the frames come from `vspipe`.

### Why Av1an's progress bar is redrawn

Av1an's bar is one line of about 120 columns before the bar itself gets any. On a
narrower terminal the line wraps, Av1an's redraws stop clearing what they drew,
and the bar ends up nowhere to be seen. Its layout cannot be changed, so on a
terminal Av1an runs on a pseudo-terminal 300 columns wide, and autotranscode
reads its bar back and draws its own to the real terminal's width: one line when
everything fits, else the bar and time left on one line and the other figures that
fit on a second. Other lines Av1an prints, and autotranscode's own log, appear
above it. When the output is not a terminal, Av1an is run as before and draws no
bar. Nor does Av1an 0.6.0 draw one when `TERM` is `dumb` (0.5.2 drew it anyway):
then only the lines above the bar appear. `screen`, tmux and terminal emulators
all set a real terminal type.

Text after a bare carriage return replaces the line it returned over, as on a
terminal. L-SMASH draws its "Creating lwi index file N%" that way, and joined, its
hundred draws wrapped over 30 rows. While scene detection runs in parts (see
[Resuming](#resuming-in-detail)), the bar's title names the part, and Av1an's
`INFO` lines, its "Scene detection" heading and the index's progress are dropped,
the bar erased when the part ends: printed for every part, they left five lines a
part, 55 for a 58-minute episode. Anything else Av1an says, an error above all, is
still shown.

Without the bar, under Docker or systemd, the log said nothing between
"encoding" and "done", which for a 4K film is days. So with no terminal a line
goes to the log once an hour: frames done, the speed, the time left, and the
projected size when the early saving check has one. It reads Av1an's
`done.json`, as that check does, so it moves a chunk at a time. The speed counts
only this run's chunks; those finished before a restart would make it look
faster than it is.

### Locales

A C++ tool, usually mkvmerge, aborts with `_S_create_c_locale name not valid` when
given a locale the machine has never generated. Every child process autotranscode
starts is given a locale verified to exist, so transcodes are immune.

## Resuming, in detail

- **Within a file**: Av1an keeps completed chunks under `work_dir`, so an
  interrupted encode restarts from the chunk it was on.
- **The scene list** is kept beside the chunks, not inside Av1an's temp directory.
  Av1an resumes only once it has written its chunk list, which comes after scene
  detection (and, with `hybrid`, after its keyframe scan and split). Stopped before
  that, it found no chunk list and detected the scenes again, hours for a 4K film,
  though its own `scenes.json` was still there. It is now passed `--scenes`, which
  it reads whenever the file exists. The file is named by a digest of the source's
  size and duration and the settings scene detection uses (chunk method,
  `sc_method`, `min_scene_len`, `extra_split_sec`, `sc_downscale_height`), so one of
  those changing detects the scenes afresh; CRF and the other encoder flags do not.
- **Scene detection itself** is all or nothing in Av1an: it keeps nothing until the
  whole film is decoded, which for 4K on an RK3588 can take more than a day. With a
  VapourSynth chunk method (`lsmash`, `ffms2`, `bestsource`), autotranscode
  therefore runs it in five-minute parts: Av1an's own detector (`--sc-only`) with
  the encode's detection settings, on a script that serves one part from the same
  index the encode uses. Each part is kept as it finishes, so a stop loses the part
  in flight, and the parts are merged into the scene list once all are done. Every
  part starts a new scene, which costs one keyframe per five minutes of film. A
  film shorter than ten minutes, an ffmpeg-based chunk method, or a part that
  fails leaves detection to Av1an in one go, as before. The probe's frame count
  plans only the first part: each part's script writes down the frames the decoder
  finds in the whole film, and the rest are planned on that, so a container that
  overstates its length cannot plan parts past the end.
- **The keyframe scan** of the `hybrid` method (`ffprobe -show_frames` over the
  whole film) is kept the same way, by an `ffprobe` wrapper on Av1an's PATH that
  saves the scan once it completes and replays it on a rerun.
- **The grain probe and table** are kept beside the chunks, with a digest of the
  grain settings, the encoder flags SVT's estimate uses, and the source's size and
  duration. A rerun reuses them unless one of those changed.
- **The staged copy** is kept when a run does not finish; the next run checks the
  size still matches and reuses it.
- **Across a library**: a JSON ledger records every finished file. A source that
  changed size is treated as new. Commands can share it: each entry is written
  under a lock (`state.json.lock` beside it) against the file as it is on disk
  then, so an `estimate` started beside a long `run` loses nothing of either's.
  A run counts the files earlier ones settled before it starts, so its count goes
  on from theirs: checked as the loop reached them, the 124 done files of a
  397-file queue sorted by estimate sat anywhere in it, and a restart began at
  `[1/397]`. A skip for what the file is (codec, length, bits per pixel, Dolby
  Vision) is recorded with a digest of the settings that decided it and stands
  while they do; one recorded without, as before, was probed for again on every
  run. An unreadable pixel format is not kept: a newer ffprobe may read it. An
  output already at the path this run would write counts without opening the
  source, so a film encoded by another machine, or in Docker, whose paths and
  ledger differ, is not numbered among the films to do. The outputs the ledger
  records are left out of the queue: written beside their sources, they were
  found with the library and probed, each run, to be skipped as AV1.
- **One `run` per work directory.** A second `run` would start on the film the
  first is encoding, since films already under way go first, then resume from the
  same chunks, share one staged copy and one output, and delete the staged source
  under the other when it finished. So `run` holds an `flock` on `run.lock` in the
  work directory and stops at once if another holds it, naming that run's pid.
  The kernel drops the lock with the process, however it ends; a pid in a file
  outlives `kill -9` and may later name another process. `--dry-run` takes no
  lock, since it writes nothing.

The work directory is named by the source path. Av1an resumes whatever chunk list
it finds there without checking it: each chunk keeps the encoder flags it was
listed with, while the filters and pixel format come from the new command line.
So every encode leaves `encode.json` beside the chunks: the source's size and
duration, the file Av1an reads, and its `-v`, `--pix-format`, `-m` and `-f`. On a
rerun, before anything else:

- a source with another size or duration discards the chunks and the index;
- a file Av1an was reading that is gone -- a staged copy deleted while staging is
  off -- discards them too, since its script names that file;
- changed flags discard them silently if no chunk had finished, and otherwise
  follow `library.on_changed_settings`: `ask` on the terminal (without one, leave
  the file for now), `restart` keeping the index, `continue` with the stored flags
  put back on the command line (remembered, so a rerun does not ask again), or
  `skip`. A file left for now keeps its chunks and its staged copy.

Staging does not change the work directory, the output path or the ledger entry,
so switching it on or off keeps half-finished work for as long as the file it was
reading is still there.

The output is muxed under a `.part` name and renamed into place only once it is
complete, so a half-written file can never be read as "already transcoded". With
staging on, the mux is written to the staging directory, usually another
filesystem, so moving it is a copy: that copy also goes to `.part` beside the
destination and is renamed once whole. A stop partway through removes the
`.part`; a power cut leaves it for `clean`.

The one weak spot is a power cut, not a signal. Av1an rewrites `done.json` in place
after each chunk (`File::create` then `write_all`, with no rename), so losing power
inside that write leaves a file it later refuses to parse, and the film restarts
from zero. The window is a fraction of a millisecond per chunk against hours of
encoding, and the cost is one film, so this is worth knowing rather than worth
engineering around. A UPS closes it entirely.

### Interlaced sources

Combing encoded into AV1 stays: players deinterlace MPEG-2 and H.264 flagged
interlaced, but an AV1 stream from this pipeline is progressive. So
`encode.deinterlace` puts bwdif first in the filter chain, ahead of the tonemap
and any denoise, since it needs the fields as they were shot.

`mode=send_frame`, one frame per frame, because chunks, the keyframe list, the
grain windows and verify all count frames. Field doubling (`send_field`) would
keep 50i's motion but break each of them.

The field order is set rather than read from frame flags, and every frame is
deinterlaced (`deint=all`). The flags lie: of the 37 files `run` would pick up whose
field order ffprobe could not give, capture.avi was the one with interlaced
picture. It is flagged progressive, and idet
counted 39,019 frames bottom field first, 25,128 top field first and 33,600
progressive. Told nothing, bwdif takes such a frame as top field first, wrong for
most of that film. Frame flags may also not survive Av1an's VapourSynth pipe. So
`auto` takes the order from ffprobe's `field_order` (`tt` and `bt` show the top
field first, `bb` and `tb` the bottom), and `tff` / `bff` force it. On 3,000
frames of capture.avi, idet found 2,229 combed before bwdif and fewer than 60 after,
with either order, and the frame count unchanged; idet cannot say which order
is right, only motion seen by eye can. bwdif ran at 110 fps on that 720x480 film,
far above what the encoder manages.

Everything that compares the encode with its source deinterlaces the source the
same way: the size estimate's samples, the grain probe, the lossless grain
excerpts, calibration (combing is pixel-scale texture and would be asked of the
grain) and `verify` and `sample`, where every combed frame would otherwise count
as damage the encoder did. The setting stays out of the settings digest while it
is off, so its arrival kept every estimate.

Of 523 video files in a test library, the 86 flagged interlaced are all DVD `.VOB`s,
which `library.extensions` leaves out.

## Dolby Vision

A Dolby Vision title is an ordinary HEVC (or AV1) video plus an RPU carrying
per-shot metadata. The RPU cannot come with us: Av1an's chunked pipeline does not
carry it through, and it would not survive the re-encode in any case. What matters
is the base layer underneath, and for every profile but 5 that is a complete
picture on its own. This is the same trade Jellyfin makes when it transcodes these
files on the fly, and the classification follows Jellyfin's own
(`MediaStream.GetVideoColorRange`), including its sanity check: a file claiming
HDR10 compatibility while tagged something else is out of spec, so the colour tags
are believed rather than the claim.

Profile 5 has no such base layer (it is IPT-PQ-C2) and decodes to badly skewed
colour without the RPU. `tonemapx` reads the RPU on the CPU, but only writes
BT.709, which is why converting profile 5 means converting to SDR.

## Docker

Containers need a Linux kernel, so on a Mac every Docker runtime runs a Linux VM,
arm64 on Apple silicon. The encoder runs the same arm64 code with the same NEON
kernels there, so CPU speed is close to native Linux. macOS's split into
performance and efficiency cores is hidden from the VM, so the big-core pinning
that `verify` does on an RK3588 does not happen, and does not need to. The VM
cannot use VideoToolbox or the GPU, which nothing here needs.

On an RK3588, `compose.rk3588.yaml` passes the video engine through, for
`verify`'s H.264 decoding, and mounts the device tree, which Docker otherwise
hides, so that `machine.profile = "auto"` finds `rk3588`. A container on Linux is
an ordinary process with its own view of the system, not a VM, so encoding in it
should run at native speed.
