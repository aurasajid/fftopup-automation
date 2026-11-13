import os
import re
import json
import time
import asyncio
import logging
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

from automation import process_freefire_topup_batch, parse_voucher, detect_provider
from namecheck import check_player_name
from pkg import detect_package, detect_package_amount, is_supported_prefix

log = logging.getLogger("tg-bot")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_ALLOWED_USERS = {
    x.strip() for x in os.getenv("TG_ALLOWED_USERS", "").split(",") if x.strip()
}

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}" if TG_BOT_TOKEN else ""

try:
    MAX_VOUCHER_REDEMPTIONS = int(os.getenv("MAX_VOUCHER_REDEMPTIONS", "10"))
    if MAX_VOUCHER_REDEMPTIONS <= 0:
        MAX_VOUCHER_REDEMPTIONS = 10
except Exception:
    MAX_VOUCHER_REDEMPTIONS = 10


# ============================================================
# SESSION STATE (in-memory, per chat)
# ============================================================
SESSIONS: dict = {}


def session_get(chat_id: str) -> dict:
    return SESSIONS.setdefault(chat_id, {})


def session_clear(chat_id: str) -> None:
    SESSIONS.pop(chat_id, None)


def session_set(chat_id: str, **kwargs) -> None:
    SESSIONS.setdefault(chat_id, {}).update(kwargs)


# ============================================================
# TIME & STRING HELPERS
# ============================================================
def get_current_bst_time() -> str:
    """Return timestamp formatted in Bangladesh Standard Time (BST, UTC+6)."""
    bst_tz = timezone(timedelta(hours=6))
    return datetime.now(bst_tz).strftime("%Y-%m-%d %I:%M:%S %p BST")


def _short_pkg_label(pkg: str) -> str:
    raw = (pkg or "").strip()
    low = raw.lower()
    if "monthly" in low:
        return "Monthly Membership"
    if "weekly" in low:
        return "Weekly Membership"
    digits = re.sub(r"\D", "", raw)
    if digits:
        return f"{digits} 💎"
    return f"{raw} 💎"


def _clean_reason_text(reason: str, message: str = "") -> str:
    r = (reason or "").strip().upper()
    if r in ("CONSUMED_VOUCHER", "ALREADY_CONSUMED"):
        return "Consumed Voucher"
    if r in ("INVALID_VOUCHER", "INVALID_SERIAL", "INVALID_PIN"):
        return "Invalid Voucher"
    if r in ("ALREADY_USED", "ALREADY_REDEEMED"):
        return "Already Used Voucher"
    if r in ("EXPIRED_VOUCHER", "EXPIRED"):
        return "Expired Voucher"
    if r == "ALERT_ERROR":
        if message and len(message) < 40:
            return message.strip()
        return "Alert Error"
    if r == "PACKAGE_NOT_FOUND":
        return "Package Not Found"
    if r == "CHECKOUT_NOT_FOUND":
        return "Checkout Not Found"
    if r == "INVALID_PREFIX":
        return "Invalid Voucher Prefix"
    if r == "NO_RESULT":
        return "No Result Detected"
    if r == "BATCH_INTERRUPTED":
        return "Batch Interrupted"
    if r:
        return r.replace("_", " ").title()
    if message:
        first_line = message.split("\n")[0].strip()
        return first_line[:35] if len(first_line) > 35 else first_line
    return "Redeem Failed"


# ---------- HTTP helpers ----------
def _http_get(url: str, timeout: int = 40) -> dict:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (FFTopUp Bot)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("HTTP GET failed: %s", e)
        return {"ok": False}


def _http_post(url: str, payload: dict, timeout: int = 15) -> dict:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
            if "message is not modified" in str(err_body.get("description", "")).lower():
                return {"ok": True, "not_modified": True}
            log.warning("HTTP POST %s error: %s", url, err_body)
            return err_body
        except Exception:
            return {"ok": False}
    except Exception as e:
        log.warning("HTTP POST failed: %s", e)
        return {"ok": False}


async def send_telegram(chat_id: str, text: str, parse_mode: Optional[str] = None) -> Optional[dict]:
    if not TG_API:
        return None
    url = f"{TG_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await asyncio.to_thread(_http_post, url, payload)


async def send_telegram_with_buttons(
    chat_id: str,
    text: str,
    buttons: list,
    parse_mode: Optional[str] = "Markdown",
) -> Optional[dict]:
    if not TG_API:
        return None
    url = f"{TG_API}/sendMessage"
    reply_markup = {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for (label, data) in row]
            for row in buttons
        ]
    }
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await asyncio.to_thread(_http_post, url, payload)


async def answer_callback_query(callback_id: str, text: str = "") -> None:
    if not TG_API or not callback_id:
        return
    url = f"{TG_API}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id, "text": text}
    await asyncio.to_thread(_http_post, url, payload)


async def edit_message_text(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: Optional[str] = None,
) -> Optional[dict]:
    if not TG_API or not message_id:
        return None
    url = f"{TG_API}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await asyncio.to_thread(_http_post, url, payload)


def _escape_md(text) -> str:
    if text is None:
        return ""
    return re.sub(r"([_\*`\[\]])", r"\\\1", str(text))


# ============================================================
# RECEIPT BUILDER (MATCHING EXACT REQUESTED FORMAT)
# ============================================================
def format_receipt_message(
    header: str,
    ff_name: str,
    uid: str,
    tg_username: str,
    tg_id: str,
    package_summary: str,
    success_count: int,
    total_count: int,
    duration_str: str,
    time_str: str,
    voucher_details: List[Dict[str, str]],
) -> str:
    """
    Builds the exact receipt structure specified:
    ✗ Voucher Redeem Failed !!
    --------------------------------
    • FF NAME    : ᏣᎬᏕ-RATANツFF
    • UID        : 4662030286
    • TG USERNAME: unipinsupplierbd
    • TG ID      : 5427080935

    • Package    : Monthly Membership
    • Success    : 0/5
    • Duration   : 19.1s
    • TIME       : 2026-10-28 08:16:08 AM BST
    --------------------------------
    --------------------------------

    Details:
    ------------
    ➥ UPBD-P-S-03545008 2274-6243-2296-6132
    | Monthly 💎 | ❌ Consumed Voucher
    """
    lines = [
        header,
        "--------------------------------",
        f"• FF NAME    : {ff_name or '—'}",
        f"• UID        : {uid}",
        f"• TG USERNAME: {tg_username or 'None'}",
        f"• TG ID      : {tg_id or '—'}",
        "",
        f"• Package    : {package_summary}",
        f"• Success    : {success_count}/{total_count}",
        f"• Duration   : {duration_str}s",
        f"• TIME       : {time_str}",
        "--------------------------------",
        "--------------------------------",
        "",
        "Details:",
        "------------",
    ]

    for item in voucher_details:
        raw_code = item.get("raw_code", "")
        pkg_badge = item.get("short_pkg", "")
        status_line = item.get("status_line", "")
        lines.append(f"➥ {raw_code}")
        lines.append(f"| {pkg_badge} | {status_line}")

    return "\n".join(lines)


# ============================================================
# MESSAGE BUILDERS
# ============================================================
def msg_player_info(data: dict, requested_uid: str) -> str:
    nickname = data.get("nickname", "—")
    uid = data.get("uid", requested_uid)
    level = data.get("level", "")
    region = data.get("region", "")
    lines = [
        "🎮 *Player Information*",
        "━━━━━━━━━━━━━━━━━━",
        f"🏷 *Name:* `{_escape_md(nickname)}`",
        f"🆔 *UID:* `{_escape_md(uid)}`",
    ]
    if level:
        lines.append(f"⭐ *Level:* `{_escape_md(level)}`")
    if region:
        lines.append(f"📍 *Region:* `{_escape_md(region)}`")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def msg_player_not_found(uid: str) -> str:
    return (
        "❌ *Player not found*\n\n"
        f"No player found with UID `{_escape_md(uid)}`.\n"
        "Check the UID and try again."
    )


def msg_welcome() -> str:
    return (
        "👋 *FF TopUp Bot — Bangladesh*\n\n"
        "⚡ *Auto Package Detection Enabled!*\n"
        "No need to pick a package manually — it is detected automatically from your voucher prefix!\n\n"
        f"📦 *Batch Support:* Up to `{MAX_VOUCHER_REDEMPTIONS}` vouchers in sequence!\n\n"
        "*Two ways to order:*\n\n"
        "*1. Interactive:*\n"
        "Send your `UID` → Send your voucher(s)\n\n"
        "*2. Direct command:*\n"
        "`UID VOUCHER` (or paste multiple vouchers)\n\n"
        "*Examples:*\n"
        "`3761036809`\n"
        "`3761036809 UPBD-Q-S-01989133 1527-6324-9731-2625`\n\n"
        "*Other commands:*\n"
        "`/name <uid>` — lookup player name\n"
        "`/id` — your user ID\n"
        "`/cancel` — cancel current session"
    )


def msg_ask_uid() -> str:
    return (
        "📱 *Send the Free Fire UID*\n\n"
        "Example: `3761036809`\n\n"
        "Send /cancel to stop."
    )


def msg_ask_voucher_direct(uid: str, name: Optional[str]) -> str:
    name_line = f"👤 Player: `{_escape_md(name)}`\n" if name else ""
    return (
        f"{name_line}"
        f"🆔 *UID:* `{uid}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎟 *Send your voucher code(s) now.*\n\n"
        f"• You can send 1 voucher or paste up to `{MAX_VOUCHER_REDEMPTIONS}` vouchers (one per line).\n"
        "• Package is detected automatically from the prefix!\n\n"
        "Example:\n"
        "`UPBD-Q-S-01989133 1527-6324-9731-2625`\n\n"
        "Send /cancel to stop."
    )


def msg_confirm_continue(uid: str) -> str:
    return (
        "⚠️ *Unable to fetch name*\n\n"
        f"Could not get the player name for UID `{uid}`.\n"
        "Are you sure you want to continue?"
    )


def msg_cancelled() -> str:
    return "🚫 Cancelled. Send /start to begin again."


# ============================================================
# BATCH TOPUP RUNNER (EDIT MESSAGE LIVE + FULL FINAL RECEIPT)
# ============================================================
async def run_batch_topup(
    chat_id: str,
    uid: str,
    raw_voucher_input: str,
    user_id: str = "",
    username: str = "",
    ff_name: Optional[str] = None,
) -> None:
    lines = [line.strip() for line in re.split(r"[\r\n]+", raw_voucher_input.strip()) if line.strip()]
    if not lines:
        await send_telegram(chat_id, "❌ No vouchers found.", parse_mode=None)
        return

    if len(lines) > MAX_VOUCHER_REDEMPTIONS:
        await send_telegram(
            chat_id,
            f"❌ Limit exceeded: You provided {len(lines)} vouchers. Maximum allowed is {MAX_VOUCHER_REDEMPTIONS} per batch.",
            parse_mode=None,
        )
        return

    # Fetch player name if not already cached
    if not ff_name or ff_name == "—":
        try:
            info = await check_player_name(uid)
            if info and info.get("nickname"):
                ff_name = info.get("nickname")
        except Exception:
            pass
    if not ff_name:
        ff_name = "—"

    # Package detection for all vouchers in batch
    vouchers = []
    packages_list = []
    voucher_details = []

    for line in lines:
        clean_serial, clean_pin = parse_voucher(line)
        detected_pkg = detect_package(clean_serial) or "25 Diamond"
        vouchers.append({
            "code": line,
            "pin": clean_pin,
            "pkg": detected_pkg,
        })
        packages_list.append(detected_pkg)
        short_b = _short_pkg_label(detected_pkg)
        voucher_details.append({
            "raw_code": line,
            "short_pkg": short_b,
            "status_line": "⏱ Queued",
        })

    # Package summary
    unique_pkgs = list(dict.fromkeys(packages_list))
    package_summary = ", ".join(unique_pkgs) if unique_pkgs else "Free Fire Diamond"

    total_v = len(vouchers)
    voucher_details[0]["status_line"] = "⏳ Processing..."

    # Initial live status message
    initial_text = format_receipt_message(
        header="⏳ Voucher Redeeming in Progress...",
        ff_name=ff_name,
        uid=uid,
        tg_username=username,
        tg_id=user_id or chat_id,
        package_summary=package_summary,
        success_count=0,
        total_count=total_v,
        duration_str="0.0",
        time_str=get_current_bst_time(),
        voucher_details=voucher_details,
    )

    init_res = await send_telegram(chat_id, initial_text, parse_mode=None)
    status_msg_id = init_res.get("result", {}).get("message_id") if isinstance(init_res, dict) else None

    t0 = time.perf_counter()

    # Progress callback: edits message in place after/during each voucher
    async def _bot_progress(idx, total, v_item, status, result_dict):
        if not status_msg_id:
            return

        elapsed_now = f"{time.perf_counter() - t0:.1f}"

        if status == "processing":
            voucher_details[idx]["status_line"] = "⏳ Processing..."
            for rem in range(idx + 1, total):
                if voucher_details[rem]["status_line"] != "⏳ Processing...":
                    voucher_details[rem]["status_line"] = "⏱ Queued"

        elif status in ("complete", "failed"):
            dur = result_dict.get("duration", 0.0)
            if status == "complete":
                voucher_details[idx]["status_line"] = f"✅ Topup Completed Successfully! ({dur}s)"
            else:
                reason = result_dict.get("reason", "")
                msg = result_dict.get("message", "")
                clean_err = _clean_reason_text(reason, msg)
                voucher_details[idx]["status_line"] = f"❌ {clean_err}"

            # If next voucher exists, set it to processing
            if idx + 1 < total:
                voucher_details[idx + 1]["status_line"] = "⏳ Processing..."

        succ_count = sum(1 for v in voucher_details if "✅" in v.get("status_line", ""))
        update_text = format_receipt_message(
            header="⏳ Voucher Redeeming in Progress...",
            ff_name=ff_name,
            uid=uid,
            tg_username=username,
            tg_id=user_id or chat_id,
            package_summary=package_summary,
            success_count=succ_count,
            total_count=total,
            duration_str=elapsed_now,
            time_str=get_current_bst_time(),
            voucher_details=voucher_details,
        )
        await edit_message_text(chat_id, status_msg_id, update_text, parse_mode=None)

    try:
        results = await process_freefire_topup_batch(
            player_uid=uid,
            vouchers=vouchers,
            on_progress=_bot_progress,
        )
    except Exception as e:
        log.exception("Batch runner error")
        results = []
        for i, v in enumerate(vouchers):
            results.append({
                "index": i + 1,
                "success": False,
                "reason": "ERROR",
                "message": str(e),
                "package": v["pkg"],
                "serial": v["code"],
                "duration": 0.0,
            })

    total_elapsed = f"{time.perf_counter() - t0:.1f}"
    final_success = sum(1 for r in results if r.get("success"))

    # Update voucher_details with final clean status texts
    for i, r in enumerate(results):
        if i < len(voucher_details):
            if r.get("success"):
                voucher_details[i]["status_line"] = "✅ Topup Completed Successfully!"
            else:
                clean_err = _clean_reason_text(r.get("reason", ""), r.get("message", ""))
                voucher_details[i]["status_line"] = f"❌ {clean_err}"

    # Header decision matching exact user requirement:
    if final_success == total_v and total_v > 0:
        final_header = "💎 Diamond Top-Up Done!!"
    else:
        final_header = "✗ Voucher Redeem Failed !!"

    final_receipt = format_receipt_message(
        header=final_header,
        ff_name=ff_name,
        uid=uid,
        tg_username=username,
        tg_id=user_id or chat_id,
        package_summary=package_summary,
        success_count=final_success,
        total_count=total_v,
        duration_str=total_elapsed,
        time_str=get_current_bst_time(),
        voucher_details=voucher_details,
    )

    # Edit the message with the final result; fallback to send if edit fails
    edited = None
    if status_msg_id:
        edited = await edit_message_text(chat_id, status_msg_id, final_receipt, parse_mode=None)
    if not edited or not edited.get("ok"):
        await send_telegram(chat_id, final_receipt, parse_mode=None)

    # Admin notification
    if TG_CHAT_ID and TG_CHAT_ID != chat_id:
        try:
            await send_telegram(
                TG_CHAT_ID,
                f"📢 Bot Order Finished\nUser: @{username} ({user_id})\nUID: {uid} ({ff_name})\n"
                f"Success: {final_success}/{total_v}\nDuration: {total_elapsed}s",
                parse_mode=None,
            )
        except Exception:
            pass

    session_clear(chat_id)


# ============================================================
# CONVERSATIONAL FLOW
# ============================================================
async def flow_start(chat_id: str) -> None:
    session_clear(chat_id)
    session_set(chat_id, stage="awaiting_uid")
    await send_telegram(chat_id, msg_ask_uid(), parse_mode="Markdown")


async def flow_after_uid(chat_id: str, uid: str) -> None:
    uid = re.sub(r"\D", "", uid)
    if not uid or len(uid) < 6:
        await send_telegram(chat_id, "❌ Invalid UID. Please send a 6–15 digit UID.", parse_mode=None)
        return

    await send_telegram(chat_id, f"🔍 Looking up `{uid}`...", parse_mode="Markdown")
    info = await check_player_name(uid)

    if info:
        name = info.get("nickname", "")
        session_set(chat_id, stage="awaiting_voucher", uid=uid, name=name, verified=True)
        await send_telegram(chat_id, msg_player_info(info, uid), parse_mode="Markdown")
        await send_telegram(chat_id, msg_ask_voucher_direct(uid, name), parse_mode="Markdown")
    else:
        session_set(chat_id, stage="awaiting_confirm", uid=uid, name=None)
        await send_telegram_with_buttons(
            chat_id,
            msg_confirm_continue(uid),
            [[
                ("✅ Yes, continue", "confirm:yes"),
                ("❌ No, cancel", "confirm:no"),
            ]],
            parse_mode="Markdown",
        )


async def flow_after_confirm(chat_id: str, uid: str, name: Optional[str]) -> None:
    session_set(chat_id, stage="awaiting_voucher", uid=uid, name=name, verified=False)
    await send_telegram(chat_id, msg_ask_voucher_direct(uid, name), parse_mode="Markdown")


# ============================================================
# MESSAGE HANDLER
# ============================================================
async def handle_message(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    sender = msg.get("from") or {}
    user_id = str(sender.get("id", ""))
    username = sender.get("username", "")
    first_name = sender.get("first_name", "")
    text = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return

    if TG_ALLOWED_USERS and user_id not in TG_ALLOWED_USERS:
        await send_telegram(chat_id, "⛔ You are not authorized to use this bot.", parse_mode=None)
        log.warning("Unauthorized user: %s", user_id)
        return

    # Commands
    if text in ("/start", "/help"):
        session_clear(chat_id)
        await send_telegram(chat_id, msg_welcome(), parse_mode="Markdown")
        return

    if text == "/id":
        await send_telegram(
            chat_id,
            f"Your chat ID: `{chat_id}`\nYour user ID: `{user_id}`",
            parse_mode="Markdown",
        )
        return

    if text == "/cancel":
        session_clear(chat_id)
        await send_telegram(chat_id, msg_cancelled(), parse_mode="Markdown")
        return

    if text == "/order":
        await flow_start(chat_id)
        return

    # /name <uid>
    name_cmd = re.match(r"^/name(?:@\w+)?\s+(\d{6,15})$", text)
    if name_cmd:
        uid = name_cmd.group(1)
        await send_telegram(chat_id, f"🔍 Looking up `{uid}`...", parse_mode="Markdown")
        info = await check_player_name(uid)
        if info:
            await send_telegram(chat_id, msg_player_info(info, uid), parse_mode="Markdown")
        else:
            await send_telegram(chat_id, msg_player_not_found(uid), parse_mode="Markdown")
        return

    # Direct command: UID followed by voucher lines
    # e.g. "3761036809 UPBD-Q-S-01989133 1527-6324-9731-2625" or multi-line
    parts = text.split(None, 1)
    if len(parts) >= 2 and re.match(r"^\d{6,15}$", parts[0]):
        uid_cand = parts[0]
        rest = parts[1].strip()

        # Check if rest begins with an optional package label e.g. "115 UPBD-..."
        pkg_alias_match = re.match(r"^([A-Za-z0-9]+)\s+(.+)$", rest, re.DOTALL)
        if pkg_alias_match:
            first_tok = pkg_alias_match.group(1)
            if first_tok.upper().startswith(("UPBD", "BDMB")):
                voucher_body = rest
            else:
                voucher_body = pkg_alias_match.group(2).strip()
        else:
            voucher_body = rest

        sess = session_get(chat_id)
        cached_name = sess.get("name") if sess.get("uid") == uid_cand else None
        session_clear(chat_id)

        await run_batch_topup(
            chat_id=chat_id,
            uid=uid_cand,
            raw_voucher_input=voucher_body,
            user_id=user_id,
            username=username,
            ff_name=cached_name,
        )
        return

    # Conversational flow
    sess = session_get(chat_id)
    stage = sess.get("stage")

    if stage == "awaiting_uid":
        if re.match(r"^\d{6,15}$", text):
            await flow_after_uid(chat_id, text)
            return
        await send_telegram(chat_id, "❌ That doesn't look like a UID.\nSend a 6–15 digit UID, or /cancel.", parse_mode=None)
        return

    if stage == "awaiting_confirm":
        low = text.lower()
        uid = sess.get("uid")
        if low in ("yes", "y", "ok", "continue", "✅"):
            await flow_after_confirm(chat_id, uid, sess.get("name"))
            return
        if low in ("no", "n", "cancel", "❌"):
            session_clear(chat_id)
            await send_telegram(chat_id, msg_cancelled(), parse_mode=None)
            return
        if re.match(r"^\d{6,15}$", text):
            await flow_after_uid(chat_id, text)
            return
        await send_telegram(chat_id, "Please tap ✅ Yes or ❌ No above, or send /cancel.", parse_mode=None)
        return

    if stage == "awaiting_voucher":
        uid = sess.get("uid")
        if not uid:
            await send_telegram(chat_id, "❌ Session expired. Send /start to begin.", parse_mode=None)
            session_clear(chat_id)
            return
        cached_name = sess.get("name")
        await run_batch_topup(
            chat_id=chat_id,
            uid=uid,
            raw_voucher_input=text,
            user_id=user_id,
            username=username,
            ff_name=cached_name,
        )
        return

    # Bare UID without stage
    bare_uid = re.match(r"^(\d{6,15})$", text)
    if bare_uid:
        await flow_after_uid(chat_id, bare_uid.group(1))
        return

    await send_telegram(
        chat_id,
        "❌ *Invalid format*\n\n"
        "*Try:*\n"
        "• `3761036809` — start interactive order\n"
        "• `3761036809 UPBD-Q-S-01989133 1527-6324-9731-2625` — instant top-up\n"
        "• `/name 3761036809` — player name lookup\n\n"
        "Send /help for instructions.",
        parse_mode="Markdown",
    )


# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================
async def handle_callback(update: dict) -> None:
    cq = update.get("callback_query") or {}
    callback_id = cq.get("id", "")
    data = cq.get("data", "")
    message = cq.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    message_id = message.get("message_id")
    sender = cq.get("from") or {}
    user_id = str(sender.get("id", ""))

    if not chat_id or not data:
        return

    if TG_ALLOWED_USERS and user_id not in TG_ALLOWED_USERS:
        await answer_callback_query(callback_id, "Not authorized")
        return

    if data.startswith("confirm:"):
        choice = data.split(":", 1)[1]
        sess = session_get(chat_id)
        uid = sess.get("uid")

        if choice == "yes":
            await answer_callback_query(callback_id, "Continuing...")
            if message_id:
                await edit_message_text(
                    chat_id, message_id,
                    f"✅ Continuing with UID {uid} (name unverified)",
                    parse_mode=None,
                )
            await flow_after_confirm(chat_id, uid, None)
        else:
            await answer_callback_query(callback_id, "Cancelled")
            if message_id:
                await edit_message_text(chat_id, message_id, msg_cancelled(), parse_mode=None)
            session_clear(chat_id)
        return

    await answer_callback_query(callback_id, "Action acknowledged")


# ============================================================
# LONG POLLING LOOP
# ============================================================
async def telegram_polling_loop():
    if not TG_API:
        log.warning("⚠️ TG_BOT_TOKEN not set — Telegram bot disabled.")
        return

    me = await asyncio.to_thread(_http_get, f"{TG_API}/getMe", 10)
    if not me.get("ok"):
        log.error("❌ Invalid Telegram token. Bot disabled.")
        return
    bot_username = me.get("result", {}).get("username", "?")
    log.info("✅ Telegram bot online: @%s", bot_username)

    await asyncio.to_thread(_http_get, f"{TG_API}/deleteWebhook?drop_pending_updates=true", 10)
    await asyncio.to_thread(_http_get, f"{TG_API}/getUpdates?offset=-1&timeout=1", 10)

    offset = 0
    consecutive_errors = 0

    while True:
        try:
            url = (
                f"{TG_API}/getUpdates?offset={offset}&timeout=25"
                f"&allowed_updates=%5B%22message%22%2C%22edited_message%22%2C%22callback_query%22%5D"
            )
            data = await asyncio.to_thread(_http_get, url, 40)

            if not data.get("ok"):
                consecutive_errors += 1
                wait = min(30, 2 ** consecutive_errors)
                log.warning("Poll error (attempt %d), retrying in %ds", consecutive_errors, wait)
                await asyncio.sleep(wait)
                continue

            consecutive_errors = 0
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    asyncio.create_task(_safe_handle_callback(update))
                else:
                    asyncio.create_task(_safe_handle(update))

        except asyncio.CancelledError:
            log.info("Polling loop cancelled")
            raise
        except Exception as e:
            log.exception("Polling loop error: %s", e)
            await asyncio.sleep(5)


async def _safe_handle(update: dict) -> None:
    try:
        await handle_message(update)
    except Exception as e:
        log.exception("Handler error: %s", e)


async def _safe_handle_callback(update: dict) -> None:
    try:
        await handle_callback(update)
    except Exception as e:
        log.exception("Callback handler error: %s", e)
