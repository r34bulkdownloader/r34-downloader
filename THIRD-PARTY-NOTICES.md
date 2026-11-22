# Third-party software

R34 Downloader itself is licensed under the GNU General Public License v3.0 (see [LICENSE](LICENSE)).

The AppImage also contains the software below, unmodified, each under its own license. Inside the AppImage
the license texts are in `usr/share/licenses/r34-downloader/`. To look at or change what's bundled, extract it
with `./R34-Downloader-x86_64.AppImage --appimage-extract`. You can replace any of these libraries with your own
build of a compatible version, for example a newer Qt.

| Component | Version | License | Source |
|---|---|---|---|
| CPython (via python-build-standalone) | 3.12.16, build 20261110 | PSF License 2.0; includes OpenSSL (Apache-2.0), libffi, zlib, bzip2, xz, sqlite, expat, mpdecimal and others under permissive licenses | https://www.python.org/downloads/source/ · https://github.com/astral-sh/python-build-standalone |
| Qt for Python: PySide6-Essentials, PySide6-Addons, shiboken6 | 6.12.0 | LGPL-3.0 (used under LGPL-3.0) | https://code.qt.io/cgit/pyside/pyside-setup.git/ · https://download.qt.io/official_releases/QtForPython/ |
| Qt (inside PySide6) | 6.12.0 | LGPL-3.0; Qt's own third-party parts (harfbuzz, freetype, libpng, libjpeg, libwebp, pcre2 and others) are listed at https://doc.qt.io/qt-6/licenses-used-in-qt.html | https://download.qt.io/official_releases/qt/ |
| FFmpeg libraries (libavcodec, libavformat, libavutil, libswresample, libswscale), shipped in the PySide6-Addons wheel for Qt Multimedia | as built by the Qt Company for Qt 6.12.0 | LGPL-2.1-or-later | https://ffmpeg.org/download.html · https://code.qt.io/cgit/qt/qtmultimedia.git/ |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| cffi | 2.1.1 | MIT | https://github.com/python-cffi/cffi |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| libxcb-cursor (Debian 0.1.4-1) | 0.1.4 | MIT/X11-style | https://deb.debian.org/debian/pool/main/x/xcb-util-cursor/ |

License texts in the AppImage:
- `GPL-3.0.txt`: this app.
- `LGPL-3.0-only.txt`: Qt and PySide6.
- `LGPL-2.1-or-later.txt`: FFmpeg.
- `Apache-2.0.txt`: cryptography and OpenSSL.
- `python-LICENSE.txt`: CPython.
- `cryptography/`, `cffi/`, `pycparser/`: the license files shipped with those packages.
- `libxcb-cursor-copyright`: Debian's copyright file for that package.
