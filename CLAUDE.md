# CLAUDE.md

AV1 library transcoder: Python policy layer over Av1an, SVT-AV1, grav1synth and
jellyfin-ffmpeg. README.md is the user guide; TECHNICAL.md explains why every
setting is what it is. Built for an RK3588 board (ROCK 5B), also runs on x86-64.

## Working on the code

- Stdlib only, Python 3.11+. Do not add dependencies, for the package or the tests.
- Tests: `python3 -m unittest discover -s tests -v` (unittest, not pytest; one
  file, `tests/test_units.py`). They must pass before any commit.
- Lint and types: `ruff check .` and `pyright` (installed with `uv tool`, in
  `~/.local/bin`). Both report nothing; keep it that way.
- Docstrings and comments say *why*, with the measurement that decided it
  (frames, dB, seconds, the film's width). Keep that style; numbers you did not
  measure do not go in.
- A behaviour change updates README.md and TECHNICAL.md in the same commit
  when they describe it. Test names are sentences about behaviour.

## On the board

- ffmpeg is jellyfin-ffmpeg: `/usr/lib/jellyfin-ffmpeg/ffmpeg` and `ffprobe`
  beside it, updated through apt. Its Rockchip MPP library is linked *statically*
  (the build pins a commit of nyanmisaka/rk-mirrors `jellyfin-mpp-next`), so
  `ldd` shows no MPP: the ffmpeg version tells you which MPP you have.
- Hardware decoders need `/dev/mpp_service`. `.venv/bin/autotranscode doctor`
  lists the usable `*_rkmpp` decoders and the big cores (A76: usually 4-7).
- Config: `~/.config/autotranscode.toml`. Work dir, ledger, staged copies and
  resumable chunks: `~/.cache/autotranscode` unless the config says otherwise.
- The user often has a `run` going for days, usually inside `screen`. Before
  anything heavy, check (`pgrep -af 'autotranscode|av1an|SvtAv1EncApp'`) and
  ask before competing with it for the A76 cores. Never kill it, and never
  delete or edit anything under the work dir: `autotranscode clean --dry-run`
  says what is safe to remove.
