import pytest

from app.errors import AppError
from app.splitter import split_all
from app.timecodes import Segment
from tests.media import make_audio, make_video, probe_duration

SEGMENTS = [Segment(0.0, 4.0, "One"), Segment(4.0, 10.0, "Two")]


def test_mp3_split(tmp_path):
    src = make_audio(tmp_path / "src.m4a", "aac")
    progress = []
    paths = split_all(src, SEGMENTS, "mp3", tmp_path / "out", on_progress=progress.append)
    assert [p.name for p in paths] == ["01 - One.mp3", "02 - Two.mp3"]
    assert probe_duration(paths[0]) == pytest.approx(4.0, abs=0.3)
    assert probe_duration(paths[1]) == pytest.approx(6.0, abs=0.3)
    assert progress == [0.5, 1.0]


def test_m4a_copy_split(tmp_path):
    src = make_audio(tmp_path / "src.m4a", "aac")
    paths = split_all(src, SEGMENTS, "m4a", tmp_path / "out")
    assert [p.suffix for p in paths] == [".m4a", ".m4a"]
    assert probe_duration(paths[0]) == pytest.approx(4.0, abs=0.5)


def test_opus_copy_split(tmp_path):
    src = make_audio(tmp_path / "src.webm", "libopus")
    paths = split_all(src, SEGMENTS, "opus", tmp_path / "out")
    assert [p.suffix for p in paths] == [".opus", ".opus"]
    assert probe_duration(paths[1]) == pytest.approx(6.0, abs=0.5)


def test_mp4_fast_split(tmp_path):
    src = make_video(tmp_path / "src.mp4")
    paths = split_all(src, [Segment(2.0, 6.0, "Mid")], "mp4", tmp_path / "out", video_mode="fast")
    assert probe_duration(paths[0]) == pytest.approx(4.0, abs=1.0)


def test_mp4_exact_split(tmp_path):
    src = make_video(tmp_path / "src.mp4")
    paths = split_all(src, [Segment(2.0, 6.0, "Mid")], "mp4", tmp_path / "out", video_mode="exact")
    assert probe_duration(paths[0]) == pytest.approx(4.0, abs=0.5)


def test_missing_source_raises_split_failed(tmp_path):
    with pytest.raises(AppError) as exc:
        split_all(tmp_path / "nope.m4a", SEGMENTS, "mp3", tmp_path / "out")
    assert exc.value.code == "SPLIT_FAILED"
