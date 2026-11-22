#!/usr/bin/env python3
"""Fail if a bundled Qt library/plugin NEEDs a library that is neither in the bundle nor an allowed host lib."""
import re
import subprocess
import sys
from pathlib import Path

appdir = Path(sys.argv[1])
ps = next(appdir.glob("usr/python/lib/python3*/site-packages/PySide6"))
qt = ps / "Qt"

HOST_OK = re.compile(
    r"^(libc|libm|libdl|libpthread|librt|libutil|libresolv|libnsl)\.so"
    r"|^ld-linux"
    r"|^(libstdc\+\+|libgcc_s)\.so"
    r"|^lib(GL|EGL|GLX|OpenGL|GLESv2)\.so"
    r"|^libX[A-Za-z0-9-]*\.so|^libxcb|^libxkbcommon|^libwayland-"
    r"|^lib(fontconfig|freetype)\.so"
    r"|^lib(glib|gobject|gthread|gio)-2\.0\.so"
    r"|^lib(pulse|pulse-simple|asound|dbus-1|z)\.so"
    r"|^lib(gssapi_krb5|krb5|k5crypto|com_err|krb5support|keyutils)\.so"
    r"|^lib(drm|gbm|va|va-drm|va-x11|vdpau)\.so"
    r"|^lib(zstd|brotlidec|brotlicommon)\.so"  # needed by Qt6Core/Qt6Network; present on every mainstream distro
)

bundled = {p.name for p in appdir.rglob("*") if p.is_file() or p.is_symlink()}
targets = []
for sub in ("platforms", "multimedia", "imageformats", "tls"):
    targets += sorted((qt / "plugins" / sub).glob("*.so*"))
targets += sorted((qt / "lib").glob("libQt6*.so*"))

bad = []
for f in targets:
    if f.is_symlink():
        continue
    out = subprocess.run(["readelf", "-d", str(f)], capture_output=True, text=True).stdout
    for need in re.findall(r"NEEDED\)\s+Shared library: \[([^\]]+)\]", out):
        if need not in bundled and not HOST_OK.match(need):
            bad.append((f.relative_to(qt), need))

print(f"NEEDED check: {len(targets)} files scanned")
for f, n in bad:
    print(f"  UNRESOLVED: {f} -> {n}")
if bad:
    print(f"ERROR: {len(bad)} unresolved NEEDED entries", file=sys.stderr)
    sys.exit(1)
print("NEEDED check: OK")
