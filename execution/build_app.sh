#!/bin/bash
# Builds the native "VD Plus.app" and installs it to /Applications.
#
#   bash execution/build_app.sh            # build + install to /Applications
#   bash execution/build_app.sh --no-install
#
# The app is self-contained: the Python engine + UI are copied into
# Contents/Resources/execution, so it runs without the project drive mounted.
# Needs: Xcode command line tools (swiftc). Run execution/fetch_engine.sh once first so the
# app carries its own Python, yt-dlp and ffmpeg; without it the app uses Homebrew's.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXE="$ROOT/execution"
BUILD="$ROOT/.tmp/build"
APP="$BUILD/VD Plus.app"
DEST="/Applications/VD Plus.app"

rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/execution/ui"

echo "Compiling…"
swiftc -O -swift-version 5 -target arm64-apple-macos13 \
  -o "$APP/Contents/MacOS/VD Plus" "$EXE/mac/VideoRipperApp.swift" \
  -framework Cocoa -framework WebKit

echo "Trash helper…"
mkdir -p "$APP/Contents/Resources/bin"
swiftc -O -swift-version 5 -target arm64-apple-macos13 -o "$APP/Contents/Resources/bin/vdtrash" "$EXE/mac/vdtrash.swift"

echo "Icon…"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
swiftc -swift-version 5 -o "$BUILD/make_icon" "$EXE/mac/make_icon.swift" -framework Cocoa
"$BUILD/make_icon" "$BUILD/icon_1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# Identifies this build. The engine reports it, so a newer app can spot and replace an engine
# left running from an older copy.
GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
BUILD_ID="$(date +%Y%m%d-%H%M%S)${GIT_SHA:+-$GIT_SHA}"
echo "$BUILD_ID" > "$APP/Contents/Resources/build-id"

echo "Bundling app code…"
cp "$EXE"/ripper_core.py "$EXE"/ripper_server.py "$EXE"/ripper_mcp.py "$APP/Contents/Resources/execution/"
cp "$EXE"/ui/index.html "$APP/Contents/Resources/execution/ui/"

# Standalone engine (from execution/fetch_engine.sh). Without it the app uses
# Homebrew python3 / yt-dlp / ffmpeg, which is fine on this Mac but not for sharing.
if [[ -d "$ROOT/.tmp/engine/python" ]]; then
  echo "Bundling standalone engine (Python with yt-dlp, ffmpeg)…"
  cp -R "$ROOT/.tmp/engine/python" "$APP/Contents/Resources/python"
  cp -R "$ROOT/.tmp/engine/bin/." "$APP/Contents/Resources/bin/"
  ENGINE="standalone"
else
  echo "No .tmp/engine: app will need Homebrew python3, yt-dlp, ffmpeg (run fetch_engine.sh to bundle)."
  ENGINE="homebrew"
fi
echo "$ENGINE" > "$APP/Contents/Resources/engine-type"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>VD Plus</string>
  <key>CFBundleDisplayName</key><string>VD Plus</string>
  <key>CFBundleIdentifier</key><string>local.vdplus</string>
  <key>CFBundleExecutable</key><string>VD Plus</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>3.0</string>
  <key>CFBundleVersion</key><string>3.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSHumanReadableCopyright</key><string>Personal build. Engine: yt-dlp + ffmpeg.</string>
</dict></plist>
EOF

# Ad-hoc sign every nested Mach-O first (Apple Silicon refuses unsigned code), then the bundle.
find "$APP/Contents/Resources" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0 |
  while IFS= read -r -d '' f; do
    file -b "$f" | grep -q Mach-O && codesign --force -s - "$f" >/dev/null 2>&1 || true
  done
codesign --force -s - "$APP"
codesign --verify --deep --strict "$APP" && echo "Signature OK (ad-hoc)"
echo "Built ($ENGINE engine, build $BUILD_ID): $APP"

if [[ "${1:-}" != "--no-install" ]]; then
  osascript -e 'tell application "VD Plus" to quit' >/dev/null 2>&1 || true
  osascript -e 'tell application "Video Ripper" to quit' >/dev/null 2>&1 || true
  sleep 1
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  touch "$DEST"  # refresh Finder/Dock icon cache
  echo "Installed: $DEST"
fi
