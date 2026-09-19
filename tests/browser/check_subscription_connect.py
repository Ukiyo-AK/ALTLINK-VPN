"""Run with Python + websockets and installed Edge. No live DB or VPN calls.

python tests/browser/check_subscription_connect.py
Screenshots are written to the gitignored data/connect-preview directory.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from altlink.utils.client_apps import CLIENT_DOWNLOADS, CLIENT_PLATFORMS, client_downloads_for_platform, detect_client_platform
from altlink.utils.qr import render_qr_png

LINK = "https://altlink.example/sub/browser-test-123456789012345678901234567890"
env = Environment(loader=FileSystemLoader(ROOT / "src/altlink/presentation/web/templates"), autoescape=select_autoescape())


async def preview(request):
    choice = request.query_params.get("platform", "auto")
    if choice not in CLIENT_PLATFORMS:
        choice = "auto"
    platform = detect_client_platform(request.headers.get("user-agent", "")) if choice == "auto" else choice
    return HTMLResponse(env.get_template("subscription_connect.html").render(
        title="ALTLINK preview", asset_version="preview", platform_choice=choice, platform=platform,
        client_platforms=CLIENT_PLATFORMS, client_downloads=CLIENT_DOWNLOADS,
        current_downloads=client_downloads_for_platform(platform), subscription_url=LINK,
        happ_import_url=f"happ://add/{LINK}", incy_import_url=f"incy://import/{LINK}",
        support_url="https://t.me/altlink_support",
        qr_data_uri="data:image/png;base64," + base64.b64encode(render_qr_png(LINK)).decode(),
    ))


class CDP:
    def __init__(self, connection):
        self.connection, self.sequence, self.errors = connection, 0, []

    async def call(self, method, **params):
        self.sequence += 1
        await self.connection.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            message = json.loads(await asyncio.wait_for(self.connection.recv(), timeout=15))
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message)
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")


async def check_page(cdp, url, width, user_agent, platform, expected, theme):
    mobile = width <= 768
    await cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=880, deviceScaleFactor=1, mobile=mobile)
    await cdp.call("Emulation.setTouchEmulationEnabled", enabled=mobile, maxTouchPoints=5 if mobile else 1)
    await cdp.call("Emulation.setUserAgentOverride", userAgent=user_agent, platform=platform)
    await cdp.call("Page.navigate", url=url)
    for _ in range(100):
        if await cdp.evaluate("document.body?.classList.contains('is-connect-enhanced')"):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(("Connect JS did not initialize", cdp.errors, await cdp.evaluate("({url: location.href, state: document.readyState, text: document.body?.innerText?.slice(0, 1000)})")))
    await cdp.evaluate(f"window.altlinkTheme.setPreference('{theme}')")
    metrics = await cdp.evaluate("""(() => ({
      overflow: document.documentElement.scrollWidth > innerWidth,
      qr: !!document.querySelector('.subscription-connect-qr').offsetWidth,
      status: document.querySelector('[data-platform-status]').textContent,
      cards: [...document.querySelectorAll('[data-client-card]')].filter(e => e.offsetWidth).length,
      url: document.querySelector('[data-app-download]').href,
      broken: [...document.images].some(e => !e.complete || e.naturalWidth === 0),
      smallActions: [...document.querySelectorAll('.button, [data-client-choice], select')].some(e => e.offsetWidth && e.getBoundingClientRect().height < 44)
    }))()""")
    assert not metrics["overflow"] and not metrics["broken"] and not metrics["smallActions"], metrics
    assert metrics["qr"] is not mobile, metrics
    assert metrics["cards"] == (1 if mobile else 2), metrics
    assert CLIENT_PLATFORMS[expected] in metrics["status"], metrics
    assert metrics["url"] == client_downloads_for_platform(expected)["happ"]["url"], metrics

    if width in (390, 1440):
        screenshot = await cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        destination = ROOT / "data/connect-preview"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{width}-{theme}.png").write_bytes(base64.b64decode(screenshot["data"]))

    for chosen in CLIENT_PLATFORMS:
        await cdp.evaluate(f"document.querySelector('[data-platform-select]').value = '{chosen}'; document.querySelector('[data-platform-select]').dispatchEvent(new Event('change'))")
        urls = await cdp.evaluate("[...document.querySelectorAll('[data-app-download]')].map(e => e.href)")
        assert urls == [link["url"] for link in client_downloads_for_platform(chosen).values()]
    if mobile:
        await cdp.evaluate("document.querySelector('[data-client-choice=incy]').click()")
        assert await cdp.evaluate("!!document.querySelector('[data-client-card=incy]').offsetWidth && !document.querySelector('[data-client-card=happ]').offsetWidth")
    await cdp.evaluate("Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { window.copiedLink = value; } } }); document.querySelector('[data-copy-button]').click()")
    assert await cdp.evaluate("window.copiedLink") == LINK
    assert await cdp.evaluate("document.querySelector('[data-copy-label]').textContent") == "Скопировано"
    await cdp.evaluate("Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw Error('denied'); } } }); document.execCommand = () => false; document.querySelector('[data-copy-button]').click()")
    assert "Не удалось" in await cdp.evaluate("document.querySelector('[data-copy-status]').textContent")
    assert not cdp.errors, cdp.errors


async def main():
    app = Starlette(routes=[
        Route("/connect/preview", preview),
        Mount("/static", StaticFiles(directory=ROOT / "src/altlink/presentation/web/static")),
        Mount("/media", StaticFiles(directory=ROOT / "media")),
    ])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/connect/preview"
    edge = os.environ.get("EDGE_BINARY", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    try:
        with tempfile.TemporaryDirectory(prefix="altlink-connect-browser-", ignore_cleanup_errors=True) as profile:
            process = subprocess.Popen([
                edge, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                "--no-first-run", "--disable-background-networking", "--no-proxy-server", "about:blank",
            ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                port_file = Path(profile) / "DevToolsActivePort"
                for _ in range(100):
                    if port_file.exists():
                        break
                    await asyncio.sleep(0.1)
                port = port_file.read_text().splitlines()[0]
                async with httpx.AsyncClient(trust_env=False) as client:
                    targets = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
                    target = next(item for item in targets if item["type"] == "page" and item["url"] == "about:blank")
                async with websockets.connect(target["webSocketDebuggerUrl"]) as connection:
                    cdp = CDP(connection)
                    await cdp.call("Runtime.enable")
                    await cdp.call("Network.enable")
                    await cdp.call("Network.setBlockedURLs", urls=["*fonts.googleapis.com*", "*fonts.gstatic.com*"])
                    await cdp.call("Page.addScriptToEvaluateOnNewDocument", source="Object.defineProperty(window, 'localStorage', {get() { throw Error('storage denied'); }});")
                    cases = [
                        (360, "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36", "Linux armv8l", "android"),
                        (390, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "iPhone", "ios"),
                        (430, "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36", "Linux armv8l", "android"),
                        (768, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15", "MacIntel", "ios"),
                        (1440, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Win32", "windows"),
                    ]
                    for case in cases:
                        for theme in ("light", "dark"):
                            await check_page(cdp, url, *case, theme)
                            print(f"PASS {case[0]}px {case[3]} {theme}", flush=True)
            finally:
                process.terminate()
                process.wait(timeout=10)
    finally:
        server.should_exit = True
        await serving


if __name__ == "__main__":
    asyncio.run(main())
