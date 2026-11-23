import pytest

from app.timecodes import (
    Segment,
    TimecodeError,
    build_segments,
    chapters_to_segments,
    parse_timecodes,
)


def test_parses_basic_lines():
    text = "0:00 Intro\n3:45 Song\n1:02:03 Outro"
    assert parse_timecodes(text) == [(0.0, "Intro"), (225.0, "Song"), (3723.0, "Outro")]


@pytest.mark.parametrize("line", [
    "0:00 - Intro",
    "0:00 – Intro",
    "0:00 — Intro",
    "01. 0:00 Intro",
    "2) 0:00 Intro",
    "[0:00] Intro",
    "(0:00) Intro",
    "0:00: Intro",
    "  0:00   Intro  ",
])
def test_separators_and_decorations(line):
    assert parse_timecodes(line) == [(0.0, "Intro")]


def test_missing_title_gets_default():
    assert parse_timecodes("0:00\n1:00 Two") == [(0.0, "Part 1"), (60.0, "Two")]


def test_skips_lines_without_timecode():
    assert parse_timecodes("Tracklist:\n0:00 A\n\nthanks for watching") == [(0.0, "A")]


def test_empty_text_returns_empty_list():
    assert parse_timecodes("") == []


@pytest.mark.parametrize("text", ["1:00 A\n0:30 B", "1:00 A\n1:00 B"])
def test_non_increasing_raises(text):
    with pytest.raises(TimecodeError):
        parse_timecodes(text)


@pytest.mark.parametrize("text", ["1:75 X", "1:75:00 X"])
def test_invalid_numbers_raise(text):
    with pytest.raises(TimecodeError):
        parse_timecodes(text)


def test_build_segments_ends_at_next_start_and_duration():
    segs = build_segments([(0.0, "A"), (60.0, "B")], 100.0)
    assert segs == [Segment(0.0, 60.0, "A"), Segment(60.0, 100.0, "B")]


def test_build_segments_start_beyond_duration_raises():
    with pytest.raises(TimecodeError):
        build_segments([(0.0, "A"), (100.0, "B")], 100.0)


def test_build_segments_empty_raises():
    with pytest.raises(TimecodeError):
        build_segments([], 100.0)


def test_chapters_to_segments():
    chapters = [
        {"start_time": 0, "end_time": 60, "title": "A"},
        {"start_time": 60, "end_time": 100.5, "title": "B"},
    ]
    assert chapters_to_segments(chapters) == [Segment(0.0, 60.0, "A"), Segment(60.0, 100.5, "B")]


def test_chapters_to_segments_empty_or_none():
    assert chapters_to_segments([]) == []
    assert chapters_to_segments(None) == []


@pytest.mark.parametrize("line", ["12:345 foo", "5:00:3 x", "12:30:45:10 x"])
def test_malformed_timestamp_boundary_is_skipped(line):
    assert parse_timecodes(line) == []
