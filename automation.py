import sys
import asyncio
import json
import re
import os
import time
import logging
from typing import Optional, List, Dict, Any, Callable

from browser_pool import get_browser, new_stealth_context, release_context
from pkg import detect_package, detect_package_amount, is_supported_prefix

log = logging.getLogger("automation")

UNIPIN_PASSTHROUGH = [
    "unipin.com",
    "cdn.unipin.com",
    "static.unipin.com",
    "upgift.com",
    "garena.my",
    "shop.garena.my",
]


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
        UniPin / UP Gift are always whitelisted (JS must run to submit).
    """
    try:
        req = route.request
        url = req.url.lower()
        rtype = (req.resource_type or "").lower()

        # UniPin and essential checkout domains always pass
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
    if "weekly" in low:
        return "Weekly Membership"
    if "monthly" in low:
        return "Monthly Membership"
    return None


# ============================================================
# PACKAGE CLICKER
# ============================================================
async def _click_package(page, diamond_amount: str) -> bool:
    raw = str(diamond_amount).strip()
    num_only = re.sub(r"\D", "", raw)

    log.info("🔎 _click_package: raw=%r num_only=%r", raw, num_only)

    label = _resolve_package_label(diamond_amount)
    if label:
        log.info("   ↳ Membership detected → looking for %r", label)
        return await _click_tile_by_text(page, label)

    if not num_only:
        log.warning("⚠️ No numeric value and no membership match: %r", raw)
        return False

    return await _click_tile_by_text(page, f"{num_only} Diamond")


async def _click_tile_by_text(page, target_text: str) -> bool:
    log.info("🔍 Adaptive search for %r...", target_text)

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
        log.warning("❌ Text %r not found on this page (URL: %s)", target_text, page.url)
        return False

    log.info("✅ Found %d matching element(s)", dump.get("count", 0))

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
        log.info("   [%d] <%s> cls=%r → vouchers=%s newtab=%s",
                 level, result.get("tag"), result.get("cls"), has_v, new_tab)

        if has_v or new_tab:
            log.info("✅ Package clicked at level %d", level)
            return True

    # Playwright locator fallbacks
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
            await page.wait_for_timeout(2500)
            if await _has_physical_vouchers(page) or len(page.context.pages) > 1:
                log.info("✅ Package clicked via locator fallback: %s", sel)
                return True
        except Exception:
            pass

    log.error("❌ Could not click the package tile: %s", target_text)
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
# REDEEM SINGLE VOUCHER FLOW (ON ACTIVE PAGE)
# ============================================================
async def _redeem_single_voucher_on_page(
    page,
    context,
    package: str,
    provider: str,
    clean_serial: str,
    clean_pin: str,
    params: dict,
) -> dict:
    """
    Submits a single voucher on the active payment page.
    Assumes user is already at or entering package selection.
    """
    t_short    = params["t_short"]
    t_result   = params["t_result"]
    p_diamond  = params["p_diamond"]
    p_tab      = params["p_tab"]
    p_provider = params["p_provider"]
    p_serial   = params["p_serial"]
    p_pin      = params["p_pin"]
    p_confirm  = params["p_confirm"]
    poll_ms    = params["poll_ms"]
    poll_fast  = params["poll_fast"]
    type_pin   = params["type_pin"]
    checkout_deadline = t_short / 1000.0

    # 1. Package Selection
    log.info("💎 Selecting package: %r", package)
    picked = await _click_package(page, package)
    if not picked:
        return {
            "success": False,
            "reason": "PACKAGE_NOT_FOUND",
            "message": f"Could not click package '{package}'",
        }
    await page.wait_for_timeout(p_diamond)

    # 2. Physical Vouchers Tab
    physical_voucher_tab = page.locator("text=/Physical Vouchers?/i").first
    try:
        await physical_voucher_tab.wait_for(timeout=t_short)
        await physical_voucher_tab.click(force=True)
        await page.wait_for_timeout(p_tab)
    except Exception as e:
        return {
            "success": False,
            "reason": "TAB_ERROR",
            "message": f"Could not select Physical Vouchers tab: {e}",
        }

    # 3. Provider Selection
    try:
        if provider == "unipin":
            await page.locator("text=/UniPin/i").first.click(force=True)
        elif provider == "upgift":
            await page.locator("text=/UP Gift/i").first.click(force=True)
        await page.wait_for_timeout(p_provider)
    except Exception as e:
        return {
            "success": False,
            "reason": "PROVIDER_ERROR",
            "message": f"Could not select provider '{provider}': {e}",
        }

    # 4. Find checkout frame with serial input
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
        return {
            "success": False,
            "reason": "CHECKOUT_NOT_FOUND",
            "message": "Serial input never appeared after package click.",
        }

    # 5. Fill Serial
    await serial_input.click()
    await serial_input.fill(clean_serial)
    await page.wait_for_timeout(p_serial)

    # 6. Fill PIN
    pin_inputs = target_scope.locator(
        "input[type='password'], input[name*='pin'], input[id*='pin']"
    )
    pin_count = await pin_inputs.count()

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

    # 7. Confirm
    confirm_btn = target_scope.locator(
        "input[type='submit'][value='Confirm'], input[value='Confirm']"
    ).first
    if await confirm_btn.is_visible(timeout=3000):
        await confirm_btn.scroll_into_view_if_needed()
        await confirm_btn.click(force=True)
    else:
        await target_scope.evaluate(
            """() => {
                const b = document.querySelector("input[type='submit'][value='Confirm']") ||
                          document.querySelector("input[value='Confirm']");
                if (b) b.click();
            }"""
        )

    # 8. Result Polling
    result = None
    deadline = loop.time() + (t_result / 1000.0)
    fast_until = loop.time() + 5.0

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

        # Sleep
        sleep_ms = poll_fast if loop.time() < fast_until else poll_ms
        await page.wait_for_timeout(sleep_ms)

    if result is None:
        result = {"success": False, "reason": "NO_RESULT",
                  "message": "No result detected within timeout."}

    return result


# ============================================================
# MULTI-VOUCHER BATCH AUTOMATION WITH BROWSER SESSION REUSE
# ============================================================
async def process_freefire_topup_batch(
    player_uid: str,
    vouchers: List[Dict[str, Any]],
    on_progress: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """
    Executes multiple voucher redemptions sequentially in the SAME browser session.
    After redeeming one voucher, opens shop.garena.my in a new tab, closes the redeemed tab,
    and proceeds with the next voucher without re-logging in with the UID.
    Emits live progress updates via `on_progress` callback after each voucher.
    """
    webrender = _bool("WEBRENDER", True)

    params = {
        "t_nav":              _int("TIMEOUT_NAV", 60000),
        "t_short":            _int("TIMEOUT_SHORT", 45000),
        "t_result":           _int("TIMEOUT_RESULT", 90000),
        "p_page":             _int("PAUSE_AFTER_PAGE", 800),
        "p_uid":              _int("PAUSE_AFTER_UID", 2500),
        "p_login":            _int("PAUSE_AFTER_LOGIN", 600),
        "p_proceed":          _int("PAUSE_AFTER_PROCEED", 400),
        "p_diamond":          _int("PAUSE_AFTER_DIAMOND", 300),
        "p_tab":              _int("PAUSE_AFTER_TAB", 300),
        "p_provider":         _int("PAUSE_AFTER_PROVIDER", 300),
        "p_serial":           _int("PAUSE_AFTER_SERIAL", 100),
        "p_pin":              _int("PAUSE_AFTER_PIN", 100),
        "p_confirm":          _int("PAUSE_BEFORE_CONFIRM", 80),
        "poll_ms":            _int("RESULT_POLL_INTERVAL", 150),
        "poll_fast":          _int("RESULT_POLL_INTERVAL_FAST", 50),
        "type_uid":           _int("TYPE_DELAY_UID", 15),
        "type_pin":           _int("TYPE_DELAY_PIN", 10),
    }

    t_nav     = params["t_nav"]
    t_short   = params["t_short"]
    p_page    = params["p_page"]
    p_uid     = params["p_uid"]
    p_login   = params["p_login"]
    p_proceed = params["p_proceed"]
    type_uid  = params["type_uid"]

    browser = await get_browser()
    context = await new_stealth_context(browser)
    page = None
    results = []

    need_routes = (not webrender) or _bool("BLOCK_TRACKERS", False)
    if need_routes:
        await context.route("**/*", _route_handler)

    async def _emit_progress(idx: int, total: int, voucher_item: dict, status: str, result_dict: dict):
        if not on_progress:
            return
        try:
            if asyncio.iscoroutinefunction(on_progress):
                await on_progress(idx, total, voucher_item, status, result_dict)
            else:
                on_progress(idx, total, voucher_item, status, result_dict)
        except Exception as e:
            log.warning("Progress callback error: %s", e)

    try:
        total_vouchers = len(vouchers)

        for idx, v_item in enumerate(vouchers):
            raw_code = v_item.get("code", "")
            raw_pin  = v_item.get("pin", "")
            raw_pkg  = v_item.get("pkg", "")

            clean_serial, clean_pin = parse_voucher(raw_code, raw_pin)
            provider = detect_provider(clean_serial)

            # Auto-detect package via pkg.py if not specified or override
            detected = detect_package(clean_serial)
            package = detected or raw_pkg or os.getenv("FALLBACK_PACKAGE", "25 Diamond")

            t_voucher_start = time.perf_counter()

            # Pre-validation check for invalid prefix/provider
            if not provider:
                err_res = {
                    "index": idx + 1,
                    "success": False,
                    "reason": "INVALID_PREFIX",
                    "message": "Invalid Voucher Prefix. Must begin with UPBD or BDMB.",
                    "package": package,
                    "serial": clean_serial,
                    "pin": clean_pin,
                    "duration": 0.0,
                }
                results.append(err_res)
                await _emit_progress(idx, total_vouchers, v_item, "failed", err_res)
                continue

            # Emit start processing event
            await _emit_progress(idx, total_vouchers, v_item, "processing", {
                "index": idx + 1,
                "package": package,
                "serial": clean_serial,
            })

            # ----------------------------------------------------
            # TAB & NAVIGATION STRATEGY
            # ----------------------------------------------------
            if idx == 0:
                # First voucher: Open new page
                page = await context.new_page()
                log.info("🌐 [1/%d] Opening shop.garena.my...", total_vouchers)
            else:
                # Subsequent vouchers: Open new tab in same browser session, close old tabs
                log.info("📑 [%d/%d] Opening shop.garena.my in new tab (session preserved)...", idx + 1, total_vouchers)
                new_page = await context.new_page()
                for p in list(context.pages):
                    if p != new_page:
                        try:
                            await p.close()
                        except Exception:
                            pass
                page = new_page

            # 1. Open Garena on the tab
            await page.goto(
                "https://shop.garena.my/?app=100067&channel=202953",
                wait_until="domcontentloaded",
                timeout=t_nav,
            )
            await page.wait_for_timeout(p_page)

            # Common selectors
            uid_input = page.locator(
                "input[placeholder*='player ID'], "
                "input[placeholder*='Player ID'], "
                "input[type='text']"
            ).first

            proceed_btn = page.locator(
                "button:has-text('Proceed to Payment'), "
                "div[role='button']:has-text('Proceed to Payment'), "
                "button:has-text('Login')"
            ).first

            if idx == 0:
                # 2. Enter UID (Voucher 1 only)
                await uid_input.wait_for(timeout=15000)
                await uid_input.click()
                await uid_input.type(str(player_uid), delay=type_uid)
                await page.wait_for_timeout(p_uid)

                # 3. Click Login
                login_btn = page.locator(
                    "button:has-text('Login'), div[role='button']:has-text('Login'), .login-btn"
                ).first
                if await login_btn.is_visible():
                    await login_btn.click()
                else:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(p_login)
            else:
                # Subsequent vouchers: Skip entering UID!
                # If session somehow dropped and UID input is visible while proceed is not, handle fallback:
                try:
                    if await uid_input.is_visible(timeout=1500) and not await proceed_btn.is_visible(timeout=1500):
                        log.info("Session re-login required for tab %d", idx + 1)
                        await uid_input.click()
                        await uid_input.type(str(player_uid), delay=type_uid)
                        await page.wait_for_timeout(p_uid)
                        login_btn = page.locator(
                            "button:has-text('Login'), div[role='button']:has-text('Login'), .login-btn"
                        ).first
                        if await login_btn.is_visible():
                            await login_btn.click()
                        else:
                            await page.keyboard.press("Enter")
                        await page.wait_for_timeout(p_login)
                except Exception:
                    pass

            # 4. Proceed to Payment (Both Voucher 1 and subsequent vouchers follow this same step!)
            await proceed_btn.wait_for(timeout=t_short)

            pages_before = len(context.pages)
            await proceed_btn.click()
            await page.wait_for_timeout(p_proceed)

            if len(context.pages) > pages_before:
                page = context.pages[-1]
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(p_proceed)

            # ----------------------------------------------------
            # REDEEM CURRENT VOUCHER
            # ----------------------------------------------------
            redemption_result = await _redeem_single_voucher_on_page(
                page,
                context,
                package,
                provider,
                clean_serial,
                clean_pin,
                params,
            )

            duration = round(time.perf_counter() - t_voucher_start, 2)
            item_outcome = {
                "index": idx + 1,
                "success": bool(redemption_result.get("success")),
                "package": package,
                "serial": clean_serial,
                "pin": clean_pin,
                "reason": redemption_result.get("reason", ""),
                "message": redemption_result.get("message", ""),
                "duration": duration,
            }
            results.append(item_outcome)

            status_str = "complete" if item_outcome["success"] else "failed"
            log.info("🏁 Voucher [%d/%d] %s in %.2fs: %s",
                     idx + 1, total_vouchers, status_str, duration, item_outcome["message"])
            await _emit_progress(idx, total_vouchers, v_item, status_str, item_outcome)

    except Exception as e:
        log.exception("Fatal batch automation error: %s", e)
        # Mark remaining unprocessed vouchers as failed
        processed_count = len(results)
        for rem_idx in range(processed_count, len(vouchers)):
            rem_item = vouchers[rem_idx]
            rem_serial, rem_pin = parse_voucher(rem_item.get("code", ""), rem_item.get("pin", ""))
            rem_pkg = detect_package(rem_serial) or rem_item.get("pkg", "")
            err_outcome = {
                "index": rem_idx + 1,
                "success": False,
                "reason": "BATCH_INTERRUPTED",
                "message": f"Batch process error: {str(e)}",
                "package": rem_pkg,
                "serial": rem_serial,
                "pin": rem_pin,
                "duration": 0.0,
            }
            results.append(err_outcome)
            await _emit_progress(rem_idx, len(vouchers), rem_item, "failed", err_outcome)

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        try:
            await context.close()
        except Exception:
            pass
        release_context()

    return results


# ============================================================
# SINGLE VOUCHER TOPUP (BACKWARD COMPATIBILITY WRAPPER)
# ============================================================
async def process_freefire_topup(
    player_uid: str,
    diamond_amount: str,
    voucher_code: str,
    pin_code: str = "",
) -> str:
    """
    Backward-compatible single voucher runner.
    Returns a JSON string matching legacy format.
    """
    vouchers = [{
        "code": voucher_code,
        "pin": pin_code,
        "pkg": diamond_amount,
    }]
    results = await process_freefire_topup_batch(player_uid, vouchers)
    if results:
        res = results[0]
        return json.dumps({
            "success": res.get("success", False),
            "reason": res.get("reason", ""),
            "message": res.get("message", ""),
            "duration": res.get("duration", 0.0),
            "package": res.get("package", ""),
        })
    return json.dumps({
        "success": False,
        "reason": "NO_RESULT",
        "message": "Topup execution produced no result",
    })


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
