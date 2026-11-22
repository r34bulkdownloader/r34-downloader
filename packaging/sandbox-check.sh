#!/usr/bin/env bash
# Runs the bundled python with every host libQt6* hidden (bwrap) and asserts the app's Qt features work
# from the bundle alone. Usage: sandbox-check.sh APPDIR
set -euo pipefail
APPDIR="$(cd "$1" && pwd)"
PY="$APPDIR/usr/python/bin/python3"
if ! command -v bwrap >/dev/null; then
  echo "WARNING: bwrap not installed - SKIPPING host-Qt-masked sandbox check (bundle is NOT verified standalone)" >&2
  exit 0
fi
args=(--ro-bind / / --dev /dev --proc /proc)
n=0
while IFS= read -r f; do
  args+=(--ro-bind /dev/null "$f"); n=$((n + 1))
done < <(find /usr/lib /usr/lib64 /usr/local/lib -maxdepth 2 -type f \( -name 'libQt6*.so*' -o -name 'libQt5*.so*' \) 2>/dev/null | sort -u)
for d in /usr/lib/qt6 /usr/lib/qt /usr/lib64/qt6 /usr/share/qt6; do
  [ -d "$d" ] && args+=(--tmpfs "$d")
done
echo "sandbox check: masking $n host libQt* files"
bwrap "${args[@]}" env -i HOME=/tmp PATH=/usr/bin QT_QPA_PLATFORM=offscreen "$PY" -I - <<'PYEOF'
from PySide6.QtWidgets import QApplication
app = QApplication([])
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtGui import QImageReader
from PySide6.QtNetwork import QSslSocket
fmts = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
missing = {"gif", "jpeg", "png", "webp"} - fmts
assert not missing, f"missing image formats: {missing}"
assert QSslSocket.supportsSsl(), "no SSL"
assert QMediaPlayer().isAvailable(), "QMediaPlayer not available (no multimedia backend)"
import r34dl.ui
print("sandbox probe OK: multimedia, image formats", sorted(fmts & {"gif","jpeg","png","webp"}), "ssl, r34dl.ui")
PYEOF
