import time

from app.jobs import JobStore, cleanup_expired, delete_job, start_cleanup_loop


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


def test_fail_unfinished(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    ids = {s: store.create({}) for s in ("queued", "downloading", "splitting", "done", "failed")}
    for s, i in ids.items():
        store.update(i, status=s)
    assert store.fail_unfinished("boom") == 3
    for s in ("queued", "downloading", "splitting"):
        job = store.get(ids[s])
        assert job["status"] == "failed"
        assert job["error"] == {"code": "INTERNAL_ERROR", "message": "boom"}
    assert store.get(ids["done"])["status"] == "done"
    assert store.get(ids["failed"])["error"] is None


def test_all_ids(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    a, b = store.create({}), store.create({})
    assert store.all_ids() == {a, b}


def test_cleanup_removes_old_orphan_dirs_only(tmp_path):
    import os

    store = JobStore(tmp_path / "jobs.db")
    live = store.create({})
    jobs = tmp_path / "jobs"
    for name in ("live", "old_orphan", "new_orphan"):
        (jobs / (live if name == "live" else name)).mkdir(parents=True, exist_ok=True)
    old = time.time() - 10 * 3600
    os.utime(jobs / live, (old, old))
    os.utime(jobs / "old_orphan", (old, old))
    cleanup_expired(store, tmp_path, ttl_hours=1)
    assert (jobs / live).exists()
    assert not (jobs / "old_orphan").exists()
    assert (jobs / "new_orphan").exists()


def test_delete_job_removes_files_and_row(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = store.create({})
    job_dir = tmp_path / "jobs" / job_id
    (job_dir / "out").mkdir(parents=True)
    (job_dir / "out" / "a.mp3").write_bytes(b"x")
    assert delete_job(store, tmp_path, job_id) is True
    assert not job_dir.exists()
    assert store.get(job_id) is None


def test_delete_job_unknown_returns_false(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    assert delete_job(store, tmp_path, "nope") is False
