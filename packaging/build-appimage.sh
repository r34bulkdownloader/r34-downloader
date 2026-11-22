#!/usr/bin/env bash
# Builds R34-Downloader-x86_64.AppImage in the repo root. Needs network (GitHub + PyPI).
set -euo pipefail

PBS_TAG=20261110
PBS_FILE="cpython-3.12.16%2B${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_FILE}"
PBS_SHA256=c2f87d4b4e8db3abddb0f116c6ccfc503d0cbdbb9ee8faacddad1b679ec12e99
AIT_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
AIT_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
CACHE="$BUILD/cache"
APPDIR="$BUILD/AppDir"
OUT="$ROOT/R34-Downloader-x86_64.AppImage"
mkdir -p "$CACHE"

fetch() { # url file sha256
  if [ ! -f "$2" ] || ! echo "$3  $2" | sha256sum -c --quiet - 2>/dev/null; then
    curl -fsSL -o "$2" "$1"
    echo "$3  $2" | sha256sum -c --quiet -
  fi
}
fetch "$PBS_URL" "$CACHE/cpython.tar.gz" "$PBS_SHA256"
fetch "$AIT_URL" "$CACHE/appimagetool-x86_64.AppImage" "$AIT_SHA256"
chmod +x "$CACHE/appimagetool-x86_64.AppImage"

# 1. Python
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr"
tar -xzf "$CACHE/cpython.tar.gz" -C "$APPDIR/usr"   # yields usr/python
PY="$APPDIR/usr/python/bin/python3"

# 2. Dependencies + app (wheels only for PySide6/shiboken)
"$PY" -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r "$ROOT/packaging/requirements-appimage.txt"
rm -rf "$ROOT/build/lib" "$ROOT"/build/bdist.*
"$PY" -m pip install --no-cache-dir --no-deps "$ROOT"

SP="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PS="$SP/PySide6"
(
  cd "$PS"
  rm -rf Qt/qml Qt/lib/libQt6WebEngine* Qt/resources/qtwebengine* Qt/translations/qtwebengine* \
         Qt/lib/libQt63D* Qt/lib/libQt6Pdf* Qt/plugins/imageformats/libqpdf.so \
         Qt/lib/libQt6Designer* Qt/lib/libQt6Help* Qt/lib/libQt6Test* Qt/lib/libQt6QuickTest* \
         Qt/lib/libQt6Nfc* Qt/lib/libQt6QuickControls* Qt/lib/libQt6Quick3D* Qt/lib/libQt6Graphs* Qt/lib/libQt6LabsStyleKit* Qt/lib/libQt6QuickDialogs2* \
         Qt/libexec/QtWebEngineProcess Qt/libexec/*designer* Qt/translations \
         Qt/plugins/designer Qt/plugins/qmltooling Qt/plugins/qmllint \
         QtWebEngine* QtQuick* QtQml* Qt3D* QtPdf* QtDesigner* QtHelp* QtTest* \
         designer lupdate assistant linguist uic rcc balsam* qml* pyside6-* \
         include typesystems glue scripts support
)
rm -rf "$SP"/pip "$SP"/pip-* "$APPDIR/usr/python/lib/python3.12/site-packages/pip"
find "$APPDIR" -name __pycache__ -type d -prune -exec rm -rf {} +

# libxcb-cursor is required by Qt's xcb plugin but missing on stock Debian/Ubuntu. Bundle Debian bookworm's copy
# (the build host's is linked against newer glibc and would break the 2.34 floor).
XCBC_DEB="$CACHE/libxcb-cursor0.deb"
fetch "https://deb.debian.org/debian/pool/main/x/xcb-util-cursor/libxcb-cursor0_0.1.4-1_amd64.deb" "$XCBC_DEB" \
  a4b3c32dc008275ffcacccc1c77c030f01aad38e232e05d5ad116b76656c607c
XCBC_TMP="$(mktemp -d)"
ar p "$XCBC_DEB" data.tar.xz | tar -xJ -C "$XCBC_TMP" ./usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0.0.0 \
  ./usr/share/doc/libxcb-cursor0/copyright
cp "$XCBC_TMP/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0.0.0" "$PS/Qt/lib/libxcb-cursor.so.0"
LIC="$APPDIR/usr/share/licenses/r34-downloader"  # licenses of everything bundled (see THIRD-PARTY-NOTICES.md)
mkdir -p "$LIC"
cp "$XCBC_TMP/usr/share/doc/libxcb-cursor0/copyright" "$LIC/libxcb-cursor-copyright"
rm -rf "$XCBC_TMP"

# Every NEEDED entry of the Qt libs/plugins must resolve inside the bundle or be an allowed host lib
"$PY" -I "$ROOT/packaging/check-needed.py" "$APPDIR"

# Standalone check: with all host libQt6* masked, multimedia/image formats/SSL/app import must still work
bash "$ROOT/packaging/sandbox-check.sh" "$APPDIR"

# glibc floor check: highest GLIBC_ symbol required by any bundled ELF must be <= 2.34
MAX_GLIBC=2.34
command -v objdump >/dev/null || { echo "ERROR: objdump (binutils) required" >&2; exit 1; }
found="$(find "$APPDIR" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print0 \
  | xargs -0 -r sh -c 'objdump -T "$@" 2>/dev/null || true' _ \
  | { grep -o 'GLIBC_[0-9.]*' || true; } | sed 's/GLIBC_//' | sort -V | tail -1)"
echo "max GLIBC symbol version required: ${found:-none}"
if [ -n "$found" ] && [ "$(printf '%s\n%s\n' "$found" "$MAX_GLIBC" | sort -V | tail -1)" != "$MAX_GLIBC" ]; then
  echo "ERROR: bundle needs glibc $found > $MAX_GLIBC" >&2; exit 1
fi
[ -n "$found" ] || { echo "ERROR: no GLIBC symbols found; check did not run" >&2; exit 1; }

# 3. Licenses and desktop integration files
cp "$ROOT/LICENSE" "$LIC/GPL-3.0.txt"
cp "$ROOT/THIRD-PARTY-NOTICES.md" "$ROOT"/packaging/licenses/*.txt "$LIC/"
cp "$APPDIR/usr/python/lib/python3.12/LICENSE.txt" "$LIC/python-LICENSE.txt"
for pkg in cryptography cffi pycparser; do
  mkdir -p "$LIC/$pkg"
  cp "$SP"/$pkg-*.dist-info/licenses/* "$LIC/$pkg/"
done
cp "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
cp "$ROOT/packaging/r34-downloader.desktop" "$APPDIR/"
QT_QPA_PLATFORM=offscreen "$PY" -I - "$APPDIR/r34-downloader.png" <<'PYEOF'
import sys
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QFont
from PySide6.QtCore import Qt, QRectF
app = QGuiApplication([])
img = QImage(256, 256, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setBrush(QColor("#2f6f4f")); p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)
f = QFont(); f.setBold(True); f.setPixelSize(100); p.setFont(f)
p.setPen(QColor("white"))
p.drawText(QRectF(0, 0, 256, 256), Qt.AlignCenter, "R34")
p.end()
img.save(sys.argv[1])
PYEOF
cp "$APPDIR/r34-downloader.png" "$APPDIR/.DirIcon"
# Wrapper so Exec=r34-downloader resolves
mkdir -p "$APPDIR/usr/bin"
printf '#!/bin/sh\nexec "$APPDIR/AppRun" "$@"\n' > "$APPDIR/usr/bin/r34-downloader"
chmod +x "$APPDIR/usr/bin/r34-downloader"

# 4. Privacy: nothing from the build machine may ship (the AppImage is handed to strangers).
#  - pip records the source folder in direct_url.json and the build path in console-script headers
#    (#!/home/<user>/.../python3); those scripts don't work inside the AppImage anyway.
#  - running the bundled python above (checks, icon) leaves .pyc files holding absolute source paths.
#    They're removed, then rebuilt with paths relative to the AppImage and hash-checked (valid at any mtime).
rm -f "$SP"/*.dist-info/direct_url.json
find "$APPDIR/usr/python/bin" -type f -exec grep -lIF "$APPDIR" {} + | xargs -r rm -f
find "$APPDIR" -name __pycache__ -type d -prune -exec rm -rf {} +
"$PY" -I -B -m compileall -f -q -j0 -s "$APPDIR" -p / \
  --invalidation-mode unchecked-hash "$APPDIR/usr/python/lib/python3.12" >/dev/null
leaks="$(grep -rlaF -e "$ROOT" -e "$HOME/" -e "/home/$(id -un)" "$APPDIR" || true)"
if [ -n "$leaks" ]; then
  echo "ERROR: paths from this computer found inside the AppImage:" >&2
  echo "$leaks" | sed "s|^$APPDIR/|  |" >&2
  exit 1
fi
echo "privacy check: no build-machine paths in the bundle"

# 5. Pack
rm -f "$OUT"
export ARCH=x86_64
if ! "$CACHE/appimagetool-x86_64.AppImage" "$APPDIR" "$OUT"; then
  "$CACHE/appimagetool-x86_64.AppImage" --appimage-extract-and-run "$APPDIR" "$OUT"
fi
du -h "$OUT"
