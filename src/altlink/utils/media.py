from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def media_root() -> Path:
    candidates = [
        Path("/app/media"),
        project_root() / "media",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def media_path(filename: str) -> Path | None:
    path = media_root() / filename
    if path.exists():
        return path
    return None
