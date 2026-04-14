from __future__ import annotations

from io import BytesIO

import qrcode


def render_qr_png(payload: str) -> bytes:
    image = qrcode.make(payload)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

