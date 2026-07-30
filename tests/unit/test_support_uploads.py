from __future__ import annotations

from types import SimpleNamespace

import pytest

from altlink.application.services.base import ConflictError
from altlink.presentation.web.routes import save_support_photo, support_photo_path


class DummyUpload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content
        self.closed = False

    async def read(self, size: int) -> bytes:
        return self._content[:size]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_support_photo_is_detected_by_content_and_saved_with_random_name(tmp_path):
    settings = SimpleNamespace(
        support_upload_dir=str(tmp_path),
        support_photo_max_bytes=1024,
    )
    upload = DummyUpload("screenshot.anything", b"\x89PNG\r\n\x1a\nimage-data")

    attachment = await save_support_photo(upload, settings)

    assert attachment is not None
    assert attachment["attachment_mime_type"] == "image/png"
    assert str(attachment["attachment_path"]).endswith(".png")
    assert attachment["attachment_original_name"] == "screenshot.anything"
    assert upload.closed is True
    stored_path = support_photo_path(settings, str(attachment["attachment_path"]))
    assert stored_path is not None
    assert stored_path.read_bytes() == b"\x89PNG\r\n\x1a\nimage-data"


@pytest.mark.asyncio
async def test_support_photo_rejects_unknown_file_content(tmp_path):
    settings = SimpleNamespace(
        support_upload_dir=str(tmp_path),
        support_photo_max_bytes=1024,
    )

    with pytest.raises(ConflictError, match="JPG, PNG или WebP"):
        await save_support_photo(DummyUpload("malware.jpg", b"not-an-image"), settings)


def test_support_photo_path_rejects_directory_traversal(tmp_path):
    settings = SimpleNamespace(support_upload_dir=str(tmp_path))

    assert support_photo_path(settings, "../secret.jpg") is None
