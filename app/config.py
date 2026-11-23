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
    file_ttl_hours: float = 24.0    # 0 = не видаляти
    rate_limit_per_min: int = 0     # 0 = вимкнено

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("DATA_DIR") or "data"),
            api_key=env.get("API_KEY") or None,
            max_duration_min=int(env.get("MAX_DURATION_MIN") or 0),
            max_concurrent_jobs=int(env.get("MAX_CONCURRENT_JOBS") or 2),
            file_ttl_hours=float(env.get("FILE_TTL_HOURS") or 24),
            rate_limit_per_min=int(env.get("RATE_LIMIT_PER_MIN") or 0),
        )
