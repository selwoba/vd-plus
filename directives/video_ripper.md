# VD Plus (Video Downloader Plus) - SOP

Personal replacement for 4K Video Downloader+ (feature parity plus extras), used through a native Mac app and by AI assistants (MCP). Formerly "Video Ripper"; this file keeps its old name.

## Pieces
| File | Role |
|---|---|
| `execution/ripper_core.py` | Engine: settings, SQLite history, queue (concurrency, pause/resume/cancel/retry, clean shutdown), analyze, search, subscriptions, clipboard watch, thumbnails, MP4/HEVC conversion, engine self-update, metadata, transcript, frames |
| `execution/ripper_server.py` | Local HTTP server on `127.0.0.1:8765`: serves `ui/index.html` + REST API. Token auth; rejects non-local Host/Origin and non-JSON POSTs |
| `execution/ripper_mcp.py` | stdio MCP server (`vd-plus`) for Claude Code, Claude Desktop, Codex. Auto-starts the HTTP server so AI downloads appear in the app queue |
| `execution/ui/index.html` | UI: toolbar, filter tabs, thumbnail rows with multi-select, details panel, quality picker, settings |
| `execution/mac/VideoRipperApp.swift` | Native macOS app (WebKit window, menus, sign-in browser, "Find video on page" sniffer). Starts the bundled engine, replaces a stale one, and stops it on Quit |
| `execution/mac/make_icon.swift`, `execution/mac/vdtrash.swift` | App icon; move-to-Trash helper |
| `execution/fetch_engine.sh` | Downloads pinned Python and ffmpeg (publisher SHA-256) and installs yt-dlp + libraries into that Python from `execution/engine-requirements.txt` (exact versions + hashes). `--update-lock` re-pins after bumping `YTDLP_VER` |
| `execution/build_app.sh` | Compiles the app, bundles engine + UI, writes `build-id`, ad-hoc signs, installs to `/Applications/VD Plus.app`. Output in `.tmp/build/` |
| `execution/make_dmg.sh` | Builds `VD Plus.dmg` in the project root (app + Applications link + READ ME FIRST.txt) |
| `tests/test_core.py` | Offline engine tests (throwaway HOME, no network). Run before every build: `python3 tests/test_core.py` |

Data: `~/Library/Application Support/VD Plus/` (`settings.json`, `ripper.db`, `token`, `cookies.txt`, `thumbs/`, `archives/` for subscriptions, `pylib/` for a self-updated engine). Log: `~/Library/Logs/VDPlus.log`. Downloads: `~/Downloads/VD Plus`.

## Engine
- In the app: the bundled Python runs `python3 -m yt_dlp` (starts in ~0.15 s; the old single-file `yt-dlp_macos` binary took ~4 s per call). ffmpeg/ffprobe are bundled in `Contents/Resources/bin`.
- Update button (bundled engine): `pip install --target` into `Application Support/VD Plus/pylib`, never into the signed app. At run time the newer of the built-in copy and `pylib` wins, so reinstalling the app cannot downgrade the engine. Refused while downloads run.
- The server asks PyPI for the latest yt-dlp version once a week; the Update button turns orange when one is out.
- Dev checkout (running `python3 execution/ripper_server.py` from the project): no bundled Python, so Homebrew `yt-dlp` and `ffmpeg` are used.
- Optional: `whisper-cli` (whisper.cpp) + a model at `~/.cache/whisper-cpp/ggml-large-v3-turbo.bin` for transcripts of videos without captions.

## Run / build
- Open `/Applications/VD Plus.app`.
- **After editing any engine/UI/Swift file: `python3 tests/test_core.py`, then `bash execution/build_app.sh`.** The app and the MCP configs use the bundled copy in `/Applications`, not the project folder. `bash execution/make_dmg.sh` for a shareable DMG.
- First time on a new machine: `bash execution/fetch_engine.sh`.
- MCP `vd-plus` points at `/Applications/VD Plus.app/Contents/Resources/execution/ripper_mcp.py`, run by the app's bundled Python. Registered in Claude Code (user scope), Claude Desktop, Codex.
- Source: private GitHub repo `selwoba/vd-plus`. `.tmp/` and `*.dmg` are not committed (the DMG is over GitHub's 100 MB file limit).

## MCP tools
`download_video`, `download_status`, `video_info`, `get_transcript`, `extract_frames`, `search_videos`, `list_downloads`.
Research flow: `video_info` -> `get_transcript` -> `download_video(purpose="research")` -> `extract_frames`.

## "For" presets and file formats
- **Original**: best stream as-is. H.264 / AV1 / HEVC are rewrapped into MP4 without re-encoding. VP9 (Instagram, YouTube above 1080p) stays MKV, because QuickTime cannot decode VP9 in any container; the row says so.
- **Mac & iPhone**: full resolution, H.264 preferred when the site has it at that resolution; VP9 is re-encoded to HEVC on the hardware encoder (10-bit and colour/HDR tags kept, status "Converting", cancellable). A 10-minute 1440p60 video takes about 5 minutes on an M4.
- **Video editing**: plain H.264 + AAC MP4 (YouTube tops out at 1080p). **Android / Windows**: MP4, full resolution.

## Learnings / edge cases
- **Keep yt-dlp current.** Version 2026.06.09 (>90 days old) failed YouTube 4K section downloads with `ffmpeg exited with code 8`; upgrading to 2026.10.21 fixed it. UI "Update engine" button runs `brew upgrade yt-dlp`. Its "installed with pip" warning is misleading; it's Homebrew.
- `brew upgrade yt-dlp` also bumps Homebrew Python (3.15 as of 2026-10-26). Fine, code is stdlib-only.
- Instagram, private, age-restricted content: set Settings > Login cookies to the browser the user is signed into. Safari cookies need Full Disk Access for the Python process.
- Subscriptions with "only new" pre-fill a download archive with every existing video id (`ie_key id` lowercased), so checks download only future uploads.
- Section/clip downloads use `--download-sections` + `--force-keyframes-at-cuts` (re-encodes the cut, slower).
- Transcript: site captions first; with none (Instagram/TikTok/local files) falls back to local Whisper (~9s for a short clip on this Mac).
- macOS has no `timeout` command; test MCP with a Python driver.
- REA MCP can open binaries, but string/decompile tools need a backend (Ghidra/Hopper/IDA/Kuna). None configured as of 2026-10-26. Used `strings` instead for the 4KVD+ feature inventory.
- WKWebView needs an explicit Edit menu or Cmd+C/V don't work; downloads (Export, Save .txt) and `<input type=file>` need WKDownloadDelegate / runOpenPanel handlers. Both are in VideoRipperApp.swift.
- Compile with `-swift-version 5` to avoid Swift 6 strict-concurrency errors.

## Security model (reviewed 2026-10-26)
- Server binds 127.0.0.1 only; rejects foreign Host (DNS rebinding), foreign Origin, non-JSON POSTs (CSRF); no CORS headers; CSP + nosniff + frame-deny on the UI.
- **Per-launch token** in `~/Library/Application Support/VD Plus/token` (0600, dir 0700). Every API call needs `X-Ripper-Token` or the HttpOnly SameSite=Strict `rt` cookie. The app/browser gets it via `/?token=` and the server swaps it for the cookie.
- `/api/ping?nonce=` returns HMAC(token, nonce). The app and MCP check it so a program squatting on port 8765 is never trusted.
- All download/settings input goes through `core.validate` / `clean_overrides`: regex-pinned values, folders limited to home or /Volumes, internal keys (`archive`) can't be set from outside. yt-dlp gets argv only (no shell), URLs after `--`.
- MCP prefixes web-sourced output (metadata, transcripts, search) with an "untrusted data" notice against prompt injection.
- WKWebView only navigates to the local app. Outside links open in the browser. Dev tools are off.
- No Apple Developer ID on this Mac, so builds are ad-hoc signed and recipients must use System Settings > Privacy & Security > Open Anyway once.
- Downloading and running fetch_engine.sh was blocked by Claude Code's auto-mode safety check (external binaries). The user has to run it or allow it.
- 2026-10-26: user approved fetch_engine.sh and it ran. Standalone DMG is 120 MB (engine 228 MB unpacked). Tested with `VR_ENGINE=bundled` (ignores Homebrew) using a clean env: bundled Python + yt-dlp + ffmpeg downloaded and merged fine.
- Bash gotcha: `"$VAR…"` (Unicode ellipsis right after a variable) fails as "unbound variable" under `set -u`. Use `${VAR}`.
- Sharing: Gmail blocks .dmg and apps inside zips, even encrypted ones. Share a Google Drive, iCloud or WeTransfer link, or AirDrop.

## v3.0: VD Plus rebrand + redesign (2026-10-26)
- Renamed to **VD Plus** (Video Downloader Plus). App `/Applications/VD Plus.app` (bundle id `local.vdplus`), DMG `VD Plus.dmg`. Data moved automatically from `Application Support/Video Ripper` to `Application Support/VD Plus`. Log `~/Library/Logs/VDPlus.log`. Default save folder `~/Downloads/VD Plus`. MCP server is now named `vd-plus` (Claude Code, Claude Desktop, Codex) and uses the app's bundled Python.
- UI follows 4KVD+'s layout: toolbar (Paste Link, Smart Mode, Download / Quality / For / Save to, globe, settings, details panel), filter tabs (All, Video, Audio, Playlists, Channels, Subscriptions, Failed), search and sort, thumbnail rows, right-click menu, details panel (info, transcript, log), status bar. Dark orange `#C2410C` on white; dark mode `#EA580C` on near-black. References: Proton Drive queue tabs, ElevenLabs/Drive file lists, Cron/Pipe single-orange accent.
- "For" device presets (`target`): original | apple | android | windows | editing (see `TARGETS` in core).
- Globe = native sign-in browser (SignInWindow in Swift). Cookies go to `Application Support/VD Plus/cookies.txt` (0600) and the `cookies=app` setting passes `--cookies`. Browser mode falls back to cookies-from-browser.
- New endpoints: `/api/clipboard`, `/api/delete` (optional move-to-Trash via Finder, argv only), `/api/choose_folder`, `/api/cookies`, `/api/cookies/clear`. `/api/jobs` now merges the live queue with saved history.
- Jobs store `meta` (thumbnail, duration, height, site, bytes) from yt-dlp `--print before_dl:INFO %(.{...})j`. Items downloaded before v3 have no thumbnails.
- Icon: `execution/mac/make_icon.swift`, a white tile with an orange rounded button, a down-pointing triangle and a VD+ wordmark.
- Editing files inside the installed .app breaks its ad-hoc signature. Always rebuild with build_app.sh.
- **Google blocks sign-in inside embedded WKWebViews.** It hangs on "This may take a moment", even with a Safari user agent. The sign-in window now cancels navigation to accounts.google.com and asks which installed browser to borrow the Google login from (`google_cookies` setting). Downloads then pass `--cookies <private tmp copy of cookies.txt> --cookies-from-browser <browser>`; yt-dlp merges both jars. Copies live in `Application Support/VD Plus/tmp` (0600), are swept after 6 h, and are deleted on Sign out. Chromium browsers trigger a keychain "Always Allow" prompt; Safari needs Full Disk Access.
- **Fallback chain for "Unsupported URL"** (2026-10-26): (1) yt-dlp normal; (2) automatic single retry with `--impersonate chrome --extractor-args generic:impersonate` when the error matches RETRY_RE (unsupported, no formats, 403/429, Cloudflare); (3) friendly error plus a **Find video on page** sniffer (SnifferWindow in Swift). It loads the page in WKWebView with the app's logins, injects a script into every frame that hooks fetch/XHR and scans `<video>` tags and performance entries for m3u8/mpd/mp4/webm (segments ignored), lists candidates best-first, then sends the chosen stream to `/api/download` with `referer`, `name_hint` and `cookies=app`. The sniffer opens automatically when a link added this session fails with "no video found". Tested with a JS-injected video page: caught and downloaded.
- DRM services (Netflix, Disney+, Prime Video, Hulu, Max, Peacock, Paramount+, Apple TV+, Spotify) get a clear "encrypted, can't be downloaded" message. We don't attempt to get around DRM.
- Example: one site's `/dload/...mp4` links return an HTML page, not the file, and yt-dlp's extractor for that site was broken on 2026-10-21 ("Unable to extract hash"). Use the watch-page URL so the sniffer catches the stream, or try Update later for an extractor fix.
- **Progress without a known size:** some hosts send no Content-Length. yt-dlp's `_percent_str` / `_speed_str` then print "Unknown" or "N/A" (with spaces), so the row sat at 0% and looked stuck while gigabytes were downloading. The progress template now prints raw `downloaded_bytes`, `total_bytes`, `total_bytes_estimate`, `speed`, `eta` and `fragment_index`/`fragment_count`. `core._progress()` formats them and sets `indeterminate` when the total is unknown, and the UI shows "X GB downloaded · speed" with an animated bar.
- With the Chrome-impersonation retry, those `/dload/` links DO download through the Generic extractor (verified 2026-10-26: a 1080p file around 2 GB or more). Their filenames are long because the generic id comes from the server's Content-Disposition header.
- **Instagram (2026-11-03):** "Instagram sent an empty media response / API is not granting access" means no login at the time of the download. The user had signed in 46 s after adding the link and VD Plus never retried. Now: `LOGIN_RE` marks such jobs `needs_login`, the row shows a **Sign in** button, and `vdSignedIn` auto-retries login-failed jobs from the last 6 h. With saved cookies, yt-dlp's Instagram extractor works (1078p VP9 to MKV). Don't use "Find video on page" for Instagram: it catches fbcdn DASH pieces with `bytestart/byteend`, which give "Invalid data" in ffmpeg. The sniffer now skips those URLs and isn't offered for login errors.
- **Automatic login popup (2026-11-03):** when a download the user started fails with a login error (`needs_login`), the UI calls `jobAction("signin")`. It opens the native sign-in window at that site's login page (`LOGIN_PAGES` in index.html: Instagram, Facebook, TikTok, X, Vimeo, YouTube to Google) with an orange explanation. It fires once per site until a sign-in completes. After Done or close, `vdSignedIn` retries each failed link once (`loginRetried` prevents popup loops).
- macOS won't let an app take focus by itself. The sign-in window uses `orderFrontRegardless` + `.moveToActiveSpace` + `requestUserAttention(.criticalRequest)`, otherwise it opens behind other apps.
- Vimeo's login page (and probably other "Continue with Google" pages) redirects the main frame to accounts.google.com on its own. The Google browser-choice prompt now appears only for user-initiated navigations (link or form, our YouTube button, or a YouTube login popup). Automatic redirects are logged and ignored. Return no longer picks a browser in that prompt. The navigation log lines are `[app …] signin:` in VDPlus.log.
- **Instagram still failing (2026-11-03), two root causes:**
  1. yt-dlp writes the cookie jar back to `--cookies` on exit. Passing the master `cookies.txt` let an Instagram response that cleared or expired the session wipe `sessionid`. Now **always** pass `_cookie_copy()`; the master file is written only by the app's export. The app also re-exports the built-in browser's logins at launch, but only if the store holds an auth cookie, so an empty store never wipes the file.
  2. `google_cookies=chrome`, set by accident earlier, made **every** download fail with `could not find chrome cookies database` (yt-dlp can't read Chrome cookies when run from the app), and `LOGIN_RE` mislabeled that as "needs sign in" because it matched the word "cookies". Fix: self-heal retry with `no_browser_cookies` when `BROWSER_COOKIE_ERR` matches, and "cookies" removed from the login regexes.
- The sign-in window now auto-detects login via site auth cookies (`authCookies` in SignInWindow: Instagram/TikTok sessionid, Facebook c_user, X auth_token, Vimeo vimeo). On each page load it saves and retries, and when opened for a failed download it closes itself once that site is logged in, including when the site was already logged in and skipped the login page.
- **Instagram carousels (2026-11-05):** a carousel is a playlist even with `--no-playlist`. Photo slides make yt-dlp report "No video formats found" and exit non-zero, while the video slides download fine. The job used to be marked error, with the misleading "No video found" sniff message. Now any job that saved files is **done**, with `job["note"]` ("Saved N videos. Photos in this post were skipped.") shown in the row, and the raw error kept only in the log. `?img_index=` in the URL is ignored; all videos in the post download.
- The user's toolbar Download type was set to Audio · MP3 (probably by accident), so the carousel videos came out as .mp3. If downloads unexpectedly give audio, check `kind` in settings.
- **MKV vs MP4 (2026-11-24):** yt-dlp's `--merge-output-format mp4/mkv` fell back to MKV even for H.264+AAC (Vimeo), and always for VP9/AV1/Opus. `core.to_mp4()` (first named `remux_to_mp4`) runs after every video download when container is "auto": ffprobe the streams; if all of them fit MP4 (video h264/hevc/av1/vp9, audio aac/mp3/opus/ac3/eac3/alac/flac, subs to mov_text), rewrap with `-c copy -movflags +faststart` (and `hvc1` tag for HEVC). Lossless, about 0.2 s. Otherwise keep the MKV. Verified with AVFoundation on the user's M4: H.264 and AV1+Opus MP4 play in QuickTime; VP9 doesn't play in any container (QuickTime limitation; use For → Mac & iPhone or IINA/VLC).
- **Trash bug (2026-11-24):** `core.trash()` scripted Finder via osascript. Without Automation permission it fails silently, and it returned the number of paths *attempted*, so "moved to Trash" was reported while nothing moved. Now it uses the bundled `bin/vdtrash` (Swift `FileManager.trashItem`, no permission needed) and returns how many files actually disappeared. `build_app.sh` compiles vdtrash, and the engine bin copy is now `bin/.`, so it merges into the existing folder instead of making `bin/bin`.
- One-off on 2026-11-24: rewrapped all 14 MKVs in ~/Downloads/VD Plus as MP4 losslessly (each check: duration within 1 s, size ≥ 90%), moved the originals to the Trash, and repointed the jobs DB. The DB stores paths JSON-escaped (`\uXXXX`), so match by parsing the JSON, not with `LIKE '%name%'`.
- **VP9 in MP4 is worse than MKV on a Mac (2026-11-24):** .mp4 opens in QuickTime by default, and QuickTime can't decode VP9, so the user's rewrapped Instagram files "stopped playing" (as .mkv they had opened in IINA/VLC). `to_mp4` re-encodes VP9 (since v3.1 only for the Mac & iPhone target) to HEVC with `hevc_videotoolbox` (2x source bitrate, min 4 Mbps, `hvc1` tag, audio copied, duration-checked). About 1 s per Instagram clip on the M4. If that encoder is missing, it keeps the MKV. Fixed the user's 9 existing files the same way; the VP9 originals are in the Trash.
- Shell gotcha: ffmpeg inside a `while read` loop eats the loop's stdin and silently skips files. Use `-nostdin` (also passed in core subprocess calls).

## v3.1: reliability pass (2026-11-24)
Found by a code review plus isolated tests; each has a test in `tests/test_core.py` unless noted.
- **Quit left downloads running.** yt-dlp runs in its own session, so killing the server orphaned it (paused ones stayed frozen). Now SIGTERM, Ctrl+C and `POST /api/quit` call `Queue.shutdown()`: SIGCONT + SIGTERM to every job's process group, jobs saved as `interrupted` (Retry resumes from the `.part` file). The app's Quit always stops the engine, including one the AI bridge started.
- **Startup depended on DNS.** `HTTPServer.server_bind()` calls `socket.getfqdn()`; measured 21 s on this Mac once, longer than the app's 15 s wait. `Server.server_bind` skips it (start: 0.1 s). Not unit-tested.
- **HDR lost in conversion** (`-pix_fmt yuv420p`). `to_mp4` now uses `main10` / `p010le` for 10-bit sources and copies colour tags. HDR10 mastering-display metadata is not carried over.
- **Deleting a waiting job deadlocked the whole app**: `delete_jobs` held `_db_lock` and called `cancel` -> `_save_job` -> `_db_lock` again. `_db_lock` is now an RLock, and deleted jobs carry `_deleted` so a still-running thread cannot write the row back.
- **AI bridge marked running downloads "interrupted"**: `init_db()` only does that with `mark_interrupted=True`, which only the server passes.
- **Stale engine after an update**: `build-id` in the bundle; `/api/ping` returns it; the app and the MCP bridge ask an engine from another build to quit (`/api/quit`, refused while downloads run) and start the current one.
- **Browser logins**: macOS denies other apps access to Chrome's data folder ("Operation not permitted"), so `--cookies-from-browser chrome` fails unless VD Plus has Full Disk Access. With "YouTube login from Chrome" set, Search and the quality picker failed outright and every download wasted an attempt. Now browser logins are used only for YouTube/Google links, any yt-dlp call retries without them, and the failure is remembered for the session (`_browser_blocked`, shown in the status bar).
- Login copies are deleted right after each yt-dlp run and swept at startup. DB connections are closed after each use. The scheduler loop survives exceptions.
- Polish: thumbnails are cached in `thumbs/` and served from `/api/thumb` (site links expire); direct-link downloads lose the bracketed junk in their names (`tidy_name`); subscription checks with nothing new no longer add rows (the subscription shows "No new videos"); multi-select with Cmd-click, Shift-click, Cmd+A and a bulk menu.
- Not done: Apple notarization and an Intel build (need a paid Apple Developer account / a second engine), and an in-app "new version of VD Plus" notice (needs a public download location; the repo is private).
