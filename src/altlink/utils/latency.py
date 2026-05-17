from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import urlparse


LATENCY_RECHECK_THRESHOLD_MS = 250
BROWSER_PROBE_DEFAULT_PORT = 44443
BROWSER_PROBE_DEFAULT_PATH = "/ping"
WHITELIST_SERVER_DOMAIN_SETTING_KEY = "monitoring.whitelist_server_domain"
LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY = "monitoring.whitelist_latency_target_domain"


def is_foreign_latency_target(server) -> bool:
    country_code = (getattr(server, "country_code", "") or "").upper()
    return bool(country_code and country_code != "RU")


def is_whitelist_latency_target(server) -> bool:
    server_type = getattr(server, "server_type", None)
    return (getattr(server_type, "value", server_type) or "") == "whitelist"


def normalize_latency_target_domain(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname or parsed.path.split("/", 1)[0].split(":", 1)[0].strip()
    normalized = host.strip().lower()
    return normalized or None


def server_probe_port(server) -> int:
    for inbound in getattr(server, "inbounds", None) or []:
        if getattr(inbound, "is_active", True) and getattr(inbound, "port", None):
            return int(inbound.port)
    return 443


def server_probe_host(server) -> str | None:
    raw = str(getattr(server, "address", "") or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname or parsed.path.split("/", 1)[0].split(":", 1)[0].strip()
    return host or None


def browser_probe_url(
    server,
    *,
    scheme: str = "https",
    port: int = BROWSER_PROBE_DEFAULT_PORT,
    path: str = BROWSER_PROBE_DEFAULT_PATH,
) -> str | None:
    host = server_probe_host(server)
    if not host:
        return None
    normalized_path = path if path.startswith("/") else f"/{path}"
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{host_for_url}:{int(port)}{normalized_path}"


async def single_probe_server_latency(
    server,
    *,
    timeout_seconds: float = 2.5,
    override_host: str | None = None,
    override_port: int | None = None,
) -> dict:
    started = time.perf_counter()
    writer = None
    try:
        host = str(override_host or "").strip() or server_probe_host(server)
        if not host:
            raise ValueError("empty server address")
        port = int(override_port) if override_port is not None else server_probe_port(server)
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_seconds,
        )
        latency_ms = max(1, round((time.perf_counter() - started) * 1000))
        return {
            "name": server.name,
            "country_code": (server.country_code or "").upper(),
            "latency_ms": latency_ms,
            "reachable": True,
            "probe_target_host": host,
            "probe_target_port": port,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": server.name,
            "country_code": (server.country_code or "").upper(),
            "latency_ms": None,
            "reachable": False,
            "error": str(exc),
            "probe_target_host": locals().get("host"),
            "probe_target_port": locals().get("port"),
        }
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def probe_server_latency(
    server,
    *,
    timeout_seconds: float = 2.5,
    override_host: str | None = None,
    override_port: int | None = None,
) -> dict:
    host_override = str(override_host or "").strip() or None
    port_override = int(override_port) if override_port is not None else None
    first_probe = await single_probe_server_latency(
        server,
        timeout_seconds=timeout_seconds,
        override_host=host_override,
        override_port=port_override,
    )

    first_latency = first_probe.get("latency_ms")
    if not first_probe.get("reachable") or first_latency is None or first_latency < LATENCY_RECHECK_THRESHOLD_MS:
        first_probe["rechecked"] = False
        first_probe["attempts"] = 1
        return first_probe

    second_probe = await single_probe_server_latency(
        server,
        timeout_seconds=timeout_seconds,
        override_host=host_override,
        override_port=port_override,
    )

    result = dict(first_probe)
    result["rechecked"] = True
    result["attempts"] = 2
    result["initial_latency_ms"] = first_latency
    if second_probe.get("reachable") and second_probe.get("latency_ms") is not None:
        result["latency_ms"] = second_probe["latency_ms"]
        result["second_latency_ms"] = second_probe["latency_ms"]
        result.pop("error", None)
    else:
        result["second_latency_ms"] = None
        if second_probe.get("error"):
            result["recheck_error"] = second_probe["error"]
    return result
