#!/bin/bash
# Builds a shareable "VD Plus.dmg" in the project folder:
# drag-to-Applications window + READ ME FIRST.txt.
#
#   bash execution/fetch_engine.sh   # once, to bundle a standalone engine (recommended for sharing)
#   bash execution/make_dmg.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/execution/build_app.sh" --no-install

APP="$ROOT/.tmp/build/VD Plus.app"
ENGINE="$(cat "$APP/Contents/Resources/engine-type")"
STAGE="$ROOT/.tmp/dmg"
OUT="$ROOT/VD Plus.dmg"

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

if [[ "$ENGINE" == "standalone" ]]; then
  NEEDS="Nothing else to install: Python, yt-dlp and ffmpeg are built in."
else
  NEEDS="This build uses Homebrew tools. Install them first (Terminal):
    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
    brew install python yt-dlp ffmpeg"
fi

cat > "$STAGE/READ ME FIRST.txt" <<EOF
VD PLUS (Video Downloader Plus)
Download videos and audio from YouTube, Instagram, TikTok, X, Facebook, Vimeo
and ~1800 other sites, in the highest quality available.

REQUIREMENTS
  • Mac with Apple Silicon (M1 or newer), macOS 13 or later.
  • $NEEDS
  • Optional, for transcripts of videos without captions:  brew install whisper-cpp
    and put a model at ~/.cache/whisper-cpp/ggml-large-v3-turbo.bin

INSTALL
  1. Drag "VD Plus" onto the Applications folder.
  2. Open it from Applications. macOS will say it can't verify the developer
     (this app isn't notarized by Apple).
  3. Open System Settings > Privacy & Security, scroll down, click "Open Anyway"
     next to VD Plus, then confirm. You only do this once.

TIPS
  • "Update" (bottom left) fixes most "it stopped working on site X" problems.
    It turns orange and says "Update available" when a newer engine is out.
  • For videos that play in QuickTime and on iPhone, set For > Mac & iPhone.
  • Downloads go to ~/Downloads/VD Plus (Save to menu in the toolbar).
  • Globe button (top right): sign in to YouTube/Instagram/etc. inside the app
    for private, age-restricted and Instagram videos.

USE WITH CLAUDE / CODEX (optional)
  Claude Code (Terminal):
    claude mcp add --scope user vd-plus -- \\
      "/Applications/VD Plus.app/Contents/Resources/python/bin/python3" \\
      "/Applications/VD Plus.app/Contents/Resources/execution/ripper_mcp.py"
  (If your build uses Homebrew, replace the first path with /opt/homebrew/bin/python3.)

PRIVACY
  Everything runs on your Mac. The app only listens on 127.0.0.1 and every
  request needs a per-launch secret only your user account can read.
  Please respect copyright and each site's terms.
EOF

hdiutil create -volname "VD Plus" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$OUT" >/dev/null
echo "DMG ($ENGINE engine): $OUT ($(du -h "$OUT" | awk '{print $1}'))"
