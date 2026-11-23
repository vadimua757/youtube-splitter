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


def test_concurrent_downloads_build_valid_zip(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    app = create_app(Settings(data_dir=tmp_path), executor=NoopExecutor(), job_fn=fake_job)
    store = app.state.store
    job_id = store.create({"url": URL})
    out = tmp_path / "jobs" / job_id / "out"
    out.mkdir(parents=True)
    parts = []
    for n in range(1, 4):
        name = f"{n:02d} - T{n}.mp3"
        (out / name).write_bytes(bytes([n]) * (4 * 1024 * 1024))
        parts.append({"n": n, "title": f"T{n}", "start": 0.0, "end": 1.0, "file": name})
    store.update(job_id, status="done", progress=1.0, title="Big", parts=parts)
    c = TestClient(app)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: c.get(f"/jobs/{job_id}/download"), range(8)))

    assert [r.status_code for r in responses] == [200] * 8
    for r in responses:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert zf.testzip() is None
            assert len(zf.namelist()) == 3
    assert not list((tmp_path / "jobs" / job_id).glob("*.tmp"))


def test_startup_fails_unfinished_jobs(tmp_path):
    from app.jobs import JobStore

    store = JobStore(tmp_path / "jobs.db")
    jid = store.create({"url": URL})
    app = create_app(Settings(data_dir=tmp_path), executor=NoopExecutor(), job_fn=fake_job)
    job = app.state.store.get(jid)
    assert job["status"] == "failed"
    assert job["error"] == {"code": "INTERNAL_ERROR", "message": "interrupted by server restart"}


def test_delete_job_removes_files_and_row(client, tmp_path):
    job_id = client.post("/jobs", json={"url": URL}).json()["job_id"]
    assert (tmp_path / "jobs" / job_id).exists()
    r = client.delete(f"/jobs/{job_id}")
    assert r.status_code == 204
    assert r.content == b""
    assert not (tmp_path / "jobs" / job_id).exists()
    assert client.app.state.store.get(job_id) is None
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_delete_running_job_conflict(tmp_path):
    c = make_client(tmp_path, executor=NoopExecutor())
    job_id = c.post("/jobs", json={"url": URL}).json()["job_id"]
    r = c.delete(f"/jobs/{job_id}")
    assert r.status_code == 409
    assert r.json()["detail"] == "job is queued"
    assert c.get(f"/jobs/{job_id}").status_code == 200


def test_delete_unknown_job_404(client):
    assert client.delete("/jobs/nope").status_code == 404


def test_delete_requires_api_key(tmp_path):
    c = make_client(tmp_path, api_key="secret")
    h = {"X-API-Key": "secret"}
    job_id = c.post("/jobs", json={"url": URL}, headers=h).json()["job_id"]
    assert c.delete(f"/jobs/{job_id}").status_code == 401
    assert c.delete(f"/jobs/{job_id}", headers=h).status_code == 204


def test_mix_playlist_url_is_reduced_to_single_video(client):
    mix = "https://www.youtube.com/watch?v=4YBGRGBj7_w&list=RD4YBGRGBj7_w&start_radio=1"
    job_id = client.post("/jobs", json={"url": mix}).json()["job_id"]
    stored = client.app.state.store.get(job_id)["request"]["url"]
    assert stored == "https://www.youtube.com/watch?v=4YBGRGBj7_w"


def test_playlist_only_url_rejected(client):
    r = client.post("/jobs", json={"url": "https://www.youtube.com/playlist?list=PL1"})
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/favicon.svg", "/favicon.ico"])
def test_favicon_served(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


def test_index_links_favicon(client):
    assert 'rel="icon"' in client.get("/").text
