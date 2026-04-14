from __future__ import annotations

import sys
from pathlib import Path

import httpx

from altlink.settings import get_settings
from altlink.utils.time import utc_now


def heartbeat_check(path: str) -> int:
    settings = get_settings()
    target = Path(path)
    if not target.exists():
        return 1
    modified_at = target.stat().st_mtime
    age_seconds = utc_now().timestamp() - modified_at
    return 0 if age_seconds <= settings.heartbeat_max_age_seconds else 1


def backend_check(url: str) -> int:
    with httpx.Client(timeout=5.0) as client:
        response = client.get(url)
        return 0 if response.status_code == 200 else 1


def main() -> int:
    if len(sys.argv) < 3:
        return 1
    mode = sys.argv[1]
    target = sys.argv[2]
    if mode == "heartbeat":
        return heartbeat_check(target)
    if mode == "backend":
        return backend_check(target)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
