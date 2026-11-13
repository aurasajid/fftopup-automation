import os
import re
import json
import time
import asyncio
import logging
import tempfile
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Union, Callable
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from automation import process_freefire_topup, process_freefire_topup_batch, parse_voucher
from bot import telegram_polling_loop, send_telegram
import browser_pool
from namecheck import check_player_name
from pkg import detect_package, detect_package_amount, is_supported_prefix

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")

HERE            = os.path.dirname(os.path.abspath(__file__))
APIKEYS_PATH    = os.path.join(HERE, "apikeys.json")
ORDERS_PATH     = os.path.join(HERE, "orders.json")
INDEX_PATH      = os.path.join(HERE, "index.html")

# ─────────────────────────────────────────────────────────────
# ENV & CONFIG
# ─────────────────────────────────────────────────────────────
TG_CHAT_ID              = os.getenv("TG_CHAT_ID", "").strip()
FALLBACK_PACKAGE        = os.getenv("FALLBACK_PACKAGE", "25 Diamond").strip() or "25 Diamond"
ENABLE_FRONTEND         = os.getenv("ENABLE_FRONTEND", "true").strip().lower() == "true"
ENABLE_TELEGRAM_BOT     = os.getenv("ENABLE_TELEGRAM_BOT", "true").strip().lower() == "true"
WEB_API_KEY             = os.getenv("WEB_API_KEY", "").strip()
MAX_ORDERS_KEPT         = int(os.getenv("MAX_ORDERS_KEPT", "1000"))

try:
    MAX_VOUCHER_REDEMPTIONS = int(os.getenv("MAX_VOUCHER_REDEMPTIONS", "10"))
    if MAX_VOUCHER_REDEMPTIONS <= 0:
        MAX_VOUCHER_REDEMPTIONS = 10
except Exception:
    MAX_VOUCHER_REDEMPTIONS = 10


# ============================================================
# API KEYS — persistent store
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
# ORDERS — persistent store & sequential queue
# ============================================================
_ORDERS: dict = {}
_orders_lock = asyncio.Lock()
_next_order_id: int = 1

# Sequential queue
_JOB_QUEUE: asyncio.Queue = asyncio.Queue()
_CURRENT_ORDER_TASK: Optional[asyncio.Task] = None
_BG_TASKS: dict = {}   # { order_id: asyncio.Task }
_PROGRESS_CALLBACKS: dict = {}  # { order_id: callable }


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
    On startup: mark stale pending/processing orders as canceled.
    """
    async with _orders_lock:
        fixed = 0
        now = time.time()
        for k, o in _ORDERS.items():
            if isinstance(o, dict) and o.get("status") in ("pending", "processing", "queued"):
                o["status"] = "canceled"
                o["reason"] = "INTERRUPTED"
                o["message"] = "Server restarted while this order was pending."
                o["updated_at"] = now
                fixed += 1
        if fixed:
            await asyncio.to_thread(_write_orders_to_disk_sync)
            log.warning("⚠️ Recovered %d stale order(s) → canceled", fixed)
        return fixed


async def _cancel_live_task(order_id: int, reason: str = "INTERRUPTED",
                            message: str = "Order was canceled.") -> None:
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


async def _create_order(
    uid: str,
    vouchers: List[Dict[str, Any]],
    api_key: str,
    source: str,
    extra: Optional[Dict[str, Any]] = None,
) -> dict:
    global _next_order_id
    async with _orders_lock:
        order_id = _next_order_id
        _next_order_id += 1
        now = time.time()

        voucher_items = []
        for idx, v in enumerate(vouchers):
            detected = detect_package(v["code"])
            clean_s, clean_p = parse_voucher(v["code"], v.get("pin", ""))
            pkg_name = detected or v.get("pkg") or FALLBACK_PACKAGE
            voucher_items.append({
                "index": idx + 1,
                "code": v["code"],
                "pin": clean_p or v.get("pin", ""),
                "serial": clean_s,
                "package": pkg_name,
                "status": "pending",
                "duration": 0.0,
                "reason": "",
                "message": "",
            })

        order = {
            "order_id": order_id,
            "uid": uid,
            "api_key": api_key,
            "source": source,
            "status": "pending",
            "reason": "",
            "message": "",
            "total_vouchers": len(voucher_items),
            "completed_vouchers": 0,
            "current_voucher": 0,
            "vouchers": voucher_items,
            # Legacy single-voucher compatibility fields
            "pkg": voucher_items[0]["package"] if voucher_items else "",
            "voucher": voucher_items[0]["code"] if voucher_items else "",
            "duration": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        if extra:
            order.update(extra)

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
        "queue_size": _JOB_QUEUE.qsize(),
        "live_tasks": len(_BG_TASKS),
        "max_voucher_redemptions": MAX_VOUCHER_REDEMPTIONS,
    }


# ============================================================
# SEQUENTIAL QUEUE WORKER
# ============================================================
async def _run_order_job(order_id: int) -> None:
    """
    Executes a single queued order containing 1 or more vouchers.
    Emits live progress updates for each voucher.
    """
    order = get_order(order_id)
    if not order:
        return

    uid_clean = order["uid"]
    vouchers = order.get("vouchers", [])
    api_key = order.get("api_key", "")
    source = order.get("source", "direct")

    log.info("📦 [order=%s] Starting sequential job: UID=%s, %d voucher(s) [%s]",
             order_id, uid_clean, len(vouchers), source)

    await _update_order(order_id, status="processing")
    t0 = time.perf_counter()

    async def _on_voucher_progress(idx, total, v_item, status, result_dict):
        async with _orders_lock:
            ord_rec = _ORDERS.get(str(order_id))
            if ord_rec:
                v_list = ord_rec.get("vouchers", [])
                if 0 <= idx < len(v_list):
                    v_list[idx]["status"] = status
                    if result_dict:
                        v_list[idx]["duration"] = result_dict.get("duration", 0.0)
                        v_list[idx]["reason"] = result_dict.get("reason", "")
                        v_list[idx]["message"] = result_dict.get("message", "")
                        if result_dict.get("package"):
                            v_list[idx]["package"] = result_dict.get("package")
                    if status in ("complete", "failed"):
                        ord_rec["completed_vouchers"] = sum(
                            1 for v in v_list if v.get("status") in ("complete", "failed")
                        )
                ord_rec["current_voucher"] = idx + 1
                ord_rec["updated_at"] = time.time()
                await asyncio.to_thread(_write_orders_to_disk_sync)

        # External callback if registered
        cb = _PROGRESS_CALLBACKS.get(order_id)
        if cb:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(order_id, idx, total, status, result_dict)
                else:
                    cb(order_id, idx, total, status, result_dict)
            except Exception as e:
                log.warning("External progress callback error: %s", e)

    try:
        results = await process_freefire_topup_batch(
            player_uid=uid_clean,
            vouchers=vouchers,
            on_progress=_on_voucher_progress,
        )
    except asyncio.CancelledError:
        duration = round(time.perf_counter() - t0, 2)
        await _update_order(
            order_id,
            status="canceled",
            reason="CANCELLED",
            message="Order was canceled before completion.",
            duration=duration,
        )
        _record_duration(duration, "canceled")
        return
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
        return

    duration = round(time.perf_counter() - t0, 2)
    success_count = sum(1 for r in results if r.get("success"))
    overall_status = "complete" if success_count > 0 else "failed"

    primary_reason = "" if overall_status == "complete" else (results[0].get("reason") if results else "FAILED")
    primary_message = (
        f"Topup Completed ({success_count}/{len(vouchers)} succeeded)"
        if overall_status == "complete"
        else (results[0].get("message") if results else "Topup failed")
    )

    await _update_order(
        order_id,
        status=overall_status,
        reason=primary_reason,
        message=primary_message,
        duration=duration,
        completed_vouchers=len(results),
    )
    _record_duration(duration, overall_status)

    log.info("✅ [order=%s] %s in %.2fs (success: %d/%d)",
             order_id, overall_status, duration, success_count, len(vouchers))

    # Consume API key usage (1 usage per successful voucher)
    if success_count > 0 and api_key and source in ("direct", "web"):
        try:
            await consume_usage(api_key, success_count)
            info = _KEYS.get(api_key, {})
            remaining = "unlimited" if info.get("unlimited") else info.get("usage", "?")
            log.info("💳 [order=%s] Consumed %d usage(s). Remaining: %s",
                     order_id, success_count, remaining)
        except Exception:
            log.exception("[order=%s] Failed to consume usage", order_id)

    # Admin Telegram notification
    if TG_CHAT_ID:
        try:
            icon = "✅" if overall_status == "complete" else "❌"
            await send_telegram(
                TG_CHAT_ID,
                f"{icon} *{source.upper()} Order #{order_id}*\n"
                f"UID: `{uid_clean}`\n"
                f"Success: `{success_count}/{len(vouchers)}`\n"
                f"Took: `{duration}s`\n"
                f"Status: `{overall_status}`",
            )
        except Exception:
            pass


async def _queue_worker_loop():
    global _CURRENT_ORDER_TASK
    log.info("🎯 Sequential queue worker started")
    while True:
        try:
            order_id = await _JOB_QUEUE.get()
            order = get_order(order_id)
            if not order or order.get("status") in ("canceled", "complete", "failed"):
                _JOB_QUEUE.task_done()
                continue

            current_task = asyncio.create_task(_run_order_job(order_id))
            _CURRENT_ORDER_TASK = current_task
            _BG_TASKS[str(order_id)] = current_task
            try:
                await current_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.exception("Queue worker job error: %s", e)
            finally:
                _BG_TASKS.pop(str(order_id), None)
                _PROGRESS_CALLBACKS.pop(order_id, None)
                _CURRENT_ORDER_TASK = None
                _JOB_QUEUE.task_done()
        except asyncio.CancelledError:
            log.info("Queue worker loop cancelled")
            break
        except Exception as e:
            log.exception("Queue worker error: %s", e)
            await asyncio.sleep(1)


# ============================================================
# LIFESPAN
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
    log.info("🚀 Starting FF TopUp service v2.1 (Sequential + Auto Package Detection)...")

    load_keys()
    load_orders()

    await _recover_pending_orders()

    # Pre-warm Chromium
    try:
        t0 = time.time()
        await browser_pool.get_browser()
        log.info("✅ Chromium pre-warmed in %.2fs", time.time() - t0)
    except Exception as e:
        log.warning("Chromium pre-warm failed: %s", e)

    # Start rotator
    try:
        browser_pool.start_rotator()
        log.info("🔄 Idle rotator started")
    except Exception as e:
        log.warning("Failed to start rotator: %s", e)

    # Start sequential queue worker
    worker_task = asyncio.create_task(_queue_worker_loop())
    worker_task.add_done_callback(_log_task_exc)

    # Start Telegram polling
    poll_task: Optional[asyncio.Task] = None
    if ENABLE_TELEGRAM_BOT:
        poll_task = asyncio.create_task(telegram_polling_loop())
        poll_task.add_done_callback(_log_task_exc)
        log.info("🤖 Telegram polling task created")
    else:
        log.info("🤖 Telegram bot DISABLED")

    try:
        yield
    finally:
        # Cancel live jobs
        worker_task.cancel()
        try: await worker_task
        except Exception: pass

        for k, task in list(_BG_TASKS.items()):
            if not task.done():
                try: task.cancel()
                except Exception: pass

        if poll_task is not None:
            poll_task.cancel()
            try: await poll_task
            except Exception: pass

        try: await browser_pool.stop_rotator()
        except Exception: pass

        try: await browser_pool.close_browser()
        except Exception: pass

        log.info("🛑 Shutdown complete")


app = FastAPI(title="FF TopUp Automation", version="2.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ============================================================
# VOUCHER & REQUEST VALIDATION HELPERS
# ============================================================
def _validate_uid(uid: str) -> str:
    uid_clean = re.sub(r"\D", "", uid or "")
    if len(uid_clean) < 6 or len(uid_clean) > 15:
        raise HTTPException(400, "Invalid UID (must be 6–15 digits)")
    return uid_clean


def _parse_vouchers_input(
    code: Optional[str] = None,
    pin: Optional[str] = None,
    vouchers: Optional[List[Any]] = None,
    pkg: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Parses and validates vouchers from single code, multi-line string, or vouchers list.
    Applies MAX_VOUCHER_REDEMPTIONS cap and auto-package detection via pkg.py.
    """
    parsed = []

    # 1. From list
    if vouchers and isinstance(vouchers, list):
        for item in vouchers:
            if isinstance(item, str):
                v_code = item.strip()
                if v_code:
                    parsed.append({"code": v_code, "pin": "", "pkg": pkg or ""})
            elif isinstance(item, dict):
                v_code = str(item.get("code") or item.get("voucher") or "").strip()
                v_pin = str(item.get("pin") or "").strip()
                v_pkg = str(item.get("pkg") or pkg or "").strip()
                if v_code:
                    parsed.append({"code": v_code, "pin": v_pin, "pkg": v_pkg})

    # 2. From code string (supports multi-line paste)
    elif code and str(code).strip():
        raw_lines = re.split(r"[\r\n]+", str(code).strip())
        for line in raw_lines:
            line_str = line.strip()
            if not line_str:
                continue
            parsed.append({
                "code": line_str,
                "pin": (pin or "").strip() if len(raw_lines) == 1 else "",
                "pkg": pkg or "",
            })

    if not parsed:
        raise HTTPException(400, "Missing voucher code")

    if len(parsed) > MAX_VOUCHER_REDEMPTIONS:
        raise HTTPException(
            400,
            f"Exceeded maximum voucher limit: submitted {len(parsed)}, "
            f"maximum allowed is {MAX_VOUCHER_REDEMPTIONS} per batch."
        )

    # Auto package detection for each voucher via pkg.py
    for v in parsed:
        clean_s, _ = parse_voucher(v["code"], v.get("pin", ""))
        detected = detect_package(clean_s)
        # If user did not provide pkg or provided fallback, detected prefix wins
        v["pkg"] = detected or v.get("pkg") or FALLBACK_PACKAGE

    return parsed


# ============================================================
# MODELS
# ============================================================
class TopUpRequest(BaseModel):
    api: Optional[str] = ""
    uid: str
    pkg: Optional[str] = ""
    code: Optional[str] = ""
    pin: Optional[str] = ""
    vouchers: Optional[List[Union[str, Dict[str, Any]]]] = None


# ============================================================
# PUBLIC ENDPOINTS
# ============================================================
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "service": "ff-topup-automation",
        "status": "online",
        "version": "2.1.0",
        "headless": os.getenv("HEADLESS", "true"),
        "webrender": os.getenv("WEBRENDER", "true"),
        "warmup": os.getenv("ENABLE_WARMUP", "false"),
        "frontend": ENABLE_FRONTEND,
        "telegram_bot": ENABLE_TELEGRAM_BOT,
        "web_api_key_set": bool(WEB_API_KEY),
        "max_voucher_redemptions": MAX_VOUCHER_REDEMPTIONS,
        "queue_size": _JOB_QUEUE.qsize(),
        "api_keys_count": len(_KEYS),
        "orders_in_memory": len(_ORDERS),
        "fallback_package": FALLBACK_PACKAGE,
    }


@app.get("/api/config")
async def public_config():
    return {
        "max_voucher_redemptions": MAX_VOUCHER_REDEMPTIONS,
        "fallback_package": FALLBACK_PACKAGE,
        "dev_name": os.getenv("DEV_NAME", "S!XU").strip(),
        "dev_link": os.getenv("DEV_LINK", "https://t.me/aura_sajid").strip(),
        "dev_link_text": os.getenv("DEV_LINK_TEXT", "S!XU").strip(),
    }


@app.get("/api/stats")
async def stats():
    return JSONResponse(_stats_snapshot())


# ============================================================
# PLAYER NAME CHECK (CENTRALIZED INTERFACE)
# ============================================================
@app.get("/api/namecheck")
@app.get("/api/player-name")
async def namecheck_endpoint(uid: str = Query(...)):
    """
    Centralized Free Fire player lookup interface.
    Normalizes third-party responses to {"nickname": ..., "uid": ...}.
    """
    clean_uid = _validate_uid(uid)
    data = await check_player_name(clean_uid)
    if not data or not data.get("nickname"):
        raise HTTPException(404, f"Player name could not be resolved for UID {clean_uid}")
    return JSONResponse(data)


# ============================================================
# ORDER TRACKING
# ============================================================
@app.get("/api/order/{order_id}")
async def get_order_status(order_id: int, api: Optional[str] = Query(None)):
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")

    if api:
        ok, reason = key_status(api)
        if not ok:
            raise HTTPException(401, f"API key rejected: {reason}")
        if order.get("api_key") != api:
            raise HTTPException(404, f"Order {order_id} not found")

    safe = {k: v for k, v in order.items() if k != "api_key"}
    return JSONResponse(safe)


@app.get("/api/orders")
async def list_orders(request: Request, limit: int = 20, api: Optional[str] = Query(None)):
    if api:
        ok, reason = key_status(api)
        if not ok:
            raise HTTPException(401, f"API key rejected: {reason}")

    ids = sorted((int(k) for k in _ORDERS.keys() if str(k).isdigit()), reverse=True)
    ids = ids[:max(1, min(limit, 200))]
    out = []
    for i in ids:
        o = _ORDERS.get(str(i))
        if o:
            out.append({k: v for k, v in o.items() if k != "api_key"})
    return JSONResponse({"count": len(out), "orders": out})


# ============================================================
# MAIN /topup ENDPOINT (LEGACY REDIRECT & JOB QUEUE)
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
    # Case 2: poll / run queued order by ID
    if id is not None:
        if not api:
            raise HTTPException(401, "API key rejected: missing key")
        _require_api_key(api, "Topup worker")

        order = get_order(id)
        if not order or order.get("api_key") != api:
            raise HTTPException(404, f"Order {id} not found")

        safe = {k: v for k, v in order.items() if k != "api_key"}
        return JSONResponse({
            "success": True,
            "order_id": order["order_id"],
            "status": order["status"],
            "message": "Order in queue. Poll /api/order/{order_id} for progress.",
            "order": safe,
            "poll_url": f"/api/order/{order['order_id']}",
        })

    # Case 1: Initial submission -> validate, create order, enqueue, redirect 307
    if not api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(api, "Topup")

    if not uid or not code:
        raise HTTPException(400, "Missing uid or code")

    uid_clean = _validate_uid(uid)
    vouchers = _parse_vouchers_input(code=code, pin=pin, pkg=pkg)

    order = await _create_order(
        uid=uid_clean,
        vouchers=vouchers,
        api_key=api,
        source="direct",
    )

    # Enqueue in sequential worker
    await _JOB_QUEUE.put(order["order_id"])

    qs = urlencode({"api": api, "id": order["order_id"]})
    return RedirectResponse(url=f"/topup?{qs}", status_code=307)


@app.post("/topup")
async def topup_post(body: TopUpRequest):
    if not body.api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(body.api, "Topup POST")

    uid_clean = _validate_uid(body.uid)
    vouchers = _parse_vouchers_input(code=body.code, pin=body.pin, vouchers=body.vouchers, pkg=body.pkg)

    order = await _create_order(
        uid=uid_clean,
        vouchers=vouchers,
        api_key=body.api,
        source="direct",
    )
    await _JOB_QUEUE.put(order["order_id"])

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "total_vouchers": len(vouchers),
        "message": f"Order queued with {len(vouchers)} voucher(s). Poll /api/order/{{order_id}} for progress.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


# ============================================================
# WEB & BATCH TOPUP ENDPOINTS
# ============================================================
@app.get("/api/topup")
async def web_topup_get(
    api: str = Query(...),
    uid: str = Query(...),
    code: str = Query(...),
    pkg: Optional[str] = Query(None),
    pin: Optional[str] = Query(""),
):
    _require_api_key(api, "Web topup")
    uid_clean = _validate_uid(uid)
    vouchers = _parse_vouchers_input(code=code, pin=pin, pkg=pkg)

    order = await _create_order(
        uid=uid_clean,
        vouchers=vouchers,
        api_key=api,
        source="web",
    )
    await _JOB_QUEUE.put(order["order_id"])

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "total_vouchers": len(vouchers),
        "message": f"Order queued with {len(vouchers)} voucher(s). Poll /api/order/{{order_id}} for progress.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


@app.post("/api/topup")
@app.post("/api/topup/batch")
async def web_topup_post(body: TopUpRequest):
    if not body.api:
        raise HTTPException(401, "API key rejected: missing key")
    _require_api_key(body.api, "Web topup")

    uid_clean = _validate_uid(body.uid)
    vouchers = _parse_vouchers_input(code=body.code, pin=body.pin, vouchers=body.vouchers, pkg=body.pkg)

    order = await _create_order(
        uid=uid_clean,
        vouchers=vouchers,
        api_key=body.api,
        source="web",
    )
    await _JOB_QUEUE.put(order["order_id"])

    return JSONResponse({
        "success": True,
        "order_id": order["order_id"],
        "status": "pending",
        "total_vouchers": len(vouchers),
        "message": f"Order queued with {len(vouchers)} voucher(s). Poll /api/order/{{order_id}} for progress.",
        "poll_url": f"/api/order/{order['order_id']}",
    })


# ============================================================
# DIRECT ALIASES
# ============================================================
@app.get("/api={api}")
async def direct_api_bare(
    api: str, request: Request,
    uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    _require_api_key(api, "Direct API")
    uid_clean = _validate_uid(uid)
    vouchers = _parse_vouchers_input(code=code, pin=pin, pkg=pkg)
    order = await _create_order(uid_clean, vouchers, api, "direct")
    await _JOB_QUEUE.put(order["order_id"])
    qs = urlencode({"api": api, "id": order["order_id"]})
    return RedirectResponse(url=f"/topup?{qs}", status_code=307)


@app.get("/api/direct")
async def direct_api_path(
    request: Request,
    api: str = Query(...), uid: str = Query(...), code: str = Query(...),
    pkg: Optional[str] = Query(None), pin: Optional[str] = Query(None),
):
    _require_api_key(api, "Direct API")
    uid_clean = _validate_uid(uid)
    vouchers = _parse_vouchers_input(code=code, pin=pin, pkg=pkg)
    order = await _create_order(uid_clean, vouchers, api, "direct")
    await _JOB_QUEUE.put(order["order_id"])
    qs = urlencode({"api": api, "id": order["order_id"]})
    return RedirectResponse(url=f"/topup?{qs}", status_code=307)


# ============================================================
# BOT QUEUE HELPER (FOR bot.py TO SUBMIT SEQUENTIAL BATCHES)
# ============================================================
async def queue_bot_order(
    uid: str,
    vouchers: List[Dict[str, Any]],
    chat_id: str,
    progress_callback: Optional[Callable] = None,
) -> dict:
    """
    Enqueues a Telegram bot batch into the unified sequential queue.
    """
    uid_clean = _validate_uid(uid)
    order = await _create_order(
        uid=uid_clean,
        vouchers=vouchers,
        api_key="",
        source="telegram",
        extra={"chat_id": chat_id},
    )
    if progress_callback:
        _PROGRESS_CALLBACKS[order["order_id"]] = progress_callback

    await _JOB_QUEUE.put(order["order_id"])
    return order


# ============================================================
# ADMIN ENDPOINTS (LOCALHOST ONLY)
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
    if not _is_localhost(request):
        raise HTTPException(403, "Forbidden")
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")
    await _cancel_live_task(order_id, reason="MANUAL_CANCEL", message="Canceled by admin.")
    return {"ok": True, "order_id": order_id}


# ============================================================
# FRONTEND
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    if not ENABLE_FRONTEND:
        return HTMLResponse("<h1>FF TopUp API</h1><p>Frontend is disabled.</p>")

    if not os.path.exists(INDEX_PATH):
        return HTMLResponse("<h1>FF TopUp API</h1><p>index.html not found.</p>")

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    injection = (
        "<script>"
        f"window.__FFT_API_KEY__ = {json.dumps(WEB_API_KEY)};\n"
        f"window.__MAX_VOUCHERS__ = {MAX_VOUCHER_REDEMPTIONS};\n"
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
