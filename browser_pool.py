import asyncio
import logging
import os
import random
import ssl
import time
from typing import Optional

from playwright.async_api import async_playwright, Browser, Playwright

log = logging.getLogger("browser-pool")


# ─────────────────────────────────────────────────────────────
# MODULE-LEVEL STATE
# ─────────────────────────────────────────────────────────────
_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_lock = asyncio.Lock()

# Idle / rotation state
_last_activity: float = time.monotonic()
_active_topups: int = 0
_rotator_task: Optional[asyncio.Task] = None


# ============================================================
# ENV HELPERS
# ============================================================
def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


# Read once at import time — restart the app to change these.
IDLE_ROTATE_SECONDS = _int("IDLE_ROTATE_SECONDS", 300)   # 5 minutes
ROTATOR_INTERVAL    = _int("ROTATOR_INTERVAL", 30)       # check every 30s


# ============================================================
# USER-AGENT / FINGERPRINT POOL
# ============================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

LOCALES = ["en-US", "en-GB", "en-MY", "en-SG", "en-IN"]
TIMEZONES = ["Asia/Dhaka", "Asia/Kuala_Lumpur", "Asia/Singapore", "Asia/Kolkata"]
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1600, "height": 900},
    {"width": 1366, "height": 768},
]


def _random_choice(seq):
    return random.choice(seq)


def _chrome_major(ua: str) -> str:
    try:
        return ua.split("Chrome/")[1].split(".")[0]
    except Exception:
        return "131"


def _platform_from_ua(ua: str) -> str:
    if "Windows" in ua:
        return "Win32"
    if "Macintosh" in ua:
        return "MacIntel"
    if "Linux" in ua:
        return "Linux x86_64"
    return "Win32"


# ============================================================
# STEALTH INIT SCRIPT
# ============================================================
def _stealth_init_script(ua: str, locale: str, platform: str, chrome_major: str) -> str:
    return f"""
    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});

    try {{
        Object.defineProperty(navigator, 'userAgentData', {{
            get: () => ({{
                brands: [
                    {{brand: 'Chromium', version: '{chrome_major}'}},
                    {{brand: 'Google Chrome', version: '{chrome_major}'}},
                    {{brand: 'Not?A_Brand', version: '24'}},
                ],
                mobile: false,
                platform: '{platform}',
            }}),
        }});
    }} catch (e) {{}}

    try {{
        Object.defineProperty(navigator, 'plugins', {{
            get: () => [
                {{name: 'PDF Viewer', filename: 'internal-pdf-viewer'}},
                {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'}},
                {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'}},
                {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer'}},
                {{name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer'}},
            ],
        }});
        Object.defineProperty(navigator, 'mimeTypes', {{
            get: () => [
                {{type: 'application/pdf'}},
                {{type: 'text/pdf'}},
            ],
        }});
    }} catch (e) {{}}

    try {{
        Object.defineProperty(navigator, 'languages', {{
            get: () => ['{locale}', 'en'],
        }});
    }} catch (e) {{}}

    if (!window.chrome) {{
        window.chrome = {{}};
    }}
    if (!window.chrome.runtime) {{
        window.chrome.runtime = {{
            connect: function() {{}},
            sendMessage: function() {{}},
            onMessage: {{addListener: function() {{}}}},
        }};
    }}

    if (navigator.permissions && navigator.permissions.query) {{
        const origQuery = navigator.permissions.query;
        navigator.permissions.query = (params) => (
            params && params.name === 'notifications'
                ? Promise.resolve({{state: Notification.permission}})
                : origQuery(params)
        );
    }}

    try {{
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return 'Intel Inc.';
            if (param === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, param);
        }};
    }} catch (e) {{}}

    (function() {{
        const origError = console.error;
        console.error = function() {{
            if (arguments[0] && typeof arguments[0] === 'string' &&
                arguments[0].includes('Runtime.enable')) return;
            return origError.apply(this, arguments);
        }};
    }})();
    """


# ============================================================
# WARM-UP (DNS + TLS pre-flight)
# ============================================================
WARMUP_DOMAINS = [
    ("shop.garena.my", 443),
    ("www.unipin.com", 443),
    ("cdn.unipin.com", 443),
]


async def _warmup_dns_tls(timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    async def _one(host: str, port: int):
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, port), timeout=timeout)
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.debug("🔥 Warmup OK: %s:%d", host, port)
        except Exception as e:
            log.debug("Warmup skipped for %s:%d — %s", host, port, e)

    await asyncio.gather(*(_one(h, p) for h, p in WARMUP_DOMAINS))


# ============================================================
# ACTIVITY TRACKING
# ============================================================
def _mark_active() -> None:
    """Call at the start of a top-up — prevents rotation while busy."""
    global _active_topups
    _active_topups += 1
    _touch()


def _mark_inactive() -> None:
    """Call at the end of a top-up — re-enables rotation."""
    global _active_topups
    _active_topups = max(0, _active_topups - 1)
    _touch()


def _touch() -> None:
    """Update the last-activity timestamp."""
    global _last_activity
    _last_activity = time.monotonic()


# ============================================================
# BROWSER TEARDOWN (shared by rotation and shutdown)
# ============================================================
async def _teardown_locked() -> None:
    """
    Close the current browser + Playwright.
    Caller must hold `_lock`.
    """
    global _playwright, _browser

    if _browser is not None:
        try:
            await _browser.close()
        except Exception as e:
            log.warning("Error closing browser: %s", e)
        _browser = None

    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


# ============================================================
# BROWSER LAUNCH (caller must hold _lock)
# ============================================================
async def _launch_browser_locked() -> Browser:
    global _playwright, _browser

    if _bool("ENABLE_WARMUP", False):
        log.info("🔥 Warming up DNS + TLS...")
        try:
            await _warmup_dns_tls()
        except Exception as e:
            log.debug("Warmup failed: %s", e)
    else:
        log.debug("ENABLE_WARMUP=false — skipping DNS+TLS warm-up")

    log.info("🚀 Launching Chromium (stealth)...")
    _playwright = await async_playwright().start()

    headless_mode = _bool("HEADLESS", True)

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-infobars",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-component-extensions-with-background-pages",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-service-autorun",
        "--password-store=basic",
        "--use-mock-keychain",
    ]

    _browser = await _playwright.chromium.launch(
        headless=headless_mode,
        args=launch_args,
    )
    _touch()
    log.info("✅ Chromium ready (headless=%s)", headless_mode)
    return _browser


# ============================================================
# GET BROWSER
# ============================================================
async def get_browser() -> Browser:
    global _playwright, _browser
    async with _lock:
        if _browser is not None:
            try:
                _ = _browser.contexts   # cheap liveness probe
                _touch()
                return _browser
            except Exception:
                log.warning("Browser died — relaunching")
                _browser = None

        return await _launch_browser_locked()


# ============================================================
# CONTEXT CREATION
# ============================================================
async def new_stealth_context(browser: Browser, extra_headers: Optional[dict] = None):
    """
    Create a fresh BrowserContext with a randomized fingerprint.
    Also marks the pool as 'active' for the duration of the top-up;
    the caller MUST call release_context() when done (see automation.py
    which does this in a `finally` block).
    """
    ua = _random_choice(USER_AGENTS)
    locale = _random_choice(LOCALES)
    tz = _random_choice(TIMEZONES)
    viewport = _random_choice(VIEWPORTS)
    platform = _platform_from_ua(ua)
    chrome_major = _chrome_major(ua)

    sec_ch_ua = (
        f'"Chromium";v="{chrome_major}", '
        f'"Google Chrome";v="{chrome_major}", '
        f'"Not?A_Brand";v="24"'
    )

    headers = {
        "Accept-Language": f"{locale},en;q=0.9",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
    }
    if extra_headers:
        headers.update(extra_headers)

    context = await browser.new_context(
        user_agent=ua,
        locale=locale,
        timezone_id=tz,
        viewport=viewport,
        screen=viewport,
        color_scheme="light",
        device_scale_factor=1,
        has_touch=False,
        is_mobile=False,
        java_script_enabled=True,
        ignore_https_errors=False,
        extra_http_headers=headers,
    )

    await context.add_init_script(
        _stealth_init_script(ua, locale, platform, chrome_major)
    )

    # Mark the pool as busy for the lifetime of this top-up.
    _mark_active()

    log.info(
        "🎭 New context: Chrome/%s | %s | %s | %dx%d",
        chrome_major, locale, tz, viewport["width"], viewport["height"],
    )
    return context


def release_context() -> None:
    """Call after the top-up finishes (success or failure)."""
    _mark_inactive()


# ============================================================
# IDLE ROTATOR
# ============================================================
async def _rotate_if_idle() -> None:
    """
    Tear down + relaunch Chromium + Playwright so the next context gets
    a brand-new process-level fingerprint.

    Skipped when any top-up is active. State is re-checked under the
    lock so a request that starts mid-rotation isn't clobbered.
    """
    # Cheap pre-check outside the lock — avoids lock contention when idle
    # for long stretches.
    if _active_topups > 0:
        return
    idle = time.monotonic() - _last_activity
    if idle < IDLE_ROTATE_SECONDS:
        return

    log.info("♻️ Idle for %.0fs — rotating browser fingerprint...", idle)

    async with _lock:
        # Re-check inside the lock — state may have changed.
        if _active_topups > 0:
            return
        if (time.monotonic() - _last_activity) < IDLE_ROTATE_SECONDS:
            return

        await _teardown_locked()

        try:
            await _launch_browser_locked()
            log.info("✅ Fingerprint rotated")
        except Exception as e:
            log.exception("Rotation failed: %s", e)


async def _rotator_loop() -> None:
    """Background task — checks every ROTATOR_INTERVAL seconds."""
    log.info(
        "🔄 Idle rotator started (every %ds, threshold %ds)",
        ROTATOR_INTERVAL, IDLE_ROTATE_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(ROTATOR_INTERVAL)
            await _rotate_if_idle()
        except asyncio.CancelledError:
            log.info("Idle rotator cancelled")
            raise
        except Exception as e:
            log.exception("Rotator loop error: %s", e)


def start_rotator() -> None:
    """Call once at app startup (inside the running event loop)."""
    global _rotator_task
    if _rotator_task is None or _rotator_task.done():
        _rotator_task = asyncio.create_task(_rotator_loop())


async def stop_rotator() -> None:
    global _rotator_task
    if _rotator_task and not _rotator_task.done():
        _rotator_task.cancel()
        try:
            await _rotator_task
        except asyncio.CancelledError:
            pass
    _rotator_task = None


# ============================================================
# SHUTDOWN
# ============================================================
async def close_browser() -> None:
    async with _lock:
        await _teardown_locked()
        log.info("🛑 Shared Chromium closed")
