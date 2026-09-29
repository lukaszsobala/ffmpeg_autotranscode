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
# Stages: deps ffmpeg svtav1 vapoursynth av1an grav1synth verify
#
# On Linux only the `deps` and `ffmpeg` stages use sudo, and only to run apt-get.
# On macOS nothing does: Homebrew runs as you. Everything built from source
# installs into a user-owned prefix ($HOME/.local by default), so the long stages
# need no privileges and leave nothing behind outside your home directory. Set
# PREFIX=/usr/local if you deliberately want a system-wide install; the script
# will then use sudo for that one `cmake --install`.

set -euo pipefail

# Pinned, not taken from a package manager: distribution packages lag far behind
# (Debian 13 has 2.3.0, without the dark bias the 1080p setting depends on), and
# a release can move quality at the same CRF. The per-resolution settings were
# judged on 4.0.0; 4.2.0 matched them within 0.2 dB on the 1080p excerpts and
# encodes 5-10% faster on ARM. Re-check the ladder on a few excerpts when moving this.
SVT_AV1_VERSION="${SVT_AV1_VERSION:-v4.2.0}"
ZIMG_VERSION="${ZIMG_VERSION:-release-3.0.6}"
# Pinned deliberately, not lazily. R73 onward added Vulkan GPU filters whose
# gpufilter.h does not compile against Debian 13's Vulkan headers
# ("invalid conversion from 'const void*' to 'void*'"), and switched to a Meson
# build that makes Cython mandatory. R72 is the last autotools release without
# that code, and since nothing here ever calls VapourSynth, newer buys us nothing.
# (macOS takes Homebrew's VapourSynth instead, which builds there.)
VAPOURSYNTH_VERSION="${VAPOURSYNTH_VERSION:-R72}"
# Built from the crate with a local patch; see stage_grav1synth.
GRAV1SYNTH_VERSION="${GRAV1SYNTH_VERSION:-0.2.0}"
PREFIX="${PREFIX:-$HOME/.local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-$HOME/.cache/autotranscode-build}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux)
    OS=linux
    command -v apt-get >/dev/null || die "this script installs with apt-get (Debian, Ubuntu \
and derivatives). On another distribution install by hand: build tools, cmake, nasm, \
pkg-config, mkvtoolnix, Python 3.11+ and the ffmpeg development headers, then run the \
ffmpeg, svtav1, vapoursynth, av1an and grav1synth stages, which need no package manager."
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
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \
    build-essential cmake git yasm nasm pkg-config ca-certificates curl gnupg xz-utils \
    autoconf automake libtool \
    clang libclang-dev llvm-dev \
    libavformat-dev libavfilter-dev libavdevice-dev libavutil-dev libswscale-dev \
    mkvtoolnix python3 python3-venv
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
  local url
  url=$(curl -fsSL https://api.github.com/repos/jellyfin/jellyfin-ffmpeg/releases/latest \
        | grep -o "https://[^\"]*_portable_${platform}-gpl\.tar\.xz" | head -1)
  [[ -n $url ]] || die "could not find the ${platform} build in jellyfin-ffmpeg's latest release"
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
  log "Installing jellyfin-ffmpeg (has tonemapx and libvmaf, and rkmpp on Rockchip boards)"
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
      sudo install -d -m 0755 /etc/apt/keyrings
      curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | sudo gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg
      echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=${arch}] https://repo.jellyfin.org/${id} ${codename} main" \
        | sudo tee /etc/apt/sources.list.d/jellyfin.sources.list >/dev/null
      if sudo apt-get update; then
        # The highest major available, rather than a name that changes every
        # release (jellyfin-ffmpeg6, 7, 8, ...).
        pkg=$(apt-cache search --names-only '^jellyfin-ffmpeg[0-9]+$' \
              | awk '{print $1}' | sort -V | tail -1)
      fi
      if [[ -z $pkg ]]; then
        warn "Jellyfin's repository has nothing for ${id} ${codename}; removing it"
        sudo rm -f /etc/apt/sources.list.d/jellyfin.sources.list
        sudo apt-get update
      fi
    fi
    if [[ -n $pkg ]]; then
      log "Selected package: ${pkg}"
      sudo apt-get install -y "$pkg"
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
    sudo cmake --install "$src/Build"
    [[ $OS == linux ]] && sudo ldconfig
  fi

  local svt="$PREFIX/bin/SvtAv1EncApp"
  [[ -x $svt ]] || die "SvtAv1EncApp not found at ${svt} after install"
  "$svt" --version | head -1
  # Homebrew's ffmpeg brings its own svt-av1, whose SvtAv1EncApp may come first on
  # PATH. Name ours in the config so the version the ladder was tuned on is used.
  if [[ $(command -v SvtAv1EncApp || true) != "$svt" ]]; then
    warn "PATH finds $(command -v SvtAv1EncApp || echo 'no SvtAv1EncApp'), not this build."
    warn "Point the config at it: paths.svtav1encapp = \"${svt}\""
  fi
}

stage_vapoursynth() {
  # This is not optional and not about chunk methods. Av1an links -lvapoursynth
  # unconditionally: no published version and no upstream branch gates it behind a
  # cargo feature, so the library must exist even though this tool never uses it
  # (encode.chunk_method 'hybrid' is FFmpeg-only).
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
  # The Python module is not optional, however unused VapourSynth is here: Av1an
  # links -lvapoursynth-script, and VSScript's API initialises by importing the
  # Python `vapoursynth` module. Without it every av1an invocation, `--version`
  # included, dies with "Failed to get VSScript API".
  #
  # Cython goes into a throwaway venv rather than system-wide, so this stays
  # privilege-free. With PREFIX=$HOME/.local the module lands in
  # ~/.local/lib/pythonX.Y/site-packages, which is Python's user site and already
  # on sys.path -- no PYTHONPATH needed.
  local venv="$BUILD_DIR/buildenv"
  [[ -d $venv ]] || python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade cython
  ( cd "$vs" && ./autogen.sh \
    && PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" ./configure \
         --prefix="$PREFIX" --with-cython="$venv/bin/cython" \
    && make -j "$JOBS" && make install )

  PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" pkg-config --modversion vapoursynth \
    || die "vapoursynth.pc not found under ${PREFIX}"
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
    av1an --version | head -1
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
    cargo install av1an --locked
  av1an --version | head -1
}

stage_grav1synth() {
  log "Building grav1synth ${GRAV1SYNTH_VERSION} (source-accurate film grain tables), patched"
  ensure_cargo
  # Patched, so not `cargo install grav1synth`: 0.2.0 (the latest release, and main
  # as of 2026-09) corrupts the sequence header when its last field ends a byte,
  # which with the usual flags means widths 513-1024 at heights 257-512 -- 640x480,
  # NTSC DVD, 854x480 -- and every such file fails at the grain step. See the patch.
  local src="$BUILD_DIR/grav1synth-${GRAV1SYNTH_VERSION}"
  mkdir -p "$BUILD_DIR"
  rm -rf "$src"
  curl -fsSL "https://static.crates.io/crates/grav1synth/grav1synth-${GRAV1SYNTH_VERSION}.crate" \
    | tar -xz -C "$BUILD_DIR"
  patch -d "$src" -p1 < "$SCRIPT_DIR/grav1synth-${GRAV1SYNTH_VERSION}-grain-flag.patch"
  cargo install --path "$src" --locked --force
  grav1synth --help >/dev/null 2>&1 || warn "grav1synth installed but --help failed"
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
    stages=(deps ffmpeg svtav1 vapoursynth av1an grav1synth verify)
  fi
  for stage in "${stages[@]}"; do
    case "$stage" in
      deps)       stage_deps ;;
      ffmpeg)     stage_ffmpeg ;;
      svtav1)     stage_svtav1 ;;
      vapoursynth) stage_vapoursynth ;;
      av1an)      stage_av1an ;;
      grav1synth) stage_grav1synth ;;
      verify)     stage_verify ;;
      *)          die "unknown stage: $stage" ;;
    esac
  done
  log "Done."
}

main "$@"
