# autotranscode with everything it drives, for Linux on arm64 or x86-64 and for
# Apple silicon Macs (whose Docker runs an arm64 Linux VM). See "Docker" in the README.
#
#   docker compose build
#   docker compose run --rm autotranscode doctor
#
# The tools are built by scripts/bootstrap.sh, stage by stage, so the image and
# a native install get the same versions, those autotranscode/toolchain.toml pins,
# and the same grav1synth patch.

ARG DEBIAN_RELEASE=trixie

FROM debian:${DEBIAN_RELEASE}-slim AS build
# Parallel jobs for the compilers; empty means one per core.
ARG JOBS=
ENV DEBIAN_FRONTEND=noninteractive \
    PREFIX=/opt/autotranscode \
    BUILD_DIR=/build \
    CARGO_INSTALL_ROOT=/opt/autotranscode \
    PATH=/opt/autotranscode/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY scripts/bootstrap.sh scripts/grav1synth-0.2.0-grain-flag.patch \
     autotranscode/toolchain.toml /src/
# One layer per stage, so that changing a pin rebuilds only what follows it.
RUN /src/bootstrap.sh deps
RUN /src/bootstrap.sh svtav1
RUN /src/bootstrap.sh vapoursynth
# VapourSynth's Python module went into the prefix, which Python does not search
# (a native install puts it in the user site, which it does). Av1an's own check
# below needs it: VSScript initialises by importing it.
RUN site="$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
 && mkdir -p "$site" \
 && printf '%s\n' "$PREFIX"/lib/python3*/site-packages > "$site/vapoursynth.pth" \
 && python3 -c 'import vapoursynth; print("vapoursynth", vapoursynth.__version__)'
RUN /src/bootstrap.sh lsmash
RUN CARGO_BUILD_JOBS="${JOBS:-$(nproc)}" /src/bootstrap.sh av1an
RUN CARGO_BUILD_JOBS="${JOBS:-$(nproc)}" /src/bootstrap.sh grav1synth
# Keep what runs, drop what only built things. Then name the Debian packages the
# binaries link against, so the runtime image installs exactly those.
RUN rm -rf "$PREFIX/include" "$PREFIX/lib/pkgconfig" "$PREFIX/share" "$PREFIX/.crates"* \
 && find "$PREFIX" \( -name '*.a' -o -name '*.la' \) -delete \
 && find "$PREFIX" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
    | xargs -0 ldd 2>/dev/null \
    | awk '$2 == "=>" && $3 ~ /^\// { print $3 }' \
    | xargs -r readlink -f | grep -v "^$PREFIX/" | sort -u \
    | xargs -r dpkg -S 2>/dev/null | cut -d: -f1 | sort -u > "$PREFIX/runtime-packages" \
 && test -s "$PREFIX/runtime-packages" \
 && cat "$PREFIX/runtime-packages"


FROM debian:${DEBIAN_RELEASE}-slim
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=build /opt/autotranscode /opt/autotranscode
COPY scripts/bootstrap.sh autotranscode/toolchain.toml /tmp/
# jellyfin-ffmpeg from Jellyfin's repository, by the same stage a native install
# uses; then the libraries the built tools need, Python, mkvtoolnix, and the time
# zones that TZ names, for log times in local time. tini is
# PID 1: it passes `docker stop`'s SIGTERM on, which autotranscode turns into a
# checkpoint, and reaps the processes Av1an leaves behind.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && PREFIX=/usr/local /tmp/bootstrap.sh ffmpeg \
 && apt-get install -y --no-install-recommends python3 mkvtoolnix tini tzdata \
      $(cat /opt/autotranscode/runtime-packages) \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/* /tmp/bootstrap.sh /tmp/toolchain.toml
# The prefix's own libraries (zimg, VapourSynth) and its Python module, which
# VapourSynth's embedded interpreter imports too, then the app itself.
RUN echo /opt/autotranscode/lib > /etc/ld.so.conf.d/autotranscode.conf && ldconfig \
 && site="$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
 && mkdir -p "$site" \
 && printf '%s\n' /opt/autotranscode/app /opt/autotranscode/lib/python3*/site-packages \
      > "$site/autotranscode.pth" \
 && printf '#!/bin/sh\nexec python3 -m autotranscode "$@"\n' > /usr/local/bin/autotranscode \
 && chmod 755 /usr/local/bin/autotranscode \
 && mkdir -m 1777 /data /config
COPY autotranscode /opt/autotranscode/app/autotranscode
RUN python3 -m compileall -q /opt/autotranscode/app

# /config holds autotranscode.toml; /data is home, so the default work_dir and
# state file land in /data/.cache/autotranscode. Both are meant to be mounted.
ENV PATH=/opt/autotranscode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/data \
    XDG_CONFIG_HOME=/config \
    LANG=C.UTF-8
WORKDIR /data
ENTRYPOINT ["/usr/bin/tini", "--", "autotranscode"]
CMD ["doctor"]
