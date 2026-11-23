import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from app.downloader import VideoInfo, build_ydl_opts, canonical_url, download, fetch_info, validate_url
from app.errors import AppError


def make_factory(info=None, exc=None, seen=None):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            if seen is not None:
                seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            if exc:
                raise exc
            return info

    return FakeYDL


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "https://youtu.be/abc",
    "http://music.youtube.com/watch?v=x",
    "https://m.youtube.com/watch?v=x",
])
def test_validate_url_accepts_youtube(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url", [
    "https://evil.com/watch?v=abc",
    "ftp://youtube.com/x",
    "https://youtube.com.evil.com/x",
    "https://youtube.com@evil.com/x",
    "not a url",
    "",
])
def test_validate_url_rejects_others(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_fetch_info_maps_fields():
    raw = {"id": "abc", "title": "T", "duration": 120,
           "chapters": [{"start_time": 0, "end_time": 60, "title": "A"}]}
    info = fetch_info("https://youtu.be/abc", ydl_factory=make_factory(info=raw))
    assert info == VideoInfo("abc", "T", 120.0, [{"start_time": 0, "end_time": 60, "title": "A"}])


def test_fetch_info_none_chapters_become_empty_list():
    raw = {"id": "abc", "title": "T", "duration": 10, "chapters": None}
    assert fetch_info("u", ydl_factory=make_factory(info=raw)).chapters == []


def test_fetch_info_maps_ytdlp_error_to_unavailable():
    factory = make_factory(exc=YtDlpDownloadError("Private video"))
    with pytest.raises(AppError) as exc:
        fetch_info("u", ydl_factory=factory)
    assert exc.value.code == "VIDEO_UNAVAILABLE"


@pytest.mark.parametrize("raw", [
    {"id": "a", "title": "T", "duration": None},
    {"id": "a", "title": "T", "duration": 10, "is_live": True},
])
def test_fetch_info_rejects_live_or_unknown_duration(raw):
    with pytest.raises(AppError) as exc:
        fetch_info("u", ydl_factory=make_factory(info=raw))
    assert exc.value.code == "VIDEO_UNAVAILABLE"


@pytest.mark.parametrize("fmt,expected", [
    ("mp3", "bestaudio/best"),
    ("m4a", "bestaudio[ext=m4a]"),
    ("opus", "bestaudio[acodec=opus]"),
])
def test_audio_format_selectors(fmt, expected, tmp_path):
    opts = build_ydl_opts(fmt, 1080, tmp_path)
    assert opts["format"] == expected
    assert opts["noplaylist"] is True
    assert "progress_hooks" not in opts


def test_mp4_opts(tmp_path):
    opts = build_ydl_opts("mp4", 720, tmp_path)
    assert "height<=720" in opts["format"]
    assert opts["merge_output_format"] == "mp4"


def test_progress_hook_reports_fraction(tmp_path):
    calls = []
    opts = build_ydl_opts("mp3", 1080, tmp_path, calls.append)
    hook = opts["progress_hooks"][0]
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
    hook({"status": "downloading", "downloaded_bytes": 10})   # без total — ігнорується
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    assert calls == [0.5]


def test_download_returns_filepath(tmp_path):
    f = tmp_path / "source.m4a"
    f.write_bytes(b"x")
    info = {"requested_downloads": [{"filepath": str(f)}]}
    seen = []
    result = download("u", "m4a", 1080, tmp_path, ydl_factory=make_factory(info=info, seen=seen))
    assert result == f
    assert seen[0]["outtmpl"].startswith(str(tmp_path))


def test_download_error_maps_to_download_failed(tmp_path):
    factory = make_factory(exc=YtDlpDownloadError("boom"))
    with pytest.raises(AppError) as exc:
        download("u", "mp3", 1080, tmp_path, ydl_factory=factory)
    assert exc.value.code == "DOWNLOAD_FAILED"


def test_download_without_file_maps_to_download_failed(tmp_path):
    with pytest.raises(AppError) as exc:
        download("u", "mp3", 1080, tmp_path, ydl_factory=make_factory(info={}))
    assert exc.value.code == "DOWNLOAD_FAILED"


@pytest.mark.parametrize("url", [
    "https://evil.com\\@youtube.com/x",
    "https://www.youtube.com/watch?v=a\\b",
    "https://user:pw@www.youtube.com/watch?v=abc",
    "https://user@youtu.be/abc",
    "https://@www.youtube.com/watch?v=abc",
])
def test_validate_url_rejects_backslash_and_userinfo(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_fetch_info_restricts_extractors():
    seen = []
    raw = {"id": "abc", "title": "T", "duration": 10}
    fetch_info("https://youtu.be/abc", ydl_factory=make_factory(info=raw, seen=seen))
    assert seen[0]["allowed_extractors"] == ["youtube"]


def test_build_ydl_opts_restricts_extractors(tmp_path):
    assert build_ydl_opts("mp3", 1080, tmp_path)["allowed_extractors"] == ["youtube"]


def test_fetch_info_rejects_playlist_result():
    raw = {"_type": "playlist", "id": "PL", "title": "T", "duration": 10}
    with pytest.raises(AppError) as ei:
        fetch_info("https://youtu.be/abc", ydl_factory=make_factory(info=raw))
    assert ei.value.code == "VIDEO_UNAVAILABLE"


def test_download_rejects_playlist_result(tmp_path):
    raw = {"_type": "playlist", "entries": []}
    with pytest.raises(AppError) as ei:
        download("https://youtu.be/abc", "mp3", 1080, tmp_path, ydl_factory=make_factory(info=raw))
    assert ei.value.code == "VIDEO_UNAVAILABLE"


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=4YBGRGBj7_w&list=RD4YBGRGBj7_w&start_radio=1",
     "https://www.youtube.com/watch?v=4YBGRGBj7_w"),
    ("https://www.youtube.com/watch?v=abc&t=30s", "https://www.youtube.com/watch?v=abc"),
    ("https://youtu.be/abc?si=xyz&list=PL1", "https://www.youtube.com/watch?v=abc"),
    ("https://music.youtube.com/watch?v=abc&list=RDAMVMabc", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/shorts/abc", "https://www.youtube.com/watch?v=abc"),
    ("https://m.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
])
def test_canonical_url_keeps_only_video_id(url, expected):
    assert canonical_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PL1",
    "https://www.youtube.com/@channel",
    "https://www.youtube.com/watch?list=PL1",
    "https://www.youtube.com/watch?v=",
    "https://youtu.be/",
])
def test_canonical_url_rejects_playlists_and_channels(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_js_runtime_enabled_for_available_binaries(monkeypatch, tmp_path):
    monkeypatch.setattr("app.downloader.shutil.which", lambda n: f"/usr/bin/{n}" if n == "node" else None)
    assert build_ydl_opts("mp3", 1080, tmp_path)["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}
    seen = []
    raw = {"id": "a", "title": "T", "duration": 10}
    fetch_info("u", ydl_factory=make_factory(info=raw, seen=seen))
    assert seen[0]["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}


def test_no_js_runtimes_key_when_none_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("app.downloader.shutil.which", lambda n: None)
    assert "js_runtimes" not in build_ydl_opts("mp3", 1080, tmp_path)
