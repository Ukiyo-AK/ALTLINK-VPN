from __future__ import annotations


CLIENT_PLATFORMS = {
    "ios": "iPhone и iPad",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "other": "Другая платформа",
}

# Official publisher links, checked 2026-09-19. Desktop downloads stay on publisher pages.
CLIENT_DOWNLOADS = {
    "happ": {
        "other": {"url": "https://www.happ.su/main/ru", "store": "Сайт Happ", "note": ""},
        "ios": {
            "url": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
            "store": "App Store",
            "note": "Если Happ недоступен в вашем App Store, выберите INCY: он есть в российском магазине.",
        },
        "android": {
            "url": "https://play.google.com/store/apps/details?id=com.happproxy&hl=ru",
            "store": "Google Play",
            "note": "",
        },
    },
    "incy": {
        "other": {"url": "https://github.com/INCY-DEV/incy-platforms#downloads", "store": "Сайт INCY", "note": ""},
        "ios": {
            "url": "https://apps.apple.com/ru/app/incy/id6756943388",
            "store": "App Store · Россия",
            "note": "",
        },
        "android": {
            "url": "https://play.google.com/store/apps/details?id=llc.itdev.incy&hl=ru",
            "store": "Google Play",
            "note": "",
        },
    },
}


def detect_client_platform(user_agent: str) -> str:
    agent = user_agent.casefold()
    if any(device in agent for device in ("iphone", "ipad", "ipod")):
        return "ios"
    if "android" in agent:
        return "android"
    if "windows" in agent:
        return "windows"
    if "macintosh" in agent or "mac os" in agent:
        return "macos"
    if "linux" in agent and "cros" not in agent:
        return "linux"
    return "other"


def client_downloads_for_platform(platform: str) -> dict[str, dict[str, str]]:
    return {app: dict(links.get(platform, links["other"])) for app, links in CLIENT_DOWNLOADS.items()}
