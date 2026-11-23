from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path

from app.errors import AppError
from app.timecodes import Segment

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _truncate_bytes(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_filename(title: str, max_len: int = 80, max_bytes: int = 120) -> str:
    title = "".join(
        "_" if unicodedata.category(c) == "Cc" else c
        for c in title
        if unicodedata.category(c) != "Cf"
    )
    name = _FORBIDDEN.sub("_", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = _truncate_bytes(name[:max_len], max_bytes).strip(" .")
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
