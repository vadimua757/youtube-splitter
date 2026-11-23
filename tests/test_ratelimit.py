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


def test_stale_keys_are_swept():
    now = [0.0]
    rl = RateLimiter(5, window=60.0, clock=lambda: now[0])
    for i in range(2000):
        assert rl.allow(f"ip{i}")
    assert len(rl) == 2000
    now[0] = 61.0
    assert rl.allow("fresh")
    assert len(rl) <= 2


def test_empty_key_removed_when_expired():
    now = [0.0]
    rl = RateLimiter(1, window=60.0, clock=lambda: now[0])
    rl.allow("a")
    now[0] = 61.0
    rl.allow("a")
    assert len(rl) == 1
