import re

import pytest

from app.splitter import build_ffmpeg_cmd, part_filename, sanitize_filename


def test_sanitize_removes_forbidden_chars():
    name = sanitize_filename('AC/DC: "Hells Bells"?')
    assert not re.search(r'[\\/:*?"<>|]', name)
    assert name


def test_sanitize_collapses_whitespace_and_strips_dots():
    assert sanitize_filename("  My  Song.  ") == "My Song"


def test_sanitize_truncates():
    assert len(sanitize_filename("x" * 200)) == 80


def test_sanitize_empty_becomes_untitled():
    assert sanitize_filename("...") == "untitled"


def test_part_filename():
    assert part_filename(1, "Intro", "mp3") == "01 - Intro.mp3"
    assert part_filename(12, "A/B", "opus") == "12 - A_B.opus"


def test_mp3_cmd_transcodes_audio_only():
    cmd = build_ffmpeg_cmd("in.m4a", "out.mp3", 5.0, 15.0, "mp3")
    assert cmd[0] == "ffmpeg"
    assert "libmp3lame" in cmd and "-vn" in cmd
    assert cmd[cmd.index("-ss") + 1] == "5.000"
    assert cmd[cmd.index("-t") + 1] == "10.000"
    assert cmd[-1] == "out.mp3"


@pytest.mark.parametrize("fmt", ["m4a", "opus"])
def test_audio_copy_cmd(fmt):
    cmd = build_ffmpeg_cmd("in", f"out.{fmt}", 0, 10, fmt)
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_mp4_fast_copies_streams():
    cmd = build_ffmpeg_cmd("in.mp4", "out.mp4", 0, 10, "mp4", "fast")
    assert cmd[cmd.index("-c") + 1] == "copy"


def test_mp4_exact_reencodes():
    cmd = build_ffmpeg_cmd("in.mp4", "out.mp4", 0, 10, "mp4", "exact")
    assert "libx264" in cmd and "copy" not in cmd


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        build_ffmpeg_cmd("in", "out.xyz", 0, 1, "xyz")


def test_sanitize_caps_utf8_bytes():
    out = sanitize_filename("😀" * 80)
    assert len(out.encode("utf-8")) <= 120
    assert out.encode("utf-8").decode("utf-8") == out
    assert out


def test_sanitize_removes_control_and_format_chars():
    out = sanitize_filename("a‮b\x7fc​d")
    assert "‮" not in out and "\x7f" not in out and "​" not in out
    assert out == "ab_cd"
