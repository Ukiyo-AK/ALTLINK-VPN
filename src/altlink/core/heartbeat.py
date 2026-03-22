from __future__ import annotations

from pathlib import Path


def touch_heartbeat(path: str) -> None:
    heartbeat = Path(path)
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text("ok", encoding="utf-8")

