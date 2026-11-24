#!/usr/bin/env bash
# Build and test the Linux packages in clean Ubuntu 22.04 containers (used by the release workflow; works locally too).
#   bash desktop/scripts/ci_linux.sh            build, then test -> dist/linux/*.deb, *.rpm, *.AppImage
#   bash desktop/scripts/ci_linux.sh --test     only test packages already in dist/linux
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/dist/linux"
mkdir -p "$OUT"
if [ -n "${MSYSTEM:-}" ]; then  # Git Bash on Windows: hand Docker Windows paths and keep /src as written
  export MSYS_NO_PATHCONV=1
  ROOT="$(cd "$ROOT" && pwd -W)"; OUT="$ROOT/dist/linux"
fi

if [ "${1:-}" != "--test" ]; then
  docker run --rm --init -v "$ROOT:/src" ubuntu:22.04 bash /src/desktop/scripts/build_linux.sh /src /src/dist/linux
fi

# Install and start each package on a fresh system with a virtual screen. Xvfb is started directly (xvfb-run can wait
# forever in a container with no terminal), and the whole test has a hard time limit so a hang fails in minutes.
timeout 1500 docker run --rm --init -v "$OUT:/pkg:ro" -v "$ROOT/desktop/scripts:/scripts:ro" ubuntu:22.04 bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  echo "[$(date +%T)] installing the test tools"
  apt-get update -qq && apt-get install -y -qq xvfb xauth curl procps rpm dbus-x11 >/dev/null 2>&1
  Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  export DISPLAY=:99
  for i in $(seq 1 30); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.5; done
  echo "[$(date +%T)] virtual screen ready"
  timeout 1200 bash /scripts/test_linux_packages.sh /pkg'
