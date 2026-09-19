"""
FF TopUp Automation — FastAPI app

- Reads API keys directly from apikeys.json (one line per key: `<key> | <usage>`)
- Uses browser_pool for a shared Chromium instance
- Launches bot.py polling in the background
- Starts the idle fingerprint rotator (browser_pool.start_rotator)
- Serves index.html at /

Note: The full top-up logic (voucher parsing, browser context release,
result polling) lives in `automation.py`. This module only wires HTTP
endpoints to it.
"""

import os
import re
import json
import time
import asyncio
import logging
import tempfile
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

from automation import process_freefire_topup
from bot import telegram_polling_loop, send_telegram
import browser_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")

HERE          = os.path.dirname(os.path.abspath(__file__))
APIKEYS_PATH  = os.path.join(HERE, "apikeys.json")
INDEX_PATH    = os.path.join(HERE, "index.html")

TG_CHAT_ID        = os.getenv("TG_CHAT_ID", "").strip()
FALLBACK_PACKAGE  = os.getenv("FALLBACK_PACKAGE", "115").strip() or "115"


# ============================================================
# API KEYS — inline loader
# ============================================================
_KEYS: dict = {}   # { key: {"usage": int|None, "unlimited": bool, "enabled": bool} }


def _parse_key_line(line: str):
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = re.split(r"\s*[|,]\s*|\s+", s, maxsplit=1)
    if len(parts) < 2:
        return None
    key = parts[0].strip()
    usage_raw = parts[1].strip().lower()
    if not key:
        return None
    if usage_raw in ("unlimited", "inf", "infinity", "-1", "∞"):
        return (key, None, True)
    try:
        usage = int(usage_raw)
    except ValueError:
        m = re.search(r"\d+", usage_raw)
        if not m:
            return None
        usage = int(m.group(0))
    return (key, usage, False)


def _load_keys_from_disk() -> dict:
    keys = {}
    if not os.path.exists(APIKEYS_PATH):
        log.warning("apikeys.json not found at %s", APIKEYS_PATH)
        return keys
    try:
        with open(APIKEYS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                parsed = _parse_key_line(line)
                if not parsed:
                    continue
                key, usage, unlimited = parsed
                keys[key] = {
                    "usage": usage if not unlimited else None,
                    "unlimited": unlimited,
                    "enabled": unlimited or (usage is not None and usage > 0),
                }
    except Exception as e:
        log.error("Failed to read apikeys.json: %s", e)
    return keys


def load_keys() -> int:
    global _KEYS
    _KEYS = _load_keys_from_disk()
    log.info("🔑 Loaded %d API key(s)", len(_KEYS))
    return len(_KEYS)


def _write_keys_to_disk_sync() -> None:
    """Blocking write — always call via asyncio.to_thread."""
    lines = [
        "# FF TopUp — API keys",
        "# Format: <key> | <usage>",
        "# usage = integer or 'unlimited'",
        "",
    ]
    for key, info in _KEYS.items():
        usage = "unlimited" if info["unlimited"] else str(info["usage"] or 0)
        lines.append(f"{key} | {usage}")
    content = "\n".join(lines) + "\n"
    try:
        fd, tmp = tempfile.mkstemp(prefix=".apikeys.", dir=HERE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, APIKEYS_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        log.error("Failed to write apikeys.json: %s", e)


def key_status(key: str):
    if key is None or not isinstance(key, str) or not key.strip():
        return (False, "missing key")
    info = _KEYS.get(key)
    if info is None:
        return (False, "unknown key")
    if not info["enabled"]:
        return (False, "key disabled or exhausted")
    return (True, "ok")


async def consume_usage(key: str, amount: int = 1) -> bool:
    """Decrement key usage and persist asynchronously (off the event loop)."""
    info = _KEYS.get(key)
    if not info:
        return False
    if info["unlimited"]:
        return True
    if info["usage"] is None or info["usage"] <= 0:
        return False
    info["usage"] = max(0, info["usage"] - amount)
    info["enabled"] = info["usage"] > 0
    await asyncio.to_thread(_write_keys_to_disk_sync)
    return True


# ============================================================
# STATS — duration counter
# ============================================================
STATS = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "started_at": time.time(),
    "durations": deque(maxlen=100),
}


def _record_duration(seconds: float, success: bool) -> None:
    STATS["total"] += 1
    if success:
        STATS["success"] += 1
    else:
        STATS["failed"] += 1
    STATS["durations"].append(round(seconds, 3))


def _stats_snapshot() -> dict:
    durs = list(STATS["durations"])
    avg = round(sum(durs) / len(durs), 2) if durs else 0.0
    last = durs[-1] if durs else 0.0
    return {
        "uptime_seconds": int(time.time() - STATS["started_at"]),
        "total": STATS["total"],
        "success": STATS["success"],
        "failed": STATS["failed"],
        "last_duration": last,
        "average_duration": avg,
        "min_duration": round(min(durs), 2) if durs else 0.0,
        "max_duration": round(max(durs), 2) if durs else 0.0,
        "recent_durations": durs[-10:],
    }


# ============================================================
# Lifespan
# ============================================================
def _log_task_exc(t: asyncio.Task):
    try:
        t.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception("Background task crashed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Starting FF TopUp service...")

    load_keys()

    # Pre-warm the shared Chromium (also runs DNS+TLS warm-up if enabled)
    try:
        t0 = time.time()
        await browser_pool.get_browser()
        log.info("✅ Chromium pre-warmed in %.2fs", time.time() - t0)
    except Exception as e:
        log.warning("Chromium pre-warm failed: %s", e)

    # Start the idle fingerprint rotator
    try:
        browser_pool.start_rotator()
        log.info(
            "🔄 Idle rotator started (rotate after %ds idle, check every %ds)",
            browser_pool.IDLE_ROTATE_SECONDS,
            browser_pool.ROTATOR_INTERVAL,
        )
    except Exception as e:
        log.warning("Failed to start rotator: %s", e)

    # Start Telegram polling
    poll_task = asyncio.create_task(telegram_polling_loop())
    poll_task.add_done_callback(_log_task_exc)
    log.info("🤖 Telegram polling task created")

    try:
        yield
    finally:
        # Stop rotator first so it doesn't relaunch during shutdown
        try:
            await browser_pool.stop_rotator()
        except Exception:
            pass

        poll_task.cancel()
        try:
            await poll_task
        except Exception:
            pass

        try:
            await browser_pool.close_browser()
        except Exception:
            pass

        log.info("🛑 Shutdown complete")


app = FastAPI(title="FF TopUp Automation", version="1.7.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ============================================================
# Helpers
# ============================================================
def _to_dict(res):
    if isinstance(res, str):
        try:
            return json.loads(res)
        except Exception:
            return {"success": False, "reason": "PARSE_ERROR", "message": res}
    return res


async def _run_topup(uid_clean: str, pkg_input: str, code: str, pin: str = "",
                     source: str = "api") -> dict:
    """
    Thin wrapper around `automation.process_freefire_topup`.
    The automation layer owns:
      - voucher normalization (parse_voucher)
      - provider detection (detect_provider)
      - browser context release (release_context)
    """
    log.info("📦 Topup: uid=%s pkg=%r code=%s…", uid_clean, pkg_input, code[:12])

    t0 = time.perf_counter()
    try:
        res = await process_freefire_topup(uid_clean, pkg_input, code, pin or "")
    except Exception as e:
        log.exception("Topup raised")
        res = {"success": False, "reason": "ERROR", "message": str(e)}

    duration = round(time.perf_counter() - t0, 2)

    res = _to_dict(res)
    res["package_input"] = pkg_input
    res["duration"] = duration

    _record_duration(duration, bool(res.get("success")))

    if TG_CHAT_ID:
        try:
            icon = "✅" if res.get("success") else "❌"
            await send_telegram(
                TG_CHAT_ID,
                f"{icon} *{source.upper()} Order*\n"
                f"UID: `{uid_clean}`\n"
                f"Package: `{pkg_input}`\n"
                f"Took: `{duration}s`\n"
                f"Result: `{res.get('reason') or 'OK'}`",
            )
        except Exception:
            pass

    return res


# ============================================================
# Models
# ============================================================
class TopUpRequest(BaseModel):
    uid: str
    pkg: str
    code: str
    pin: Optional[str] = ""


# ============================================================
# Public endpoints
# ============================================================
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "service": "ff-topup-automation",
        "status": "online",
        "headless": os.getenv("HEADLESS", "true"),
        "webrender": os.getenv("WEBRENDER", "true"),
        "warmup": os.getenv("ENABLE_WARMUP", "false"),
        "idle_rotate_seconds": browser_pool.IDLE_ROTATE_SECONDS,
        "rotator_interval": browser_pool.ROTATOR_INTERVAL,
        "tg_enabled": bool(os.getenv("TG_BOT_TOKEN", "").strip()),
        "api_keys_count": len(_KEYS),
        "api_keys_enabled": sum(1 for k in _KEYS.values() if k["enabled"]),
        "fallback_package": FALLBACK_PACKAGE,
    }


@app.get("/api/config")
async def public_config():
    return {
        "player_api_urls": [
            u.strip()
            for u in os.getenv(
                "PLAYER_API_URLS",
                "https://ffnamecheck.vercel.app/info?uid={uid}",
            ).split(",")
            if u.strip()
        ],
        "player_api_region": os.getenv("PLAYER_API_REGION", "").strip(),
        "dev_name": os.getenv("DEV_NAME", "").strip(),
        "dev_link": os.getenv("DEV_LINK", "").strip(),
        "dev_link_text": os.getenv("DEV_LINK_TEXT", "").strip(),
    }


@app.get("/api/stats")
async def stats():
    return JSONResponse(_stats_snapshot())


# ============================================================
# Direct API — API-key protected
# ============================================================
async def _direct_api_handler(request: Request, api: str, uid: str, code: str,
                              pkg: Optional[str] = None, pin: Optional[str] = None):
    ok, reason = key_status(api)
    if not ok:
        log.warning(
            "Direct API rejected: key=%r reason=%s from %s",
            (api or "")[:8] + "…", reason,
            request.client.host if request.client else "?",
        )
        raise HTTPException(401, f"API key rejected: {reason}")

    uid_clean = re.sub(r"\D", "", uid or "")
    if len(uid_clean) < 6 or len(uid_clean) > 15:
        raise HTTPException(400, "Invalid UID (must be 6–15 digits)")

    if pkg and pkg.strip():
        pkg_input = pkg.strip()
    else:
        pkg_input = FALLBACK_PACKAGE
        log.warning(
            "Direct API: pkg missing — falling back to %r (key=%s…)",
            FALLBACK_PACKAGE, api[:6],
        )

    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    res = await _run_topup(uid_clean, pkg_input, code, pin or "", source="direct")

    if isinstance(res, dict) and res.get("success"):
        consumed = await consume_usage(api, 1)
        info = _KEYS.get(api, {})
        remaining = "unlimited" if info.get("unlimited") else info.get("usage", "?")
        log.info("💳 Usage consumed: key=%s…, remaining=%s", api[:6], remaining)
        res["usage_remaining"] = remaining
        res["usage_written"] = bool(consumed)

    return JSONResponse(res)


@app.get("/api={api}")
async def direct_api_bare(
    api: str, request: Request,
    uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    return await _direct_api_handler(request, api, uid, code, pkg, pin)


@app.get("/topup")
async def direct_api_topup(
    request: Request,
    api: str = Query(...), uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    return await _direct_api_handler(request, api, uid, code, pkg, pin)


@app.get("/api/direct")
async def direct_api_path(
    request: Request,
    api: str = Query(...), uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    return await _direct_api_handler(request, api, uid, code, pkg, pin)


# ============================================================
# Admin (localhost only)
# ============================================================
def _is_localhost(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "localhost", "::1")


@app.get("/api/keys")
async def list_keys(request: Request):
    if not _is_localhost(request):
        raise HTTPException(403, "Forbidden")
    masked = {}
    for k, v in _KEYS.items():
        masked[k[:4] + "…" + k[-4:] if len(k) > 8 else "***"] = v
    return JSONResponse(masked)


@app.post("/api/keys/reload")
async def reload_keys(request: Request):
    if not _is_localhost(request):
        raise HTTPException(403, "Forbidden")
    count = load_keys()
    return {"ok": True, "count": count}


# ============================================================
# Public top-up (no key) — used by index.html
# ============================================================
@app.get("/api/topup")
async def topup_get(
    uid: str = Query(...), pkg: str = Query(...), code: str = Query(...),
    pin: Optional[str] = Query(""),
):
    uid_clean = re.sub(r"\D", "", uid)
    if not uid_clean:
        raise HTTPException(400, "Invalid uid")
    if not pkg.strip():
        raise HTTPException(400, "Invalid pkg")
    return JSONResponse(
        await _run_topup(uid_clean, pkg.strip(), code, pin or "", source="api")
    )


@app.post("/api/topup")
async def topup_post(body: TopUpRequest):
    uid_clean = re.sub(r"\D", "", body.uid)
    if not uid_clean:
        raise HTTPException(400, "Invalid uid")
    if not body.pkg.strip():
        raise HTTPException(400, "Invalid pkg")
    return JSONResponse(
        await _run_topup(uid_clean, body.pkg.strip(), body.code, body.pin or "", source="api")
    )


# ============================================================
# Frontend
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>FF TopUp API</h1>"
        "<p>Frontend not found. Place <code>index.html</code> next to "
        "<code>main.py</code>.</p>"
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)