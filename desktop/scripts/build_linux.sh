#!/usr/bin/env bash
# Build KeiroLake's Linux packages (.deb, .rpm, AppImage) on Ubuntu 22.04 (native or WSL).
#   sudo bash desktop/scripts/build_linux.sh [path-to-repo] [output-dir]
# Building on 22.04 (glibc 2.35) keeps the packages installable on most current distributions.
set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
OUT="${2:-$SRC/dist/linux}"
WORK="${WORK:-/root/keirolake-build}"
NODE_VERSION="22.12.0"
export DEBIAN_FRONTEND=noninteractive
log() { printf '\n==> %s\n' "$*"; }

log "System packages"
apt-get update -qq
apt-get install -y -qq build-essential curl wget file rsync xz-utils pkg-config libssl-dev patchelf rpm desktop-file-utils squashfs-tools \
  libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libsecret-1-dev libxdo-dev software-properties-common >/dev/null
if ! command -v python3.12 >/dev/null; then
  # The deadsnakes mirror sometimes answers 503: retry it, and fall back to a standalone Python 3.12 (uv) if it stays down.
  if ! (add-apt-repository -y ppa:deadsnakes/ppa >/dev/null && apt-get -o Acquire::Retries=6 update -qq         && apt-get -o Acquire::Retries=6 install -y -qq python3.12 python3.12-venv python3.12-dev >/dev/null); then
    log "deadsnakes unavailable: using a standalone Python 3.12"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
    uv python install 3.12
    ln -sf "$(uv python find 3.12)" /usr/local/bin/python3.12
  fi
fi

log "Node $NODE_VERSION"
if ! node --version 2>/dev/null | grep -q "v$NODE_VERSION"; then
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" -o /tmp/node.tar.xz
  tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
fi
node --version

log "Rust"
if ! command -v cargo >/dev/null && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null
fi
export PATH="$HOME/.cargo/bin:$PATH"
rustc --version

log "Copying sources to $WORK"
mkdir -p "$WORK"
rsync -a --delete \
  --exclude node_modules --exclude target --exclude .venv --exclude data --exclude dist --exclude 'desktop/build' \
  --exclude 'desktop/src-tauri/binaries/*.exe' --exclude 'desktop/src-tauri/binaries/*-windows-*' --exclude .git \
  "$SRC/" "$WORK/"
cd "$WORK"

log "Web UI"
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build)

log "Engine (Python) and Qdrant sidecars"
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -q --upgrade pip
backend/.venv/bin/pip install -q -e "backend[desktop]"
backend/.venv/bin/python desktop/scripts/build_sidecars.py

log "Desktop packages"
cd desktop
npm ci --no-audit --no-fund --loglevel=error
export APPIMAGE_EXTRACT_AND_RUN=1 NO_STRIP=true
mkdir -p "$OUT"
collect() { find src-tauri/target/release/bundle \( -name '*.deb' -o -name '*.rpm' -o -name '*.AppImage' \) -newer package.json -exec cp -v {} "$OUT/" \; ; }
touch package.json  # marks the start of this build, so only fresh packages are collected

npx tauri build --config src-tauri/tauri.bundle.conf.json --bundles deb,rpm
collect

# linuxdeploy writes an RPATH into every binary in usr/bin, which breaks Qdrant's static-pie build
# (it segfaults on start). Put the untouched binary back and rebuild the image with appimagetool.
repack_appimage() {
  local dir=src-tauri/target/release/bundle/appimage tool=/tmp/keirolake-appimagetool
  local image; image=$(ls "$dir"/*.AppImage | head -1)
  if [ ! -x "$tool/squashfs-root/appimagetool-prefix/usr/bin/appimagetool" ]; then
    rm -rf "$tool"; mkdir -p "$tool"
    (cd "$tool" && ~/.cache/tauri/linuxdeploy-plugin-appimage.AppImage --appimage-extract >/dev/null)
  fi
  cp src-tauri/binaries/qdrant-x86_64-unknown-linux-gnu "$dir/KeiroLake.AppDir/usr/bin/qdrant"
  ARCH=x86_64 "$tool/squashfs-root/appimagetool-prefix/usr/bin/appimagetool" --no-appstream "$dir/KeiroLake.AppDir" "$image"
  touch "$image"
}

log "AppImage (its tools download from GitHub; retried because that download can time out)"
for attempt in 1 2 3 4; do
  if npx tauri build --config src-tauri/tauri.bundle.conf.json --bundles appimage; then repack_appimage; collect; break; fi
  echo "AppImage attempt $attempt failed; retrying in 20 s"; sleep 20
done

log "Packages in $OUT"
ls -la "$OUT"
