from __future__ import annotations

import os
import secrets
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.config import Settings
from app.downloader import canonical_url, validate_url
from app.jobs import JobRunner, JobStore, delete_job, start_cleanup_loop
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
    store.fail_unfinished("interrupted by server restart")
    runner = JobRunner(
        store, data_dir,
        max_workers=settings.max_concurrent_jobs,
        max_duration_min=settings.max_duration_min,
        executor=executor, job_fn=job_fn,
    )
    zip_lock = threading.Lock()
    limiter = RateLimiter(settings.rate_limit_per_min) if settings.rate_limit_per_min else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = start_cleanup_loop(store, data_dir, settings.file_ttl_hours) if settings.file_ttl_hours else None
        yield
        if stop:
            stop()

    app = FastAPI(title="YouTube Splitter", lifespan=lifespan)
    app.state.store = store

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

    # /favicon.ico віддає той самий SVG: браузери самі просять цей шлях.
    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
    def create_job(body: JobRequest, request: Request):
        if limiter is not None and not limiter.allow(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "too many requests")
        try:
            url = canonical_url(validate_url(body.url))
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

    @app.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(require_key)])
    def delete_job_endpoint(job_id: str):
        job = get_job_or_404(job_id)
        if job["status"] not in ("done", "failed"):
            raise HTTPException(409, f"job is {job['status']}")
        delete_job(store, data_dir, job_id)
        return Response(status_code=204)

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
            with zip_lock:
                if not zip_path.exists():
                    fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=zip_path.parent)
                    os.close(fd)
                    try:
                        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_STORED) as zf:
                            for part in parts:
                                zf.write(out_dir / part["file"], arcname=part["file"])
                        os.replace(tmp_name, zip_path)
                    except BaseException:
                        Path(tmp_name).unlink(missing_ok=True)
                        raise
        name = sanitize_filename(job["title"] or job_id)
        return FileResponse(zip_path, filename=f"{name}.zip", media_type="application/zip")

    return app
