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

    def all_ids(self) -> set[str]:
        with self._conn() as c:
            return {r["id"] for r in c.execute("SELECT id FROM jobs")}

    def fail_unfinished(self, message: str) -> int:
        error = json.dumps({"code": "INTERNAL_ERROR", "message": message})
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='failed', error=? WHERE status IN ('queued','downloading','splitting')",
                (error,),
            )
            return cur.rowcount

    def delete(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def cleanup_expired(store: JobStore, data_dir, ttl_hours: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    ids = store.expired_ids(now - ttl_hours * 3600)
    for job_id in ids:
        shutil.rmtree(Path(data_dir) / "jobs" / job_id, ignore_errors=True)
        store.delete(job_id)
    jobs_dir = Path(data_dir) / "jobs"
    if jobs_dir.is_dir():
        known = store.all_ids()
        for d in jobs_dir.iterdir():
            try:
                if d.is_dir() and d.name not in known and d.stat().st_mtime < now - ttl_hours * 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                log.exception("orphan cleanup failed for %s", d)
    return len(ids)


def delete_job(store: JobStore, data_dir, job_id: str) -> bool:
    if store.get(job_id) is None:
        return False
    shutil.rmtree(Path(data_dir) / "jobs" / job_id, ignore_errors=True)
    store.delete(job_id)
    return True


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
    row = store.get(job_id)
    if row is None:
        log.warning("job %s not found, skipping", job_id)
        return
    req = row["request"]
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
