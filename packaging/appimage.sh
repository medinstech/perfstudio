#!/usr/bin/env bash
#
# Build the Linux AppImage from a PyInstaller bundle.
#
#     python -m PyInstaller perfstudio.spec --noconfirm
#     packaging/appimage.sh
#
# The Windows counterpart of this is `packaging/perfstudio.nsi`, and the two do the same
# job by opposite means.  NSIS writes an installer that unpacks the bundle into Program
# Files and registers it; an AppImage installs nothing.  It is the bundle itself, in a
# squashfs image, behind a small runtime that mounts that image and runs what is inside --
# one file, chmod +x, double-click.  There is no Linux equivalent of "an installer",
# because there are a dozen of them and no two distributions agree; a file that runs
# everywhere is the closest thing to the same promise.
#
# What has to be in an AppDir is fixed by the format: an `AppRun` to start, a `.desktop`
# file naming it, and an icon whose basename matches the desktop file's `Icon=`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

BUILD_DIR="${BUILD_DIR:-dist/perfstudio}"
OUT_DIR="${OUT_DIR:-releases}"
ARCH="${ARCH:-$(uname -m)}"

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/perfstudio/version.py)
[ -n "$version" ] || { echo "cannot read __version__ out of src/perfstudio/version.py" >&2; exit 1; }
[ -x "$BUILD_DIR/perfstudio" ] || { echo "no bundle at $BUILD_DIR - run PyInstaller first" >&2; exit 1; }

appdir="build/AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/share/applications" \
         "$appdir/usr/share/icons/hicolor/256x256/apps" "$OUT_DIR"

cp -a "$BUILD_DIR/." "$appdir/usr/bin/"

# The icon has to be a PNG -- the runtime and every desktop that reads the `.desktop`
# file want one, and the .ico the Windows build uses is not it.  The generated mark is
# already 256, so it is copied rather than converted: a conversion step is a thing that
# can silently produce a blank square.
cp src/perfstudio/ui/assets/perfstudio.png "$appdir/perfstudio.png"
cp "$appdir/perfstudio.png" "$appdir/usr/share/icons/hicolor/256x256/apps/perfstudio.png"

# `StartupWMClass` is what pairs the running window with this launcher, so the taskbar
# shows one icon with the right name instead of a second, generic entry beside it.  Qt
# reports the executable's name there.
#
# `MimeType` is the Linux half of the file association the Windows installer writes and
# the macOS bundle declares in its plist -- all three so that double-clicking a board
# opens it, on whichever machine somebody is at.
cat > "$appdir/perfstudio.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PerfStudio
GenericName=Perfboard layout designer
Comment=Design circuits on perfboard and get a soldering guide you can build from
Exec=perfstudio %f
Icon=perfstudio
Terminal=false
Categories=Development;Electronics;Engineering;
Keywords=perfboard;stripboard;veroboard;electronics;PCB;soldering;netlist;
MimeType=application/x-perfstudio-board;
StartupWMClass=perfstudio
DESKTOP
cp "$appdir/perfstudio.desktop" "$appdir/usr/share/applications/perfstudio.desktop"

# A script rather than a symlink to the binary.  A symlink would work -- the bootloader
# finds its own directory through /proc/self/exe, which resolves through one -- but this
# is also where the two things a frozen Qt application needs on a strange machine get
# set, and there is nowhere else to put them.
cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
# `dirname $0` and not $APPDIR: the runtime exports APPDIR, but a user who extracted the
# image with --appimage-extract and ran AppRun by hand gets nothing, and that is the
# fallback path for a machine with no libfuse2.
root="$(dirname "$(readlink -f "$0")")"
# Qt looks for its platform plugins relative to the binary and finds them, but only once
# it knows not to trust a QT_ inherited from the host -- a QT_PLUGIN_PATH pointing at the
# system Qt is the classic way for a bundled application to load half of one Qt and half
# of another and abort.
unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec "$root/usr/bin/perfstudio" "$@"
APPRUN
chmod +x "$appdir/AppRun"

# appimagetool is not packaged by any distribution, so it is fetched.  Pinned to a
# release rather than `continuous`: the tool that builds a release artefact is part of
# the release, and "whatever was on the server that morning" is not something a build can
# be reproduced from.
tool="build/appimagetool-x86_64.AppImage"
if [ ! -x "$tool" ]; then
    curl -fsSL -o "$tool" \
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
    chmod +x "$tool"
fi

# The runtime is fetched HERE rather than left to appimagetool, and that is a fix rather
# than a preference.  An AppImage is a squashfs image behind a small runtime, and
# appimagetool downloads that runtime itself at build time -- with a fetcher that does not
# follow a redirect.  GitHub answered one with a 302 during the v0.9.0 release and the job
# died on `Failed to download runtime: server returned status code 302`, twenty minutes
# after the same job had passed on a dry run.  curl -L follows it, and `--runtime-file` is
# the way round that appimagetool's own error message names.
#
# It also closes the hole in the paragraph above.  The tool is pinned because "whatever was
# on the server that morning" is not something a release can be reproduced from -- and the
# runtime, which is the part that actually ends up INSIDE the artefact, was being fetched
# from `continuous` by something nobody could see.  Now it is a visible input with a name.
runtime="build/appimage-runtime-${ARCH}"
if [ ! -s "$runtime" ]; then
    curl -fsSL -o "$runtime" \
        "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}"
fi

# The tool is itself an AppImage, so on a machine or a CI runner with no FUSE it cannot
# mount itself either.  This is the documented way round that, and it is why the build
# does not need root to install libfuse2.
export APPIMAGE_EXTRACT_AND_RUN=1
out="$OUT_DIR/perfstudio-${version}-${ARCH}.AppImage"
rm -f "$out"
ARCH="$ARCH" "$tool" --runtime-file "$runtime" "$appdir" "$out"

ls -l "$out"
