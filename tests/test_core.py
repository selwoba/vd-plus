#!/usr/bin/env python3
"""
Offline tests for the VD Plus engine (execution/ripper_core.py).

    python3 tests/test_core.py

Runs against a throwaway HOME, so real settings, history and downloads are never touched.
No network. The media tests need ffmpeg + ffprobe on PATH and are skipped without them;
the HEVC tests also need the Mac hardware encoder (hevc_videotoolbox).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="vdplus-test-")
os.environ["HOME"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
import ripper_core as core  # noqa: E402

HAVE_FFMPEG = bool(core.FFMPEG and core.FFPROBE)


def make_clip(path, codec, seconds=2, ten_bit=False):
    """Small synthetic clip with a sine-wave audio track."""
    cmd = [core.FFMPEG, "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=duration={seconds}"]
    if codec == "vp9":
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "600k", "-deadline", "realtime", "-cpu-used", "8"]
        if ten_bit:
            cmd += ["-vf", "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:"
                           "colorspace=bt2020nc", "-profile:v", "2"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    subprocess.run(cmd + ["-c:a", "aac", str(path)], check=True, stdin=subprocess.DEVNULL)


def video_stream(path):
    return next(s for s in core._probe_streams(path) if s["codec_type"] == "video")


class QuietQueue(core.Queue):
    """A queue whose scheduler never starts anything, for testing queue bookkeeping."""

    def _scheduler(self):
        pass


def fake_job(queue, status="queued", **extra):
    job = {"id": os.urandom(5).hex(), "url": "https://example.com/v", "title": "t", "status": status,
           "percent": 0.0, "speed": "", "eta": "", "size": "", "files": [], "log": [], "error": "",
           "opts": dict(core.DEFAULTS), "source": "ui", "created": time.time(), "finished": None,
           "proc": None, "playlist_count": 0, "playlist_index": 0, "meta": {}}
    job.update(extra)
    queue.jobs[job["id"]] = job
    core._save_job(job)
    return job


class Validation(unittest.TestCase):
    def test_rejects_bad_values(self):
        for bad in ({"format_id": "137;rm -rf"}, {"proxy": "http://x --exec rm"}, {"quality": "best; ls"},
                    {"cookies": "netscape"}, {"start": "1:00 --exec"}, {"target": "linux"}):
            with self.assertRaises(ValueError, msg=bad):
                core.clean_overrides(bad)

    def test_folders_stay_in_home_or_volumes(self):
        with self.assertRaises(ValueError):
            core.validate({"out_dir": "/etc"})
        with self.assertRaises(ValueError):
            core.validate({"out_dir": "relative/path"})
        inside = str(Path(_HOME) / "Movies")
        self.assertEqual(core.validate({"out_dir": inside})["out_dir"], str(Path(inside).resolve()))

    def test_internal_keys_cannot_be_set_from_outside(self):
        out = core.clean_overrides({"archive": "/tmp/x", "sub_id": "abc", "no_browser_cookies": True, "quality": "720"})
        self.assertEqual(out, {"quality": "720"})

    def test_name_hint_is_sanitised(self):
        self.assertEqual(core.clean_overrides({"name_hint": 'My: Video / 100%'})["name_hint"], "My  Video   100%%")


class Arguments(unittest.TestCase):
    def args(self, **o):
        return core.build_download_args({**core.DEFAULTS, "out_dir": _HOME, **o})

    def sort_of(self, a):
        return a[a.index("-S") + 1]

    def test_original_keeps_best_codec_order(self):
        self.assertEqual(self.sort_of(self.args()), "res,fps,hdr:12,vcodec,acodec,br")

    def test_mac_target_is_full_resolution_and_prefers_h264_at_that_resolution(self):
        s = self.sort_of(self.args(target="apple", quality="2160"))
        self.assertTrue(s.startswith("res:2160,fps"))
        self.assertLess(s.index("res"), s.index("vcodec:h264"))

    def test_editing_target_is_plain_h264_mp4(self):
        a = self.args(target="editing")
        self.assertTrue(self.sort_of(a).startswith("vcodec:h264"))
        self.assertEqual(a[a.index("--merge-output-format") + 1], "mp4")

    def test_audio_and_clip_options(self):
        a = self.args(kind="audio", audio_format="flac")
        self.assertIn("-x", a)
        self.assertEqual(a[a.index("--audio-format") + 1], "flac")
        a = self.args(start="1:00", end="2:00")
        self.assertEqual(a[a.index("--download-sections") + 1], "*1:00-2:00")

    def test_single_video_never_pulls_a_playlist(self):
        self.assertIn("--no-playlist", self.args())
        self.assertIn("--yes-playlist", self.args(playlist=True))


class Progress(unittest.TestCase):
    def parse(self, line):
        job = {}
        core._progress(job, core.PROG_RE.match(line))
        return job

    def test_known_total(self):
        j = self.parse("PROG 52428800 209715200 NA 2000000 75 NA NA")
        self.assertEqual((j["percent"], j["indeterminate"], j["eta"]), (25.0, False, "1:15"))

    def test_unknown_total_is_indeterminate_not_zero_percent(self):
        j = self.parse("PROG 1180695552 NA NA 8400000.5 NA NA NA")
        self.assertTrue(j["indeterminate"])
        self.assertEqual(j["size"], "1.2 GB downloaded")

    def test_fragments(self):
        self.assertEqual(self.parse("PROG 3000000 NA NA 1500000 NA 12 240")["percent"], 5.0)


class ErrorClassification(unittest.TestCase):
    def test_login_messages(self):
        self.assertTrue(core.LOGIN_RE.search("Instagram sent an empty media response"))
        self.assertTrue(core.LOGIN_RE.search("Sign in to confirm your age"))

    def test_unreadable_browser_logins_are_not_a_login_problem(self):
        e = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
        self.assertTrue(core.BROWSER_COOKIE_ERR.search(e))
        self.assertFalse(core.LOGIN_RE.search(e))

    def test_drm(self):
        self.assertTrue(core._is_drm("https://www.netflix.com/watch/1"))
        self.assertTrue(core._is_drm("https://example.com/v", "ERROR: This video is DRM protected"))
        self.assertFalse(core._is_drm("https://imax.com/x"))


class Logins(unittest.TestCase):
    def test_engine_gets_a_copy_and_the_copy_is_removed(self):
        core.COOKIE_FILE.write_text("# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\ta\tb\n")
        try:
            a = core.common_args({"cookies": "app"})
            copy = Path(a[a.index("--cookies") + 1])
            self.assertNotEqual(copy, core.COOKIE_FILE)
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            copy.write_text("wiped by the site")  # what yt-dlp does when a session is cleared
            core.drop_cookie_copies(a)
            self.assertFalse(copy.exists())
            self.assertIn(".example.com", core.COOKIE_FILE.read_text())
        finally:
            core.COOKIE_FILE.unlink()

    def test_browser_logins_only_for_youtube_links(self):
        s = {"cookies": "app", "google_cookies": "chrome"}
        self.assertIn("--cookies-from-browser", core.common_args(s, "https://www.youtube.com/watch?v=x"))
        self.assertIn("--cookies-from-browser", core.common_args(s))  # search: no link yet
        self.assertNotIn("--cookies-from-browser", core.common_args(s, "https://www.instagram.com/p/x/"))
        self.assertNotIn("--cookies-from-browser", core.common_args(s, "https://notyoutube.com.evil.example/"))

    def test_unreadable_browser_is_remembered_and_skipped(self):
        s = {"cookies": "app", "google_cookies": "chrome"}
        a = core.common_args(s, "https://youtu.be/x")
        err = 'ERROR: could not find chrome cookies database in "/Users/x/Library/Application Support/Google/Chrome"'
        try:
            self.assertFalse(core.browser_login_failed(a, "ERROR: Video unavailable"))
            self.assertTrue(core.browser_login_failed(a, err))
            self.assertNotIn("--cookies-from-browser", core.common_args(s, "https://youtu.be/x"))
            self.assertNotIn("--cookies-from-browser", core.common_args({"cookies": "chrome"}))
        finally:
            core._browser_blocked.clear()


class Engine(unittest.TestCase):
    def test_version_compare(self):
        self.assertGreater(core._vkey("2026.11.23"), core._vkey("2026.10.21"))
        self.assertGreater(core._vkey("2026.11.23.1"), core._vkey("2026.11.23"))

    def test_package_version_is_read_without_importing(self):
        d = Path(tempfile.mkdtemp(dir=_HOME))
        (d / "yt_dlp").mkdir()
        (d / "yt_dlp" / "version.py").write_text("__version__ = '2026.11.23'\n")
        self.assertEqual(core._pkg_version(d), "2026.11.23")
        self.assertEqual(core._pkg_version(d / "missing"), "")


class Database(unittest.TestCase):
    def setUp(self):
        core.init_db()
        self.q = QuietQueue()

    def test_only_the_server_marks_jobs_interrupted(self):
        j = fake_job(self.q, "downloading")
        core.init_db()  # what the AI bridge does while the app is downloading
        self.assertEqual(core.job_from_db(j["id"])["status"], "downloading")
        core.init_db(mark_interrupted=True)  # what the server does at startup
        self.assertEqual(core.job_from_db(j["id"])["status"], "interrupted")

    def test_deleting_a_waiting_job_does_not_deadlock(self):
        j = fake_job(self.q, "queued")
        t = threading.Thread(target=core.delete_jobs, args=(self.q, [j["id"]]), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "delete_jobs hung")
        self.assertIsNone(core.job_from_db(j["id"]))
        self.assertNotIn(j["id"], self.q.jobs)

    def test_a_deleted_job_is_not_written_back(self):
        j = fake_job(self.q, "downloading")
        core.delete_jobs(self.q, [j["id"]])
        core._save_job(j)  # the download thread finishing after the user removed it
        self.assertIsNone(core.job_from_db(j["id"]))

    def test_get_finds_old_history_rows(self):
        j = fake_job(self.q, "done")
        del self.q.jobs[j["id"]]
        self.assertEqual(self.q.get(j["id"])["id"], j["id"])


class QuitAndScheduler(unittest.TestCase):
    def setUp(self):
        core.init_db()

    def test_quit_stops_running_and_paused_downloads(self):
        q = QuietQueue()
        procs = [subprocess.Popen(["sleep", "60"], start_new_session=True) for _ in range(2)]
        running = fake_job(q, "downloading", proc=procs[0])
        paused = fake_job(q, "paused", proc=procs[1])
        os.killpg(procs[1].pid, signal.SIGSTOP)
        done = fake_job(q, "done")
        self.assertEqual(q.shutdown(), 2)
        for p in procs:
            self.assertIsNotNone(p.wait(5))
        self.assertEqual(core.job_from_db(running["id"])["status"], "interrupted")
        self.assertEqual(core.job_from_db(paused["id"])["status"], "interrupted")
        self.assertEqual(core.job_from_db(done["id"])["status"], "done")

    def test_scheduler_survives_an_error(self):
        started = threading.Event()

        class Q(core.Queue):
            def _run(self, job):
                job["status"] = "done"
                started.set()

        real, calls = core.get_settings, []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return real()

        core.get_settings = flaky
        try:
            q = Q()
            time.sleep(1.3)  # scheduler hits the error at least twice
        finally:
            core.get_settings = real
        fake_job(q, "queued")
        with q.cv:
            q.cv.notify_all()
        self.assertTrue(started.wait(5), "scheduler died after an error")


class Files(unittest.TestCase):
    def test_direct_link_names_are_tidied(self):
        d = Path(tempfile.mkdtemp(dir=_HOME))
        ugly = d / "17970648-1080p [17970648-1080p.mp4？dload=SITE - [abc] Title (1080)].mp4"
        ugly.write_text("x")
        self.assertEqual(Path(core.tidy_name(str(ugly))).name, "17970648-1080p.mp4")
        again = d / "17970648-1080p [other].mp4"
        again.write_text("y")
        self.assertEqual(Path(core.tidy_name(str(again))).name, "17970648-1080p (2).mp4")

    def test_thumbnail_lookup_rejects_odd_ids(self):
        self.assertEqual(core.thumb_bytes("../token"), (None, ""))
        self.assertEqual(core.thumb_bytes(""), (None, ""))
        self.assertEqual(core._img_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(core._img_type(b"<html>"), "")

    def test_transcript_parsing_drops_rolling_duplicates(self):
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello there\n\n"
               "00:00:03.000 --> 00:00:05.000\nhello there\ngeneral <c>kenobi</c>\n")
        self.assertEqual(core._parse_vtt(vtt), [(1, "hello there"), (3, "general kenobi")])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not found")
class Mp4(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=_HOME))

    def test_h264_is_rewrapped_without_reencoding(self):
        src = self.d / "a.mkv"
        make_clip(src, "h264")
        out = Path(core.to_mp4(str(src)))
        self.assertEqual(out.suffix, ".mp4")
        self.assertFalse(src.exists())
        self.assertEqual(video_stream(out)["codec_name"], "h264")
        self.assertAlmostEqual(core.probe_duration(out), 2.0, delta=0.3)

    def test_vp9_stays_mkv_unless_converting(self):
        src = self.d / "b.mkv"
        make_clip(src, "vp9")
        self.assertEqual(core.to_mp4(str(src)), str(src))
        self.assertTrue(src.exists())

    def test_existing_mp4_is_never_overwritten(self):
        src = self.d / "c.mkv"
        make_clip(src, "h264")
        (self.d / "c.mp4").write_text("mine")
        self.assertEqual(core.to_mp4(str(src)), str(src))
        self.assertEqual((self.d / "c.mp4").read_text(), "mine")

    @unittest.skipUnless(HAVE_FFMPEG and core._has_hevc_encoder(), "no hardware HEVC encoder")
    def test_vp9_converts_to_hevc_with_progress(self):
        src = self.d / "d.mkv"
        make_clip(src, "vp9", seconds=3)
        seen, procs = [], []
        out = Path(core.to_mp4(str(src), convert=True, on_progress=seen.append, on_proc=procs.append))
        self.assertEqual(out.suffix, ".mp4")
        v = video_stream(out)
        self.assertEqual(v["codec_name"], "hevc")
        self.assertEqual(v["pix_fmt"], "yuv420p")
        self.assertEqual(seen[0], 0.0)
        self.assertEqual(len(procs), 1)
        self.assertFalse((self.d / "d.part.mp4").exists())

    @unittest.skipUnless(HAVE_FFMPEG and core._has_hevc_encoder(), "no hardware HEVC encoder")
    def test_hdr_stays_10_bit_and_keeps_its_colour_tags(self):
        src = self.d / "e.mkv"
        make_clip(src, "vp9", ten_bit=True)
        self.assertIn("10", video_stream(src)["pix_fmt"])
        out = core.to_mp4(str(src), convert=True)
        v = video_stream(out)
        self.assertEqual(v["codec_name"], "hevc")
        self.assertIn("10", v["pix_fmt"])
        self.assertEqual((v.get("color_transfer"), v.get("color_primaries")), ("smpte2084", "bt2020"))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1, exit=False)
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
