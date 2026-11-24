# VD Plus

A personal video downloader for macOS (Apple Silicon). Paste a link, get the best-quality file. Works with YouTube, Instagram, TikTok, X, Facebook, Vimeo and the other sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports.

It is a native Mac window around a small local engine:

- **Engine** (`execution/ripper_core.py`, `ripper_server.py`): Python standard library only. Runs yt-dlp and ffmpeg, keeps the queue and history, and serves the UI on `127.0.0.1`.
- **App** (`execution/mac/VideoRipperApp.swift`): WebKit window, menus, a sign-in browser for sites that need a login, and "Find video on page" for sites yt-dlp does not recognise.
- **AI bridge** (`execution/ripper_mcp.py`): an MCP server so Claude Code, Claude Desktop and Codex can download videos, read transcripts and look at frames.

## Build

Needs Xcode command line tools (`xcode-select --install`). Apple Silicon only.

```bash
bash execution/fetch_engine.sh   # once: bundled Python, yt-dlp, ffmpeg (all checksum-verified)
python3 tests/test_core.py       # offline tests, no network, uses a throwaway HOME
bash execution/build_app.sh      # compiles and installs /Applications/VD Plus.app
bash execution/make_dmg.sh       # optional: shareable "VD Plus.dmg"
```

The app is ad-hoc signed, not notarized. On another Mac, open it once via System Settings > Privacy & Security > Open Anyway.

## Updating yt-dlp in the bundle

Sites change often, so the engine needs to stay current. The app's **Update** button updates the installed copy. To change the version new builds ship with, set `YTDLP_VER` in `execution/fetch_engine.sh` and run:

```bash
bash execution/fetch_engine.sh --update-lock
```

That re-resolves the packages and rewrites `execution/engine-requirements.txt` with exact versions and hashes.

## Notes

- Everything runs locally. The server only listens on `127.0.0.1` and every request needs a per-launch token that only your macOS account can read.
- DRM-protected services (Netflix and the like) are not supported, by design.
- `directives/video_ripper.md` is the working log: how the pieces fit, what broke and why.
- Respect copyright and each site's terms of use.
