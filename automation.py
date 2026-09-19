"""
FF TopUp Automation — Garena Shop (Bangladesh)

All behaviour is controlled via .env:
  • WEBRENDER=true|false   → full render vs plain text
  • HEADLESS=true|false    → handled by browser_pool (process-wide)
  • BLOCK_TRACKERS=true|false
  • All timeouts / pauses / type delays / poll intervals from .env

Browser is reused via browser_pool (shared Chromium). The context is
always released back to the pool in `finally`, so the idle rotator
works for every caller (FastAPI, Telegram bot, CLI).

Exports:
  - process_freefire_topup(uid, pkg, voucher, pin="") -> JSON string
  - parse_voucher(code, pin="") -> (clean_serial, clean_pin)
  - detect_provider(clean_serial) -> "unipin" | "upgift" | None
"""

import sys
import asyncio
import json
import re
import os
from typing import Optional

from browser_pool import get_browser, new_stealth_context, release_context

UNIPIN_PASSTHROUGH = ["unipin.com", "cdn.unipin.com", "static.unipin.com"]


# ============================================================
# ENV HELPERS
# ============================================================
def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


# ============================================================
# VOUCHER HELPERS
# ============================================================
def parse_voucher(code: str, pin: str = "") -> tuple:
    """
    Normalise a voucher into (serial, pin).

    Accepts any of:
        "BDMB-P-S-16413059 2692-9479-3554-7960"
        "BDMB-P-S-16413059", pin="2692-9479-3554-7960"
        "BDMBPS16413059, 2692..."     (comma separated)

    If `pin` is supplied AND `code` also contains a whitespace/comma
    separator, the embedded pin in `code` wins (it's more specific).
    """
    code = (code or "").strip()
    pin = (pin or "").strip()

    # Always try to split the code first
    parts = re.split(r"[\s,]+", code, maxsplit=1)
    serial_part = parts[0]
    embedded_pin = parts[1].strip() if len(parts) > 1 else ""

    # Prefer the pin embedded in the code; fall back to the explicit arg
    pin = embedded_pin or pin

    clean_serial = re.sub(r"[^A-Za-z0-9]", "", serial_part).upper()
    clean_pin = re.sub(r"[^A-Za-z0-9]", "", pin)
    return clean_serial, clean_pin


def detect_provider(clean_serial: str) -> Optional[str]:
    if clean_serial.startswith("BDMB"):
        return "unipin"
    if clean_serial.startswith("UPBD"):
        return "upgift"
    return None


# ============================================================
# ROUTE HANDLER (respects WEBRENDER + BLOCK_* flags)
# ============================================================
async def _route_handler(route):
    """
    Full-render (WEBRENDER=true):
        Block only trackers. Everything else loads.
    Plain-text (WEBRENDER=false):
        Block trackers + images + stylesheets + fonts + media.
        UniPin is always whitelisted (its JS must run to submit).
    """
    try:
        req = route.request
        url = req.url.lower()
        rtype = (req.resource_type or "").lower()

        # UniPin always gets everything
        if any(d in url for d in UNIPIN_PASSTHROUGH):
            await route.continue_()
            return

        # Trackers
        if _bool("BLOCK_TRACKERS", True):
            tracker_domains = (
                "google-analytics.com",
                "googletagmanager.com",
                "doubleclick.net",
                "hotjar.com",
                "clarity.ms",
                "sentry.io",
                "segment.io",
                "mixpanel.com",
                "amplitude.com",
            )
            if any(d in url for d in tracker_domains):
                await route.abort()
                return

        # Plain-text mode → block heavy resource types
        if not _bool("WEBRENDER", True):
            if _bool("BLOCK_IMAGES", True) and rtype in ("image", "imageset", "favicon"):
                await route.abort()
                return
            if _bool("BLOCK_STYLESHEETS", True) and rtype == "stylesheet":
                await route.abort()
                return
            if _bool("BLOCK_FONTS", True) and rtype == "font":
                await route.abort()
                return
            if _bool("BLOCK_MEDIA", True) and rtype == "media":
                await route.abort()
                return

        await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


# ============================================================
# PACKAGE ALIASES
# ============================================================
MEMBERSHIP_ALIASES = {
    "161": "Weekly Membership",
    "800": "Monthly Membership",
}

MEMBERSHIP_TEXT = {
    "weekly":             "Weekly Membership",
    "week":               "Weekly Membership",
    "weekly membership":  "Weekly Membership",
    "monthly":            "Monthly Membership",
    "month":              "Monthly Membership",
    "monthly membership": "Monthly Membership",
}


def _resolve_package_label(diamond_amount: str) -> Optional[str]:
    raw = str(diamond_amount).strip()
    low = raw.lower()
    if raw in MEMBERSHIP_ALIASES:
        return MEMBERSHIP_ALIASES[raw]
    if low in MEMBERSHIP_TEXT:
        return MEMBERSHIP_TEXT[low]
    return None


# ============================================================
# PACKAGE CLICKER
# ============================================================
async def _click_package(page, diamond_amount: str) -> bool:
    raw = str(diamond_amount).strip()
    num_only = re.sub(r"\D", "", raw)

    print(f"🔎 _click_package: raw={raw!r} num_only={num_only!r}")

    label = _resolve_package_label(diamond_amount)
    if label:
        print(f"   ↳ Membership detected → looking for '{label}'")
        return await _click_tile_by_text(page, label)

    if not num_only:
        print(f"⚠️ No numeric value and no membership match: {raw!r}")
        return False

    return await _click_tile_by_text(page, f"{num_only} Diamond")


async def _click_tile_by_text(page, target_text: str) -> bool:
    print(f"\n🔍 Adaptive search for '{target_text}'...")

    dump = await page.evaluate(
        """(target) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const all = Array.from(document.querySelectorAll('*'));
            const hits = all.filter(el => norm(el.textContent) === target);
            if (hits.length === 0) return { found: false };

            hits.sort((a, b) =>
                a.querySelectorAll('*').length - b.querySelectorAll('*').length);

            const chain = [];
            let cur = hits[0];
            let depth = 0;
            while (cur && cur !== document.body && depth < 12) {
                chain.push({
                    depth,
                    tag: cur.tagName ? cur.tagName.toLowerCase() : '',
                    cls: String(cur.className || '').slice(0, 120),
                    id: cur.id || '',
                    role: cur.getAttribute('role') || '',
                });
                cur = cur.parentElement;
                depth++;
            }
            return { found: true, count: hits.length, chain };
        }""",
        target_text,
    )

    if not dump.get("found"):
        print(f"❌ Text '{target_text}' not found on this page")
        print(f"   Current URL: {page.url}")
        sample = await page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                     .map(el => (el.textContent || '').replace(/\\s+/g,' ').trim())
                     .filter(t => t.length < 40 && /\\b\\d+\\s*Diamond\\b|Membership/i.test(t))
                     .slice(0, 10)"""
        )
        print(f"   Nearby candidates: {sample}")
        return False

    print(f"✅ Found {dump.get('count', 0)} matching element(s)")

    print("🎯 Trying each ancestor level...")
    for level in range(0, min(len(dump["chain"]), 12)):
        result = await page.evaluate(
            """({target, level}) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                const all = Array.from(document.querySelectorAll('*'));
                const hits = all.filter(el => norm(el.textContent) === target);
                if (hits.length === 0) return { ok: false, why: 'gone' };
                hits.sort((a, b) =>
                    a.querySelectorAll('*').length - b.querySelectorAll('*').length);

                let cur = hits[0];
                for (let i = 0; i < level; i++) {
                    if (!cur.parentElement) return { ok: false, why: 'no parent' };
                    cur = cur.parentElement;
                    if (cur === document.body) return { ok: false, why: 'body' };
                }

                const info = {
                    tag: cur.tagName ? cur.tagName.toLowerCase() : '',
                    cls: String(cur.className || '').slice(0, 100),
                };

                try { cur.scrollIntoView({block: 'center'}); } catch(e) {}
                try { cur.click(); } catch(e) {}
                try {
                    ['mousedown', 'mouseup', 'click'].forEach(type => {
                        cur.dispatchEvent(new MouseEvent(type, {
                            bubbles: true, cancelable: true, view: window,
                        }));
                    });
                } catch(e) {}

                return { ok: true, ...info };
            }""",
            {"target": target_text, "level": level},
        )

        if not result or not result.get("ok"):
            continue

        await page.wait_for_timeout(2500)

        has_v = await _has_physical_vouchers(page)
        new_tab = len(page.context.pages) > 1
        print(f"   [{level}] <{result['tag']}> cls='{result['cls']}' → vouchers={has_v} newtab={new_tab}")

        if has_v or new_tab:
            print(f"✅✅✅ Package clicked at level {level}")
            return True

    print("\n🔧 Playwright locator fallbacks...")
    for sel in [
        f"text=/{re.escape(target_text)}/i",
        f"text={target_text}",
    ]:
        try:
            loc = page.locator(sel).first
            count = await loc.count()
            if count == 0:
                continue
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.click(timeout=5000, force=True)
            print(f"   ✅ Clicked via {sel}")
            await page.wait_for_timeout(2500)
            if await _has_physical_vouchers(page) or len(page.context.pages) > 1:
                print("✅✅✅ Package clicked via locator fallback")
                return True
        except Exception as e:
            print(f"   ↳ {sel} failed: {e}")

    print("\n❌ Could not click the package tile")
    return False


async def _has_physical_vouchers(page) -> bool:
    try:
        return await page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                     .some(el => (el.textContent || '').toLowerCase().includes('physical voucher'))"""
        )
    except Exception:
        return False


# ============================================================
# MAIN AUTOMATION
# ============================================================
async def process_freefire_topup(player_uid: str, diamond_amount: str,
                                 voucher_code: str, pin_code: str = "") -> str:
    """
    Run a full top-up flow. Returns a JSON string.

    Note: browser visibility (headless) is a process-wide setting owned
    by browser_pool. Configure it via HEADLESS in .env — not per call.
    """
    webrender = _bool("WEBRENDER", True)

    # ---- Tunables from .env ----
    t_nav             = _int("TIMEOUT_NAV", 60000)
    t_short           = _int("TIMEOUT_SHORT", 45000)   # Playwright timeouts (ms)
    t_result          = _int("TIMEOUT_RESULT", 90000)
    checkout_deadline = t_short / 1000.0               # loop deadline (seconds)

    p_page    = _int("PAUSE_AFTER_PAGE", 800)
    p_uid     = _int("PAUSE_AFTER_UID", 2500)
    p_login   = _int("PAUSE_AFTER_LOGIN", 600)
    p_proceed = _int("PAUSE_AFTER_PROCEED", 400)
    p_diamond = _int("PAUSE_AFTER_DIAMOND", 300)
    p_tab     = _int("PAUSE_AFTER_TAB", 300)
    p_provider= _int("PAUSE_AFTER_PROVIDER", 300)
    p_serial  = _int("PAUSE_AFTER_SERIAL", 100)
    p_pin     = _int("PAUSE_AFTER_PIN", 100)
    p_confirm = _int("PAUSE_BEFORE_CONFIRM", 80)
    poll_ms   = _int("RESULT_POLL_INTERVAL", 150)

    type_uid  = _int("TYPE_DELAY_UID", 15)
    type_pin  = _int("TYPE_DELAY_PIN", 10)

    # ---- Normalize voucher up-front (single source of truth) ----
    clean_serial, clean_pin = parse_voucher(voucher_code, pin_code)
    provider = detect_provider(clean_serial)
    if not provider:
        return json.dumps({
            "success": False,
            "reason": "INVALID_PREFIX",
            "message": "Invalid Voucher Prefix",
        })

    # ---- Get shared browser + fresh context ----
    browser = await get_browser()
    context = await new_stealth_context(browser)
    page = await context.new_page()

    # Route handler installed only when needed
    need_routes = (not webrender) or _bool("BLOCK_TRACKERS", False)
    if need_routes:
        await context.route("**/*", _route_handler)
        mode = "PLAIN-TEXT" if not webrender else "FULL (trackers blocked)"
        print(f"🔒 Route handler installed — mode: {mode}")

    try:
        # ---------- 1. Open Garena ----------
        await page.goto(
            "https://shop.garena.my/?app=100067&channel=202953",
            wait_until="domcontentloaded",
            timeout=t_nav,
        )
        await page.wait_for_timeout(p_page)

        # ---------- 2. UID ----------
        uid_input = page.locator(
            "input[placeholder*='player ID'], "
            "input[placeholder*='Player ID'], "
            "input[type='text']"
        ).first
        await uid_input.wait_for(timeout=15000)
        await uid_input.click()
        await uid_input.type(str(player_uid), delay=type_uid)
        await page.wait_for_timeout(p_uid)

        # ---------- 3. Login ----------
        login_btn = page.locator(
            "button:has-text('Login'), div[role='button']:has-text('Login'), .login-btn"
        ).first
        if await login_btn.is_visible():
            await login_btn.click()
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(p_login)

        # ---------- 4. Proceed to Payment ----------
        proceed_btn = page.locator(
            "button:has-text('Proceed to Payment'), "
            "div[role='button']:has-text('Proceed to Payment'), "
            "button:has-text('Login')"
        ).first
        await proceed_btn.wait_for(timeout=t_short)

        pages_before = len(context.pages)
        await proceed_btn.click()
        await page.wait_for_timeout(p_proceed)

        if len(context.pages) > pages_before:
            page = context.pages[-1]
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(p_proceed)

        # ---------- 5. Package selection ----------
        print(f"💎 Selecting package: {diamond_amount!r}")
        picked = await _click_package(page, diamond_amount)
        if not picked:
            return json.dumps({
                "success": False,
                "reason": "PACKAGE_NOT_FOUND",
                "message": f"Could not click package '{diamond_amount}'",
            })

        await page.wait_for_timeout(p_diamond)

        # ---------- 6. Physical Vouchers tab ----------
        physical_voucher_tab = page.locator("text=/Physical Vouchers?/i").first
        await physical_voucher_tab.wait_for(timeout=t_short)
        await physical_voucher_tab.click(force=True)
        await page.wait_for_timeout(p_tab)

        # ---------- 7. Provider ----------
        if provider == "unipin":
            await page.locator("text=/UniPin/i").first.click(force=True)
        elif provider == "upgift":
            await page.locator("text=/UP Gift/i").first.click(force=True)

        await page.wait_for_timeout(p_provider)

        # ---------- 8. Find checkout frame ----------
        print("⏳ Looking for checkout frame with serial input...")

        SERIAL_SELECTOR = (
            "input#serial, "
            "input[name='serial'], "
            "input[placeholder*='Serial'], "
            "input[placeholder*='UPBD']"
        )

        target_scope = None
        serial_input = None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + checkout_deadline
        while loop.time() < deadline:
            for scope in [page] + list(page.frames):
                try:
                    loc = scope.locator(SERIAL_SELECTOR).first
                    if await loc.count() > 0:
                        target_scope = scope
                        serial_input = loc
                        break
                except Exception:
                    continue
            if serial_input is not None:
                break
            await page.wait_for_timeout(300)

        if serial_input is None:
            return json.dumps({
                "success": False,
                "reason": "CHECKOUT_NOT_FOUND",
                "message": "Serial input never appeared after package click.",
            })

        frame_url = getattr(target_scope, "url", "main")
        print(f"✅ Using frame: {str(frame_url)[:100]}")

        # ---------- 9. Serial ----------
        await serial_input.click()
        await serial_input.fill(clean_serial)
        await page.wait_for_timeout(p_serial)
        print(f"✅ Serial filled: {clean_serial}")

        # ---------- 10. PIN ----------
        pin_inputs = target_scope.locator(
            "input[type='password'], input[name*='pin'], input[id*='pin']"
        )
        pin_count = await pin_inputs.count()
        print(f"🔎 PIN inputs found: {pin_count}")

        if pin_count >= 4 and len(clean_pin) >= 12:
            chunks = [clean_pin[i:i+4] for i in range(0, len(clean_pin), 4)]
            for idx, chunk in enumerate(chunks[:4]):
                inp = pin_inputs.nth(idx)
                await inp.click()
                await inp.fill(chunk)
                await page.wait_for_timeout(type_pin)
        elif pin_count > 0:
            await pin_inputs.first.click()
            if len(clean_pin) == 16:
                formatted = "-".join(clean_pin[i:i+4] for i in range(0, 16, 4))
                await pin_inputs.first.fill(formatted)
            else:
                await pin_inputs.first.fill(clean_pin)

        await page.wait_for_timeout(p_pin)
        await page.wait_for_timeout(p_confirm)

        # ---------- 11. Confirm ----------
        confirm_btn = target_scope.locator(
            "input[type='submit'][value='Confirm'], input[value='Confirm']"
        ).first
        if await confirm_btn.is_visible(timeout=3000):
            await confirm_btn.scroll_into_view_if_needed()
            await confirm_btn.click(force=True)
            print("✅ Confirm clicked")
        else:
            await target_scope.evaluate(
                """() => {
                    const b = document.querySelector("input[type='submit'][value='Confirm']") ||
                              document.querySelector("input[value='Confirm']");
                    if (b) b.click();
                }"""
            )
            print("✅ Confirm clicked via JS")

        # ---------- 12. Result polling (fast first 5s, then normal) ----------
        result = None
        deadline = loop.time() + (t_result / 1000.0)
        fast_until = loop.time() + 5.0
        poll_fast = _int("RESULT_POLL_INTERVAL_FAST", poll_ms)

        while loop.time() < deadline:
            current = None
            for frame in page.frames:
                if "unibox" in (frame.url or "").lower():
                    current = frame
                    break
            if current is None:
                current = page

            try:
                frame_html = await current.content()
            except Exception:
                frame_html = ""
            try:
                main_html = await page.content()
            except Exception:
                main_html = ""

            raw_text = frame_html + "\n" + main_html
            text = raw_text.lower()
            text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)

            url = ((current.url or "") + " " + (page.url or "")).lower()

            # Consumed / already used
            if ("consumed voucher" in text or "already consumed" in text
                    or "voucher has been used" in text):
                result = {"success": False, "reason": "CONSUMED_VOUCHER",
                          "message": "Voucher is already consumed/used."}
                break

            # Invalid serial / pin
            if "invalid serial" in text or "invalid pin" in text:
                result = {"success": False, "reason": "INVALID_VOUCHER",
                          "message": "Invalid voucher code. Check the serial and PIN."}
                break

            # UniPin danger alert
            if 'alert-danger' in raw_text.lower() or 'role="alert"' in raw_text.lower():
                alert_msg = ""
                try:
                    m = re.search(
                        r'<div[^>]*class="[^"]*alert[^"]*alert-danger[^"]*"[^>]*>(.*?)</div>',
                        raw_text, flags=re.DOTALL | re.IGNORECASE,
                    )
                    if m:
                        alert_msg = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
                        alert_msg = re.sub(r"\s+", " ", alert_msg)
                except Exception:
                    pass
                if alert_msg:
                    result = {"success": False, "reason": "ALERT_ERROR",
                              "message": alert_msg[:200]}
                    break

            if "already used" in text or "already redeemed" in text:
                result = {"success": False, "reason": "ALREADY_USED",
                          "message": "Voucher has already been used/redeemed."}
                break
            if "expired" in text and "voucher" in text:
                result = {"success": False, "reason": "EXPIRED_VOUCHER",
                          "message": "Voucher has expired."}
                break
            if ("invalid voucher" in text or "invalid code" in text
                    or "transaction not found" in text):
                result = {"success": False, "reason": "INVALID_VOUCHER",
                          "message": "Invalid voucher code. Check the serial and PIN."}
                break

            # Success
            if any(w in text for w in (
                "transaction successful", "topup success", "successfully redeemed",
                "completed successfully", "diamonds added", "credited successfully",
            )):
                result = {"success": True, "message": "Topup Completed Successfully!"}
                break
            if "/success" in url or "/completed" in url:
                result = {"success": True, "message": "Topup Completed Successfully!"}
                break

            # Adaptive sleep
            sleep_ms = poll_fast if loop.time() < fast_until else poll_ms
            await page.wait_for_timeout(sleep_ms)

        if result is None:
            result = {"success": False, "reason": "NO_RESULT",
                      "message": "No result detected within timeout."}

        print(f"📤 Result: {result}")
        return json.dumps(result)

    except Exception as e:
        print(f"❌ Exception: {e}")
        return json.dumps({"success": False, "reason": "ERROR", "message": str(e)})

    finally:
        # 1) Close the context (not the shared browser)
        try:
            await context.close()
        except Exception:
            pass
        # 2) Tell browser_pool this top-up is done, so the idle rotator
        #    can run again. Without this, rotation never fires after the
        #    first Telegram order.
        release_context()


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) > 4:
        p_uid, d_amount, v_serial, v_pin = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
        res = asyncio.run(process_freefire_topup(p_uid, d_amount, v_serial, v_pin))
    elif len(sys.argv) == 4:
        p_uid, d_amount, v_code = sys.argv[1], sys.argv[2], sys.argv[3]
        res = asyncio.run(process_freefire_topup(p_uid, d_amount, v_code))
    else:
        res = json.dumps({"success": False, "reason": "INVALID_PARAMS", "message": "Missing Arguments"})

    print(res)