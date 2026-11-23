from pathlib import Path

from app.config import Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.data_dir == Path("data")
    assert s.api_key is None
    assert s.max_duration_min == 0
    assert s.max_concurrent_jobs == 2
    assert s.file_ttl_hours == 24
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


def test_empty_ttl_uses_default():
    assert Settings.from_env({"FILE_TTL_HOURS": ""}).file_ttl_hours == 24


def test_zero_ttl_disables_cleanup():
    assert Settings.from_env({"FILE_TTL_HOURS": "0"}).file_ttl_hours == 0
