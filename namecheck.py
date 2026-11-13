import os
import re
import json
import logging
from typing import Optional, Dict, Any
import httpx

log = logging.getLogger("namecheck")

# Default providers in priority order
DEFAULT_PROVIDERS = [
    "https://topupkoro.com/api/checkuid?uid={uid}",
    "https://ffnamecheck.vercel.app/info?uid={uid}",
]

def get_provider_urls() -> list[str]:
    raw = os.getenv("PLAYER_API_URLS", "")
    if raw.strip():
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if urls:
            return urls
    return list(DEFAULT_PROVIDERS)

def _clean_uid(uid: Any) -> str:
    return re.sub(r"\D", "", str(uid or "")).strip()

def _extract_candidate(data: Any) -> Optional[str]:
    """
    Search recursively or by prioritized keys for player nickname.
    Third-party providers return names under different keys:
    nickname, name, playerid, idname, message, username, player_name, etc.
    """
    if not data:
        return None

    if isinstance(data, str):
        # Could be raw text or json string
        try:
            data = json.loads(data)
        except Exception:
            return data.strip() if len(data.strip()) > 1 else None

    if isinstance(data, dict):
        # 1. Direct prioritized keys
        keys_to_check = [
            "nickname",
            "name",
            "playerid",
            "idname",
            "message",
            "username",
            "player_name",
            "playerName",
            "nick",
        ]
        for k in keys_to_check:
            val = data.get(k)
            if val is not None and isinstance(val, (str, int)):
                s = str(val).strip()
                if s:
                    return s

        # 2. Check nested dicts like data['data'] or data['result']
        for nested_key in ("data", "result", "response", "player"):
            if nested_key in data and isinstance(data[nested_key], dict):
                cand = _extract_candidate(data[nested_key])
                if cand:
                    return cand

    return None

def _is_invalid_nickname(candidate: str, uid: str) -> bool:
    """
    Determine if the candidate nickname is an error message, invalid echo, or placeholder.
    """
    if not candidate:
        return True

    c_clean = candidate.strip()
    if not c_clean:
        return True

    # If provider echoes back the input UID itself (common for Topupkoro and others when UID not found)
    if c_clean == uid:
        return True

    # Rejection regex for error messages returned as candidate names
    low = c_clean.lower()
    error_patterns = [
        r"^http\s*error",
        r"not\s*found",
        r"invalid\s*uid",
        r"uid\s*invalid",
        r"invalid\s*player",
        r"player\s*not\s*found",
        r"no\s*player",
        r"user\s*not\s*found",
        r"error",
        r"failed",
        r"quota\s*exceeded",
        r"^null$",
        r"^undefined$",
        r"^false$",
        r"^none$",
    ]
    for pat in error_patterns:
        if re.search(pat, low):
            return True

    return False

def _build_url(template: str, uid: str) -> str:
    url = template.replace("{uid}", uid).replace("[users uid]", uid).replace("[uid]", uid)
    region = os.getenv("PLAYER_API_REGION", "").strip()
    if region and not re.search(r"[?&]region=", url, re.IGNORECASE):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}region={region}"
    return url

async def check_player_name(uid: Any) -> Optional[Dict[str, Any]]:
    """
    Fetch Free Fire player nickname by ID from centralized third-party APIs.
    Normalizes the returned payload to include a 'nickname' field.
    Returns None if the player is not found or all providers fail.
    """
    clean_uid_val = _clean_uid(uid)
    if not clean_uid_val or len(clean_uid_val) < 6 or len(clean_uid_val) > 15:
        log.warning("Invalid UID supplied for name check: %r", uid)
        return None

    providers = get_provider_urls()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
        for provider_template in providers:
            url = _build_url(provider_template, clean_uid_val)
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.debug("Provider %s returned HTTP %s", url, resp.status_code)
                    continue

                content_type = resp.headers.get("content-type", "")
                raw_data = None
                if "application/json" in content_type:
                    try:
                        raw_data = resp.json()
                    except Exception:
                        raw_data = resp.text
                else:
                    try:
                        raw_data = resp.json()
                    except Exception:
                        raw_data = resp.text

                candidate = _extract_candidate(raw_data)
                if not candidate or _is_invalid_nickname(candidate, clean_uid_val):
                    log.debug("Provider %s returned invalid nickname candidate: %r", url, candidate)
                    continue

                # Extract optional metadata if available in dict
                region = ""
                level = ""
                if isinstance(raw_data, dict):
                    region = str(raw_data.get("region") or raw_data.get("AccountRegion") or "")
                    level = str(raw_data.get("level") or raw_data.get("AccountLevel") or "")

                log.info("✅ Player found: UID=%s -> %s (via %s)", clean_uid_val, candidate, url)
                return {
                    "nickname": candidate,
                    "uid": clean_uid_val,
                    "region": region,
                    "level": level,
                    "provider": provider_template,
                }
            except Exception as e:
                log.debug("Provider %s failed with error: %s", url, e)
                continue

    log.warning("❌ Failed to resolve player name for UID %s across %d provider(s)", clean_uid_val, len(providers))
    return None

def check_player_name_sync(uid: Any) -> Optional[Dict[str, Any]]:
    """
    Synchronous fallback wrapper for non-async contexts.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If running inside an existing loop, use an executor or thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(check_player_name(uid))).result()
    else:
        return asyncio.run(check_player_name(uid))
