from __future__ import annotations

import re
from dataclasses import dataclass


class TimecodeError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    title: str


_LINE = re.compile(
    r"""^\s*
    (?:\d+[.)]\s+)?                       # необовʼязкова нумерація: "01. " або "2) "
    [\[(]?
    (?P<ts>(?:\d{1,2}:)?\d{1,2}:\d{2})    # m:ss або h:mm:ss
    (?!\d|:\d)                            # без зайвих цифр/секцій після таймкоду
    [\])]?
    \s*
    (?:[-–—:]\s*)?                        # роздільник
    (?P<title>.*?)
    \s*$""",
    re.VERBOSE,
)


def _to_seconds(ts: str) -> float:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 3:
        h, m, s = parts
        if m >= 60:
            raise TimecodeError(f"invalid timecode: {ts}")
    else:
        h = 0
        m, s = parts
    if s >= 60:
        raise TimecodeError(f"invalid timecode: {ts}")
    return float(h * 3600 + m * 60 + s)


def parse_timecodes(text: str) -> list[tuple[float, str]]:
    entries: list[tuple[float, str]] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        start = _to_seconds(match["ts"])
        title = match["title"].strip() or f"Part {len(entries) + 1}"
        if entries and start <= entries[-1][0]:
            raise TimecodeError(f"timecodes must strictly increase: {match['ts']}")
        entries.append((start, title))
    return entries


def build_segments(entries: list[tuple[float, str]], duration: float) -> list[Segment]:
    if not entries:
        raise TimecodeError("no timecodes found")
    segments = []
    for i, (start, title) in enumerate(entries):
        if start >= duration:
            raise TimecodeError(f"timecode {start:.0f}s is beyond video duration {duration:.0f}s")
        end = entries[i + 1][0] if i + 1 < len(entries) else duration
        segments.append(Segment(start, end, title))
    return segments


def chapters_to_segments(chapters: list[dict] | None) -> list[Segment]:
    return [
        Segment(float(c["start_time"]), float(c["end_time"]), c.get("title") or f"Part {i}")
        for i, c in enumerate(chapters or [], 1)
    ]
