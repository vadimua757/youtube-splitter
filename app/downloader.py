from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qs, urlparse

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


_VIDEO_ID = re.compile(r"[\w-]+")


def canonical_url(url: str) -> str:
    """Лишає в посиланні тільки ID відео (прибирає list=, start_radio=, t= тощо).

    Посилання на «мікс»/плейлист із конкретним відео (`watch?v=ID&list=RD…`) обробляє
    екстрактор плейлистів, тому для завантаження одного відео будуємо чистий watch-URL.
    Посилання без ID відео (канали, чисті плейлисти) відхиляються.
    """
    parsed = urlparse(url)
    video_id = None
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        parts = parsed.path.split("/")
        if len(parts) > 2 and parts[1] in ("shorts", "live", "embed"):
            video_id = parts[2]
    if not video_id or not _VIDEO_ID.fullmatch(video_id):
        raise ValueError("не вдалося знайти відео в посиланні (канали та плейлисти не підтримуються)")
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_url(url: str) -> str:
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as e:
        raise ValueError("invalid url") from e
    if "\\" in url or "@" in (parsed.netloc or "") or parsed.username or parsed.password:
        raise ValueError("invalid url")
    if parsed.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise ValueError("only YouTube URLs are supported")
    return url


def _short(e: Exception) -> str:
    return str(e)[:500]


def _reject_non_video(raw) -> None:
    if raw and raw.get("_type") not in (None, "video"):
        raise AppError("VIDEO_UNAVAILABLE", "playlists and channels are not supported")


def _js_runtime_opts() -> dict:
    """Вмикає JS-рантайми, знайдені на машині: без них YouTube часто віддає
    «This video is not available» або не віддає формати (виклики n/sig)."""
    found = {}
    for name in ("deno", "node", "bun", "quickjs"):
        path = shutil.which(name)
        if path:
            found[name] = {"path": path}
    return {"js_runtimes": found} if found else {}


def fetch_info(url: str, ydl_factory=None) -> VideoInfo:
    factory = ydl_factory or yt_dlp.YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "allowed_extractors": ["youtube"], **_js_runtime_opts()}
    try:
        with factory(opts) as ydl:
            raw = ydl.extract_info(url, download=False)
    except YoutubeDLError as e:
        raise AppError("VIDEO_UNAVAILABLE", _short(e)) from e
    _reject_non_video(raw)
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
        "allowed_extractors": ["youtube"],
        "quiet": True,
        "no_warnings": True,
        **_js_runtime_opts(),
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
        _reject_non_video(info)
        path = Path(info["requested_downloads"][0]["filepath"])
    except YoutubeDLError as e:
        raise AppError("DOWNLOAD_FAILED", _short(e)) from e
    except (KeyError, IndexError, TypeError) as e:
        raise AppError("DOWNLOAD_FAILED", "yt-dlp returned no file") from e
    if not path.exists():
        raise AppError("DOWNLOAD_FAILED", "downloaded file is missing")
    return path
