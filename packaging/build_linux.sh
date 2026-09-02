#!/usr/bin/env bash
# Build the Linux distribution of lossless-toolbox (todo 17).
#
# Steps:
#   1. Fetch the SHA-256-pinned BtbN FFmpeg-Builds static build (ffmpeg +
#      ffprobe) for the host architecture, verify its digest against
#      packaging/checksums.lock, and extract the two binaries into
#      resources/bin/.
#   2. Run PyInstaller against packaging/lossless-toolbox.spec (onedir,
#      windowed) -> dist/lossless-toolbox/.
#   3. Pack dist/lossless-toolbox/ into a tar.gz.
#   4. Wrap the onedir into an AppImage with the pinned linuxdeploy AppImage.
#
# The download is pinned by SHA-256. Direct GitHub release-asset transfers are
# sometimes blocked on locked-down networks, so a proxy mirror is tried as a
# transport fallback; the digest check still anchors every artifact to the
# canonical URL recorded in checksums.lock.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PACKAGING="$ROOT/packaging"
RESOURCES="$ROOT/resources"
BIN_DIR="$RESOURCES/bin"
LOCK="$PACKAGING/checksums.lock"
SPEC="$PACKAGING/lossless-toolbox.spec"
DESKTOP="$PACKAGING/lossless-toolbox.desktop"
ICON="$RESOURCES/icon.png"
DIST="$ROOT/dist"
BUILD="$ROOT/build"
APP_NAME="lossless-toolbox"
PYINSTALLER="${PYINSTALLER:-$ROOT/.venv/bin/pyinstaller}"

# Transport fallbacks (digest is still verified against the canonical URL).
PROXIES=("https://gh-proxy.com" "https://ghfast.top")

log() { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build] error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- architecture mapping ---------------------------------------------
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) FFMPEG_ARCH=linux64; LD_ARCH=x86_64 ;;
  aarch64|arm64) FFMPEG_ARCH=linuxarm64; LD_ARCH=aarch64 ;;
  *) die "unsupported host architecture: $ARCH" ;;
esac

FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${FFMPEG_ARCH}-gpl.tar.xz"
LINUXDEPLOY_URL="https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-${LD_ARCH}.AppImage"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${LD_ARCH}"

# --- checksum helpers --------------------------------------------------
sha256_of() { sha256sum "$1" | awk '{print $1}'; }

# Echo the pinned digest for a canonical URL (empty if unpinned).
locked_sha() {
  awk -v url="$1" '$1 == url { print $2 }' "$LOCK"
}

# Fetch $1 into $2, trying direct then proxies, verifying against the lock.
fetch_pinned() {
  local url="$1" out="$2" expected got candidate ok=0
  expected="$(locked_sha "$url")"
  [[ -n "$expected" ]] || die "no pinned SHA-256 for $url in $LOCK"

  if [[ -f "$out" ]] && [[ "$(sha256_of "$out")" == "$expected" ]]; then
    log "already present and verified: $(basename "$out")"
    return
  fi

  local candidates=("$url")
  local p
  for p in "${PROXIES[@]}"; do candidates+=("$p/$url"); done

  for candidate in "${candidates[@]}"; do
    log "fetching $candidate"
    if curl -fsSL --retry 3 --retry-delay 3 -C - -o "$out" "$candidate"; then
      got="$(sha256_of "$out")"
      if [[ "$got" == "$expected" ]]; then
        log "SHA-256 verified: $got"
        ok=1
        break
      fi
      printf '[build] digest mismatch for %s: got %s want %s (trying next)\n' \
        "$candidate" "$got" "$expected" >&2
    fi
  done
  [[ "$ok" == "1" ]] || die "failed to fetch and verify $url"
}

# --- 1. ffmpeg/ffprobe sidecars ---------------------------------------
mkdir -p "$BIN_DIR" "$BUILD"
FFMPEG_TARBALL="$BUILD/ffmpeg-${FFMPEG_ARCH}.tar.xz"
fetch_pinned "$FFMPEG_URL" "$FFMPEG_TARBALL"

EXTRACT_DIR="$BUILD/ffmpeg-${FFMPEG_ARCH}"
rm -rf "$EXTRACT_DIR"
mkdir -p "$EXTRACT_DIR"
tar -xf "$FFMPEG_TARBALL" -C "$EXTRACT_DIR"

FFMPEG_SRC="$(find "$EXTRACT_DIR" -type f -name ffmpeg -path '*/bin/*' | head -1)"
FFPROBE_SRC="$(find "$EXTRACT_DIR" -type f -name ffprobe -path '*/bin/*' | head -1)"
[[ -n "$FFMPEG_SRC" && -n "$FFPROBE_SRC" ]] || die "ffmpeg/ffprobe not found in $FFMPEG_TARBALL"
cp "$FFMPEG_SRC" "$BIN_DIR/ffmpeg"
cp "$FFPROBE_SRC" "$BIN_DIR/ffprobe"
chmod +x "$BIN_DIR/ffmpeg" "$BIN_DIR/ffprobe"
log "sidecars staged in $BIN_DIR"

# --- 2. PyInstaller ----------------------------------------------------
log "running PyInstaller"
"$PYINSTALLER" "$SPEC" --noconfirm --distpath "$DIST" --workpath "$BUILD/pyinstaller"

ONEDIR="$DIST/$APP_NAME"
[[ -x "$ONEDIR/$APP_NAME" ]] || die "PyInstaller output $ONEDIR/$APP_NAME not found"

# --- 3. tar.gz ----------------------------------------------------------
TARBALL="$DIST/$APP_NAME-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m).tar.gz"
log "packing $TARBALL"
tar -C "$DIST" -czf "$TARBALL" "$APP_NAME"

# --- 4. AppImage --------------------------------------------------------
LINUXDEPLOY="$BUILD/linuxdeploy-${LD_ARCH}.AppImage"
fetch_pinned "$LINUXDEPLOY_URL" "$LINUXDEPLOY"
chmod +x "$LINUXDEPLOY"

# appimagetool (bundled inside linuxdeploy) downloads the type-2 runtime from
# GitHub when packaging; fetch it here (pinned) and hand it over explicitly so
# the build works on networks where release-asset downloads are blocked.
RUNTIME="$BUILD/runtime-${LD_ARCH}"
fetch_pinned "$RUNTIME_URL" "$RUNTIME"
chmod +x "$RUNTIME"

APPDIR="$BUILD/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -r "$ONEDIR/." "$APPDIR/usr/bin/"
cp "$DESKTOP" "$APPDIR/usr/share/applications/$APP_NAME.desktop"
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
ln -sf usr/bin/$APP_NAME "$APPDIR/AppRun"

# Package in a dedicated dir so the produced .AppImage is the only match.
OUTDIR="$BUILD/appimage_out"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

log "building AppImage with linuxdeploy"
(cd "$OUTDIR" && LDAI_RUNTIME_FILE="$RUNTIME" "$LINUXDEPLOY" \
  --appdir "$APPDIR" \
  --executable "$APPDIR/usr/bin/$APP_NAME" \
  --desktop-file "$APPDIR/usr/share/applications/$APP_NAME.desktop" \
  --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png" \
  --output appimage)

APPIMAGE="$(ls -1 "$OUTDIR"/*.AppImage 2>/dev/null | head -1)"
[[ -n "$APPIMAGE" ]] || die "linuxdeploy did not produce an AppImage"
mv "$APPIMAGE" "$DIST/$APP_NAME-$(uname -m).AppImage"

log "done:"
log "  onedir:   $ONEDIR"
log "  tarball:  $TARBALL"
log "  AppImage: $DIST/$APP_NAME-$(uname -m).AppImage"
