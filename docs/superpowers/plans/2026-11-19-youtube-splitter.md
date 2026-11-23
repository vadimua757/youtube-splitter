# YouTube Splitter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сервіс на FastAPI, який завантажує відео/аудіо з YouTube і за таймкодами з назвами (текст або chapters) ріже результат на фрагменти.

**Architecture:** Один процес FastAPI. `POST /jobs` створює завдання в SQLite і віддає його вбудованому `JobRunner` (пул потоків). Воркер виконує конвеєр `fetch_info → download (yt-dlp) → split (ffmpeg)` і складає фрагменти на диск у `data/jobs/<id>/out`. Усі зовнішні виклики (`yt-dlp`, `ffmpeg`) інʼєктуються, тому конвеєр тестується без мережі.

**Tech Stack:** Python 3.11+ (локально 3.14), FastAPI, uvicorn, yt-dlp (як бібліотека), ffmpeg/ffprobe (підпроцес), SQLite (`sqlite3`), pytest, httpx (для `TestClient`).

Спека: `docs/superpowers/specs/2026-11-19-youtube-splitter-design.md`.

## Примітки до виконання

- **Git не використовується** (рішення користувача). У планах немає кроків commit; замість них після кожного завдання запускається повний набір тестів.
- **beads (`bd`) недоступний** на цій машині, тому кроки з трекером завдань пропущені.
- **Перед використанням yt-dlp та FastAPI звіряйтеся з актуальною документацією** (вимога CLAUDE.md: Context7 `resolve-library-id` → `query-docs`, якщо інструмент доступний у сесії; інакше офіційні docs). Особливо: опції `format`, `merge_output_format`, `progress_hooks`, `requested_downloads` у yt-dlp та `lifespan` у FastAPI.
- **Відхилення від спеки (дрібні):** (1) унікальність імен файлів забезпечує числовий префікс `NN - `, окремий суфікс для дублікатів не потрібен; (2) повтор при збої застосовується лише до завантаження (збій `ffmpeg` не тимчасовий).
- Усі команди виконуються з кореня проєкту `/Users/vadim/PhpstormProjects/youtube-splitter` з активованим venv (`source .venv/bin/activate`).

## Структура файлів

```
pyproject.toml              конфіг pytest
requirements.txt            runtime-залежності
requirements-dev.txt        + pytest, httpx
app/__init__.py
app/errors.py               AppError(code, message)
app/config.py               Settings.from_env
app/timecodes.py            парсинг таймкодів, Segment, build_segments, chapters_to_segments
app/splitter.py             sanitize_filename, part_filename, build_ffmpeg_cmd, split_all
app/downloader.py           validate_url, VideoInfo, fetch_info, build_ydl_opts, download
app/jobs.py                 JobStore, run_job, JobRunner, cleanup_expired, start_cleanup_loop
app/ratelimit.py            RateLimiter
app/api.py                  create_app (FastAPI)
static/index.html           веб-форма
tests/__init__.py
tests/media.py              генерація тестових медіафайлів через ffmpeg
tests/test_*.py
Dockerfile, docker-compose.yml, README.md
```

---

### Task 0: Каркас проєкту, `errors`, `config`

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `app/__init__.py`, `app/errors.py`, `app/config.py`, `tests/__init__.py`, `tests/test_config.py`

- [ ] **Step 1: Створити файли залежностей і конфіг pytest**

`requirements.txt`:
```
fastapi>=0.115
uvicorn[standard]>=0.30
yt-dlp
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
httpx>=0.27
```

`pyproject.toml`:
```toml
[project]
name = "youtube-splitter"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-m 'not network'"
markers = ["network: hits real YouTube (deselected by default)"]
```

`app/__init__.py` і `tests/__init__.py` — порожні файли.

- [ ] **Step 2: Створити venv і встановити залежності**

Run:
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -q -r requirements-dev.txt && python -c "import fastapi, yt_dlp, httpx, pytest; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Написати падаючий тест для config**

`tests/test_config.py`:
```python
from pathlib import Path

from app.config import Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.data_dir == Path("data")
    assert s.api_key is None
    assert s.max_duration_min == 0
    assert s.max_concurrent_jobs == 2
    assert s.file_ttl_hours == 0
    assert s.rate_limit_per_min == 0


def test_overrides():
    s = Settings.from_env({
        "DATA_DIR": "/tmp/x",
        "API_KEY": "k",
        "MAX_DURATION_MIN": "90",
        "MAX_CONCURRENT_JOBS": "4",
        "FILE_TTL_HOURS": "1.5",
        "RATE_LIMIT_PER_MIN": "10",
    })
    assert s.data_dir == Path("/tmp/x")
    assert s.api_key == "k"
    assert s.max_duration_min == 90
    assert s.max_concurrent_jobs == 4
    assert s.file_ttl_hours == 1.5
    assert s.rate_limit_per_min == 10


def test_empty_api_key_means_disabled():
    assert Settings.from_env({"API_KEY": ""}).api_key is None
```

- [ ] **Step 4: Запустити тест, переконатися що падає**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.config'`)

- [ ] **Step 5: Реалізація**

`app/errors.py`:
```python
class AppError(Exception):
    """Помилка конвеєра з машинним кодом (VIDEO_UNAVAILABLE, SPLIT_FAILED, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
```

`app/config.py`:
```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    api_key: str | None = None
    max_duration_min: int = 0       # 0 = без ліміту
    max_concurrent_jobs: int = 2
    file_ttl_hours: float = 0       # 0 = не видаляти
    rate_limit_per_min: int = 0     # 0 = вимкнено

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("DATA_DIR") or "data"),
            api_key=env.get("API_KEY") or None,
            max_duration_min=int(env.get("MAX_DURATION_MIN") or 0),
            max_concurrent_jobs=int(env.get("MAX_CONCURRENT_JOBS") or 2),
            file_ttl_hours=float(env.get("FILE_TTL_HOURS") or 0),
            rate_limit_per_min=int(env.get("RATE_LIMIT_PER_MIN") or 0),
        )
```

- [ ] **Step 6: Запустити тест, переконатися що проходить**

Run: `pytest tests/test_config.py -v`
Expected: 3 passed

---

### Task 1: Парсинг таймкодів

**Files:**
- Create: `app/timecodes.py`, `tests/test_timecodes.py`

- [ ] **Step 1: Написати падаючі тести**

`tests/test_timecodes.py`:
```python
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
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_timecodes.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.timecodes'`)

- [ ] **Step 3: Реалізація**

`app/timecodes.py`:
```python
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
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_timecodes.py -v`
Expected: усі тести passed

- [ ] **Step 5: Повний набір**

Run: `pytest -q`
Expected: усі passed

---

### Task 2: Splitter — імена файлів і команди ffmpeg (чисті функції)

**Files:**
- Create: `app/splitter.py`, `tests/test_splitter_unit.py`

- [ ] **Step 1: Написати падаючі тести**

`tests/test_splitter_unit.py`:
```python
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
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_splitter_unit.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.splitter'`)

- [ ] **Step 3: Реалізація (без `split_all`, його додамо в Task 3)**

`app/splitter.py`:
```python
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from app.errors import AppError
from app.timecodes import Segment

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(title: str, max_len: int = 80) -> str:
    name = _FORBIDDEN.sub("_", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_len].strip(" .")
    return name or "untitled"


def part_filename(n: int, title: str, fmt: str) -> str:
    return f"{n:02d} - {sanitize_filename(title)}.{fmt}"


def build_ffmpeg_cmd(src, dst, start: float, end: float, fmt: str, video_mode: str = "fast") -> list[str]:
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    ss, dur = f"{start:.3f}", f"{end - start:.3f}"
    src, dst = str(src), str(dst)
    if fmt == "mp3":
        return base + ["-ss", ss, "-t", dur, "-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "2", dst]
    if fmt in ("m4a", "opus"):
        return base + ["-ss", ss, "-t", dur, "-i", src, "-vn", "-c:a", "copy", dst]
    if fmt == "mp4":
        if video_mode == "exact":
            return base + ["-i", src, "-ss", ss, "-t", dur,
                           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", dst]
        return base + ["-ss", ss, "-t", dur, "-i", src,
                       "-c", "copy", "-avoid_negative_ts", "make_zero", dst]
    raise ValueError(f"unsupported format: {fmt}")
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_splitter_unit.py -v`
Expected: усі passed

---

### Task 3: Splitter — `split_all` (інтеграція з реальним ffmpeg)

**Files:**
- Create: `tests/media.py`, `tests/test_splitter_integration.py`
- Modify: `app/splitter.py` (дописати `split_all` наприкінці)

- [ ] **Step 1: Хелпери генерації медіа**

`tests/media.py`:
```python
import subprocess
from pathlib import Path


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_audio(path: Path, codec: str, seconds: int = 10) -> Path:
    _ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", codec, str(path)])
    return path


def make_video(path: Path, seconds: int = 10) -> Path:
    _ffmpeg([
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])
    return path


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(out.strip())
```

- [ ] **Step 2: Написати падаючі тести**

`tests/test_splitter_integration.py`:
```python
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
```

- [ ] **Step 3: Запустити, переконатися що падає**

Run: `pytest tests/test_splitter_integration.py -v`
Expected: FAIL (`ImportError: cannot import name 'split_all'`)

- [ ] **Step 4: Дописати `split_all` у `app/splitter.py`**

```python
def split_all(
    src,
    segments: Sequence[Segment],
    fmt: str,
    out_dir,
    video_mode: str = "fast",
    on_progress: Callable[[float], None] | None = None,
) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, seg in enumerate(segments, 1):
        dst = out_dir / part_filename(i, seg.title, fmt)
        cmd = build_ffmpeg_cmd(src, dst, seg.start, seg.end, fmt, video_mode)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "")[-500:].strip()
            raise AppError("SPLIT_FAILED", tail or "ffmpeg failed") from e
        except FileNotFoundError as e:
            raise AppError("SPLIT_FAILED", "ffmpeg not found") from e
        paths.append(dst)
        if on_progress:
            on_progress(i / len(segments))
    return paths
```

- [ ] **Step 5: Запустити, переконатися що проходить**

Run: `pytest tests/test_splitter_integration.py tests/test_splitter_unit.py -v`
Expected: усі passed. Якщо `test_mp4_fast_split` виходить за допуск, збільшіть `abs` до 1.5: у режимі `fast` межі залежать від ключових кадрів (це очікувана поведінка за спекою).

---

### Task 4: Downloader (yt-dlp за інтерфейсом, тести без мережі)

**Files:**
- Create: `app/downloader.py`, `tests/test_downloader.py`

- [ ] **Step 1: Написати падаючі тести**

`tests/test_downloader.py`:
```python
import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from app.downloader import VideoInfo, build_ydl_opts, download, fetch_info, validate_url
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
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_downloader.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.downloader'`)

- [ ] **Step 3: Реалізація**

`app/downloader.py`:
```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import YoutubeDLError

from app.errors import AppError

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


@dataclass(frozen=True)
class VideoInfo:
    id: str
    title: str
    duration: float
    chapters: list[dict]


def validate_url(url: str) -> str:
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as e:
        raise ValueError("invalid url") from e
    if parsed.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise ValueError("only YouTube URLs are supported")
    return url


def _short(e: Exception) -> str:
    return str(e)[:500]


def fetch_info(url: str, ydl_factory=None) -> VideoInfo:
    factory = ydl_factory or yt_dlp.YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    try:
        with factory(opts) as ydl:
            raw = ydl.extract_info(url, download=False)
    except YoutubeDLError as e:
        raise AppError("VIDEO_UNAVAILABLE", _short(e)) from e
    if not raw or raw.get("is_live") or not raw.get("duration"):
        raise AppError("VIDEO_UNAVAILABLE", "live streams or videos without duration are not supported")
    return VideoInfo(
        id=raw["id"],
        title=raw.get("title") or raw["id"],
        duration=float(raw["duration"]),
        chapters=list(raw.get("chapters") or []),
    )


def build_ydl_opts(fmt: str, quality: int, dest_dir, progress_cb: Callable[[float], None] | None = None) -> dict:
    audio = {"mp3": "bestaudio/best", "m4a": "bestaudio[ext=m4a]", "opus": "bestaudio[acodec=opus]"}
    if fmt == "mp4":
        selector = (
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={quality}][ext=mp4]/best[height<={quality}]"
        )
    elif fmt in audio:
        selector = audio[fmt]
    else:
        raise ValueError(f"unsupported format: {fmt}")
    opts: dict = {
        "format": selector,
        "outtmpl": str(Path(dest_dir) / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if fmt == "mp4":
        opts["merge_output_format"] = "mp4"
    if progress_cb:
        def hook(d: dict) -> None:
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes")
            if total and done is not None:
                progress_cb(min(done / total, 1.0))
        opts["progress_hooks"] = [hook]
    return opts


def download(url: str, fmt: str, quality: int, dest_dir, progress_cb=None, ydl_factory=None) -> Path:
    factory = ydl_factory or yt_dlp.YoutubeDL
    opts = build_ydl_opts(fmt, quality, dest_dir, progress_cb)
    try:
        with factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"])
    except YoutubeDLError as e:
        raise AppError("DOWNLOAD_FAILED", _short(e)) from e
    except (KeyError, IndexError, TypeError) as e:
        raise AppError("DOWNLOAD_FAILED", "yt-dlp returned no file") from e
    if not path.exists():
        raise AppError("DOWNLOAD_FAILED", "downloaded file is missing")
    return path
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_downloader.py -v`
Expected: усі passed

---

### Task 5: `JobStore` і очищення прострочених завдань

**Files:**
- Create: `app/jobs.py`, `tests/test_job_store.py`

- [ ] **Step 1: Написати падаючі тести**

`tests/test_job_store.py`:
```python
import time

from app.jobs import JobStore, cleanup_expired, start_cleanup_loop


def test_create_and_get(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = store.create({"url": "u"})
    job = store.get(job_id)
    assert job["status"] == "queued"
    assert job["progress"] == 0
    assert job["request"] == {"url": "u"}
    assert job["parts"] == []
    assert job["error"] is None


def test_get_unknown_returns_none(tmp_path):
    assert JobStore(tmp_path / "jobs.db").get("nope") is None


def test_update_fields(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = store.create({})
    store.update(job_id, status="failed", progress=0.5, title="T",
                 error={"code": "X", "message": "m"}, parts=[{"n": 1}])
    job = store.get(job_id)
    assert (job["status"], job["progress"], job["title"]) == ("failed", 0.5, "T")
    assert job["error"] == {"code": "X", "message": "m"}
    assert job["parts"] == [{"n": 1}]


def test_update_rejects_unknown_field(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = store.create({})
    try:
        store.update(job_id, bogus=1)
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_cleanup_expired_removes_only_old_finished_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    old = store.create({}, created_at=0.0)
    new = store.create({}, created_at=10000.0)
    running = store.create({}, created_at=0.0)
    store.update(old, status="done")
    store.update(new, status="done")
    store.update(running, status="downloading")
    old_dir = tmp_path / "jobs" / old
    old_dir.mkdir(parents=True)
    (old_dir / "f.mp3").write_bytes(b"x")

    removed = cleanup_expired(store, tmp_path, ttl_hours=1, now=4000.0)

    assert removed == 1
    assert store.get(old) is None and not old_dir.exists()
    assert store.get(new) is not None
    assert store.get(running) is not None


def test_cleanup_loop_removes_expired_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = store.create({}, created_at=0.0)
    store.update(job_id, status="done")
    stop = start_cleanup_loop(store, tmp_path, ttl_hours=1, interval=0.05)
    try:
        deadline = time.time() + 3
        while store.get(job_id) is not None and time.time() < deadline:
            time.sleep(0.05)
        assert store.get(job_id) is None
    finally:
        stop()
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_job_store.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.jobs'`)

- [ ] **Step 3: Реалізація**

`app/jobs.py`:
```python
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import downloader, splitter
from app.errors import AppError
from app.timecodes import (
    Segment,
    TimecodeError,
    build_segments,
    chapters_to_segments,
    parse_timecodes,
)

log = logging.getLogger(__name__)

_UPDATABLE = {"status", "progress", "title", "parts", "error"}
_JSON_FIELDS = {"parts", "error"}


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, created_at REAL, status TEXT, progress REAL,
                    title TEXT, error TEXT, request TEXT, parts TEXT)"""
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, request: dict, created_at: float | None = None) -> str:
        job_id = uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, created_at, status, progress, request) VALUES (?,?,?,?,?)",
                (job_id, time.time() if created_at is None else created_at, "queued", 0.0, json.dumps(request)),
            )
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "progress": row["progress"],
            "title": row["title"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "request": json.loads(row["request"]),
            "parts": json.loads(row["parts"]) if row["parts"] else [],
        }

    def update(self, job_id: str, **fields) -> None:
        cols, vals = [], []
        for key, value in fields.items():
            if key not in _UPDATABLE:
                raise KeyError(key)
            cols.append(f"{key}=?")
            vals.append(json.dumps(value) if key in _JSON_FIELDS else value)
        if not cols:
            return
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", (*vals, job_id))

    def expired_ids(self, older_than: float) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM jobs WHERE created_at < ? AND status IN ('done','failed')", (older_than,)
            ).fetchall()
        return [r["id"] for r in rows]

    def delete(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def cleanup_expired(store: JobStore, data_dir, ttl_hours: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    ids = store.expired_ids(now - ttl_hours * 3600)
    for job_id in ids:
        shutil.rmtree(Path(data_dir) / "jobs" / job_id, ignore_errors=True)
        store.delete(job_id)
    return len(ids)


def start_cleanup_loop(store: JobStore, data_dir, ttl_hours: float, interval: float = 600.0) -> Callable[[], None]:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                cleanup_expired(store, data_dir, ttl_hours)
            except Exception:
                log.exception("cleanup failed")
            stop.wait(interval)

    threading.Thread(target=loop, daemon=True, name="job-cleanup").start()
    return stop.set
```

(Імпорти `downloader`, `splitter`, `ThreadPoolExecutor`, `Segment`, `TimecodeError`, `build_segments`, `chapters_to_segments`, `parse_timecodes`, `AppError` використовуються в Task 6–7.)

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_job_store.py -v`
Expected: усі passed

---

### Task 6: Конвеєр `run_job`

**Files:**
- Modify: `app/jobs.py` (дописати наприкінці)
- Create: `tests/test_run_job.py`

- [ ] **Step 1: Написати падаючі тести**

`tests/test_run_job.py`:
```python
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
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_run_job.py -v`
Expected: FAIL (`ImportError: cannot import name 'run_job'`)

- [ ] **Step 3: Дописати наприкінці `app/jobs.py`**

```python
def _choose_segments(req: dict, info: downloader.VideoInfo) -> list[Segment]:
    text = req.get("timecodes")
    try:
        if text and text.strip():
            return build_segments(parse_timecodes(text), info.duration)
        if req.get("use_chapters"):
            segments = chapters_to_segments(info.chapters)
            if segments:
                return segments
    except TimecodeError as e:
        raise AppError("INVALID_TIMECODES", str(e)) from e
    return [Segment(0.0, info.duration, info.title)]


def _download_with_retry(call):
    try:
        return call()
    except AppError as e:
        if e.code != "DOWNLOAD_FAILED":
            raise
        return call()


def run_job(job_id: str, store: JobStore, data_dir, *, max_duration_min: int = 0,
            fetch_info=None, download=None, split=None) -> None:
    fetch_info = fetch_info or downloader.fetch_info
    download = download or downloader.download
    split = split or splitter.split_all
    req = store.get(job_id)["request"]
    job_dir = Path(data_dir) / "jobs" / job_id
    src_dir, out_dir = job_dir / "src", job_dir / "out"
    fmt = req["format"]
    try:
        store.update(job_id, status="downloading", progress=0.0)
        info = fetch_info(req["url"])
        store.update(job_id, title=info.title)
        if max_duration_min and info.duration > max_duration_min * 60:
            raise AppError("TOO_LONG", f"video is longer than {max_duration_min} min")
        segments = _choose_segments(req, info)

        src_dir.mkdir(parents=True, exist_ok=True)

        def on_download(frac: float) -> None:
            store.update(job_id, progress=round(min(frac, 1.0) * 0.6, 3))

        src = _download_with_retry(
            lambda: download(req["url"], fmt, req.get("video_quality", 1080), src_dir, on_download)
        )

        store.update(job_id, status="splitting", progress=0.6)

        def on_split(frac: float) -> None:
            store.update(job_id, progress=round(0.6 + min(frac, 1.0) * 0.4, 3))

        paths = split(src, segments, fmt, out_dir, req.get("video_mode", "fast"), on_split)
        parts = [
            {"n": i, "title": seg.title, "start": seg.start, "end": seg.end, "file": path.name}
            for i, (seg, path) in enumerate(zip(segments, paths), 1)
        ]
        store.update(job_id, status="done", progress=1.0, parts=parts)
    except AppError as e:
        store.update(job_id, status="failed", error={"code": e.code, "message": e.message})
    except Exception as e:
        log.exception("job %s crashed", job_id)
        store.update(job_id, status="failed", error={"code": "INTERNAL_ERROR", "message": str(e)[:500]})
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_run_job.py -v`
Expected: усі passed

---

### Task 7: `JobRunner`

**Files:**
- Modify: `app/jobs.py` (дописати наприкінці)
- Create: `tests/test_job_runner.py`

- [ ] **Step 1: Написати падаючий тест**

`tests/test_job_runner.py`:
```python
from app.jobs import JobRunner, JobStore


class SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def test_submit_calls_job_fn_with_settings(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    calls = []

    def job_fn(job_id, store_, data_dir, **kwargs):
        calls.append((job_id, store_, data_dir, kwargs))

    runner = JobRunner(store, tmp_path, max_duration_min=30, executor=SyncExecutor(), job_fn=job_fn)
    runner.submit("abc")
    assert calls == [("abc", store, tmp_path, {"max_duration_min": 30})]


def test_default_executor_runs_job_in_background(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    done = []
    runner = JobRunner(store, tmp_path, max_workers=1, job_fn=lambda *a, **k: done.append(a[0]))
    runner.submit("xyz")
    runner._executor.shutdown(wait=True)
    assert done == ["xyz"]
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_job_runner.py -v`
Expected: FAIL (`ImportError: cannot import name 'JobRunner'`)

- [ ] **Step 3: Дописати наприкінці `app/jobs.py`**

```python
class JobRunner:
    def __init__(self, store: JobStore, data_dir, max_workers: int = 2, max_duration_min: int = 0,
                 executor=None, job_fn=None):
        self._store = store
        self._data_dir = data_dir
        self._max_duration_min = max_duration_min
        self._job_fn = job_fn or run_job
        self._executor = executor or ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")

    def submit(self, job_id: str) -> None:
        self._executor.submit(
            self._job_fn, job_id, self._store, self._data_dir, max_duration_min=self._max_duration_min
        )
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_job_runner.py -v && pytest -q`
Expected: усі passed

---

### Task 8: `RateLimiter`

**Files:**
- Create: `app/ratelimit.py`, `tests/test_ratelimit.py`

- [ ] **Step 1: Написати падаючий тест**

`tests/test_ratelimit.py`:
```python
from app.ratelimit import RateLimiter


def test_limits_per_key_within_window():
    now = [0.0]
    rl = RateLimiter(2, window=60.0, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")
    assert rl.allow("b")


def test_window_expires():
    now = [0.0]
    rl = RateLimiter(1, window=60.0, clock=lambda: now[0])
    assert rl.allow("a")
    assert not rl.allow("a")
    now[0] = 61.0
    assert rl.allow("a")
```

- [ ] **Step 2: Запустити, переконатися що падає**

Run: `pytest tests/test_ratelimit.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.ratelimit'`)

- [ ] **Step 3: Реалізація**

`app/ratelimit.py`:
```python
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True
```

- [ ] **Step 4: Запустити, переконатися що проходить**

Run: `pytest tests/test_ratelimit.py -v`
Expected: 2 passed

---

### Task 9: HTTP API

**Files:**
- Create: `app/api.py`, `static/index.html` (мінімальна заглушка на цьому кроці; повна версія в Task 10), `tests/test_api.py`

- [ ] **Step 1: Заглушка сторінки (потрібна тесту `GET /`)**

`static/index.html`:
```html
<!doctype html><html><head><meta charset="utf-8"><title>YouTube Splitter</title></head><body></body></html>
```

- [ ] **Step 2: Написати падаючі тести**

`tests/test_api.py`:
```python
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.timecodes import parse_timecodes

URL = "https://www.youtube.com/watch?v=abc"


class SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class NoopExecutor:
    def submit(self, fn, *args, **kwargs):
        pass


def fake_job(job_id, store, data_dir, **kwargs):
    req = store.get(job_id)["request"]
    count = len(parse_timecodes(req["timecodes"] or "")) or 1
    out = data_dir / "jobs" / job_id / "out"
    out.mkdir(parents=True)
    parts = []
    for n in range(1, count + 1):
        name = f"{n:02d} - T{n}.mp3"
        (out / name).write_bytes(f"data{n}".encode())
        parts.append({"n": n, "title": f"T{n}", "start": 0.0, "end": 1.0, "file": name})
    store.update(job_id, status="done", progress=1.0, title="My Video", parts=parts)


def make_client(tmp_path, executor=None, **settings):
    app = create_app(Settings(data_dir=tmp_path, **settings), executor=executor or SyncExecutor(), job_fn=fake_job)
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text


def test_create_and_get_job(client):
    r = client.post("/jobs", json={"url": URL, "timecodes": "0:00 A\n1:00 B"})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done" and job["progress"] == 1.0
    assert [p["file"] for p in job["parts"]] == ["01 - T1.mp3", "02 - T2.mp3"]
    assert job["error"] is None and job["title"] == "My Video"


@pytest.mark.parametrize("body", [
    {"url": "https://evil.com/x"},
    {"url": URL, "timecodes": "1:00 A\n0:30 B"},
    {"url": URL, "timecodes": "just words"},
    {"url": URL, "format": "wav"},
    {"url": URL, "video_quality": 5},
])
def test_invalid_requests_rejected(client, body):
    assert client.post("/jobs", json=body).status_code == 422


def test_unknown_job_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/files/1").status_code == 404
    assert client.get("/jobs/nope/download").status_code == 404


def test_file_download_and_bounds(client):
    job_id = client.post("/jobs", json={"url": URL, "timecodes": "0:00 A\n1:00 B"}).json()["job_id"]
    r = client.get(f"/jobs/{job_id}/files/2")
    assert r.status_code == 200 and r.content == b"data2"
    assert client.get(f"/jobs/{job_id}/files/0").status_code == 404
    assert client.get(f"/jobs/{job_id}/files/3").status_code == 404


def test_download_zip_for_multiple_parts(client):
    job_id = client.post("/jobs", json={"url": URL, "timecodes": "0:00 A\n1:00 B"}).json()["job_id"]
    r = client.get(f"/jobs/{job_id}/download")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["01 - T1.mp3", "02 - T2.mp3"]


def test_download_single_part_returns_file(client):
    job_id = client.post("/jobs", json={"url": URL}).json()["job_id"]
    r = client.get(f"/jobs/{job_id}/download")
    assert r.status_code == 200 and r.content == b"data1"


def test_files_before_done_conflict(tmp_path):
    c = make_client(tmp_path, executor=NoopExecutor())
    job_id = c.post("/jobs", json={"url": URL}).json()["job_id"]
    assert c.get(f"/jobs/{job_id}").json()["status"] == "queued"
    assert c.get(f"/jobs/{job_id}/files/1").status_code == 409
    assert c.get(f"/jobs/{job_id}/download").status_code == 409


def test_api_key_required_when_configured(tmp_path):
    c = make_client(tmp_path, api_key="secret")
    body = {"url": URL}
    assert c.post("/jobs", json=body).status_code == 401
    assert c.post("/jobs", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    ok = c.post("/jobs", json=body, headers={"X-API-Key": "secret"})
    assert ok.status_code == 202
    job_id = ok.json()["job_id"]
    assert c.get(f"/jobs/{job_id}").status_code == 401
    assert c.get(f"/jobs/{job_id}", headers={"X-API-Key": "secret"}).status_code == 200
    assert c.get("/").status_code == 200


def test_rate_limit(tmp_path):
    c = make_client(tmp_path, rate_limit_per_min=2)
    assert c.post("/jobs", json={"url": URL}).status_code == 202
    assert c.post("/jobs", json={"url": URL}).status_code == 202
    assert c.post("/jobs", json={"url": URL}).status_code == 429
```

- [ ] **Step 3: Запустити, переконатися що падає**

Run: `pytest tests/test_api.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.api'`)

- [ ] **Step 4: Реалізація**

`app/api.py`:
```python
from __future__ import annotations

import os
import secrets
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.downloader import validate_url
from app.jobs import JobRunner, JobStore, start_cleanup_loop
from app.ratelimit import RateLimiter
from app.splitter import sanitize_filename
from app.timecodes import TimecodeError, parse_timecodes

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class JobRequest(BaseModel):
    url: str
    format: Literal["mp3", "m4a", "opus", "mp4"] = "mp3"
    timecodes: str | None = None
    use_chapters: bool = False
    video_quality: int = Field(1080, ge=144, le=2160)
    video_mode: Literal["fast", "exact"] = "fast"


def create_app(settings: Settings | None = None, executor=None, job_fn=None) -> FastAPI:
    settings = settings or Settings.from_env()
    data_dir = Path(settings.data_dir)
    store = JobStore(data_dir / "jobs.db")
    runner = JobRunner(
        store, data_dir,
        max_workers=settings.max_concurrent_jobs,
        max_duration_min=settings.max_duration_min,
        executor=executor, job_fn=job_fn,
    )
    limiter = RateLimiter(settings.rate_limit_per_min) if settings.rate_limit_per_min else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = start_cleanup_loop(store, data_dir, settings.file_ttl_hours) if settings.file_ttl_hours else None
        yield
        if stop:
            stop()

    app = FastAPI(title="YouTube Splitter", lifespan=lifespan)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            return
        given = (x_api_key or "").encode()
        if not secrets.compare_digest(given, settings.api_key.encode()):
            raise HTTPException(401, "invalid api key")

    def get_job_or_404(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    def get_done_job(job_id: str) -> dict:
        job = get_job_or_404(job_id)
        if job["status"] != "done":
            raise HTTPException(409, f"job is {job['status']}")
        return job

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
    def create_job(body: JobRequest, request: Request):
        if limiter and not limiter.allow(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "too many requests")
        try:
            url = validate_url(body.url)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if body.timecodes and body.timecodes.strip():
            try:
                entries = parse_timecodes(body.timecodes)
            except TimecodeError as e:
                raise HTTPException(422, str(e)) from e
            if not entries:
                raise HTTPException(422, "no timecodes found in text")
        payload = body.model_dump()
        payload["url"] = url
        job_id = store.create(payload)
        runner.submit(job_id)
        return {"job_id": job_id}

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def get_job(job_id: str):
        job = get_job_or_404(job_id)
        return {k: job[k] for k in ("id", "status", "progress", "title", "error", "parts")}

    @app.get("/jobs/{job_id}/files/{n}", dependencies=[Depends(require_key)])
    def get_file(job_id: str, n: int):
        job = get_done_job(job_id)
        if not 1 <= n <= len(job["parts"]):
            raise HTTPException(404, "part not found")
        path = data_dir / "jobs" / job_id / "out" / job["parts"][n - 1]["file"]
        return FileResponse(path, filename=path.name)

    @app.get("/jobs/{job_id}/download", dependencies=[Depends(require_key)])
    def download_all(job_id: str):
        job = get_done_job(job_id)
        out_dir = data_dir / "jobs" / job_id / "out"
        parts = job["parts"]
        if len(parts) == 1:
            path = out_dir / parts[0]["file"]
            return FileResponse(path, filename=path.name)
        zip_path = data_dir / "jobs" / job_id / "all.zip"
        if not zip_path.exists():
            tmp = zip_path.with_suffix(f".{os.getpid()}.tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                for part in parts:
                    zf.write(out_dir / part["file"], arcname=part["file"])
            os.replace(tmp, zip_path)
        name = sanitize_filename(job["title"] or job_id)
        return FileResponse(zip_path, filename=f"{name}.zip", media_type="application/zip")

    return app
```

- [ ] **Step 5: Запустити, переконатися що проходить**

Run: `pytest tests/test_api.py -v && pytest -q`
Expected: усі passed

---

### Task 10: Веб-сторінка

**Files:**
- Modify: `static/index.html` (повна версія замість заглушки)

Автотестів для JS немає (YAGNI); перевірка руками в Task 12.

- [ ] **Step 1: Записати повну сторінку**

`static/index.html`:
```html
<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Splitter</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; background: #f6f6f7; color: #222; margin: 0; }
  main { max-width: 640px; margin: 2rem auto; padding: 0 1rem; }
  form, section { background: #fff; padding: 1rem 1.25rem; border-radius: 8px; box-shadow: 0 1px 3px #0002; margin-bottom: 1rem; }
  label { display: block; margin: .75rem 0 .25rem; font-weight: 600; }
  label.row { font-weight: 400; }
  input[type=url], input[type=password], select, textarea { width: 100%; box-sizing: border-box; padding: .5rem; font: inherit; }
  textarea { font-family: ui-monospace, monospace; }
  button { padding: .6rem 1.2rem; font: inherit; cursor: pointer; margin-top: 1rem; }
  progress { width: 100%; }
  ul { padding-left: 1.2rem; }
  li { margin: .25rem 0; }
  .error { color: #b00020; }
</style>
</head>
<body>
<main>
  <h1>YouTube Splitter</h1>
  <form id="f">
    <label for="url">Посилання на YouTube</label>
    <input id="url" type="url" required placeholder="https://www.youtube.com/watch?v=...">

    <label for="format">Формат</label>
    <select id="format">
      <option value="mp3">Аудіо — mp3</option>
      <option value="m4a">Аудіо — m4a (без перекодування)</option>
      <option value="opus">Аудіо — opus (без перекодування)</option>
      <option value="mp4">Відео — mp4</option>
    </select>

    <div id="videoOpts" hidden>
      <label for="quality">Якість відео</label>
      <select id="quality">
        <option value="480">480p</option>
        <option value="720">720p</option>
        <option value="1080" selected>1080p</option>
      </select>
      <label for="mode">Нарізання відео</label>
      <select id="mode">
        <option value="fast">Швидко (межі по ключових кадрах)</option>
        <option value="exact">Точно (з перекодуванням)</option>
      </select>
    </div>

    <label for="timecodes">Таймкоди — по одному в рядку, наприклад <code>0:00 Вступ</code></label>
    <textarea id="timecodes" rows="8" placeholder="0:00 Intro&#10;3:45 Song&#10;1:02:03 Outro"></textarea>

    <label class="row"><input id="chapters" type="checkbox"> Якщо таймкодів немає — взяти розділи відео</label>

    <label for="apikey">API-ключ (якщо сервіс його вимагає)</label>
    <input id="apikey" type="password" autocomplete="off">

    <button id="go" type="submit">Завантажити</button>
  </form>

  <section id="status" hidden>
    <div id="msg"></div>
    <progress id="bar" max="1" value="0"></progress>
    <ul id="parts"></ul>
    <button id="zip" type="button" hidden>Завантажити все</button>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const LABELS = { queued: 'У черзі', downloading: 'Завантаження', splitting: 'Нарізання', done: 'Готово', failed: 'Помилка' };

const authHeaders = () => {
  const key = $('apikey').value.trim();
  return key ? { 'X-API-Key': key } : {};
};

$('format').addEventListener('change', () => { $('videoOpts').hidden = $('format').value !== 'mp4'; });

function show(text, progress, isError = false) {
  $('status').hidden = false;
  $('msg').textContent = text;
  $('msg').className = isError ? 'error' : '';
  $('bar').value = progress;
}

function errorText(detail) {
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}

async function saveFile(url, filename) {
  const r = await fetch(url, { headers: authHeaders() });
  if (!r.ok) { show('Не вдалося завантажити файл: HTTP ' + r.status, 1, true); return; }
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

function renderParts(jobId, job) {
  const ul = $('parts');
  ul.replaceChildren();
  for (const p of job.parts) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.style.marginTop = '0';
    btn.textContent = p.file;
    btn.addEventListener('click', () => saveFile(`/jobs/${jobId}/files/${p.n}`, p.file));
    li.appendChild(btn);
    ul.appendChild(li);
  }
  const zip = $('zip');
  zip.hidden = job.parts.length < 2;
  zip.onclick = () => saveFile(`/jobs/${jobId}/download`, (job.title || 'result') + '.zip');
}

async function poll(jobId) {
  for (;;) {
    const r = await fetch(`/jobs/${jobId}`, { headers: authHeaders() });
    const job = await r.json();
    if (!r.ok) throw new Error(errorText(job.detail));
    if (job.status === 'failed') {
      show(`${LABELS.failed}: ${job.error.code} — ${job.error.message}`, job.progress, true);
      return;
    }
    if (job.status === 'done') {
      show(`${LABELS.done}: ${job.title}`, 1);
      renderParts(jobId, job);
      return;
    }
    show(`${LABELS[job.status] || job.status}… ${Math.round(job.progress * 100)}%`, job.progress);
    await new Promise(res => setTimeout(res, 1000));
  }
}

$('f').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {
    url: $('url').value.trim(),
    format: $('format').value,
    timecodes: $('timecodes').value.trim() || null,
    use_chapters: $('chapters').checked,
    video_quality: Number($('quality').value),
    video_mode: $('mode').value,
  };
  $('go').disabled = true;
  $('parts').replaceChildren();
  $('zip').hidden = true;
  show('Створюю завдання…', 0);
  try {
    const r = await fetch('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(errorText(data.detail));
    await poll(data.job_id);
  } catch (err) {
    show('Помилка: ' + err.message, 0, true);
  } finally {
    $('go').disabled = false;
  }
});
</script>
</body>
</html>
```

- [ ] **Step 2: Перевірити, що тест сторінки досі проходить**

Run: `pytest tests/test_api.py::test_index_served -v`
Expected: PASS

---

### Task 11: Docker, README, мережевий smoke-тест

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `README.md`, `.dockerignore`, `tests/test_smoke_network.py`

- [ ] **Step 1: Docker-файли**

`Dockerfile`:
```dockerfile
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yml`:
```yaml
services:
  splitter:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      DATA_DIR: /data
      # API_KEY: "change-me"
      # MAX_DURATION_MIN: "180"
      # MAX_CONCURRENT_JOBS: "2"
      # FILE_TTL_HOURS: "24"
      # RATE_LIMIT_PER_MIN: "10"
    restart: unless-stopped
```

`.dockerignore`:
```
.venv
data
tests
docs
__pycache__
```

- [ ] **Step 2: README**

`README.md`:
````markdown
# YouTube Splitter

Завантажує відео або аудіодоріжку з YouTube і за потреби ріже її на фрагменти за таймкодами з назвами.

## Запуск

Локально (потрібні Python 3.11+ та ffmpeg):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.api:create_app --factory --port 8000
```

Docker:

```bash
docker compose up --build
```

Відкрийте http://localhost:8000.

## Таймкоди

Текст у форматі опису YouTube, по одному в рядку:

```
0:00 Вступ
3:45 - Перша пісня
1:02:03 – Фінал
```

Кінець фрагмента = початок наступного; останній триває до кінця відео. Якщо таймкодів немає, можна взяти розділи (chapters) відео (`use_chapters`); якщо немає й їх — повертається цілий файл.

## API

```
POST /jobs   {"url": "...", "format": "mp3|m4a|opus|mp4", "timecodes": "...", "use_chapters": false,
              "video_quality": 1080, "video_mode": "fast|exact"}   → 202 {"job_id": "..."}
GET  /jobs/{id}                 статус, прогрес, список фрагментів
GET  /jobs/{id}/files/{n}       окремий фрагмент (n з 1)
GET  /jobs/{id}/download        zip з усіма фрагментами (або сам файл, якщо він один)
```

Статуси: `queued → downloading → splitting → done | failed`. Коди помилок: `VIDEO_UNAVAILABLE`, `INVALID_TIMECODES`, `TOO_LONG`, `DOWNLOAD_FAILED`, `SPLIT_FAILED`, `INTERNAL_ERROR`.

## Налаштування (змінні оточення)

| Змінна | За замовчуванням | Значення |
|---|---|---|
| `DATA_DIR` | `data` | куди писати БД і файли |
| `API_KEY` | порожньо | якщо задано, API вимагає заголовок `X-API-Key` |
| `MAX_DURATION_MIN` | `0` | максимальна тривалість відео, 0 = без ліміту |
| `MAX_CONCURRENT_JOBS` | `2` | кількість завдань, що виконуються паралельно |
| `FILE_TTL_HOURS` | `0` | видаляти завершені завдання старші за N годин, 0 = ніколи |
| `RATE_LIMIT_PER_MIN` | `0` | ліміт створення завдань з однієї IP за хвилину, 0 = вимкнено |

## Обслуговування

YouTube часто змінюється, тому `yt-dlp` потрібно оновлювати: `pip install -U yt-dlp` (для Docker — перезбирати образ).

## Правові умови

Завантаження з YouTube може суперечити його умовам використання. Публічну версію сервісу слід обмежувати контентом, на який у користувачів є права.

## Тести

```bash
pip install -r requirements-dev.txt
pytest                  # без мережі
pytest -m network       # smoke-тест із реальним YouTube
```
````

- [ ] **Step 3: Мережевий smoke-тест**

`tests/test_smoke_network.py`:
```python
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
```

- [ ] **Step 4: Переконатися, що звичайний прогін не чіпає мережу**

Run: `pytest -q`
Expected: усі passed, smoke-тест у рядку `1 deselected`

---

### Task 12: Повна перевірка

- [ ] **Step 1: Повний набір тестів**

Run: `pytest -v`
Expected: усі passed, 0 failed

- [ ] **Step 2: Мережевий smoke-тест**

Run: `pytest -m network -v`
Expected: PASS. Якщо падає з помилкою про виклики/JS challenge або відсутні формати, це типова вимога сучасного yt-dlp до зовнішнього JS-рантайму (наприклад, deno): звірте актуальну документацію yt-dlp, встановіть рантайм і додайте відповідну опцію в `build_ydl_opts` та `fetch_info`; потім додайте рантайм у `Dockerfile`. Якщо падає з `Sign in to confirm you're not a bot`, це обмеження YouTube для IP; перевіряйте з іншої мережі.

- [ ] **Step 3: Запустити сервер і пройти сценарій у браузері**

Run: `uvicorn app.api:create_app --factory --port 8000`

У браузері на http://localhost:8000: вставити `https://www.youtube.com/watch?v=jNQXAC9IVRw`, формат mp3, таймкоди `0:00 Start` і `0:10 End`, натиснути «Завантажити». Очікувано: прогрес доходить до «Готово», зʼявляються два файли та кнопка «Завантажити все»; обидва файли та zip завантажуються. Перевірити ще: некоректний URL (`https://example.com`) показує помилку 422; таймкод за межами тривалості (`5:00 X`) завершується статусом `INVALID_TIMECODES`.

- [ ] **Step 4: Перевірити Docker-збірку (за наявності Docker)**

Run: `docker compose up --build -d && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/ && docker compose down`
Expected: `200` (не `curl -I`: HEAD на `/` повертає 405, маршрут лише GET)

---

## Self-review

**Покриття спеки:**
- Формати mp3/m4a/opus/mp4, режими `fast`/`exact`: Task 2–3, 4.
- Таймкоди (текст, роздільники, h:mm:ss), chapters, пріоритет: Task 1, 6.
- API (`POST /jobs`, статус, файли, zip/один файл): Task 9. Статуси та прогрес: Task 6.
- Помилки `VIDEO_UNAVAILABLE`, `INVALID_TIMECODES` (422 одразу або на першому етапі), `DOWNLOAD_FAILED`, `SPLIT_FAILED`, `TOO_LONG`, `INTERNAL_ERROR`: Task 4, 6, 9.
- Імена файлів: Task 2.
- Безпека: allowlist URL (Task 4), аргументи масивом без shell (Task 2–3), `API_KEY`, `MAX_DURATION_MIN`, `MAX_CONCURRENT_JOBS`, `FILE_TTL_HOURS`, rate limit (Task 0, 5–9), застереження ToS (Task 11).
- Тестування (TDD), структура, Docker, README: Task 0–11.
- Поза обсягом (плейлисти, акаунти тощо): не реалізується.

**Плейсхолдери:** немає; у кожному кроці повний код і команди.

**Узгодженість типів:** `AppError(code, message)`, `Segment(start, end, title)`, `VideoInfo(id, title, duration, chapters)`, `JobStore.create/get/update/expired_ids/delete`, `run_job(job_id, store, data_dir, *, max_duration_min, fetch_info, download, split)`, `JobRunner(store, data_dir, max_workers, max_duration_min, executor, job_fn)`, `create_app(settings, executor, job_fn)`, `split_all(src, segments, fmt, out_dir, video_mode, on_progress)` та `download(url, fmt, quality, dest_dir, progress_cb)` узгоджені між завданнями та тестами.
