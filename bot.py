"""
Telegram bot using LONG POLLING.
Supports:
  1. Conversational flow: send UID → pick package → send voucher
  2. Direct command: UID package voucher [pin]
  3. Name lookup: bare UID or /name <uid>

Result message mirrors the web receipt:
    💎 Diamond Top-Up Done!!
    • uid   : ...
    • took  : 2.6s
    Package: weekly membership
    --------------------------------
    Voucher: BDMB-Q-S-16413059
    | 2692-9479-3554-7960
"""

import os
import re
import json
import time
import asyncio
import logging
import urllib.parse
import urllib.request
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from automation import process_freefire_topup, parse_voucher, detect_provider

log = logging.getLogger("tg-bot")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_ALLOWED_USERS = {
    x.strip() for x in os.getenv("TG_ALLOWED_USERS", "").split(",") if x.strip()
}

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}" if TG_BOT_TOKEN else ""

# ------------------------------------------------------------
# PLAYER NAME API — multi-endpoint, configured via .env
# ------------------------------------------------------------
_DEFAULT_PLAYER_API = "https://ffnamecheck.vercel.app/info?uid={uid}"

PLAYER_API_URLS = [
    u.strip()
    for u in os.getenv("PLAYER_API_URLS", _DEFAULT_PLAYER_API).split(",")
    if u.strip()
] or [_DEFAULT_PLAYER_API]

PLAYER_API_REGION = os.getenv("PLAYER_API_REGION", "").strip()


# ============================================================
# SESSION STATE (in-memory, per chat)
# ============================================================
# Structure: { chat_id: { "stage": "...", "uid": "...", "name": "...",
#                          "package": "...", "verified": bool } }
SESSIONS: dict = {}


def session_get(chat_id: str) -> dict:
    return SESSIONS.setdefault(chat_id, {})


def session_clear(chat_id: str) -> None:
    SESSIONS.pop(chat_id, None)


def session_set(chat_id: str, **kwargs) -> None:
    SESSIONS.setdefault(chat_id, {}).update(kwargs)


# ============================================================
# PACKAGE OPTIONS (inline buttons)
# ============================================================
PACKAGE_BUTTONS = [
    ("25 💎",   "pkg:25"),
    ("50 💎",   "pkg:50"),
    ("115 💎",  "pkg:115"),
    ("240 💎",  "pkg:240"),
    ("610 💎",  "pkg:610"),
    ("1240 💎", "pkg:1240"),
    ("2530 💎", "pkg:2530"),
    ("Weekly 💳",  "pkg:weekly"),
    ("Monthly 💳", "pkg:monthly"),
]

# Split into rows of 3 for the inline keyboard
PACKAGE_KEYBOARD_ROWS = [
    PACKAGE_BUTTONS[0:3],
    PACKAGE_BUTTONS[3:6],
    PACKAGE_BUTTONS[6:9],
]


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
    except Exception as e:
        log.warning("HTTP POST failed: %s", e)
        return {"ok": False}


async def send_telegram(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    if not TG_API:
        return
    url = f"{TG_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    await asyncio.to_thread(_http_post, url, payload)


async def send_telegram_with_buttons(
    chat_id: str,
    text: str,
    buttons: list,
    parse_mode: str = "Markdown",
) -> None:
    """
    buttons: list of rows. Each row is a list of (label, callback_data) tuples.
    """
    if not TG_API:
        return
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
        "parse_mode": parse_mode,
        "reply_markup": reply_markup,
    }
    await asyncio.to_thread(_http_post, url, payload)


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
    parse_mode: str = "Markdown",
) -> None:
    if not TG_API:
        return
    url = f"{TG_API}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await asyncio.to_thread(_http_post, url, payload)


# ============================================================
# PLAYER NAME FETCH (multi-endpoint, smart fallback)
# ============================================================
def _is_bad_player_payload(data) -> bool:
    if not isinstance(data, dict):
        return True
    nick = str(data.get("nickname") or "").strip()
    if not nick:
        return True
    # Reject values the third-party APIs sometimes return on failure
    if re.match(r"^HTTP\s+Error", nick, re.IGNORECASE):
        return True
    if re.search(r"not\s*found", nick, re.IGNORECASE) and len(nick) < 30:
        return True
    return False


def _build_player_url(template: str, uid: str) -> str:
    url = template.replace("{uid}", urllib.parse.quote(uid))
    # Append region if not already present and PLAYER_API_REGION is configured
    if PLAYER_API_REGION and not re.search(r"[?&]region=", url, re.IGNORECASE):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}region={urllib.parse.quote(PLAYER_API_REGION)}"
    return url


async def fetch_player_info(uid: str) -> Optional[dict]:
    uid = re.sub(r"\D", "", str(uid or ""))
    if not uid or len(uid) < 6:
        return None

    for template in PLAYER_API_URLS:
        url = _build_player_url(template, uid)
        data = await asyncio.to_thread(_http_get, url, 15)
        if _is_bad_player_payload(data):
            log.debug("Player API failed/skipped: %s", url)
            continue
        # Ensure 'uid' is present so downstream code can display it
        data.setdefault("uid", uid)
        return data

    return None


def _escape_md(text) -> str:
    if text is None:
        return ""
    return re.sub(r"([_\*`\[\]])", r"\\\1", str(text))


# ============================================================
# FORMATTERS — mirror the web receipt
# ============================================================
def _prettify_package(pkg: str) -> str:
    """weekly → weekly membership, 115 → 115 diamond, etc."""
    if not pkg:
        return "—"
    v = str(pkg).strip()
    low = v.lower()
    if low in ("weekly", "weekly membership", "week", "161"):
        return "weekly membership"
    if low in ("monthly", "monthly membership", "month", "800"):
        return "monthly membership"
    if re.fullmatch(r"\d+", v):
        return f"{v} diamond"
    return v


def _split_voucher(code: str) -> tuple:
    """Return (serial, pin) from a raw voucher string."""
    v = (code or "").strip()
    if not v:
        return ("", "")
    m = re.match(r"^([^\s,]+)[\s,]+(.+)$", v)
    if m:
        return (m.group(1).strip(), m.group(2).strip())
    return (v, "")


def msg_result_new(uid: str, package_label: str, res: dict,
                   voucher_serial: str, voucher_pin: str,
                   elapsed_secs: str) -> str:
    """
    Build the Telegram result message matching the web receipt:
        💎 Diamond Top-Up Done!!
        • uid   : ...
        • took  : 2.6s
        Package: weekly membership
        --------------------------------
        Voucher: BDMB-Q-S-16413059
        | 2692-9479-3554-7960
    """
    ok = bool(res.get("success"))

    if ok:
        head = "💎 *Dɪᴀᴍᴏɴᴅ Tᴏᴘ-Uᴘ Dᴏɴᴇ ! !*"
    else:
        reason = (res.get("reason") or "FAILED").upper()
        head = f"❌ *Tᴏᴘ-Uᴘ Fᴀɪʟᴇᴅ*  —  `{_escape_md(reason)}`"

    lines = [
        head,
        "",
        f"• ᴜɪᴅ   : `{_escape_md(uid)}`",
        f"• ᴛᴏᴏᴋ  : `{elapsed_secs}s`",
        "",
        f"Package: `{_escape_md(package_label)}`",
        "──────────────────",
        f"Voucher: `{_escape_md(voucher_serial)}`",
    ]
    if voucher_pin:
        lines.append(f"| `{_escape_md(voucher_pin)}`")

    # NOTE: The Playwright call log (res["message"]) is intentionally NOT
    # shown to Telegram users. It contains internal browser automation
    # details that are noisy and not useful to end users.
    return "\n".join(lines)


# ============================================================
# MESSAGE BUILDERS
# ============================================================
def msg_player_info(data: dict, requested_uid: str) -> str:
    nickname = data.get("nickname", "—")
    uid = data.get("uid", requested_uid)
    level = data.get("AccountLevel", "")
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
        "*Two ways to order:*\n\n"
        "*1. Interactive:*\n"
        "Send your UID → pick package → send voucher\n\n"
        "*2. Direct command:*\n"
        "`UID PACKAGE VOUCHER`\n\n"
        "*Examples:*\n"
        "`3761036809`\n"
        "`3761036809 115 UPBD-P-S-01989133 1527-6324-9731-2625`\n"
        "`3761036809 weekly UPBD-P-S-01989133 1527-6324-9731-2625`\n"
        "`3761036809 monthly UPBD-P-S-01989133 1527-6324-9731-2625`\n\n"
        "*Other commands:*\n"
        "`/name <uid>` — player name\n"
        "`/id` — your chat/user ID\n"
        "`/cancel` — cancel current flow\n\n"
        "⚡ Packages: 25, 30, 115, 240, 610, 1240, 2530, weekly, monthly"
    )


def msg_ask_uid() -> str:
    return (
        "📱 *Send the Free Fire UID*\n\n"
        "Example: `3761036809`\n\n"
        "Send /cancel to stop."
    )


def msg_ask_package(uid: str, name: Optional[str]) -> str:
    if name:
        return (
            f"👤 *Player:* `{_escape_md(name)}`\n"
            f"🆔 *UID:* `{uid}`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎯 *Choose a package:*"
        )
    return (
        f"🆔 *UID:* `{uid}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 *Choose a package:*"
    )


def msg_ask_voucher(uid: str, package: str, name: Optional[str]) -> str:
    name_line = f"👤 {_escape_md(name)}\n" if name else ""
    label = _prettify_package(package)
    return (
        f"{name_line}"
        f"🆔 *UID:* `{uid}`\n"
        f"📦 *Package:* `{label}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎟 *Send the voucher code now.*\n\n"
        "Format:\n"
        "`UPBD-P-S-01989133 1527-6324-9731-2625`\n"
        "or just the serial + pin.\n\n"
        "Send /cancel to stop."
    )


def msg_confirm_continue(uid: str) -> str:
    return (
        "⚠️ *Unable to fetch name*\n\n"
        f"Could not get the player name for UID `{uid}`.\n"
        "Are you sure you want to continue?"
    )


def msg_received(uid: str, package: str, code: str) -> str:
    """Short 'processing' message — the final result uses msg_result_new()."""
    short = code[:30] + ("…" if len(code) > 30 else "")
    label = _prettify_package(package)
    return (
        "🚀 *Processing Top-Up*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 UID: `{_escape_md(uid)}`\n"
        f"📦 Package: `{_escape_md(label)}`\n"
        f"🎟 Voucher: `{_escape_md(short)}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Please wait..."
    )


def msg_invalid_voucher() -> str:
    return (
        "❌ *Invalid voucher prefix*\n\n"
        "Voucher must start with `UPBD` or `BDMB`.\n\n"
        "Example:\n"
        "`UPBD-P-S-01989133 1527-6324-9731-2625`"
    )


def msg_cancelled() -> str:
    return "🚫 Cancelled. Send /start to begin again."


# ============================================================
# PACKAGE NORMALIZATION
# ============================================================
def normalize_package(pkg: str) -> Optional[str]:
    pkg = (pkg or "").strip().lower()

    # Direct numbers
    num_match = re.match(r"^(\d+)\s*(?:💎|diamonds?|d)?$", pkg)
    if num_match:
        return num_match.group(1)

    # Aliases
    if pkg in ("weekly", "week", "w", "161"):
        return "weekly"
    if pkg in ("monthly", "month", "m", "800"):
        return "monthly"

    return None


# ============================================================
# CORE TOPUP RUNNER — measures elapsed time
# ============================================================
async def run_topup(chat_id: str, uid: str, package: str, voucher: str) -> None:
    # Pre-check: fail fast without spinning up a browser if the
    # voucher prefix is obviously wrong.
    clean_serial, _ = parse_voucher(voucher)
    if not detect_provider(clean_serial):
        await send_telegram(chat_id, msg_invalid_voucher())
        return

    # Send the "processing" message first
    await send_telegram(chat_id, msg_received(uid, package, voucher))

    # Measure elapsed time around the automation call.
    # NOTE: process_freefire_topup now expects the RAW voucher string
    # (it parses it internally via parse_voucher). It also no longer
    # accepts a `headless` kwarg — browser visibility is a process-wide
    # setting owned by browser_pool and read from .env.
    t0 = time.perf_counter()
    try:
        res = await process_freefire_topup(uid, package, voucher)
        if isinstance(res, str):
            res = json.loads(res)
    except Exception as e:
        log.exception("Topup failed")
        res = {"success": False, "reason": "ERROR", "message": str(e)}
    elapsed_secs = f"{time.perf_counter() - t0:.1f}"

    # Prepare voucher display pieces
    serial_for_display, pin_for_display = _split_voucher(voucher)
    package_label = _prettify_package(package)

    # Final result — same layout as the web receipt
    await send_telegram(
        chat_id,
        msg_result_new(
            uid=uid,
            package_label=package_label,
            res=res,
            voucher_serial=serial_for_display,
            voucher_pin=pin_for_display,
            elapsed_secs=elapsed_secs,
        ),
    )

    # Admin notification
    if TG_CHAT_ID and TG_CHAT_ID != chat_id:
        try:
            await send_telegram(
                TG_CHAT_ID,
                f"📢 *User order*\nChat: `{chat_id}`\nUID: `{uid}`\n"
                f"Package: `{package_label}`\n"
                f"Took: `{elapsed_secs}s`\n"
                f"Status: `{'OK' if res.get('success') else res.get('reason')}`",
            )
        except Exception:
            pass

    session_clear(chat_id)


# ============================================================
# FLOW STEPS
# ============================================================
async def flow_start(chat_id: str) -> None:
    session_clear(chat_id)
    session_set(chat_id, stage="awaiting_uid")
    await send_telegram(chat_id, msg_ask_uid())


async def flow_after_uid(chat_id: str, uid: str) -> None:
    uid = re.sub(r"\D", "", uid)
    if not uid or len(uid) < 6:
        await send_telegram(chat_id, "❌ Invalid UID. Please send a 6–15 digit UID.")
        return

    await send_telegram(chat_id, f"🔍 Looking up `{uid}`...")
    info = await fetch_player_info(uid)

    if info:
        name = info.get("nickname", "")
        session_set(chat_id, stage="awaiting_package", uid=uid, name=name, verified=True)
        await send_telegram(chat_id, msg_player_info(info, uid))
        await send_telegram_with_buttons(
            chat_id,
            "🎯 *Choose a package:*",
            PACKAGE_KEYBOARD_ROWS,
        )
    else:
        session_set(chat_id, stage="awaiting_confirm", uid=uid, name=None)
        await send_telegram_with_buttons(
            chat_id,
            msg_confirm_continue(uid),
            [[
                ("✅ Yes, continue", "confirm:yes"),
                ("❌ No, cancel", "confirm:no"),
            ]],
        )


async def flow_after_confirm(chat_id: str, uid: str, name: Optional[str]) -> None:
    session_set(chat_id, stage="awaiting_package", uid=uid, name=name, verified=False)
    await send_telegram_with_buttons(
        chat_id,
        msg_ask_package(uid, name),
        PACKAGE_KEYBOARD_ROWS,
    )


async def flow_package_chosen(chat_id: str, package: str) -> None:
    sess = session_get(chat_id)
    uid = sess.get("uid")
    name = sess.get("name")
    if not uid:
        await send_telegram(chat_id, "❌ Session expired. Send /start to begin.")
        session_clear(chat_id)
        return

    session_set(chat_id, stage="awaiting_voucher", package=package)
    await send_telegram(chat_id, msg_ask_voucher(uid, package, name))


async def flow_voucher_received(chat_id: str, voucher: str) -> None:
    sess = session_get(chat_id)
    uid = sess.get("uid")
    package = sess.get("package")
    if not uid or not package:
        await send_telegram(chat_id, "❌ Session expired. Send /start to begin.")
        session_clear(chat_id)
        return

    await run_topup(chat_id, uid, package, voucher)


# ============================================================
# MESSAGE HANDLER
# ============================================================
async def handle_message(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    sender = msg.get("from") or {}
    user_id = str(sender.get("id", ""))
    text = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return

    # Auth
    if TG_ALLOWED_USERS and user_id not in TG_ALLOWED_USERS:
        await send_telegram(chat_id, "⛔ You are not authorized to use this bot.")
        log.warning("Unauthorized user: %s", user_id)
        return

    # ----- Global commands -----
    if text in ("/start", "/help"):
        session_clear(chat_id)
        await send_telegram(chat_id, msg_welcome())
        return

    if text == "/id":
        await send_telegram(
            chat_id,
            f"Your chat ID: `{chat_id}`\nYour user ID: `{user_id}`",
        )
        return

    if text == "/cancel":
        session_clear(chat_id)
        await send_telegram(chat_id, msg_cancelled())
        return

    if text == "/order":
        await flow_start(chat_id)
        return

    # /name <uid>
    name_cmd = re.match(r"^/name(?:@\w+)?\s+(\d{6,15})$", text)
    if name_cmd:
        uid = name_cmd.group(1)
        await send_telegram(chat_id, f"🔍 Looking up `{uid}`...")
        info = await fetch_player_info(uid)
        if info:
            await send_telegram(chat_id, msg_player_info(info, uid))
        else:
            await send_telegram(chat_id, msg_player_not_found(uid))
        return

    # ============================================================
    # DIRECT COMMAND: UID PACKAGE VOUCHER...
    # ============================================================
    order_match = re.match(
        r"^(\d{6,15})\s+([A-Za-z0-9]+)\s+(.+)$",
        text,
        re.DOTALL,
    )
    if order_match:
        uid_raw = order_match.group(1)
        pkg_raw = order_match.group(2)
        voucher = order_match.group(3).strip()

        package = normalize_package(pkg_raw)
        if package:
            clean_serial, _ = parse_voucher(voucher)
            if not detect_provider(clean_serial):
                await send_telegram(chat_id, msg_invalid_voucher())
                return
            session_clear(chat_id)
            await run_topup(chat_id, uid_raw, package, voucher)
            return

    # ============================================================
    # CONVERSATIONAL FLOW
    # ============================================================
    sess = session_get(chat_id)
    stage = sess.get("stage")

    # ------- Stage: awaiting_uid -------
    if stage == "awaiting_uid":
        if re.match(r"^\d{6,15}$", text):
            await flow_after_uid(chat_id, text)
            return
        await send_telegram(
            chat_id,
            "❌ That doesn't look like a UID.\n"
            "Send a 6–15 digit UID, or /cancel.",
        )
        return

    # ------- Stage: awaiting_confirm -------
    if stage == "awaiting_confirm":
        low = text.lower()
        uid = sess.get("uid")
        if low in ("yes", "y", "ok", "continue", "✅"):
            await flow_after_confirm(chat_id, uid, sess.get("name"))
            return
        if low in ("no", "n", "cancel", "❌"):
            session_clear(chat_id)
            await send_telegram(chat_id, msg_cancelled())
            return
        if re.match(r"^\d{6,15}$", text):
            await flow_after_uid(chat_id, text)
            return
        await send_telegram(
            chat_id,
            "Please tap ✅ Yes or ❌ No above, or send /cancel.",
        )
        return

    # ------- Stage: awaiting_package -------
    if stage == "awaiting_package":
        pkg = normalize_package(text)
        if pkg:
            await flow_package_chosen(chat_id, pkg)
            return
        if re.match(r"^\d{6,15}$", text):
            await flow_after_uid(chat_id, text)
            return
        await send_telegram(
            chat_id,
            "❌ Please choose a package using the buttons above, "
            "or type: 25 / 30 / 115 / 240 / 610 / 1240 / 2530 / weekly / monthly",
        )
        return

    # ------- Stage: awaiting_voucher -------
    if stage == "awaiting_voucher":
        clean_serial, _ = parse_voucher(text)
        if not detect_provider(clean_serial):
            await send_telegram(chat_id, msg_invalid_voucher())
            return
        await flow_voucher_received(chat_id, text)
        return

    # ============================================================
    # NO STAGE — BAREWORD / BARE UID
    # ============================================================
    bare_uid = re.match(r"^(\d{6,15})$", text)
    if bare_uid:
        await flow_after_uid(chat_id, bare_uid.group(1))
        return

    await send_telegram(
        chat_id,
        "❌ *Invalid format*\n\n"
        "*Try:*\n"
        "• `3761036809` — start order (pick package with buttons)\n"
        "• `/order` — same as above\n"
        "• `3761036809 115 UPBD-P-S-01989133 1527-6324-9731-2625` — direct order\n"
        "• `/name 3761036809` — player name\n\n"
        "Send /help for full instructions.",
    )


# ============================================================
# CALLBACK QUERY HANDLER (inline buttons)
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

    # ---------- Package button ----------
    if data.startswith("pkg:"):
        pkg = data.split(":", 1)[1]
        sess = session_get(chat_id)
        if sess.get("stage") != "awaiting_package":
            await answer_callback_query(callback_id, "Session expired")
            return
        await answer_callback_query(callback_id, f"Selected: {_prettify_package(pkg)}")
        session_set(chat_id, package=pkg, stage="awaiting_voucher")
        if message_id:
            new_text = msg_ask_voucher(
                sess.get("uid"),
                pkg,
                sess.get("name"),
            )
            await edit_message_text(chat_id, message_id, new_text)
        return

    # ---------- Confirm yes/no ----------
    if data.startswith("confirm:"):
        choice = data.split(":", 1)[1]
        sess = session_get(chat_id)
        uid = sess.get("uid")

        if choice == "yes":
            await answer_callback_query(callback_id, "Continuing...")
            if message_id:
                await edit_message_text(
                    chat_id, message_id,
                    f"✅ Continuing with UID `{uid}` (name not verified)",
                )
            await flow_after_confirm(chat_id, uid, None)
        else:
            await answer_callback_query(callback_id, "Cancelled")
            if message_id:
                await edit_message_text(chat_id, message_id, msg_cancelled())
            session_clear(chat_id)
        return

    await answer_callback_query(callback_id, "Unknown action")


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

    await asyncio.to_thread(
        _http_get, f"{TG_API}/deleteWebhook?drop_pending_updates=true", 10
    )
    await asyncio.to_thread(
        _http_get, f"{TG_API}/getUpdates?offset=-1&timeout=1", 10
    )

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
                log.warning(
                    "Poll error (attempt %d), retrying in %ds",
                    consecutive_errors, wait,
                )
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