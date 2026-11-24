#!/usr/bin/env bash
# Install and start KeiroLake's Linux packages, and check the engine and Qdrant come up.
#   sudo bash desktop/scripts/test_linux_packages.sh <dir-with-packages>
set -uo pipefail
PKG="${1:?package directory}"
DATA="$HOME/.local/share/app.keirolake.desktop"
pass=0; fail=0
newest() { ls "$PKG"/$1 2>/dev/null | sort -V | tail -1; }  # the folder may hold older versions too
DEB=$(newest 'KeiroLake_*_amd64.deb'); APPIMAGE=$(newest 'KeiroLake_*_amd64.AppImage'); RPM=$(newest 'KeiroLake-*.x86_64.rpm')
echo "testing: $(basename "$DEB") $(basename "$APPIMAGE") $(basename "$RPM")"
ok() { echo "PASS  $*"; pass=$((pass+1)); }
bad() { echo "FAIL  $*"; fail=$((fail+1)); }

wait_engine() {  # $1 = label
  local port=""
  for _ in $(seq 1 90); do
    port=$(grep -ao "Uvicorn running on http://127.0.0.1:[0-9]*" "$DATA/logs/engine.log" 2>/dev/null | tail -1 | grep -o "[0-9]*$")
    [ -n "$port" ] && break; sleep 2
  done
  if [ -z "$port" ]; then bad "$1: engine did not start"; tail -20 "$DATA/logs/engine.log" 2>/dev/null; return; fi
  ok "$1: engine listening on 127.0.0.1:$port"
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/api/net")
  [ "$code" = "401" ] && ok "$1: API refuses requests without the launch token (401)" || bad "$1: expected 401, got $code"
  v1=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/v1/me")
  [ "$v1" = "401" ] && ok "$1: data API refuses requests without a key (401)" || bad "$1: data API expected 401, got $v1"
  if pgrep -x qdrant >/dev/null; then ok "$1: native Qdrant server running"
  elif [ -d "$DATA/qdrant" ]; then echo "NOTE  $1: Qdrant server could not start here; using the embedded vector store"; ok "$1: vector store available (embedded fallback)"
  else bad "$1: no vector store"; tail -5 "$DATA/logs/qdrant.log" 2>/dev/null; fi
  pgrep -f keirolake-engine >/dev/null && ok "$1: engine process alive" || bad "$1: engine process missing"
}

stop_app() { pkill -f keirolake-desktop; pkill -f keirolake-engine; pkill -x qdrant; sleep 3; }

echo "== .deb"
dpkg-deb -I "$DEB" | grep -E "Package|Version|Depends" | sed 's/^/   /'
apt-get install -y -qq "$DEB" >/dev/null 2>&1 && ok "deb installs with apt (dependencies resolved)" || bad "deb install failed"
for f in /usr/bin/keirolake-desktop /usr/bin/keirolake-engine /usr/bin/qdrant; do [ -x "$f" ] && ok "installed $f" || bad "missing $f"; done
ls /usr/share/applications/*.desktop 2>/dev/null | grep -qi keiro && ok "desktop menu entry installed" || bad "no .desktop entry"
rm -rf "$DATA"
(keirolake-desktop >/tmp/keirolake-deb.out 2>&1 &)
wait_engine "deb"
stop_app
apt-get remove -y -qq keiro-lake >/dev/null 2>&1 || apt-get remove -y -qq keirolake >/dev/null 2>&1
command -v keirolake-desktop >/dev/null && bad "deb uninstall left the binary" || ok "deb uninstalls cleanly"

echo "== AppImage"
[ -x "$APPIMAGE" ] || { cp "$APPIMAGE" /tmp/k.AppImage && chmod +x /tmp/k.AppImage && APPIMAGE=/tmp/k.AppImage; }
rm -rf "$DATA"
(APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGE" >/tmp/keirolake-appimage.out 2>&1 &)
wait_engine "AppImage"
stop_app

echo "== .rpm"
rpm -qpi "$RPM" 2>/dev/null | grep -E "^(Name|Version|Architecture|Size)" | sed 's/^/   /'
rpm -qpl "$RPM" 2>/dev/null | grep -E "bin/(keirolake-desktop|keirolake-engine|qdrant)$" | wc -l | grep -q 3 && ok "rpm contains the app, engine and Qdrant" || bad "rpm contents incomplete"
rpm -qpR "$RPM" 2>/dev/null | grep -qi webkit && ok "rpm declares the WebKitGTK dependency" || bad "rpm missing webkit dependency"

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
