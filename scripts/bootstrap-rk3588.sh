#!/usr/bin/env bash
# Install everything autotranscode drives, on a Debian/Ubuntu arm64 RK3588 board.
#
# Nothing here is exotic; it is the sequence that turns a bare board into a machine
# where `autotranscode doctor` passes. Re-running it is safe.
#
#   ./scripts/bootstrap-rk3588.sh            # everything
#   ./scripts/bootstrap-rk3588.sh ffmpeg     # just one stage
#
# Stages: deps ffmpeg svtav1 av1an grav1synth verify

set -euo pipefail

SVT_AV1_VERSION="${SVT_AV1_VERSION:-v4.0.0}"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="${BUILD_DIR:-$HOME/.cache/autotranscode-build}"
JOBS="${JOBS:-$(nproc)}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $(uname -m) == aarch64 ]] || warn "not aarch64 ($(uname -m)); this script targets RK3588 boards"

stage_deps() {
  log "Installing build and runtime dependencies"
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \
    build-essential cmake git yasm nasm pkg-config ca-certificates curl gnupg \
    clang libclang-dev llvm-dev \
    libavformat-dev libavfilter-dev libavdevice-dev libavutil-dev libswscale-dev \
    mkvtoolnix python3 python3-venv
}

stage_ffmpeg() {
  log "Installing jellyfin-ffmpeg (has libvmaf, rkmpp and rkrga; the distro build has none)"
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
    | sudo gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg

  local id codename
  id=$(. /etc/os-release && echo "${ID}")
  codename=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
  [[ -n $codename ]] || die "cannot determine distro codename from /etc/os-release"

  echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=arm64] https://repo.jellyfin.org/${id} ${codename} main" \
    | sudo tee /etc/apt/sources.list.d/jellyfin.sources.list >/dev/null
  sudo apt-get update

  # Pick the highest jellyfin-ffmpeg major available rather than hardcoding a name
  # that changes every release (jellyfin-ffmpeg6, 7, 8, ...).
  local pkg
  pkg=$(apt-cache search --names-only '^jellyfin-ffmpeg[0-9]+$' \
        | awk '{print $1}' | sort -V | tail -1)
  [[ -n $pkg ]] || die "no jellyfin-ffmpeg package found for ${id}/${codename}"
  log "Selected package: ${pkg}"
  sudo apt-get install -y "$pkg"

  local bin=/usr/lib/jellyfin-ffmpeg/ffmpeg
  [[ -x $bin ]] || die "expected ${bin} after install"
  "$bin" -hide_banner -version | head -1
  if "$bin" -hide_banner -hwaccels | grep -q rkmpp; then
    log "rkmpp hardware decoding is available"
  else
    warn "rkmpp not listed; hardware decode will not be used (encoding is unaffected)"
  fi
}

stage_svtav1() {
  log "Building SVT-AV1 ${SVT_AV1_VERSION} (SvtAv1EncApp)"
  # Av1an drives the encoder binary directly. It is also the only way to reach
  # --fgs-table, which is documented as available through the SvtAv1Enc interface only.
  mkdir -p "$BUILD_DIR"
  local src="$BUILD_DIR/SVT-AV1"
  if [[ -d $src/.git ]]; then
    git -C "$src" fetch --depth 1 origin "$SVT_AV1_VERSION"
    git -C "$src" checkout -q FETCH_HEAD
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
  sudo cmake --install "$src/Build"
  sudo ldconfig
  command -v SvtAv1EncApp >/dev/null || die "SvtAv1EncApp not on PATH after install"
  SvtAv1EncApp --version | head -1
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
  ensure_cargo
  # No VapourSynth here, so the FFmpeg-only 'hybrid' chunk method is what we use.
  cargo install av1an --locked
  av1an --version | head -1
}

stage_grav1synth() {
  log "Installing grav1synth (source-accurate film grain tables)"
  ensure_cargo
  cargo install grav1synth --locked
  grav1synth --help >/dev/null 2>&1 || warn "grav1synth installed but --help failed"
}

stage_verify() {
  log "Verifying with autotranscode doctor"
  export PATH="$HOME/.cargo/bin:$PATH"
  # Run from the repo root so `python3 -m autotranscode` resolves the package.
  local root
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  ( cd "$root" && python3 -m autotranscode --profile rk3588 doctor ) \
    || die "doctor reported missing prerequisites (see above)"
  cat <<'EOF'

Next steps:
  cp config.example.toml ~/.config/autotranscode.toml   # then set machine.profile = "rk3588"
  autotranscode -c ~/.config/autotranscode.toml measure /path/to/a/few/films
  # calibrate grain.threshold_db from those numbers, then:
  autotranscode -c ~/.config/autotranscode.toml run /path/to/library
EOF
}

main() {
  local stages=("$@")
  if [[ ${#stages[@]} -eq 0 ]]; then
    stages=(deps ffmpeg svtav1 av1an grav1synth verify)
  fi
  for stage in "${stages[@]}"; do
    case "$stage" in
      deps)       stage_deps ;;
      ffmpeg)     stage_ffmpeg ;;
      svtav1)     stage_svtav1 ;;
      av1an)      stage_av1an ;;
      grav1synth) stage_grav1synth ;;
      verify)     stage_verify ;;
      *)          die "unknown stage: $stage" ;;
    esac
  done
  log "Done."
}

main "$@"
