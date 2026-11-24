#!/usr/bin/env bash
# Install everything autotranscode drives, on Debian/Ubuntu (arm64 or x86-64,
# RK3588 boards included) or on macOS with Homebrew (Apple silicon or Intel).
#
# Nothing here is exotic; it is the sequence that turns a bare machine into one
# where `autotranscode doctor` passes. Re-running it is safe.
#
#   ./scripts/bootstrap.sh            # everything
#   ./scripts/bootstrap.sh ffmpeg     # just one stage
#
# Stages: deps ffmpeg svtav1 vapoursynth lsmash av1an grav1synth svtfg scenechange verify
#
# On Linux only the `deps` and `ffmpeg` stages use sudo, and only to run apt-get;
# as root, as in the Dockerfile, they run it directly.
# On macOS nothing does: Homebrew runs as you. Everything built from source
# installs into a user-owned prefix ($HOME/.local by default), so the long stages
# need no privileges and leave nothing behind outside your home directory. Set
# PREFIX=/usr/local if you deliberately want a system-wide install; the script
# will then use sudo for that one `cmake --install`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The versions come from autotranscode/toolchain.toml, which also tells `doctor`
# what to expect, and which says why each is pinned where it is. Beside this
# script is where the Dockerfile puts a copy. Each can still be overridden from
# the environment, and `doctor` will then report it as untested.
TOOLCHAIN="${TOOLCHAIN:-$SCRIPT_DIR/../autotranscode/toolchain.toml}"
[[ -f $TOOLCHAIN ]] || TOOLCHAIN="$SCRIPT_DIR/toolchain.toml"
# The version = "..." of one [section] of it.
pin() {
  local value
  value=$(awk -v section="[$1]" '
    /^\[/ { inside = ($1 == section); next }
    inside && $1 == "version" { sub(/^[^"]*"/, ""); sub(/".*$/, ""); print; exit }
  ' "$TOOLCHAIN" 2>/dev/null)
  [[ -n $value ]] || { printf 'no [%s] version in %s\n' "$1" "$TOOLCHAIN" >&2; exit 1; }
  printf '%s' "$value"
}
FFMPEG_MAJOR="${FFMPEG_MAJOR:-$(pin ffmpeg | cut -d. -f1)}"
SVT_AV1_VERSION="${SVT_AV1_VERSION:-v$(pin svtav1)}"
ZIMG_VERSION="${ZIMG_VERSION:-release-$(pin zimg)}"
VAPOURSYNTH_VERSION="${VAPOURSYNTH_VERSION:-R$(pin vapoursynth)}"
AV1AN_VERSION="${AV1AN_VERSION:-$(pin av1an)}"
# "0.2.0+autotranscode.1": the crate's version, then the patch's revision, which
# the patch builds into `grav1synth --version`.
GRAV1SYNTH_VERSION="${GRAV1SYNTH_VERSION:-$(pin grav1synth | cut -d+ -f1)}"
SVTFG_COMMIT="${SVTFG_COMMIT:-$(pin svtfg)}"
SCENECHANGE_VERSION="${SCENECHANGE_VERSION:-$(pin scenechange)}"
LSMASH_WORKS_COMMIT="${LSMASH_WORKS_COMMIT:-$(pin lsmash)}"
PREFIX="${PREFIX:-$HOME/.local}"
BUILD_DIR="${BUILD_DIR:-$HOME/.cache/autotranscode-build}"
# Sources and build trees are deleted once a stage has installed: nothing that runs
# needs them, and they come to over 500 MB. KEEP_BUILD_DIR=1 keeps them, to look
# into a failed build or to rebuild without fetching again.
KEEP_BUILD_DIR="${KEEP_BUILD_DIR:-0}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }
# sudo, except as root: root has no need of it, and a container has no sudo.
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# Run a command with the active virtualenv off PATH. VapourSynth embeds Python
# and takes the first python3 on PATH as its home; from an activated venv that is
# the venv's, without the vapoursynth module, and Av1an panics with "VSScript API
# not available", even for --version. autotranscode leaves its venv out of its
# children's PATH for the same reason (util.child_env).
outside_venv() {
  (
    if [[ -n ${VIRTUAL_ENV:-} ]]; then
      PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$VIRTUAL_ENV/bin" | paste -sd: -)
      unset VIRTUAL_ENV
    fi
    "$@"
  )
}
# Delete what a stage built from, and BUILD_DIR once nothing else is in it. Only
# the stage's own trees: another bootstrap may be building in BUILD_DIR meanwhile.
done_with() {
  [[ $KEEP_BUILD_DIR == 1 ]] && return
  rm -rf "$@"
  rmdir "$BUILD_DIR" 2>/dev/null || true
}

case "$(uname -s)" in
  Linux)
    OS=linux
    command -v apt-get >/dev/null || die "this script installs with apt-get (Debian, Ubuntu \
and derivatives). On another distribution install by hand: build tools, cmake, nasm, \
pkg-config, mkvtoolnix, Python 3.11+ and the ffmpeg development headers, then run the \
ffmpeg, svtav1, vapoursynth, lsmash, av1an, grav1synth, svtfg and scenechange stages, which need no package \
manager."
    JOBS="${JOBS:-$(nproc)}"
    ;;
  Darwin)
    OS=mac
    command -v brew >/dev/null || die "install Homebrew first: https://brew.sh"
    JOBS="${JOBS:-$(sysctl -n hw.ncpu)}"
    ;;
  *) die "unsupported system: $(uname -s) (Linux and macOS only)" ;;
esac

stage_deps() {
  log "Installing build and runtime dependencies"
  if [[ $OS == mac ]]; then
    # ffmpeg is Homebrew's, for its development headers (Av1an and grav1synth link
    # against libav*). The one autotranscode runs is jellyfin-ffmpeg, see `ffmpeg`.
    brew install cmake nasm pkg-config git mkvtoolnix python@3.13 ffmpeg
    return
  fi
  as_root apt-get update
  as_root apt-get install -y --no-install-recommends \
    build-essential cmake git yasm nasm pkg-config ca-certificates curl gnupg xz-utils \
    autoconf automake libtool \
    clang libclang-dev llvm-dev \
    libavformat-dev libavfilter-dev libavdevice-dev libavutil-dev libswscale-dev \
    zlib1g-dev mkvtoolnix python3 python3-dev python3-venv
}

# jellyfin-ffmpeg's portable build: two self-contained binaries, for systems its
# apt repository does not cover. Lands where autotranscode looks for it.
install_portable_ffmpeg() {
  local platform
  case "$OS/$(uname -m)" in
    mac/arm64)              platform=macarm64 ;;
    mac/x86_64)             platform=mac64 ;;
    linux/aarch64)          platform=linuxarm64 ;;
    linux/x86_64)           platform=linux64 ;;
    *) die "no portable jellyfin-ffmpeg build for $OS/$(uname -m)" ;;
  esac
  # The newest release of the pinned major: releases come newest first. sed, not
  # head, reads to the end, so nothing upstream dies of SIGPIPE under pipefail.
  local url
  url=$(curl -fsSL "https://api.github.com/repos/jellyfin/jellyfin-ffmpeg/releases?per_page=50" \
        | grep -o "https://[^\"]*_${FFMPEG_MAJOR}\.[^\"]*_portable_${platform}-gpl\.tar\.xz" \
        | sed -n 1p)
  [[ -n $url ]] || die "could not find a ${platform} build of jellyfin-ffmpeg ${FFMPEG_MAJOR} \
in its recent releases"
  local dest="$PREFIX/lib/jellyfin-ffmpeg"
  log "Downloading ${url##*/} into ${dest}"
  mkdir -p "$dest"
  curl -fsSL "$url" | tar -xJ -C "$dest" ffmpeg ffprobe
  FFMPEG_BIN="$dest/ffmpeg"
  if [[ $PREFIX != "$HOME/.local" ]]; then
    warn "autotranscode looks in ~/.local/lib/jellyfin-ffmpeg; set paths.ffmpeg = \"${dest}/ffmpeg\""
    warn "and paths.ffprobe = \"${dest}/ffprobe\" in the config"
  fi
}

stage_ffmpeg() {
  log "Installing jellyfin-ffmpeg ${FFMPEG_MAJOR} (has tonemapx and libvmaf, and rkmpp on Rockchip boards)"
  FFMPEG_BIN=/usr/lib/jellyfin-ffmpeg/ffmpeg
  if [[ $OS == mac ]]; then
    install_portable_ffmpeg
  else
    local id like codename arch
    id=$(. /etc/os-release && echo "${ID}")
    like=$(. /etc/os-release && echo "${ID_LIKE:-}")
    codename=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
    arch=$(dpkg --print-architecture)
    # Jellyfin publishes for Debian and Ubuntu; a derivative takes its parent's
    # repository only when it shares the codename, which Mint and Pop do not.
    if [[ $id != debian && $id != ubuntu ]]; then
      if [[ " $like " == *" ubuntu "* ]]; then id=ubuntu
      elif [[ " $like " == *" debian "* ]]; then id=debian
      fi
    fi

    local pkg=""
    if [[ -n $codename && ($id == debian || $id == ubuntu) \
          && ($arch == arm64 || $arch == amd64) ]]; then
      as_root install -d -m 0755 /etc/apt/keyrings
      curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | as_root gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg
      echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=${arch}] https://repo.jellyfin.org/${id} ${codename} main" \
        | as_root tee /etc/apt/sources.list.d/jellyfin.sources.list >/dev/null
      # The pinned major, not the newest: a new major is a new toolchain, which
      # toolchain.toml moves to once it has been tried.
      if as_root apt-get update \
          && apt-cache show "jellyfin-ffmpeg${FFMPEG_MAJOR}" >/dev/null 2>&1; then
        pkg="jellyfin-ffmpeg${FFMPEG_MAJOR}"
      fi
      if [[ -z $pkg ]]; then
        warn "Jellyfin's repository has no jellyfin-ffmpeg${FFMPEG_MAJOR} for ${id} ${codename}; removing it"
        as_root rm -f /etc/apt/sources.list.d/jellyfin.sources.list
        as_root apt-get update
      fi
    fi
    if [[ -n $pkg ]]; then
      log "Selected package: ${pkg}"
      as_root apt-get install -y "$pkg"
    else
      install_portable_ffmpeg
    fi
  fi

  [[ -x $FFMPEG_BIN ]] || die "expected ${FFMPEG_BIN} after install"
  "$FFMPEG_BIN" -hide_banner -version | head -1
  if "$FFMPEG_BIN" -hide_banner -hwaccels | grep -q rkmpp; then
    log "rkmpp hardware decoding is available"
  fi
}

stage_svtav1() {
  log "Building SVT-AV1 ${SVT_AV1_VERSION} (SvtAv1EncApp)"
  # Av1an drives the encoder binary directly. It is also the only way to reach
  # --fgs-table, which is documented as available through the SvtAv1Enc interface only.
  mkdir -p "$BUILD_DIR"
  local src="$BUILD_DIR/SVT-AV1"
  if [[ -d $src/.git ]]; then
    # Fetch the tag as a tag: the build names its version with `git describe`,
    # and a bare FETCH_HEAD checkout reports a commit hash that autotranscode
    # cannot read a version from.
    git -C "$src" fetch --depth 1 --force origin \
      "refs/tags/${SVT_AV1_VERSION}:refs/tags/${SVT_AV1_VERSION}"
    git -C "$src" checkout -q "$SVT_AV1_VERSION"
  else
    git clone --depth 1 --branch "$SVT_AV1_VERSION" \
      https://gitlab.com/AOMediaCodec/SVT-AV1.git "$src"
  fi
  cmake -S "$src" -B "$src/Build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_TESTING=OFF
  cmake --build "$src/Build" -j "$JOBS"

  # Install without privileges when the prefix is ours, which is the default.
  if [[ -w ${PREFIX} || ! -e ${PREFIX} ]]; then
    cmake --install "$src/Build"
  else
    warn "${PREFIX} is not writable; installing with sudo"
    as_root cmake --install "$src/Build"
    [[ $OS == linux ]] && as_root ldconfig
  fi
  done_with "$src"

  local svt="$PREFIX/bin/SvtAv1EncApp"
  [[ -x $svt ]] || die "SvtAv1EncApp not found at ${svt} after install"
  "$svt" --version | head -1
  # Homebrew's ffmpeg brings its own svt-av1, whose SvtAv1EncApp may come first on
  # PATH. Name ours in the config so the version the settings were tuned on is used.
  if [[ $(command -v SvtAv1EncApp || true) != "$svt" ]]; then
    warn "PATH finds $(command -v SvtAv1EncApp || echo 'no SvtAv1EncApp'), not this build."
    warn "Point the config at it: paths.svtav1encapp = \"${svt}\""
  fi
}

stage_vapoursynth() {
  # Needed twice over. Av1an links -lvapoursynth unconditionally -- no published
  # version gates it behind a cargo feature -- and the lsmash chunk method, which
  # the lsmash stage adds, runs through VapourSynth.
  if [[ $OS == mac ]]; then
    log "Installing VapourSynth from Homebrew"
    brew install vapoursynth
    return
  fi
  # Debian ships no VapourSynth packages at all, hence a source build.
  log "Building zimg and VapourSynth into ${PREFIX}"
  mkdir -p "$BUILD_DIR"

  local zimg="$BUILD_DIR/zimg"
  if [[ ! -d $zimg/.git ]]; then
    git clone --depth 1 --branch "$ZIMG_VERSION" \
      https://github.com/sekrit-twc/zimg.git "$zimg"
  fi
  ( cd "$zimg" && ./autogen.sh && ./configure --prefix="$PREFIX" && make -j "$JOBS" \
    && make install )

  local vs="$BUILD_DIR/vapoursynth"
  if [[ ! -d $vs/.git ]]; then
    git clone --depth 1 --branch "$VAPOURSYNTH_VERSION" \
      https://github.com/vapoursynth/vapoursynth.git "$vs"
  fi
  # The Python module is not optional: Av1an links -lvapoursynth-script, and VSScript's API initialises by importing the
  # Python `vapoursynth` module. Without it every av1an invocation, `--version`
  # included, dies with "Failed to get VSScript API".
  #
  # Cython goes into a throwaway venv rather than system-wide, so this stays
  # privilege-free. With PREFIX=$HOME/.local the module lands in
  # ~/.local/lib/pythonX.Y/site-packages, which is Python's user site and already
  # on sys.path -- no PYTHONPATH needed.
  #
  # The system's Python, whose headers stage_deps installed (python3-dev), named
  # outright: configure otherwise takes the first of `python`, `python3.N`... on
  # PATH, and a newer one from uv, pyenv or Homebrew has no python-3.N.pc for it
  # ("Package 'python-3.15' not found").
  local python=/usr/bin/python3
  [[ -x $python ]] || python=$(command -v python3)
  local venv="$BUILD_DIR/buildenv"
  local version='import sys; print(sys.version_info[:2])'
  # One left by a run that took another Python is made again.
  if [[ -d $venv && $("$venv/bin/python" -c "$version" 2>/dev/null) != "$("$python" -c "$version")" ]]; then
    rm -rf "$venv"
  fi
  [[ -d $venv ]] || "$python" -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade cython
  ( cd "$vs" && ./autogen.sh \
    && PYTHON="$python" PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" ./configure \
         --prefix="$PREFIX" --with-cython="$venv/bin/cython" \
    && make -j "$JOBS" && make install )

  PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" pkg-config --modversion vapoursynth \
    || die "vapoursynth.pc not found under ${PREFIX}"
  done_with "$zimg" "$vs" "$venv"
}

stage_lsmash() {
  # L-SMASH-Works' VapourSynth plugin, for encode.chunk_method "lsmash" (which
  # "auto" picks when it loads). It indexes a film in seconds and hands Av1an exact
  # frames. Without it Av1an falls back to hybrid, which first decodes the whole
  # film on one core to find its keyframes: about ten hours for 4K on an RK3588.
  log "Building the L-SMASH-Works VapourSynth plugin"
  mkdir -p "$BUILD_DIR"
  local src="$BUILD_DIR/lsmash-works"
  [[ -d $src/.git ]] \
    || git clone --depth 1 https://github.com/HomeOfAviSynthPlusEvolution/L-SMASH-Works.git "$src"
  # Its bundled FFmpeg fork is left out: the system's FFmpeg 7 libraries build and
  # decode frame-exact, and the fork is a large build of its own.
  ( cd "$src" && git fetch --depth 1 origin "$LSMASH_WORKS_COMMIT" \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --depth 1 l-smash obuparse xxHash )
  rm -rf "$src/build"
  # Position-independent code, or the static l-smash will not link into the
  # plugin on arm64. Optional decoders off: FFmpeg decodes everything we read.
  PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" cmake -S "$src" -B "$src/build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DBUILD_AVS_PLUGIN=OFF -DBUILD_VS_PLUGIN=ON -DENABLE_DAV1D=OFF -DENABLE_VPX=OFF \
    -DENABLE_XML2=OFF -DENABLE_MFX=OFF -DZLIB_USE_STATIC_LIBS=OFF
  cmake --build "$src/build" -j "$JOBS"
  # Only the plugin: `cmake --install` would also drop l-smash's, obuparse's and
  # xxHash's static libraries and headers into the prefix, which nothing uses.
  # VapourSynth autoloads plugins from <its libdir>/vapoursynth.
  local plugins="$PREFIX/lib/vapoursynth"
  [[ $OS == mac ]] && plugins="$(brew --prefix)/lib/vapoursynth"
  local built
  built=$(find "$src/build" -maxdepth 1 -name 'libLSMASHSource.*' -type f | head -1)
  [[ -n $built ]] || die "the plugin did not build"
  mkdir -p "$plugins"
  install -m 755 "$built" "$plugins/"
  # The plugin carries no version; this is how `doctor` knows which it is.
  printf '%s\n' "$LSMASH_WORKS_COMMIT" > "$plugins/LSMASHSource.commit"
  log "Installed $(basename "$built") (${LSMASH_WORKS_COMMIT:0:12}) into ${plugins}"
  done_with "$src"
}

ensure_cargo() {
  if ! command -v cargo >/dev/null; then
    log "Installing Rust toolchain"
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path
  fi
  export PATH="$HOME/.cargo/bin:$PATH"
  command -v cargo >/dev/null || die "cargo still not on PATH"
}

stage_av1an() {
  log "Installing Av1an (scene detection, chunking, parallelism, resume)"
  if [[ $OS == mac ]]; then
    brew install av1an
    outside_venv av1an --version | head -1
    warn "This is Homebrew's Av1an, not necessarily ${AV1AN_VERSION}; \`autotranscode doctor\` says."
    return
  fi
  ensure_cargo
  [[ -f $PREFIX/lib/pkgconfig/vapoursynth.pc ]] \
    || die "VapourSynth not found under ${PREFIX}; run the 'vapoursynth' stage first"
  # The vapoursynth crate emits `cargo:rustc-link-lib` without a search path, so
  # -L is required for a prefix the linker does not already know about. The rpath
  # then lets the finished binary find the libraries without LD_LIBRARY_PATH.
  PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" \
  RUSTFLAGS="-L $PREFIX/lib -C link-arg=-Wl,-rpath,$PREFIX/lib" \
    cargo install --git https://github.com/rust-av/Av1an --tag "v${AV1AN_VERSION}" \
      --locked av1an
  outside_venv av1an --version | head -1
}

stage_grav1synth() {
  log "Building grav1synth ${GRAV1SYNTH_VERSION} (source-accurate film grain tables), patched"
  ensure_cargo
  # Patched, so not `cargo install grav1synth`: 0.2.0 (the latest release, and main
  # as of 2026-09) corrupts the sequence header when its last field ends a byte,
  # which with the usual flags means widths 513-1024 at heights 257-512 -- 640x480,
  # NTSC DVD, 854x480 -- and every such file fails at the grain step. It also clamps
  # full-range video to 16-235 wherever it applies grain. See the patch.
  local src="$BUILD_DIR/grav1synth-${GRAV1SYNTH_VERSION}"
  mkdir -p "$BUILD_DIR"
  rm -rf "$src"
  curl -fsSL "https://static.crates.io/crates/grav1synth/grav1synth-${GRAV1SYNTH_VERSION}.crate" \
    | tar -xz -C "$BUILD_DIR"
  patch -d "$src" -p1 < "$SCRIPT_DIR/grav1synth-${GRAV1SYNTH_VERSION}-grain-flag.patch"
  cargo install --path "$src" --locked --force
  grav1synth --version || warn "grav1synth installed but --version failed"
  done_with "$src"
}

stage_svtfg() {
  # SVT-AV1-Essential's standalone grain estimator, for grain.estimator = "svtfg"
  # (the default). Only this tool is built from Essential: mainline SVT-AV1 stays
  # the encoder. It needs nothing beyond what the svtav1 stage does, and builds in
  # under a minute.
  log "Building SvtFgEssential (SVT-AV1-Essential ${SVTFG_COMMIT:0:12})"
  mkdir -p "$BUILD_DIR"
  local src="$BUILD_DIR/SVT-AV1-Essential"
  if [[ ! -d $src/.git ]]; then
    git init -q "$src"
    git -C "$src" remote add origin https://github.com/nekotrix/SVT-AV1-Essential.git
  fi
  git -C "$src" fetch --depth 1 origin "$SVTFG_COMMIT"
  git -C "$src" checkout -q FETCH_HEAD
  rm -rf "$src/build" "$src/Bin"
  cmake -S "$src" -B "$src/build" \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_APPS=OFF -DBUILD_TESTING=OFF \
    -DBUILD_SHARED_LIBS=OFF -DSVT_AV1_LTO=OFF
  cmake --build "$src/build" --target SvtFgEssential -j "$JOBS"
  local built="$src/Bin/Release/SvtFgEssential"
  [[ -x $built ]] || die "SvtFgEssential did not build"
  # Without privileges when the prefix is ours, which is the default.
  into_prefix() { if [[ -w ${PREFIX} || ! -e ${PREFIX} ]]; then "$@"; else as_root "$@"; fi; }
  into_prefix mkdir -p "$PREFIX/bin"
  into_prefix install -m 755 "$built" "$PREFIX/bin/SvtFgEssential"
  # Its --version names the tool (1.0.0), not the estimator, which lives in the
  # codec library and changes with Essential's releases: this is how `doctor`
  # knows which it is.
  printf '%s\n' "$SVTFG_COMMIT" | into_prefix tee "$PREFIX/bin/SvtFgEssential.commit" >/dev/null
  "$PREFIX/bin/SvtFgEssential" --version
  done_with "$src"
}

stage_scenechange() {
  # av-scenechange's own command-line tool: `regrain` finds scene cuts with it on
  # frames from the video engine, twice as fast as Av1an's detection. The crate
  # Av1an detects with, at the version Av1an's lock file takes. Builds in under a
  # minute once Av1an has filled cargo's cache.
  log "Installing av-scenechange ${SCENECHANGE_VERSION} (scene cuts for regrain)"
  ensure_cargo
  cargo install av-scenechange --version "$SCENECHANGE_VERSION" --locked --force
  # It has no --version: this is how `doctor` knows which it is.
  # Where cargo put it: CARGO_INSTALL_ROOT (as in the Dockerfile), else CARGO_HOME.
  local bin="${CARGO_INSTALL_ROOT:-${CARGO_HOME:-$HOME/.cargo}}/bin"
  printf '%s\n' "$SCENECHANGE_VERSION" > "$bin/av-scenechange.version"
  [[ -x $bin/av-scenechange ]] || die "av-scenechange did not install"
}

# The first Python on PATH that is 3.11 or newer (tomllib), or nothing.
find_python() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$candidate"
      return
    fi
  done
}

stage_verify() {
  log "Verifying with autotranscode doctor"
  export PATH="$HOME/.cargo/bin:$PREFIX/bin:$PATH"
  local python
  python=$(find_python)
  [[ -n $python ]] || die "autotranscode needs Python 3.11 or newer; none found on PATH"
  # Run from the repo root so `python -m autotranscode` resolves the package.
  local root
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  ( cd "$root" && "$python" -m autotranscode doctor ) \
    || die "doctor reported missing prerequisites (see above)"
  cat <<EOF

Next steps:
  # Put the user prefix on PATH for future shells, if it is not already:
  #   echo 'export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"' >> ~/.profile
  ${python} -m venv .venv && .venv/bin/pip install -e .
  cp config.example.toml ~/.config/autotranscode.toml
  .venv/bin/autotranscode measure /path/to/a/few/films
  # calibrate grain.threshold_db from those numbers, then:
  .venv/bin/autotranscode run /path/to/library
EOF
}

main() {
  local stages=("$@")
  if [[ ${#stages[@]} -eq 0 ]]; then
    stages=(deps ffmpeg svtav1 vapoursynth lsmash av1an grav1synth svtfg scenechange verify)
  fi
  for stage in "${stages[@]}"; do
    case "$stage" in
      deps)       stage_deps ;;
      ffmpeg)     stage_ffmpeg ;;
      svtav1)     stage_svtav1 ;;
      vapoursynth) stage_vapoursynth ;;
      lsmash)     stage_lsmash ;;
      av1an)      stage_av1an ;;
      grav1synth) stage_grav1synth ;;
      svtfg)      stage_svtfg ;;
      scenechange) stage_scenechange ;;
      verify)     stage_verify ;;
      *)          die "unknown stage: $stage" ;;
    esac
  done
  log "Done."
}

main "$@"
