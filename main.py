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
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from automation import process_freefire_topup
from bot import telegram_polling_loop, send_telegram
import browser_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")

HERE            = os.path.dirname(os.path.abspath(__file__))
APIKEYS_PATH    = os.path.join(HERE, "apikeys.json")
ORDERS_PATH     = os.path.join(HERE, "orders.json")
INDEX_PATH      = os.path.join(HERE, "index.html")

# ─────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────
TG_CHAT_ID              = os.getenv("TG_CHAT_ID", "").strip()
FALLBACK_PACKAGE        = os.getenv("FALLBACK_PACKAGE", "115").strip() or "115"
ENABLE_FRONTEND         = os.getenv("ENABLE_FRONTEND", "true").strip().lower() == "true"
ENABLE_TELEGRAM_BOT     = os.getenv("ENABLE_TELEGRAM_BOT", "true").strip().lower() == "true"
WEB_API_KEY             = os.getenv("WEB_API_KEY", "").strip()
MAX_ORDERS_KEPT         = int(os.getenv("MAX_ORDERS_KEPT", "1000"))


# ============================================================
# API KEYS — inline loader
# ============================================================
_KEYS: dict = {}
_keys_lock = asyncio.Lock()


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
            try: os.unlink(tmp)
            except Exception: pass
            raise
    except Exception as e:
        log.error("Failed to write apikeys.json: %s", e)


def key_status(key: str):
    """Return (ok: bool, reason: str)."""
    if key is None or not isinstance(key, str) or not key.strip():
        return (False, "missing key")
    info = _KEYS.get(key)
    if info is None:
        return (False, "unknown key")
    if not info["enabled"]:
        return (False, "key disabled or exhausted")
    return (True, "ok")


async def consume_usage(key: str, amount: int = 1) -> bool:
    async with _keys_lock:
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


def _require_api_key(api: str, source: str) -> None:
    ok, reason = key_status(api)
    if not ok:
        log.warning(
            "%s rejected: key=%r reason=%s",
            source,
            (api[:8] + "…") if api else "(missing)",
            reason,
        )
        raise HTTPException(401, f"API key rejected: {reason}")


# ============================================================
# ORDERS — persistent store
# ============================================================
_ORDERS: dict = {}
_orders_lock = asyncio.Lock()
_next_order_id: int = 1

# Track live background tasks by order_id so we can cancel them
_BG_TASKS: dict = {}   # { order_id: asyncio.Task }


def _load_orders_from_disk() -> dict:
    global _next_order_id
    if not os.path.exists(ORDERS_PATH):
        return {}
    try:
        with open(ORDERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        nums = [int(k) for k in data.keys() if str(k).isdigit()]
        _next_order_id = (max(nums) + 1) if nums else 1
        return data
    except Exception as e:
        log.error("Failed to read orders.json: %s", e)
        return {}


def _write_orders_to_disk_sync() -> None:
    if len(_ORDERS) > MAX_ORDERS_KEPT:
        sorted_ids = sorted(
            (int(k) for k in _ORDERS.keys() if str(k).isdigit()),
            reverse=True,
        )
        keep = set(str(i) for i in sorted_ids[:MAX_ORDERS_KEPT])
        for k in list(_ORDERS.keys()):
            if k not in keep:
                del _ORDERS[k]

    try:
        fd, tmp = tempfile.mkstemp(prefix=".orders.", dir=HERE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_ORDERS, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ORDERS_PATH)
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass
            raise
    except Exception as e:
        log.error("Failed to write orders.json: %s", e)


def load_orders() -> int:
    global _ORDERS
    _ORDERS = _load_orders_from_disk()
    log.info("📋 Loaded %d order(s)", len(_ORDERS))
    return len(_ORDERS)


async def _recover_pending_orders() -> int:
    """
    On startup: any order still in 'pending' state is stale — the process
    that was running it is gone. Flip to 'canceled' with an explanation.
    """
    async with _orders_lock:
        fixed = 0
        now = time.time()
        for k, o in _ORDERS.items():
            if isinstance(o, dict) and o.get("status") == "pending":
                o["status"] = "canceled"
                o["reason"] = "INTERRUPTED"
                o["message"] = "Server restarted while this order was still pending."
                o["updated_at"] = now
                fixed += 1
        if fixed:
            await asyncio.to_thread(_write_orders_to_disk_sync)
        if fixed:
            log.warning("⚠️  Recovered %d stale pending order(s) → canceled", fixed)
        return fixed


async def _cancel_live_task(order_id: int, reason: str = "INTERRUPTED",
                            message: str = "Order was canceled.") -> None:
    """
    Cancel the background task for an order (if any) and flip the record
    to 'canceled'.
    """
    task = _BG_TASKS.get(str(order_id))
    if task and not task.done():
        try:
            task.cancel()
        except Exception:
            pass

    await _update_order(
        order_id,
        status="canceled",
        reason=reason,
        message=message,
    )


async def _create_order(uid: str, pkg: str, voucher: str,
                        api_key: str, source: str) -> dict:
    global _next_order_id
    async with _orders_lock:
        order_id = _next_order_id
        _next_order_id += 1
        now = time.time()
        order = {
            "order_id":   order_id,
            "uid":        uid,
            "pkg":        pkg,
            "voucher":    voucher,
            "api_key":    api_key,
            "source":     source,
            "status":     "pending",
            "reason":     "",
            "message":    "",
            "duration":   0.0,
            "created_at": now,
            "updated_at": now,
        }
        _ORDERS[str(order_id)] = order
        await asyncio.to_thread(_write_orders_to_disk_sync)
        return order


async def _update_order(order_id, **fields) -> None:
    async with _orders_lock:
        k = str(order_id)
        if k not in _ORDERS:
            return
        _ORDERS[k].update(fields)
        _ORDERS[k]["updated_at"] = time.time()
        await asyncio.to_thread(_write_orders_to_disk_sync)


def get_order(order_id) -> Optional[dict]:
    return _ORDERS.get(str(order_id))


def order_exists_for_key(order_id, api_key: str) -> bool:
    """Return True if `api_key` owns `order_id`."""
    o = get_order(order_id)
    if not o:
        return False
    return o.get("api_key") == api_key


# ============================================================
# STATS
# ============================================================
STATS = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "canceled": 0,
    "started_at": time.time(),
    "durations": deque(maxlen=100),
}


def _record_duration(seconds: float, status: str) -> None:
    STATS["total"] += 1
    if status == "complete":
        STATS["success"] += 1
    elif status == "canceled":
        STATS["canceled"] += 1
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
        "canceled": STATS["canceled"],
        "last_duration": last,
        "average_duration": avg,
        "min_duration": round(min(durs), 2) if durs else 0.0,
        "max_duration": round(max(durs), 2) if durs else 0.0,
        "recent_durations": durs[-10:],
        "orders_in_memory": len(_ORDERS),
        "live_tasks": len(_BG_TASKS),
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
    load_orders()

    # Mark stale pending orders as canceled
    await _recover_pending_orders()

    # Pre-warm the shared Chromium
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
    poll_task: Optional[asyncio.Task] = None
    if ENABLE_TELEGRAM_BOT:
        poll_task = asyncio.create_task(telegram_polling_loop())
        poll_task.add_done_callback(_log_task_exc)
        log.info("🤖 Telegram polling task created")
    else:
        log.info("🤖 Telegram bot DISABLED (ENABLE_TELEGRAM_BOT=false)")

    if ENABLE_FRONTEND and not WEB_API_KEY:
        log.warning(
            "⚠️  ENABLE_FRONTEND=true but WEB_API_KEY is not set — "
            "the web form will fail with 401."
        )
    elif ENABLE_FRONTEND and WEB_API_KEY:
        if WEB_API_KEY not in _KEYS:
            log.warning(
                "⚠️  WEB_API_KEY=%s… is not present in apikeys.json.",
                WEB_API_KEY[:8],
            )
        else:
            log.info("🌐 Web frontend enabled — using WEB_API_KEY=%s…", WEB_API_KEY[:8])

    try:
        yield
    finally:
        # Cancel all live topup tasks so their orders become 'canceled'
        for k, task in list(_BG_TASKS.items()):
            if not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        # Give tasks a moment to unwind
        if _BG_TASKS:
            try:
                await asyncio.wait(list(_BG_TASKS.values()), timeout=3.0)
            except Exception:
                pass

        # Flip any remaining pending orders to canceled
        async with _orders_lock:
            now = time.time()
            for k, o in _ORDERS.items():
                if isinstance(o, dict) and o.get("status") == "pending":
                    o["status"] = "canceled"
                    o["reason"] = "SHUTDOWN"
                    o["message"] = "Server shut down while this order was pending."
                    o["updated_at"] = now
            try:
                await asyncio.to_thread(_write_orders_to_disk_sync)
            except Exception:
                pass

        try: await browser_pool.stop_rotator()
        except Exception: pass

        if poll_task is not None:
            poll_task.cancel()
            try: await poll_task
            except Exception: pass

        try: await browser_pool.close_browser()
        except Exception: pass

        log.info("🛑 Shutdown complete")


app = FastAPI(title="FF TopUp Automation", version="2.0.0", lifespan=lifespan)
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


def _validate_uid_pkg(uid: str, pkg: Optional[str]) -> tuple:
    uid_clean = re.sub(r"\D", "", uid or "")
    if len(uid_clean) < 6 or len(uid_clean) > 15:
        raise HTTPException(400, "Invalid UID (must be 6–15 digits)")

    if pkg and pkg.strip():
        pkg_input = pkg.strip()
    else:
        pkg_input = FALLBACK_PACKAGE
        log.warning("pkg missing — falling back to %r", FALLBACK_PACKAGE)

    return uid_clean, pkg_input


async def _run_topup_job(order_id: int, uid_clean: str, pkg_input: str,
                         code: str, pin: str, api_key: str,
                         source: str) -> None:
    """
    Background worker. On browser close / cancellation, the order is
    flipped to 'canceled'. On normal completion it becomes 'complete'
    or 'failed'.
    """
    log.info("📦 [order=%s] Topup [%s]: uid=%s pkg=%r code=%s…",
             order_id, source, uid_clean, pkg_input, code[:12])

    t0 = time.perf_counter()
    try:
        res = await process_freefire_topup(uid_clean, pkg_input, code, pin or "")
    except asyncio.CancelledError:
        # Task was cancelled — likely server shutdown or browser teardown
        duration = round(time.perf_counter() - t0, 2)
        await _update_order(
            order_id,
            status="canceled",
            reason="CANCELLED",
            message="Order was canceled before completion.",
            duration=duration,
        )
        _record_duration(duration, "canceled")
        log.warning("🛑 [order=%s] Cancelled after %.2fs", order_id, duration)
        raise
    except Exception as e:
        duration = round(time.perf_counter() - t0, 2)
        await _update_order(
            order_id,
            status="failed",
            reason="ERROR",
            message=str(e),
            duration=duration,
        )
        _record_duration(duration, "failed")
        log.exception("❌ [order=%s] Raised after %.2fs", order_id, duration)
        return

    duration = round(time.perf_counter() - t0, 2)
    res = _to_dict(res)
    success = bool(res.get("success"))
    reason  = "" if success else (res.get("reason") or "FAILED")
    message = res.get("message") or ("Topup Completed Successfully!" if success else "")

    status = "complete" if success else "failed"

    # Detect "browser closed" style errors that should be canceled, not failed
    lower_msg = (message or "").lower()
    browser_closed = any(x in lower_msg for x in (
        "target page, context or browser has been closed",
        "browser has been closed",
        "browser closed",
        "context or browser has been closed",
    ))
    if not success and browser_closed:
        status = "canceled"
        reason = "BROWSER_CLOSED"
        message = "The browser was closed before the order completed."

    await _update_order(
        order_id,
        status=status,
        reason=reason,
        message=message,
        duration=duration,
    )

    _record_duration(duration, status)

    log.info("✅ [order=%s] %s (%.2fs) reason=%r",
             order_id, status, duration, reason)

    # Consume usage ONLY on complete
    if status == "complete" and api_key and source in ("direct", "web"):
        try:
            await consume_usage(api_key, 1)
            info = _KEYS.get(api_key, {})
            remaining = "unlimited" if info.get("unlimited") else info.get("usage", "?")
            log.info("💳 [order=%s] Usage consumed: key=%s…, remaining=%s",
                     order_id, api_key[:6], remaining)
        except Exception:
            log.exception("[order=%s] Failed to consume usage", order_id)

    # Admin notification
    if TG_CHAT_ID:
        try:
            icon = {"complete": "✅", "failed": "❌", "canceled": "🛑"}.get(status, "❔")
            await send_telegram(
                TG_CHAT_ID,
                f"{icon} *{source.upper()} Order #{order_id}*\n"
                f"UID: `{uid_clean}`\n"
                f"Package: `{pkg_input}`\n"
                f"Took: `{duration}s`\n"
                f"Status: `{status}`\n"
                f"Result: `{reason or 'OK'}`",
            )
        except Exception:
            pass


async def _queue_topup(uid_clean: str, pkg_input: str, code: str,
                       pin: str, api_key: str, source: str) -> dict:
    order = await _create_order(
        uid=uid_clean, pkg=pkg_input, voucher=code,
        api_key=api_key, source=source,
    )

    task = asyncio.create_task(
        _run_topup_job(
            order["order_id"], uid_clean, pkg_input, code, pin, api_key, source
        )
    )
    _BG_TASKS[str(order["order_id"])] = task
    task.add_done_callback(lambda t: _BG_TASKS.pop(str(order["order_id"]), None))
    task.add_done_callback(_log_task_exc)

    return order


# ============================================================
# Models
# ============================================================
class TopUpRequest(BaseModel):
    api: Optional[str] = ""
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
        "frontend": ENABLE_FRONTEND,
        "telegram_bot": ENABLE_TELEGRAM_BOT,
        "web_api_key_set": bool(WEB_API_KEY),
        "idle_rotate_seconds": browser_pool.IDLE_ROTATE_SECONDS,
        "rotator_interval": browser_pool.ROTATOR_INTERVAL,
        "tg_enabled": bool(os.getenv("TG_BOT_TOKEN", "").strip()) and ENABLE_TELEGRAM_BOT,
        "api_keys_count": len(_KEYS),
        "api_keys_enabled": sum(1 for k in _KEYS.values() if k["enabled"]),
        "orders_in_memory": len(_ORDERS),
        "live_tasks": len(_BG_TASKS),
        "next_order_id": _next_order_id,
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
# Order tracking
# ============================================================
@app.get("/api/order/{order_id}")
async def get_order_status(order_id: int, api: Optional[str] = Query(None)):
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")

    # If the caller provided a key, enforce that it owns this order
    if api:
        ok, reason = key_status(api)
        if not ok:
            raise HTTPException(401, f"API key rejected: {reason}")
        if order.get("api_key") != api:
            # Do not reveal that the order exists for someone else
            raise HTTPException(404, f"Order {order_id} not found")

    safe = {k: v for k, v in order.items() if k != "api_key"}
    return JSONResponse(safe)


@app.get("/api/orders")
async def list_orders(request: Request, limit: int = 20,
                      api: Optional[str] = Query(None)):
    # Public listing is allowed but never leaks api_key.
    # If a key is provided, we still enforce it is valid (defense in depth).
    if api:
        ok, reason = key_status(api)
        if not ok:
            raise HTTPException(401, f"API key rejected: {reason}")

    ids = sorted((int(k) for k in _ORDERS.keys() if str(k).isdigit()), reverse=True)
    ids = ids[:max(1, min(limit, 200))]
    out = []
    for i in ids:
        o = _ORDERS.get(str(i))
        if not o:
            continue
        out.append({k: v for k, v in o.items() if k != "api_key"})
    return JSONResponse({"count": len(out), "orders": out})


# ============================================================
# Main /topup endpoint
#
# Two behaviors depending on query parameters:
#
#   1. GET /topup?api=KEY&uid=...&pkg=...&code=...
#        → validate key, create order, 307 redirect to (2)
#
#   2. GET /topup?api=KEY&id=N
#        → queue the background job and return JSON
#
# Any other combination → 400.
# ============================================================
@app.get("/topup")
async def topup_endpoint(
    request: Request,
    api: Optional[str]   = Query(None),
    id:  Optional[int]   = Query(None, alias="id"),
    uid: Optional[str]   = Query(None),
    pkg: Optional[str]   = Query(None),
    code: Optional[str]  = Query(None),
    pin: Optional[str]   = Query(None),
):
    # ─── Case 2: order already created, just run it ───
    if id is not None:
        # Key is mandatory
        if not api:
            raise HTTPException(401, "API key rejected: missing key")
        _require_api_key(api, "Topup worker")

        # The order must exist AND belong to this key
        order = get_order(id)
        if not order:
            raise HTTPException(404, f"Order {id} not found")
        if order.get("api_key") != api:
            # Don't leak existence
            raise HTTPException(404, f"Order {id} not found")

        # If already processed, return the current state (idempotent)
        if order.get("status") != "pending":
            safe = {k: v for k, v in order.items() if k != "api_key"}
            return JSONResponse({
                "success": True,
                "order_id": order["order_id"],
                "status": order["status"],
                "message": "Order already processed.",
                "order": safe,
                "poll_url": f"/api/order/{order['order_id']}",
            })

        # Spawn the background job (do not create a new order)
        uid_clean, pkg_input = _validate_uid_pkg(order["uid"], order["pkg"])
        task = asyncio.create_task(
            _run_topup_job(
                order["order_id"],
                uid_clean,
                pkg_input,
                order["voucher"],
                order.get("pin", "") or "",
                order["api_key"],
                order["source"],
            )
        )
        _BG_TASKS[str(order["order_id"])] = task
        task.add_done_callback(lambda t: _BG_TASKS.pop(str(order["order_id"]), None))
        task.add_done_callback(_log_task_exc)

        return JSONResponse({
            "success": True,
            "order_id": order["order_id"],
            "status": "pending",
            "message": "Order queued. Poll /api/order/{order_id} for the result.",
            "poll_url": f"/api/order/{order['order_id']}",
        })

    # ─── Case 1: initial request — validate & redirect ───
    # Key is mandatory
    if not api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(api, "Topup")

    # uid, pkg, code required for the initial request
    if not uid or not code:
        raise HTTPException(400, "Missing uid or code")

    uid_clean, pkg_input = _validate_uid_pkg(uid, pkg)

    code = code.strip()

    # Create the order record now so the redirect target has a valid id
    order = await _create_order(
        uid=uid_clean,
        pkg=pkg_input,
        voucher=code,
        api_key=api,
        source="direct",
    )

    # Build redirect to /topup?api=...&id=N  (same key, new id)
    qs = urlencode({"api": api, "id": order["order_id"]})
    redirect_url = f"/topup?{qs}"

    log.info("↪️  Redirecting /topup → %s (uid=%s pkg=%s)", redirect_url, uid_clean, pkg_input)

    return RedirectResponse(url=redirect_url, status_code=307)


# POST /topup — same as Case 1/2 but from a JSON body. No redirect here;
# the JSON response includes the order_id and the client polls /api/order.
@app.post("/topup")
async def topup_post(body: TopUpRequest):
    if not body.api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(body.api, "Topup POST")

    uid_clean, pkg_input = _validate_uid_pkg(body.uid, body.pkg)

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    order = await _queue_topup(
        uid_clean=uid_clean,
        pkg_input=pkg_input,
        code=code,
        pin=body.pin or "",
        api_key=body.api,
        source="direct",
    )

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "message": "Order queued. Poll /api/order/{order_id} for the result.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


# ============================================================
# Alternate direct-API aliases (same redirect behavior)
# ============================================================
@app.get("/api={api}")
async def direct_api_bare(
    api: str, request: Request,
    uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    _require_api_key(api, "Direct API")
    uid_clean, pkg_input = _validate_uid_pkg(uid, pkg)
    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    order = await _create_order(uid_clean, pkg_input, code, api, "direct")
    qs = urlencode({"api": api, "id": order["order_id"]})
    return RedirectResponse(url=f"/topup?{qs}", status_code=307)


@app.get("/api/direct")
async def direct_api_path(
    request: Request,
    api: str = Query(...), uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    _require_api_key(api, "Direct API")
    uid_clean, pkg_input = _validate_uid_pkg(uid, pkg)
    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    order = await _create_order(uid_clean, pkg_input, code, api, "direct")
    qs = urlencode({"api": api, "id": order["order_id"]})
    return RedirectResponse(url=f"/topup?{qs}", status_code=307)


# ============================================================
# Web top-up (API-key protected) — same shape as POST /topup
# ============================================================
@app.get("/api/topup")
async def web_topup_get(
    api: str = Query(...),
    uid: str = Query(...), pkg: str = Query(...), code: str = Query(...),
    pin: Optional[str] = Query(""),
):
    _require_api_key(api, "Web topup")

    uid_clean, pkg_input = _validate_uid_pkg(uid, pkg)

    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    order = await _queue_topup(
        uid_clean=uid_clean,
        pkg_input=pkg_input,
        code=code,
        pin=pin or "",
        api_key=api,
        source="web",
    )

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "message": "Order queued. Poll /api/order/{order_id} for the result.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


@app.post("/api/topup")
async def web_topup_post(body: TopUpRequest):
    if not body.api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(body.api, "Web topup")

    uid_clean, pkg_input = _validate_uid_pkg(body.uid, body.pkg)

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "Missing voucher code")

    order = await _queue_topup(
        uid_clean=uid_clean,
        pkg_input=pkg_input,
        code=code,
        pin=body.pin or "",
        api_key=body.api,
        source="web",
    )

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "message": "Order queued. Poll /api/order/{order_id} for the result.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


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


@app.post("/api/orders/reload")
async def reload_orders(request: Request):
    if not _is_localhost(request):
        raise HTTPException(403, "Forbidden")
    count = load_orders()
    return {"ok": True, "count": count}


@app.post("/api/orders/cancel/{order_id}")
async def cancel_order_endpoint(order_id: int, request: Request):
    """Localhost-only: force-cancel a live order."""
    if not _is_localhost(request):
        raise HTTPException(403, "Forbidden")
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")
    await _cancel_live_task(order_id, reason="MANUAL_CANCEL",
                            message="Canceled by admin.")
    return {"ok": True, "order_id": order_id}


# ============================================================
# Frontend
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    if not ENABLE_FRONTEND:
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>FF TopUp API</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:640px;"
            "margin:60px auto;padding:0 20px;color:#222}"
            "code{background:#f3f3f7;padding:2px 6px;border-radius:4px}"
            "</style></head><body>"
            "<h1>FF TopUp API</h1>"
            "<p>The web UI is disabled on this deployment.</p>"
            "<p>API endpoints:</p>"
            "<ul>"
            "<li><code>GET /api/health</code></li>"
            "<li><code>GET /api/stats</code></li>"
            "<li><code>GET /api/order/{id}</code></li>"
            "<li><code>GET /topup?api=...&uid=...&pkg=...&code=...</code> → 307 redirect</li>"
            "<li><code>GET /topup?api=...&id=...</code> → run queued order</li>"
            "</ul>"
            "</body></html>"
        )

    if not os.path.exists(INDEX_PATH):
        return HTMLResponse(
            "<h1>FF TopUp API</h1>"
            "<p>Frontend not found. Place <code>index.html</code> next to "
            "<code>main.py</code>.</p>"
        )

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    injection = (
        "<script>"
        f"window.__FFT_API_KEY__ = {json.dumps(WEB_API_KEY)};"
        "</script>"
    )
    if "</head>" in html:
        html = html.replace("</head>", injection + "</head>", 1)
    else:
        html = injection + html

    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
