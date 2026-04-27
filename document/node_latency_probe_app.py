from __future__ import annotations

import os

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse


app = FastAPI(title="ALTLINK Node Latency Probe")


@app.get("/ping", status_code=204)
async def ping() -> Response:
    return Response(status_code=204)


@app.head("/ping", status_code=204)
async def ping_head() -> Response:
    return Response(status_code=204)


@app.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "node_latency_probe_app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "44443")),
        proxy_headers=True,
    )
