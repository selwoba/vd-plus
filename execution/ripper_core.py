"""
VD Plus (Video Downloader Plus) - shared engine.

Everything the UI server and the AI (MCP) server need:
  * settings + SQLite history (~/Library/Application Support/VD Plus)
  * download queue with concurrency, pause/resume/cancel/retry
  * analyze (formats, playlists), search, subscriptions, clipboard watch
  * research helpers: metadata, transcripts (captions or local Whisper), frames

Download engine is yt-dlp + ffmpeg (bundled in the app; Homebrew in a dev checkout). Stdlib only.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

DATA_DIR = Path.home() / "Library" / "Application Support" / "VD Plus"
_OLD_DATA = Path.home() / "Library" / "Application Support" / "Video Ripper"
if _OLD_DATA.exists() and not DATA_DIR.exists():
    _OLD_DATA.rename(DATA_DIR)  # carry history/subscriptions over from the Video Ripper name
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DATA_DIR, 0o700)  # history, token and settings are private to this user
DB_PATH = DATA_DIR / "ripper.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
TOKEN_PATH = DATA_DIR / "token"
COOKIE_FILE = DATA_DIR / "cookies.txt"   # written by the app's built-in sign-in browser (0600)
ARCHIVE_DIR = DATA_DIR / "archives"
ARCHIVE_DIR.mkdir(exist_ok=True)
PYLIB = DATA_DIR / "pylib"                            # self-updated engine (yt-dlp + its libraries)
THUMBS = DATA_DIR / "thumbs"                          # cached thumbnails, one per job
RESOURCES = Path(__file__).resolve().parent.parent    # app bundle: Contents/Resources
BUNDLE_BIN = RESOURCES / "bin"                        # ffmpeg, ffprobe, vdtrash
BUNDLE_PY = RESOURCES / "python" / "bin" / "python3"
ENGINE_SPEC = "yt-dlp[default,curl-cffi]"             # what "Update" installs from PyPI
ENGINE_CHECK = DATA_DIR / "engine_check.json"
try:
    BUILD_ID = (RESOURCES / "build-id").read_text().strip()
except OSError:
    BUILD_ID = "dev"
shutil.rmtree(DATA_DIR / "bin", ignore_errors=True)   # pre-3.1 copy of the single-file engine

os.environ["PATH"] = f"{BUNDLE_BIN}:/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")


def _pkg_version(root):
    """Version of the yt_dlp package under root, read without importing it."""
    try:
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", (Path(root) / "yt_dlp" / "version.py").read_text())
        return m.group(1) if m else ""
    except (OSError, TypeError):
        return ""


def _vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def resolve_engine():
    """How to run yt-dlp. In the app: the bundled Python runs the yt_dlp package, using whichever is
    newer of the copy inside the app and the self-updated copy in Application Support (so reinstalling
    the app can never downgrade it). In a dev checkout: Homebrew's yt-dlp."""
    if BUNDLE_PY.exists():
        site = next(iter((RESOURCES / "python" / "lib").glob("python3.*/site-packages")), None)
        built_in, updated = _pkg_version(site), _pkg_version(PYLIB)
        if updated and _vkey(updated) > _vkey(built_in):
            return {"kind": "bundled", "version": updated,
                    "cmd": ["/usr/bin/env", f"PYTHONPATH={PYLIB}", str(BUNDLE_PY), "-s", "-m", "yt_dlp"]}
        if built_in:
            return {"kind": "bundled", "version": built_in,
                    "cmd": ["/usr/bin/env", "-u", "PYTHONPATH", str(BUNDLE_PY), "-s", "-m", "yt_dlp"]}
    brew = shutil.which("yt-dlp", path="/opt/homebrew/bin:/usr/local/bin") or shutil.which("yt-dlp")
    return {"kind": "homebrew", "version": "", "cmd": [brew]} if brew else None


ENGINE = resolve_engine()


def ytdlp_cmd():
    return list(ENGINE["cmd"])


def engine_version():
    if ENGINE and not ENGINE["version"]:  # Homebrew: ask once, then remember
        ENGINE["version"] = subprocess.run(ytdlp_cmd() + ["--version"], capture_output=True, text=True).stdout.strip()
    return ENGINE["version"] if ENGINE else ""


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
WHISPER = shutil.which("whisper-cli")
WHISPER_MODELS = [
    Path.home() / ".cache" / "whisper-cpp" / "ggml-large-v3-turbo.bin",
    Path.home() / ".cache" / "whisper-cpp" / "ggml-base.en.bin",
]

DEFAULTS = {
    "out_dir": str(Path.home() / "Downloads" / "VD Plus"),
    "research_dir": str(Path.home() / "Downloads" / "VD Plus" / "Research"),
    "smart_mode": True,          # paste = download immediately with these defaults
    "kind": "video",             # video | audio
    "quality": "best",           # best | 4320 | 2160 | 1440 | 1080 | 720 | 480 | 360
    "target": "original",        # original | apple | android | windows | editing  ("For" menu)
    "compatible": False,         # prefer H.264/AAC MP4 (plays everywhere)
    "container": "auto",         # auto (mp4 when possible, else mkv) | mp4 | mkv | webm
    "audio_format": "mp3",       # mp3 | m4a | opus | flac | wav | best
    "audio_lang": "",            # preferred audio track language, e.g. en, es
    "subs": False,
    "sub_langs": "en.*,en",
    "embed_subs": True,
    "thumbnail": True,
    "sponsorblock": False,       # cut sponsor segments (YouTube)
    "split_chapters": False,
    "live_from_start": False,
    "subdirs": True,             # playlists/channels/search get their own folder
    "cookies": "",               # app (built-in sign-in) | chrome | safari | firefox | brave | edge | arc | ""
    "google_cookies": "",        # browser whose Google/YouTube login to add (Google blocks in-app sign-in)
    "proxy": "",
    "rate_limit": "",            # e.g. 5M, 800K
    "concurrency": 3,
    "notify": True,
    "prevent_sleep": True,
    "clipboard_watch": False,
    "sub_check_hours": 6,
}
COOKIE_BROWSERS = {"chrome", "safari", "firefox", "brave", "edge", "arc", "chromium", "opera", "vivaldi"}
VIDEO_HOSTS = re.compile(
    r"https?://([\w-]+\.)*(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|"
    r"facebook\.com|fb\.watch|vimeo\.com|twitch\.tv|dailymotion\.com|reddit\.com|soundcloud\.com|"
    r"bilibili\.com|rumble\.com|bitchute\.com|threads\.net|linkedin\.com|pinterest\.com|tumblr\.com|"
    r"flickr\.com|streamable\.com|kick\.com|bandcamp\.com|odysee\.com|snapchat\.com)/\S+",
    re.I,
)
PROG_RE = re.compile(r"^PROG " + r"\s+".join([r"(\S+)"] * 7))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hbytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def _progress(job, m):
    """Raw byte counts from yt-dlp -> percent/size/speed/eta strings. Works when the site
    doesn't send a file size (percent unknown -> 'indeterminate', show bytes so far)."""
    done, total, est, speed, eta, fi, fc = (_num(x) for x in m.groups())
    total = total or est
    if total:
        job["percent"] = min(100.0, (done or 0) * 100 / total)
        job["size"] = f"{_hbytes(done or 0)} of {_hbytes(total)}"
        job["indeterminate"] = False
    elif fi and fc:
        job["percent"] = min(100.0, fi * 100 / fc)
        job["size"] = f"{_hbytes(done)} so far" if done else f"part {int(fi)} of {int(fc)}"
        job["indeterminate"] = False
    else:
        job["indeterminate"] = True
        job["size"] = f"{_hbytes(done)} downloaded" if done else ""
    job["speed"] = f"{_hbytes(speed)}/s" if speed else ""
    job["eta"] = (f"{int(eta) // 3600}:{int(eta) % 3600 // 60:02d}:{int(eta) % 60:02d}" if eta and eta >= 3600
                  else f"{int(eta) // 60}:{int(eta) % 60:02d}" if eta else "")


# ---------------------------------------------------------------- settings
_settings_lock = threading.Lock()


def get_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text()))
    except Exception:
        pass
    return s


def save_settings(patch):
    clean = validate({k: v for k, v in patch.items() if k in DEFAULTS})
    with _settings_lock:
        s = get_settings()
        s.update(clean)
        SETTINGS_PATH.write_text(json.dumps(s, indent=2))
    return s


# ---------------------------------------------------------------- input validation
# Everything that reaches yt-dlp from the UI/API/AI goes through here. Values are
# passed as argv (never a shell), but we still pin them to expected shapes.
_PATTERNS = {
    "quality": r"best|\d{3,4}",
    "kind": r"video|audio",
    "container": r"auto|mp4|mkv|webm",
    "audio_format": r"mp3|m4a|opus|flac|wav|best",
    "audio_lang": r"[A-Za-z]{0,3}(-[A-Za-z0-9]{1,8})?",
    "sub_langs": r"[\w.*,\-]{0,100}",
    "format_id": r"[\w\-.=]{1,80}",
    "playlist_items": r"[\d,:\- ]{0,300}",
    "start": r"((\d{1,2}:){0,2}\d{1,2}(\.\d+)?)?",
    "end": r"((\d{1,2}:){0,2}\d{1,2}(\.\d+)?|inf)?",
    "rate_limit": r"(\d+(\.\d+)?[KMGkmg]?)?",
    "proxy": r"((https?|socks5h?|socks4a?)://[\w.\-:@%]+/?)?",
    "cookies": r"|app|" + "|".join(sorted(COOKIE_BROWSERS)),
    "target": r"original|apple|android|windows|editing",
    "google_cookies": r"|" + "|".join(sorted(COOKIE_BROWSERS)),
    "referer": r"(https?://[^\s\"'<>]{1,2000})?",
}
_BOOLS = {"impersonate", "smart_mode", "compatible", "subs", "embed_subs", "thumbnail", "sponsorblock", "split_chapters",
          "live_from_start", "subdirs", "notify", "prevent_sleep", "clipboard_watch", "playlist"}
_RANGES = {"concurrency": (1, 10, int), "sub_check_hours": (0.25, 168, float), "playlist_end": (1, 10000, int)}
_PATHS = {"out_dir", "research_dir"}
# What callers (UI, API, AI) may override per download. Internal keys like 'archive' are not here.
OVERRIDE_KEYS = (set(DEFAULTS) | {"format_id", "playlist", "playlist_items", "playlist_end", "start", "end",
                                  "impersonate", "referer", "name_hint"}) - {
    "smart_mode", "clipboard_watch", "sub_check_hours", "concurrency", "prevent_sleep", "research_dir"}


def _safe_dir(v):
    p = Path(str(v)).expanduser()
    if not p.is_absolute():
        raise ValueError(f"Folder must be an absolute path: {v}")
    p = p.resolve()
    home = Path.home().resolve()
    if not (p == home or home in p.parents or Path("/Volumes") in p.parents):
        raise ValueError(f"Folder must be inside your home folder or an external drive: {v}")
    return str(p)


def validate(d, allowed=None):
    out = {}
    for k, v in (d or {}).items():
        if allowed is not None and k not in allowed:
            continue  # silently drop unknown/internal keys
        if k in _BOOLS:
            out[k] = bool(v)
        elif k in _RANGES:
            lo, hi, typ = _RANGES[k]
            try:
                out[k] = min(max(typ(v), lo), hi)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid number for {k}")
        elif k in _PATHS:
            out[k] = _safe_dir(v) if v else DEFAULTS[k]
        elif k == "name_hint":
            # Title for streams found by the page sniffer (raw stream URLs have no real title).
            v = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', " ", str(v or "")).strip()[:150]
            out[k] = v.replace("%", "%%")
        elif k in _PATTERNS:
            v = "" if v is None else str(v).strip()
            if not re.fullmatch(_PATTERNS[k], v):
                raise ValueError(f"Invalid value for {k}: {v[:60]}")
            out[k] = v
    return out


def clean_overrides(o):
    return validate(o, OVERRIDE_KEYS)


# ---------------------------------------------------------------- database
_db_lock = threading.RLock()  # re-entrant: delete -> cancel -> save must not deadlock
ACTIVE = ("queued", "starting", "downloading", "processing", "converting", "paused")


@contextlib.contextmanager
def db():
    """One short-lived connection per operation: committed on success, always closed."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db(mark_interrupted=False):
    """mark_interrupted: only the server (which owns the queue) passes True at startup. The AI bridge
    opens the same database while downloads may be running and must not touch their status."""
    with _db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, url TEXT, title TEXT, status TEXT, percent REAL,
            files TEXT, error TEXT, opts TEXT, source TEXT, created REAL, finished REAL);
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, enabled INTEGER DEFAULT 1,
            opts TEXT, last_check REAL DEFAULT 0, last_result TEXT DEFAULT '', created REAL);
        """)
        if mark_interrupted:
            # Anything left mid-flight by a crash/quit is resumable, mark it so.
            con.execute("UPDATE jobs SET status='interrupted' WHERE status IN "
                        "('queued','starting','downloading','processing','converting','paused')")
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
        if "meta" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN meta TEXT DEFAULT '{}'")
    os.chmod(DB_PATH, 0o600)


def _save_job(job):
    if job.get("_deleted"):
        return  # removed by the user while it was still running: don't resurrect the row
    with _db_lock, db() as con:
        con.execute(
            "INSERT OR REPLACE INTO jobs (id,url,title,status,percent,files,error,opts,source,created,finished,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["url"], job["title"], job["status"], job["percent"],
             json.dumps(job["files"]), job["error"], json.dumps(job["opts"]),
             job["source"], job["created"], job.get("finished"), json.dumps(job.get("meta") or {})),
        )


def _row(r):
    d = dict(r)
    d["files"] = json.loads(d["files"] or "[]")
    d["opts"] = json.loads(d["opts"] or "{}")
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def job_from_db(jid):
    with _db_lock, db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (str(jid),)).fetchone()
    return _row(r) if r else None


def history(limit=200, query=""):
    sql = "SELECT * FROM jobs"
    args = []
    if query:
        sql += " WHERE title LIKE ? OR url LIKE ?"
        args = [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY created DESC LIMIT ?"
    args.append(limit)
    with _db_lock, db() as con:
        rows = con.execute(sql, args).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- yt-dlp args
GOOGLE_HOSTS = re.compile(r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com|google\.com)(/|:|$)", re.I)
_browser_blocked = set()  # browsers whose logins we couldn't read this session (macOS protects app data)


def _browser_ok(s, name):
    return name in COOKIE_BROWSERS and name not in _browser_blocked and not s.get("no_browser_cookies")


def common_args(s, url=None):
    """Login + proxy options. url: the link being fetched, when there is one."""
    a = ["--no-colors", "--ffmpeg-location", FFMPEG or "ffmpeg"]
    if s.get("cookies") == "app":
        # In-app logins (Instagram, TikTok, ...) plus, for YouTube/Google links only, the user's
        # browser: Google refuses sign-in inside embedded browsers. yt-dlp merges both jars and
        # writes the result back to --cookies on exit, so it always gets a throwaway private copy
        # (a site that clears the session mid-request would otherwise wipe the saved login).
        g = s.get("google_cookies")
        use_browser = _browser_ok(s, g) and (url is None or bool(GOOGLE_HOSTS.match(url)))
        if COOKIE_FILE.exists():
            a += ["--cookies", _cookie_copy()]
        if use_browser:
            a += ["--cookies-from-browser", g]
    elif _browser_ok(s, s.get("cookies")):
        a += ["--cookies-from-browser", s["cookies"]]
    if s.get("proxy"):
        a += ["--proxy", s["proxy"]]
    return a


def browser_login_failed(args, output):
    """True if this yt-dlp run died because a browser's logins couldn't be read. Remembers the
    browser, so the rest of the session doesn't waste an attempt on it for every download."""
    if "--cookies-from-browser" not in args or not BROWSER_COOKIE_ERR.search(output or ""):
        return False
    _browser_blocked.add(args[args.index("--cookies-from-browser") + 1])
    return True


def drop_cookie_copies(args):
    """Delete the throwaway login copy a yt-dlp run was given (see common_args)."""
    for i, a in enumerate(args[:-1]):
        if a == "--cookies" and Path(args[i + 1]).parent == COOKIE_TMP:
            Path(args[i + 1]).unlink(missing_ok=True)


def run_ytdlp(args, timeout=120):
    """Run yt-dlp to completion with the user's login/proxy settings."""
    url = next((a for a in reversed(args) if re.match(r"https?://", a)), None)
    for _ in range(2):
        common = common_args(get_settings(), url)
        try:
            r = subprocess.run(ytdlp_cmd() + common + args, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        finally:
            drop_cookie_copies(common)
        if r.returncode == 0 or not browser_login_failed(common, r.stderr):
            break  # otherwise: once more, without the browser whose logins can't be read
    return r


TARGETS = {
    # Full resolution that plays in QuickTime, Photos, iPhone: H.264 when the site has it at the chosen
    # resolution, otherwise the best stream, converted to HEVC after download if QuickTime can't decode it.
    "apple": {"mac_play": True},
    # Premiere / Resolve / Final Cut: plain H.264 + AAC MP4 (YouTube tops out at 1080p in H.264).
    "editing": {"compatible": True},
    # VLC / Android / Windows handle VP9/AV1, so keep full resolution but force MP4.
    "android": {"container": "mp4"},
    "windows": {"container": "mp4"},
    "original": {},
}


COOKIE_TMP = DATA_DIR / "tmp"


def _cookie_copy():
    COOKIE_TMP.mkdir(mode=0o700, exist_ok=True)
    for old in COOKIE_TMP.glob("cookies-*.txt"):  # sweep copies from finished runs
        if time.time() - old.stat().st_mtime > 6 * 3600:
            old.unlink(missing_ok=True)
    dst = COOKIE_TMP / f"cookies-{uuid.uuid4().hex[:12]}.txt"
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(COOKIE_FILE.read_bytes())
    return str(dst)


def sweep_login_copies():
    """Server startup: nothing is running yet, so any login copy still here is a leftover."""
    if COOKIE_TMP.exists():
        for f in COOKIE_TMP.glob("cookies-*.txt"):
            f.unlink(missing_ok=True)


def build_download_args(o):
    """o = settings merged with per-download overrides."""
    o = dict(o)
    o.update(TARGETS.get(o.get("target") or "original", {}))
    a = []
    if o.get("format_id"):
        fid = o["format_id"]
        a += ["-f", f"{fid}+ba/{fid}"] if o.get("kind") != "audio" else ["-f", fid]
    elif o.get("kind") == "audio":
        a += ["-f", "ba/b"]
    else:
        lang = o.get("audio_lang")
        a += ["-f", f"bv*+ba[language^={lang}]/bv*+ba/b" if lang else "bv*+ba/b"]
        q = str(o.get("quality") or "best")
        res = f"res:{q}" if q.isdigit() else "res"
        sort = (f"vcodec:h264,{res},fps,acodec:m4a" if o.get("compatible")
                else f"{res},fps,hdr:12,vcodec:h264,acodec:aac" if o.get("mac_play")
                else f"{res},fps,hdr:12,vcodec,acodec,br")
        a += ["-S", sort]

    if o.get("kind") == "audio":
        a += ["-x", "--audio-format", o.get("audio_format") or "mp3", "--audio-quality", "0"]
    else:
        c = o.get("container") or "auto"
        a += ["--merge-output-format", "mp4" if o.get("compatible") else ("mp4/mkv" if c == "auto" else c)]

    a += ["--embed-metadata", "--embed-chapters"]
    if o.get("thumbnail", True):
        a += ["--embed-thumbnail"]
    if o.get("subs"):
        a += ["--write-subs", "--write-auto-subs", "--sub-langs", o.get("sub_langs") or "en.*,en"]
        if o.get("embed_subs", True) and o.get("kind") != "audio":
            a += ["--embed-subs"]
    if o.get("sponsorblock"):
        a += ["--sponsorblock-remove", "sponsor,selfpromo,interaction,intro,outro"]
    if o.get("split_chapters"):
        a += ["--split-chapters"]
    if o.get("live_from_start"):
        a += ["--live-from-start"]
    if o.get("rate_limit"):
        a += ["--limit-rate", o["rate_limit"]]
    if o.get("start") or o.get("end"):  # "Create Highlights" / trim
        a += ["--download-sections", f"*{o.get('start') or '0'}-{o.get('end') or 'inf'}",
              "--force-keyframes-at-cuts"]
    if o.get("playlist"):
        a += ["--yes-playlist"]
        if o.get("playlist_items"):
            a += ["--playlist-items", o["playlist_items"]]
        if o.get("playlist_end"):
            a += ["--playlist-end", str(o["playlist_end"])]
    else:
        a += ["--no-playlist"]
    if o.get("archive"):
        a += ["--download-archive", o["archive"]]
    if o.get("impersonate"):
        a += ["--impersonate", "chrome", "--extractor-args", "generic:impersonate"]
    if o.get("referer"):
        ref = o["referer"]
        origin = re.match(r"^https?://[^/]+", ref).group(0)
        a += ["--referer", ref, "--add-header", f"Origin:{origin}"]

    out_dir = Path(o.get("out_dir") or DEFAULTS["out_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = (o["name_hint"] + " [%(id).40s].%(ext)s") if o.get("name_hint") else "%(title).170B [%(id)s].%(ext)s"
    if o.get("playlist") and o.get("subdirs", True):
        tmpl = "%(playlist_title,playlist_id,uploader)s/%(playlist_index|0)03d - " + name
    else:
        tmpl = name
    a += ["-o", str(out_dir / tmpl),
          "-o", "chapter:" + str(out_dir / "%(title).120B [%(id)s]" / "%(section_number)03d - %(section_title)s.%(ext)s")]
    return a


# ---------------------------------------------------------------- queue
class Queue:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.caffeinate = None
        self.listeners = []  # callables(job) on finish
        threading.Thread(target=self._scheduler, daemon=True).start()

    # public -----------------------------------------------------------
    def add(self, url, overrides=None, source="ui", title=None):
        o = get_settings()
        o.update(overrides or {})
        job = {"id": uuid.uuid4().hex[:10], "url": url, "title": title or url,
               "status": "queued", "percent": 0.0, "speed": "", "eta": "", "size": "",
               "files": [], "log": [], "error": "", "opts": o, "source": source,
               "created": time.time(), "finished": None, "proc": None,
               "playlist_count": 0, "playlist_index": 0, "meta": {}}
        with self.cv:
            self.jobs[job["id"]] = job
            self.cv.notify_all()
        _save_job(job)
        return job

    def get(self, jid):
        return self.jobs.get(jid) or job_from_db(jid)

    def list(self):
        with self.lock:
            return [public(j) for j in sorted(self.jobs.values(), key=lambda j: -j["created"])]

    def cancel(self, jid):
        j = self.jobs.get(jid)
        if not j:
            return
        if j["status"] in ("queued", "starting"):
            j["status"] = "cancelled"
            _save_job(j)
        elif j.get("proc") and j["proc"].poll() is None:
            j["status"] = "cancelled"
            self._signal(j, signal.SIGCONT)
            self._signal(j, signal.SIGTERM)

    def pause(self, jid):
        j = self.jobs.get(jid)
        if j and j.get("proc") and j["proc"].poll() is None and j["status"] in ("downloading", "processing", "converting"):
            self._signal(j, signal.SIGSTOP)
            j["_prev"] = j["status"]
            j["status"] = "paused"

    def resume(self, jid):
        j = self.jobs.get(jid)
        if j and j["status"] == "paused":
            self._signal(j, signal.SIGCONT)
            j["status"] = j.pop("_prev", "downloading")

    def retry(self, jid):
        old = self.get(jid)
        if not old:
            return None
        opts = old["opts"] if isinstance(old["opts"], dict) else json.loads(old["opts"])
        return self.add(old["url"], opts, source=old.get("source", "ui"), title=old.get("title"))

    def clear_finished(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j["status"] in ("done", "error", "cancelled")]:
                del self.jobs[k]

    def wait(self, jid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            j = self.jobs.get(jid)
            if not j or j["status"] in ("done", "error", "cancelled", "interrupted"):
                break
            time.sleep(1)
        return self.jobs.get(jid)

    def active_count(self):
        with self.lock:
            return sum(1 for j in self.jobs.values() if j["status"] in ACTIVE)

    def shutdown(self):
        """The app is quitting: stop every download and conversion so nothing keeps running unseen.
        Partial files stay on disk, so Retry picks up where it left off."""
        with self.lock:
            jobs = [j for j in self.jobs.values() if j["status"] in ACTIVE]
        for j in jobs:
            j["_quit"] = True
            j["status"] = "interrupted"
            if j.get("proc") and j["proc"].poll() is None:
                self._signal(j, signal.SIGCONT)  # a paused process can't act on SIGTERM
                self._signal(j, signal.SIGTERM)
            _save_job(j)
        if self.caffeinate and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
        return len(jobs)

    # internals --------------------------------------------------------
    def _signal(self, j, sig):
        try:
            os.killpg(j["proc"].pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _active(self):
        return [j for j in self.jobs.values() if j["status"] in ACTIVE and j["status"] != "queued"]

    def _scheduler(self):
        while True:
            try:
                with self.cv:
                    limit = max(1, int(get_settings().get("concurrency") or 3))
                    queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"),
                                    key=lambda j: j["created"])
                    if queued and len(self._active()) < limit:
                        job = queued[0]
                        job["status"] = "starting"
                        threading.Thread(target=self._run, args=(job,), daemon=True).start()
                        continue
                    self._sleep_guard()
                    self.cv.wait(timeout=1)
            except Exception:
                time.sleep(0.5)  # never let the scheduler die: without it nothing would ever start

    def _sleep_guard(self):
        want = bool(self._active()) and get_settings().get("prevent_sleep")
        running = self.caffeinate and self.caffeinate.poll() is None
        if want and not running:
            self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not want and running:
            self.caffeinate.terminate()

    def _wake(self):
        with self.cv:
            self.cv.notify_all()

    def _run(self, job):
        o = job["opts"]
        if job["status"] == "cancelled":  # cancelled between being scheduled and starting
            _save_job(job)
            return self._wake()
        common = common_args(o, job["url"])
        cmd = ytdlp_cmd() + ["--newline", "--progress-template",
                             "download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                             "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s "
                             "%(progress.fragment_index)s %(progress.fragment_count)s",
                             "--print", "before_dl:TITLE %(title)s",
                             "--print", "before_dl:PLI %(playlist_index|0)s %(n_entries|0)s",
                             "--print", "before_dl:INFO %(.{thumbnail,duration,height,extractor_key,ext,uploader,playlist_title})j",
                             "--print", "after_move:FILE %(filepath)s",
                             "--no-simulate", "--retries", "10", "--fragment-retries", "10",
                             "--concurrent-fragments", "4"]
        cmd += common + build_download_args(o) + ["--", job["url"]]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, bufsize=1, start_new_session=True)
        except Exception as e:
            drop_cookie_copies(common)
            job.update(status="error", error=str(e))
            _save_job(job)
            return self._wake()
        job["proc"] = proc
        if job["status"] == "cancelled" or job.get("_quit"):
            self._signal(job, signal.SIGTERM)
        else:
            job["status"] = "downloading"
            _save_job(job)
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                m = PROG_RE.match(line)
                if m:
                    _progress(job, m)
                    if job["status"] == "processing":
                        job["status"] = "downloading"
                    continue
                if line.startswith("TITLE "):
                    if not o.get("playlist") or job["title"] == job["url"]:
                        job["title"] = line[6:]
                    job["current"] = line[6:]
                elif line.startswith("INFO "):
                    if not job["meta"]:
                        try:
                            job["meta"] = json.loads(line[5:])
                            threading.Thread(target=cache_thumb, args=(job,), daemon=True).start()
                        except ValueError:
                            pass
                elif line.startswith("PLI "):
                    p = line.split()
                    job["playlist_index"], job["playlist_count"] = int(p[1] or 0), int(p[2] or 0)
                elif line.startswith("FILE "):
                    job["files"].append(line[5:])
                elif re.match(r"^\[(Merger|ExtractAudio|EmbedThumbnail|Metadata|VideoConvertor|FFmpeg\w*|SponsorBlock|ModifyChapters|SplitChapters)\]", line):
                    if job["status"] == "downloading":
                        job["status"] = "processing"
                if line.startswith("ERROR:"):
                    job["error"] = line[6:].strip()
                job["log"].append(line)
                del job["log"][:-300]
            proc.wait()
        finally:
            proc.stdout.close()
            drop_cookie_copies(common)
        if job.get("_quit"):
            return  # app is quitting; shutdown() already recorded this job as interrupted
        rc = proc.returncode
        tail = job["error"] + " " + " ".join(job["log"][-6:])
        if rc != 0 and job["status"] != "cancelled" and browser_login_failed(common, tail):
            # The borrowed browser's logins couldn't be read: don't fail the download over it.
            job.update(error="", percent=0.0, status="downloading")
            job["log"].append("VD Plus: couldn't read the browser's logins, retrying without them…")
            return self._run(job)
        if (rc != 0 and job["status"] != "cancelled" and not o.get("impersonate") and not job["files"]
                and not _is_drm(job["url"], tail) and RETRY_RE.search(tail)):
            # Site not recognised / blocked: try again looking like a regular Chrome browser.
            o["impersonate"] = True
            job.update(error="", percent=0.0, status="downloading")
            job["log"].append("VD Plus: retrying as a regular Chrome browser…")
            return self._run(job)
        job["finished"] = time.time()
        if job["status"] != "cancelled" and job["files"]:
            self._post_process(job)
            if job.get("_quit"):
                return
        conv_stopped = job["status"] == "cancelled" and job.pop("_conv", False)
        if conv_stopped:
            job["status"] = "processing"  # the download itself finished; only the conversion was stopped
        job["meta"]["bytes"] = sum(os.path.getsize(f) for f in job["files"] if os.path.exists(f))
        if job["status"] != "cancelled":
            if rc == 0 or job["files"]:
                # Carousels / playlists: some items may fail (e.g. photos have no video) while the
                # videos were saved. That's a success, with a note.
                job.update(status="done", percent=100.0, eta="", speed="")
                if rc != 0:
                    job["error"] = ""  # partial success: keep the raw message in the log only
                    n = len(job["files"])
                    photos = re.search(r"No video formats found", " ".join(job["log"]), re.I)
                    job["meta"]["note"] = (f"Saved {n} video{'s' if n != 1 else ''}. "
                                           + ("Photos in this post were skipped." if photos
                                              else "Some items couldn't be downloaded."))
                elif conv_stopped:
                    job["meta"]["note"] = "Conversion stopped. Kept the original file."
                thumb_from_file(job)
            else:
                job["status"] = "error"
                if not job["error"]:
                    job["error"] = job["log"][-1] if job["log"] else f"yt-dlp exited {rc}"
                raw = job["error"]
                if _is_drm(job["url"], tail):
                    job["error"] = ("This service encrypts its videos (DRM), so they can't be downloaded. "
                                    "VD Plus works with any video that isn't copy-protected.")
                elif NOT_FOUND_RE.search(raw):
                    job["can_sniff"] = True
                    job["error"] = ("No video found in this page's code. Use \u201cFind video on page\u201d to open it "
                                    "like a browser and catch the stream.")
                elif LOGIN_RE.search(raw):
                    job["needs_login"] = True
                    job["error"] = ("This site needs you to be signed in. Click the globe (top right), log in, "
                                    "then click Retry. VD Plus retries automatically after you sign in.")
        if o.get("sub_id"):
            # Subscription check: record the outcome on the subscription, and don't leave an empty
            # "No new videos" row in the list every few hours.
            n = len(job["files"])
            subs_update(o["sub_id"], last_result=("Check failed" if job["status"] == "error" else
                                                  f"{n} new video{'s' if n != 1 else ''}" if n else "No new videos"))
            if job["status"] == "done" and not n:
                job["_deleted"] = True
                with self.lock:
                    self.jobs.pop(job["id"], None)
                with _db_lock, db() as con:
                    con.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
                return self._wake()
        _save_job(job)
        if o.get("notify") and job["status"] in ("done", "error"):
            notify(("Downloaded: " if job["status"] == "done" else "Failed: ") + job["title"])
        for fn in self.listeners:
            try:
                fn(job)
            except Exception:
                pass
        self._wake()

    def _post_process(self, job):
        """After yt-dlp: tidy the names of direct-link downloads, then make MP4s (see to_mp4)."""
        o = job["opts"]
        if job["meta"].get("extractor_key") == "Generic":
            job["files"] = [tidy_name(f) for f in job["files"]]
        if o.get("kind") == "audio" or (o.get("container") or "auto") != "auto":
            return
        convert = bool(TARGETS.get(o.get("target") or "original", {}).get("mac_play"))
        total, out = len(job["files"]), []
        for n, f in enumerate(job["files"], 1):
            if job["status"] == "cancelled" or job.get("_quit"):
                out.append(f)
                continue

            def progress(pct, n=n):
                job["_conv"] = True
                if job["status"] in ("downloading", "processing"):
                    job["status"] = "converting"
                job.update(percent=pct, indeterminate=False, speed="", eta="",
                           size=f"file {n} of {total}" if total > 1 else "")

            out.append(to_mp4(f, convert=convert, on_progress=progress,
                              on_proc=lambda p: job.__setitem__("proc", p)))
        job["files"] = out
        if not convert and any(f.lower().endswith(".mkv") for f in out):
            job["meta"]["note"] = ("Saved as MKV to keep the original quality. "
                                   "For files QuickTime can play, set For to Mac & iPhone.")


RETRY_RE = re.compile(r"Unsupported URL|No video formats|Unable to (extract|download webpage)|HTTP Error 40[13]|"
                      r"Cloudflare|captcha|Got HTTP Error 429", re.I)
BROWSER_COOKIE_ERR = re.compile(r"could not find \w+ cookies database|failed to (decrypt|load|read).{0,40}cookie|"
                                r"keyring|Safe Storage|Operation not permitted.{0,80}Cookies", re.I)
LOGIN_RE = re.compile(r"log ?in|sign in|private|age.?restrict|confirm your age|rate.?limit|confirm you|"
                      r"empty media response|not granting access|requires authentication|members.only", re.I)
NOT_FOUND_RE = re.compile(r"Unsupported URL|No video formats|Unable to extract|no suitable", re.I)
DRM_DOMAINS = ("netflix.com", "disneyplus.com", "primevideo.com", "hulu.com", "max.com", "hbomax.com",
               "peacocktv.com", "paramountplus.com", "tv.apple.com", "music.apple.com", "spotify.com")


def _is_drm(url, text=""):
    host = (re.match(r"^https?://([^/?#]+)", url or "") or [None, ""])[1].lower().split(":")[0]
    if any(host == d or host.endswith("." + d) for d in DRM_DOMAINS):
        return True
    if re.search(r"amazon\.[a-z.]+/(gp/video|.*/dp/.*video)", url or "", re.I):
        return True
    return bool(re.search(r"\bDRM\b", text))


def public(job):
    d = {k: v for k, v in job.items() if k not in ("proc", "opts") and not k.startswith("_")}
    d["log"] = job.get("log", [])[-60:]
    o = job.get("opts") if isinstance(job.get("opts"), dict) else {}
    d["kind"] = o.get("kind", "video")
    d["playlist"] = bool(o.get("playlist"))
    d["target"] = o.get("target", "original")
    d.setdefault("meta", {})
    return d


def all_jobs(queue, limit=500):
    """Live queue + saved history as one list (the main window shows both, like 4KVD)."""
    with queue.lock:
        running = list(queue.jobs.values())
    live = {j["id"]: public(j) for j in running}
    out = list(live.values())
    for h in history(limit):
        if h["id"] not in live:
            h = dict(h)
            h.update(speed="", eta="", size="", log=[], playlist_count=0, playlist_index=0)
            out.append(public(h))
    return sorted(out, key=lambda j: -(j.get("created") or 0))[:limit]


def trash(paths):
    """Move files to the Trash (recoverable). Uses the bundled vdtrash helper (FileManager.trashItem,
    no permissions needed); falls back to scripting Finder. Returns how many files actually moved."""
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return 0
    helper = BUNDLE_BIN / "vdtrash"
    if helper.exists():
        subprocess.run([str(helper)] + paths, capture_output=True, timeout=120)
    else:
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "repeat with p in argv", "-e", 'tell application "Finder" to delete (POSIX file (p as text))',
                        "-e", "end repeat", "-e", "end run"] + paths, capture_output=True, timeout=60)
    return sum(1 for p in paths if not os.path.exists(p))


def delete_jobs(queue, ids, trash_files=False):
    n_files = 0
    for jid in ids:
        j = queue.jobs.get(jid)
        if j:
            j["_deleted"] = True  # a still-running job must not write its row back when it stops
            if j["status"] in ACTIVE:
                queue.cancel(jid)
        files = list((j or {}).get("files") or [])
        with _db_lock, db() as con:
            if not files:
                r = con.execute("SELECT files FROM jobs WHERE id=?", (jid,)).fetchone()
                files = json.loads(r["files"]) if r and r["files"] else []
            con.execute("DELETE FROM jobs WHERE id=?", (jid,))
        with queue.lock:
            queue.jobs.pop(jid, None)
        if re.fullmatch(r"[0-9a-f]{6,32}", str(jid)):
            (THUMBS / f"{jid}.img").unlink(missing_ok=True)
        if trash_files:
            n_files += trash(files)
    return n_files


def notify(msg):
    try:
        subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                          'display notification (item 1 of argv) with title "VD Plus"',
                          "-e", "end run", msg[:200]])
    except Exception:
        pass


# ---------------------------------------------------------------- analyze / search
def _run_json(args, timeout=120):
    r = run_ytdlp(["-J", "--no-warnings"] + args, timeout=timeout)
    if r.returncode != 0:
        err = [l for l in r.stderr.splitlines() if l.startswith("ERROR")]
        raise RuntimeError((err[-1] if err else r.stderr.strip()[-400:]) or "yt-dlp failed")
    return json.loads(r.stdout)


def _fmt_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def analyze(url):
    """Formats / playlist entries for the quality picker."""
    info = _run_json(["--flat-playlist", "--", url])
    if info.get("_type") == "playlist":
        entries = [{"id": e.get("id"), "title": e.get("title") or e.get("id"),
                    "url": e.get("url") or e.get("webpage_url"), "duration": e.get("duration")}
                   for e in (info.get("entries") or []) if e]
        return {"type": "playlist", "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
                "count": len(entries), "entries": entries[:1000], "url": url,
                "thumbnail": (info.get("thumbnails") or [{}])[-1].get("url")}
    fmts = info.get("formats") or []
    dur = info.get("duration") or 0
    best_audio = max((f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * dur
                      for f in fmts if f.get("vcodec") == "none"), default=0)
    by_h = {}
    for f in fmts:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * dur
        key = (h, f.get("dynamic_range") or "SDR")
        cur = by_h.get(key)
        score = (f.get("fps") or 0, f.get("tbr") or 0)
        if not cur or score > cur["_score"]:
            by_h[key] = {"format_id": f["format_id"], "height": h, "fps": f.get("fps"),
                         "vcodec": (f.get("vcodec") or "").split(".")[0], "ext": f.get("ext"),
                         "hdr": key[1] != "SDR", "size": _fmt_size(size + (best_audio if f.get("acodec") == "none" else 0)),
                         "_score": score}
    video = sorted(by_h.values(), key=lambda v: (-v["height"], -v["hdr"], -(v["fps"] or 0)))
    for v in video:
        v.pop("_score")
    langs = sorted({f.get("language") for f in fmts if f.get("vcodec") == "none" and f.get("language")})
    return {
        "type": "video", "url": url, "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
        "duration": dur, "thumbnail": info.get("thumbnail"), "is_live": info.get("is_live"),
        "video_formats": video, "audio_languages": langs,
        "subtitles": sorted((info.get("subtitles") or {}).keys()),
        "auto_subtitles": sorted(k for k in (info.get("automatic_captions") or {}).keys() if "-" not in k)[:40],
        "chapters": [{"start": c.get("start_time"), "title": c.get("title")} for c in (info.get("chapters") or [])],
    }


def search(query, limit=20, site="youtube"):
    prefix = {"youtube": "ytsearch", "soundcloud": "scsearch", "bilibili": "bilisearch",
              "youtube_new": "ytsearchdate"}.get(site, "ytsearch")
    info = _run_json(["--flat-playlist", f"{prefix}{int(limit)}:{query}"], timeout=90)
    return [{"id": e.get("id"), "title": e.get("title"), "url": e.get("url"),
             "channel": e.get("channel") or e.get("uploader"), "duration": e.get("duration"),
             "views": e.get("view_count"),
             "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url")}
            for e in info.get("entries") or [] if e]


# ---------------------------------------------------------------- subscriptions
def subs_list():
    with _db_lock, db() as con:
        return [dict(r, opts=json.loads(r["opts"] or "{}")) for r in con.execute(
            "SELECT * FROM subscriptions ORDER BY created DESC")]


def subs_add(url, title="", only_new=True, opts=None):
    sid = uuid.uuid4().hex[:10]
    archive = str(ARCHIVE_DIR / f"{sid}.txt")
    if only_new:
        # Record everything already on the channel so only future uploads download.
        r = run_ytdlp(["--flat-playlist", "--print", "%(ie_key)s %(id)s", "--no-warnings", "--", url], timeout=600)
        lines = [f"{p[0].lower()} {p[1]}" for p in (l.split(" ", 1) for l in r.stdout.splitlines()) if len(p) == 2]
        Path(archive).write_text("\n".join(lines) + "\n")
        if not title:
            title = url
    with _db_lock, db() as con:
        con.execute("INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?)",
                    (sid, url, title or url, 1, json.dumps(clean_overrides(opts)), time.time() if only_new else 0,
                     "", time.time()))
    return sid


def subs_update(sid, **kw):
    with _db_lock, db() as con:
        for k, v in kw.items():
            if k in ("enabled", "title", "last_check", "last_result"):
                con.execute(f"UPDATE subscriptions SET {k}=? WHERE id=?", (v, sid))


def subs_delete(sid):
    with _db_lock, db() as con:
        con.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    try:
        (ARCHIVE_DIR / f"{sid}.txt").unlink()
    except FileNotFoundError:
        pass


def subs_check(queue, sid=None, force=False):
    hours = float(get_settings().get("sub_check_hours") or 6)
    n = 0
    for s in subs_list():
        if sid and s["id"] != sid:
            continue
        if not s["enabled"] and not force:
            continue
        if not force and time.time() - (s["last_check"] or 0) < hours * 3600:
            continue
        o = dict(s["opts"])
        o.update(playlist=True, playlist_end=30, archive=str(ARCHIVE_DIR / f"{s['id']}.txt"), notify=False,
                 sub_id=s["id"])
        queue.add(s["url"], o, source="subscription", title=f"[Subscription] {s['title']}")
        subs_update(s["id"], last_check=time.time())
        n += 1
    return n


def start_background(queue):
    """Subscription timer + clipboard watcher."""
    def subs_loop():
        while True:
            try:
                subs_check(queue)
            except Exception:
                pass
            time.sleep(60)

    def clip_loop():
        last = ""
        while True:
            time.sleep(1.5)
            if not get_settings().get("clipboard_watch"):
                continue
            try:
                txt = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout.strip()
            except Exception:
                continue
            if txt == last:
                continue
            first = last == ""
            last = txt
            if first:
                continue  # don't grab whatever was already on the clipboard at launch
            try:
                with queue.lock:
                    known = any(j["url"] == txt for j in queue.jobs.values())
                if VIDEO_HOSTS.fullmatch(txt) and not known:
                    queue.add(txt, source="clipboard")
            except Exception:
                pass

    def engine_loop():
        while True:
            try:
                if time.time() - engine_state().get("checked", 0) > 7 * 86400:
                    check_engine_latest()
            except Exception:
                pass
            time.sleep(6 * 3600)

    for fn in (subs_loop, clip_loop, engine_loop):
        threading.Thread(target=fn, daemon=True).start()


# ---------------------------------------------------------------- engine updates
def engine_state():
    try:
        return json.loads(ENGINE_CHECK.read_text())
    except Exception:
        return {}


def check_engine_latest():
    """Ask PyPI for the newest yt-dlp version (sites change often; an old engine is the usual
    reason a download stops working). Only the version number is fetched."""
    with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=8) as r:
        latest = json.loads(r.read())["info"]["version"]
    ENGINE_CHECK.write_text(json.dumps({"checked": time.time(), "latest": latest}))
    return latest


def engine_outdated():
    latest = engine_state().get("latest", "")
    return bool(latest) and _vkey(latest) > _vkey(engine_version())


def update_engine():
    """Bundled engine: install the newest yt-dlp (and its libraries) into Application Support, next to
    the app rather than inside it, then switch to it. Dev checkout: brew upgrade."""
    global ENGINE
    if ENGINE["kind"] == "bundled":
        new = DATA_DIR / "pylib.new"
        shutil.rmtree(new, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([str(BUNDLE_PY), "-s", "-m", "pip", "install", "--target", str(new), "--only-binary", ":all:",
                            "--disable-pip-version-check", "--no-input", "--quiet", ENGINE_SPEC],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL, env=env)
        got = _pkg_version(new)
        ok = r.returncode == 0 and bool(got)
        if ok and _vkey(got) > _vkey(engine_version()):
            shutil.rmtree(PYLIB, ignore_errors=True)
            new.rename(PYLIB)
        else:
            shutil.rmtree(new, ignore_errors=True)
    else:
        brew = shutil.which("brew")
        r = subprocess.run([brew, "upgrade", "yt-dlp"] if brew else ytdlp_cmd() + ["-U"],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
        ok = r.returncode == 0
    ENGINE = resolve_engine()
    try:
        check_engine_latest()
    except Exception:
        pass
    return {"ok": ok, "version": engine_version(), "output": (r.stdout + r.stderr)[-2000:]}


# ---------------------------------------------------------------- thumbnails
def _img_type(data):
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def cache_thumb(job):
    """Keep our own copy of the thumbnail: site thumbnail links (Instagram especially) expire."""
    url = (job.get("meta") or {}).get("thumbnail") or ""
    host = (re.match(r"^https?://([^/:?#]+)", url) or [None, ""])[1].lower()
    if not host or re.match(r"(localhost$|127\.|10\.|192\.168\.|169\.254\.|0\.|\[)", host):
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(3_000_001)
        if 0 < len(data) <= 3_000_000 and _img_type(data) and not job.get("_deleted"):
            THUMBS.mkdir(mode=0o700, exist_ok=True)
            (THUMBS / f"{job['id']}.img").write_bytes(data)
            job["meta"]["thumb"] = 1
            _save_job(job)
    except Exception:
        pass


def thumb_from_file(job):
    """No site thumbnail (direct links, sniffed streams): grab a frame from the saved video."""
    if job["meta"].get("thumb") or not FFMPEG:
        return
    f = next((f for f in job["files"] if re.search(r"\.(mp4|mkv|webm|mov|m4v)$", f, re.I) and os.path.exists(f)), None)
    if not f:
        return
    THUMBS.mkdir(mode=0o700, exist_ok=True)
    out = THUMBS / f"{job['id']}.img"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", "1", "-i", f, "-frames:v", "1",
                    "-vf", "scale=320:-2", "-c:v", "mjpeg", "-f", "image2", str(out)],
                   capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    if out.exists() and out.stat().st_size:
        job["meta"]["thumb"] = 1


def thumb_bytes(jid):
    if not re.fullmatch(r"[0-9a-f]{6,32}", str(jid or "")):
        return None, ""
    try:
        data = (THUMBS / f"{jid}.img").read_bytes()
    except OSError:
        return None, ""
    return data, _img_type(data)


def tidy_name(path):
    """Direct-link downloads get names like 'clip [clip.mp4?token=...].mp4': drop the bracketed part."""
    p = Path(path)
    m = re.fullmatch(r"(.+?) \[.*\](\.[A-Za-z0-9]+)", p.name)
    if not m or not p.exists():
        return path
    stem, ext = m.group(1).strip(), m.group(2)
    dst, n = p.with_name(stem + ext), 2
    while dst.exists():
        dst, n = p.with_name(f"{stem} ({n}){ext}"), n + 1
    p.rename(dst)
    return str(dst)


# ---------------------------------------------------------------- research helpers
def metadata(url):
    info = _run_json(["--no-playlist", "--", url])
    keep = ["id", "title", "uploader", "channel", "channel_url", "upload_date", "duration", "view_count",
            "like_count", "comment_count", "description", "tags", "categories", "webpage_url",
            "extractor_key", "is_live", "language", "thumbnail"]
    out = {k: info.get(k) for k in keep if info.get(k) is not None}
    out["chapters"] = [{"start": c.get("start_time"), "title": c.get("title")} for c in info.get("chapters") or []]
    out["subtitle_languages"] = sorted((info.get("subtitles") or {}).keys())
    out["max_resolution"] = max((f.get("height") or 0 for f in info.get("formats") or []), default=0)
    return out


def _ts(sec):
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:d}:{sec % 60:02d}"


def _parse_vtt(text):
    """VTT -> [(start_seconds, line)], de-duplicating YouTube's rolling auto-captions."""
    out, last = [], ""
    start = 0.0
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"(\d+):(\d+):(\d+)[.,](\d+)\s+-->", block) or re.search(r"(\d+):(\d+)[.,](\d+)\s+-->", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] if len(g) == 4 else g[0] * 60 + g[1]
        lines = block.split("\n")
        idx = next(i for i, l in enumerate(lines) if "-->" in l)
        for l in lines[idx + 1:]:
            l = re.sub(r"<[^>]+>", "", l).strip()
            l = re.sub(r"&nbsp;", " ", l)
            if l and l != last:
                out.append((start, l))
                last = l
    return out


def _format_transcript(rows, timestamps=True, every=30):
    if not timestamps:
        return " ".join(l for _, l in rows)
    out, bucket, bstart = [], [], None
    for t, l in rows:
        if bstart is None:
            bstart = t
        if t - bstart >= every and bucket:
            out.append(f"[{_ts(bstart)}] " + " ".join(bucket))
            bucket, bstart = [], t
        bucket.append(l)
    if bucket:
        out.append(f"[{_ts(bstart)}] " + " ".join(bucket))
    return "\n".join(out)


def transcript(target, lang="en", timestamps=True):
    """Captions from the site if available, otherwise local Whisper on the audio."""
    with tempfile.TemporaryDirectory() as tmp:
        if re.match(r"^https?://", target, re.I):
            r = run_ytdlp(["--skip-download", "--write-subs", "--write-auto-subs",
                           "--sub-langs", f"{lang}.*,{lang}", "--sub-format", "vtt/best",
                           "--convert-subs", "vtt", "--no-playlist", "--no-warnings",
                           "-o", f"{tmp}/s.%(ext)s", "--", target], timeout=180)
            vtts = sorted(Path(tmp).glob("*.vtt"), key=lambda p: ("orig" not in p.name, len(p.name)))
            if vtts:
                rows = _parse_vtt(vtts[0].read_text(errors="ignore"))
                if rows:
                    return {"source": "captions", "file": vtts[0].name,
                            "text": _format_transcript(rows, timestamps)}
            # No captions: pull audio and transcribe locally.
            r = run_ytdlp(["-f", "ba/b", "-x", "--audio-format", "wav", "--no-playlist",
                           "--postprocessor-args", "ExtractAudio:-ar 16000 -ac 1",
                           "-o", f"{tmp}/a.%(ext)s", "--", target], timeout=1800)
            wav = Path(tmp) / "a.wav"
            if not wav.exists():
                raise RuntimeError("Could not get captions or audio: " + r.stderr.strip()[-300:])
        else:
            src = Path(target).expanduser()
            if not src.exists():
                raise RuntimeError(f"File not found: {src}")
            wav = Path(tmp) / "a.wav"
            subprocess.run([FFMPEG, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=1800)
        return {"source": "whisper", "text": _whisper(wav, lang, timestamps)}


def _whisper(wav, lang, timestamps):
    model = next((m for m in WHISPER_MODELS if m.exists()), None)
    if not WHISPER or not model:
        raise RuntimeError("No captions on this video and Whisper isn't set up "
                           "(need whisper-cli + a ggml model in ~/.cache/whisper-cpp).")
    out = str(wav)[:-4]
    subprocess.run([WHISPER, "-m", str(model), "-f", str(wav), "-l", lang or "auto", "-ovtt", "-of", out],
                   capture_output=True, text=True, timeout=3600)
    vtt = Path(out + ".vtt")
    if not vtt.exists():
        raise RuntimeError("Whisper transcription failed.")
    return _format_transcript(_parse_vtt(vtt.read_text(errors="ignore")), timestamps)


_HEVC_ENC = None


def _has_hevc_encoder():
    global _HEVC_ENC
    if _HEVC_ENC is None:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        _HEVC_ENC = "hevc_videotoolbox" in r.stdout
    return _HEVC_ENC


def _video_bitrate(path):
    for args in (["-select_streams", "v:0", "-show_entries", "stream=bit_rate"], ["-show_entries", "format=bit_rate"]):
        r = subprocess.run([FFPROBE, "-v", "error", *args, "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            return int(r.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            continue
    return 0


MP4_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4"}
MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "alac", "flac"}
MP4_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text"}


def _probe_streams(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,pix_fmt,color_transfer,color_primaries,color_space",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or "{}").get("streams", [])


def _ffmpeg_with_progress(cmd, duration, on_progress=None, on_proc=None):
    """Run a long ffmpeg job in its own process group (so Cancel/Pause/Quit can reach it),
    reporting percent done from ffmpeg's -progress output."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    if on_proc:
        on_proc(proc)
    if on_progress:
        on_progress(0.0)
    with proc.stdout:
        for line in proc.stdout:
            if line.startswith("out_time_us=") and duration and on_progress:
                try:
                    on_progress(max(0.0, min(99.0, int(line.split("=", 1)[1]) / 1e6 * 100 / duration)))
                except ValueError:
                    pass
    return proc.wait() == 0


def to_mp4(path, convert=False, on_progress=None, on_proc=None):
    """Turn a finished .mkv into an .mp4 when that is safe, and return the path to use from now on.

    yt-dlp's 'mp4/mkv' merge falls back to MKV more often than needed (even for H.264 + AAC).
      * Every stream fits MP4 and QuickTime can decode the video: rewrap, no re-encode (about a second).
      * VP9 video (Instagram, YouTube above 1080p): QuickTime can't decode it in any container, so a VP9
        .mp4 is worse than the .mkv (it opens in QuickTime and fails). Kept as MKV unless convert=True
        ("For: Mac & iPhone"): then re-encoded to HEVC on the Mac's hardware encoder at twice the source
        bitrate, keeping 10-bit depth and the colour/HDR tags.
      * Anything else stays MKV."""
    src = Path(path)
    if src.suffix.lower() != ".mkv" or not src.exists() or not FFPROBE:
        return path
    try:
        streams = _probe_streams(src)
    except Exception:
        return path
    ok = {"video": MP4_VIDEO, "audio": MP4_AUDIO, "subtitle": MP4_SUBS}
    video = {}
    for st in streams:
        kind, codec = st.get("codec_type"), st.get("codec_name")
        if kind == "attachment" or (kind == "video" and codec in ("mjpeg", "png")):
            continue  # embedded cover art / fonts are dropped below
        if kind in ok and codec not in ok[kind]:
            return path
        if kind == "video" and not video:
            video = st
    dst = src.with_suffix(".mp4")
    if dst.exists():
        return path
    vp9 = video.get("codec_name") == "vp9"
    if vp9 and not (convert and _has_hevc_encoder()):
        return path
    tmp = src.with_name(src.stem + ".part.mp4")
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    if vp9:
        # Hardware HEVC needs more bits than VP9 for the same picture: twice the source bitrate for
        # low-bitrate clips (Instagram), 1.5x for high-bitrate ones (1440p/4K YouTube), never under 4 Mbps.
        br = _video_bitrate(src) or 2_000_000
        target = max(int(br * (2 if br < 3_000_000 else 1.5)), 4_000_000)
        cmd += ["-c:v", "hevc_videotoolbox", "-b:v", str(target), "-maxrate", str(target * 3 // 2),
                "-bufsize", str(target * 2), "-tag:v", "hvc1"]
        if "10" in (video.get("pix_fmt") or ""):
            cmd += ["-profile:v", "main10", "-pix_fmt", "p010le"]  # keep 10-bit (HDR) video 10-bit
        else:
            cmd += ["-pix_fmt", "yuv420p"]
        for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                          ("-colorspace", "color_space")):
            if video.get(key) and video[key] != "unknown":
                cmd += [flag, video[key]]
        cmd += ["-c:a", "copy", "-c:s", "mov_text", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp)]
        good = _ffmpeg_with_progress(cmd, probe_duration(src), on_progress, on_proc)
    else:
        cmd += ["-c", "copy", "-c:s", "mov_text"]
        if video.get("codec_name") == "hevc":
            cmd += ["-tag:v", "hvc1"]  # QuickTime only plays HEVC-in-MP4 tagged hvc1
        cmd += ["-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
        good = r.returncode == 0
    size_ok = tmp.exists() and (vp9 or tmp.stat().st_size >= src.stat().st_size * 0.9)
    if not good or not size_ok or abs(probe_duration(tmp) - probe_duration(src)) > 1.0:
        tmp.unlink(missing_ok=True)
        return path
    tmp.rename(dst)
    src.unlink()
    return str(dst)


def probe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(path, count=12, every=0, width=768, out_dir=None):
    """Evenly spaced (or every N seconds) JPEG stills for visual analysis."""
    src = Path(path).expanduser()
    if not src.exists():
        raise RuntimeError(f"File not found: {src}")
    dur = probe_duration(src)
    out = Path(out_dir or (src.parent / f"{src.stem} frames"))
    out.mkdir(parents=True, exist_ok=True)
    if every and every > 0:
        times = [t for t in frange(0, dur, every)][:200]
    else:
        count = max(1, min(int(count), 200))
        times = [dur * (i + 0.5) / count for i in range(count)]
    files = []
    for t in times:
        f = out / f"{_ts(t).replace(':', '-')}.jpg"
        subprocess.run([FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(f)], capture_output=True, timeout=60)
        if f.exists():
            files.append({"time": _ts(t), "seconds": round(t, 2), "path": str(f)})
    return {"duration": dur, "folder": str(out), "frames": files}


def frange(a, b, step):
    while a < b:
        yield a
        a += step
