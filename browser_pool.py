import asyncio
import logging
import os
import time
from typing import Optional

from cloakbrowser import launch_async
from playwright.async_api import Browser

log = logging.getLogger("browser-pool")


# ─────────────────────────────────────────────────────────────
# MODULE-LEVEL STATE
# ─────────────────────────────────────────────────────────────
_browser: Optional[Browser] = None
_lock = asyncio.Lock()

_last_activity: float = time.monotonic()
_active_topups: int = 0
_rotator_task: Optional[asyncio.Task] = None


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


IDLE_ROTATE_SECONDS = _int("IDLE_ROTATE_SECONDS", 300)
ROTATOR_INTERVAL    = _int("ROTATOR_INTERVAL", 30)


# ============================================================
# ACTIVITY TRACKING
# ============================================================
def _mark_active() -> None:
    global _active_topups
    _active_topups += 1
    _touch()


def _mark_inactive() -> None:
    global _active_topups
    _active_topups = max(0, _active_topups - 1)
    _touch()


def _touch() -> None:
    global _last_activity
    _last_activity = time.monotonic()


# ============================================================
# BROWSER TEARDOWN
# ============================================================
async def _teardown_locked() -> None:
    global _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception as e:
            log.warning("Error closing browser: %s", e)
        _browser = None


# ============================================================
# BROWSER LAUNCH (CloakBrowser)
# ============================================================
async def _launch_browser_locked() -> Browser:
    global _browser
    log.info("🚀 Launching CloakBrowser (stealth)...")

    headless_mode = _bool("HEADLESS", True)

    _browser = await launch_async(headless=headless_mode)
    _touch()
    log.info("✅ CloakBrowser ready (headless=%s)", headless_mode)
    return _browser


# ============================================================
# GET BROWSER
# ============================================================
async def get_browser() -> Browser:
    global _browser
    async with _lock:
        if _browser is not None:
            try:
                _ = _browser.contexts  # cheap liveness probe
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
    CloakBrowser already created a full random fingerprint at launch.
    At the context level we only need to pass business-specific headers.

    NOTE: we deliberately OMIT extra_http_headers when there are none,
    because passing `None` explicitly is not equivalent to omitting the
    argument in some Playwright builds.
    """
    context_kwargs = {}
    if extra_headers:
        context_kwargs["extra_http_headers"] = dict(extra_headers)

    context = await browser.new_context(**context_kwargs)

    _mark_active()
    log.info("🎭 New context created (CloakBrowser handles fingerprint)")
    return context


def release_context() -> None:
    _mark_inactive()


# ============================================================
# IDLE ROTATOR
# ============================================================
async def _rotate_if_idle() -> None:
    if _active_topups > 0:
        return
    idle = time.monotonic() - _last_activity
    if idle < IDLE_ROTATE_SECONDS:
        return

    log.info("♻️ Idle for %.0fs — rotating browser fingerprint...", idle)

    async with _lock:
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
        log.info("🛑 CloakBrowser closed")