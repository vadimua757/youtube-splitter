from app.downloader import VideoInfo
from app.errors import AppError
from app.jobs import JobStore, run_job
from app.splitter import part_filename

CHAPTERS = [
    {"start_time": 0, "end_time": 40, "title": "Ch1"},
    {"start_time": 40, "end_time": 100, "title": "Ch2"},
]


class Harness:
    def __init__(self, tmp_path, info=None, download_errors=None, fetch_error=None):
        self.tmp = tmp_path
        self.store = JobStore(tmp_path / "jobs.db")
        self.info = info or VideoInfo("abc", "My Video", 100.0, CHAPTERS)
        self.download_errors = list(download_errors or [])
        self.fetch_error = fetch_error
        self.download_calls = 0

    def fetch_info(self, url):
        if self.fetch_error:
            raise self.fetch_error
        return self.info

    def download(self, url, fmt, quality, dest_dir, progress_cb=None):
        self.download_calls += 1
        if self.download_errors:
            raise self.download_errors.pop(0)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "source.m4a"
        path.write_bytes(b"x")
        if progress_cb:
            progress_cb(0.5)
        return path

    def split(self, src, segments, fmt, out_dir, video_mode="fast", on_progress=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, seg in enumerate(segments, 1):
            p = out_dir / part_filename(i, seg.title, fmt)
            p.write_bytes(b"y")
            paths.append(p)
        if on_progress:
            on_progress(1.0)
        return paths

    def run(self, request, **kwargs):
        full = {"url": "https://youtu.be/abc", "format": "mp3", "timecodes": None,
                "use_chapters": False, "video_quality": 1080, "video_mode": "fast", **request}
        job_id = self.store.create(full)
        run_job(job_id, self.store, self.tmp, fetch_info=self.fetch_info,
                download=self.download, split=self.split, **kwargs)
        return self.store.get(job_id)


def test_explicit_timecodes(tmp_path):
    h = Harness(tmp_path)
    job = h.run({"timecodes": "0:00 Intro\n0:30 Song"})
    assert job["status"] == "done" and job["progress"] == 1.0
    assert job["title"] == "My Video"
    assert job["parts"][0] == {"n": 1, "title": "Intro", "start": 0.0, "end": 30.0, "file": "01 - Intro.mp3"}
    assert job["parts"][1]["end"] == 100.0
    assert not (tmp_path / "jobs" / job["id"] / "src").exists()


def test_whole_file_when_nothing_specified(tmp_path):
    job = Harness(tmp_path).run({})
    assert [p["title"] for p in job["parts"]] == ["My Video"]
    assert (job["parts"][0]["start"], job["parts"][0]["end"]) == (0.0, 100.0)


def test_chapters_used_when_requested(tmp_path):
    job = Harness(tmp_path).run({"use_chapters": True})
    assert [p["title"] for p in job["parts"]] == ["Ch1", "Ch2"]


def test_chapters_requested_but_video_has_none(tmp_path):
    h = Harness(tmp_path, info=VideoInfo("abc", "My Video", 100.0, []))
    assert [p["title"] for p in h.run({"use_chapters": True})["parts"]] == ["My Video"]


def test_explicit_timecodes_beat_chapters(tmp_path):
    job = Harness(tmp_path).run({"timecodes": "0:00 Only", "use_chapters": True})
    assert [p["title"] for p in job["parts"]] == ["Only"]


def test_timecode_beyond_duration_fails(tmp_path):
    job = Harness(tmp_path).run({"timecodes": "0:00 A\n5:00 B"})
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_TIMECODES"


def test_unavailable_video_fails_without_download(tmp_path):
    h = Harness(tmp_path, fetch_error=AppError("VIDEO_UNAVAILABLE", "private"))
    job = h.run({})
    assert job["status"] == "failed"
    assert job["error"] == {"code": "VIDEO_UNAVAILABLE", "message": "private"}
    assert h.download_calls == 0


def test_download_retried_once(tmp_path):
    h = Harness(tmp_path, download_errors=[AppError("DOWNLOAD_FAILED", "flaky")])
    job = h.run({})
    assert job["status"] == "done"
    assert h.download_calls == 2


def test_download_fails_after_one_retry(tmp_path):
    h = Harness(tmp_path, download_errors=[AppError("DOWNLOAD_FAILED", "a"), AppError("DOWNLOAD_FAILED", "b")])
    job = h.run({})
    assert job["status"] == "failed" and job["error"]["code"] == "DOWNLOAD_FAILED"
    assert h.download_calls == 2


def test_other_errors_not_retried(tmp_path):
    h = Harness(tmp_path, download_errors=[AppError("VIDEO_UNAVAILABLE", "x")])
    job = h.run({})
    assert job["error"]["code"] == "VIDEO_UNAVAILABLE"
    assert h.download_calls == 1


def test_too_long_video_rejected(tmp_path):
    h = Harness(tmp_path, info=VideoInfo("abc", "Long", 7200.0, []))
    job = h.run({}, max_duration_min=60)
    assert job["status"] == "failed" and job["error"]["code"] == "TOO_LONG"
    assert h.download_calls == 0


def test_unexpected_exception_becomes_internal_error(tmp_path):
    h = Harness(tmp_path)
    h.fetch_info = lambda url: (_ for _ in ()).throw(RuntimeError("boom"))
    job = h.run({})
    assert job["status"] == "failed" and job["error"]["code"] == "INTERNAL_ERROR"


def test_run_job_missing_row_returns_quietly(tmp_path):
    h = Harness(tmp_path)
    run_job("missing", h.store, tmp_path, fetch_info=h.fetch_info, download=h.download, split=h.split)
    assert h.download_calls == 0
