import pytest

from app.downloader import download, fetch_info
from app.splitter import split_all
from app.timecodes import build_segments, parse_timecodes

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # «Me at the zoo», ~19 с


@pytest.mark.network
def test_real_video_mp3_split(tmp_path):
    info = fetch_info(URL)
    segments = build_segments(parse_timecodes("0:00 Start\n0:10 End"), info.duration)
    src = download(URL, "mp3", 1080, tmp_path / "src")
    paths = split_all(src, segments, "mp3", tmp_path / "out")
    assert [p.name for p in paths] == ["01 - Start.mp3", "02 - End.mp3"]
    assert all(p.stat().st_size > 1000 for p in paths)
