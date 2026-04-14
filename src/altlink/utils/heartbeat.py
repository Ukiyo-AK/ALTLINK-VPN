from __future__ import annotations

from pathlib import Path

from altlink.utils.time import utc_now


def touch_heartbeat(path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(utc_now().isoformat(), encoding="utf-8")

