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
