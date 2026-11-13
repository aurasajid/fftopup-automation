import re
from typing import Optional, Dict, Any, List

# ─────────────────────────────────────────────────────────────
# PACKAGE PREFIX SPECIFICATION
# ─────────────────────────────────────────────────────────────
# BDMB-T-S... / UPBD-Q-S... → 25 Diamond
# BDMB-U-S... / UPBD-R-S... → 50 Diamond
# BDMB-J-S... / UPBD-G-S... → 115 Diamond
# BDMB-I-S... / UPBD-F-S... → 240 Diamond
# BDMB-Q-S... / UPBD-N-S... → Weekly Membership
# BDMB-K-S... / UPBD-H-S... → 610 Diamond
# BDMB-S-S... / UPBD-P-S... → Monthly Membership
# BDMB-L-S... / UPBD-I-S... → 1240 Diamond
# BDMB-M-S... / UPBD-J-S... → 2530 Diamond

PACKAGE_DEFINITIONS = [
    {
        "package": "25 Diamond",
        "amount": "25",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-T-S",
            "upgift": "UPBD-Q-S",
        },
        "clean_prefixes": ["BDMBTS", "UPBDQS"],
    },
    {
        "package": "50 Diamond",
        "amount": "50",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-U-S",
            "upgift": "UPBD-R-S",
        },
        "clean_prefixes": ["BDMBUS", "UPBDRS"],
    },
    {
        "package": "115 Diamond",
        "amount": "115",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-J-S",
            "upgift": "UPBD-G-S",
        },
        "clean_prefixes": ["BDMBJS", "UPBDGS"],
    },
    {
        "package": "240 Diamond",
        "amount": "240",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-I-S",
            "upgift": "UPBD-F-S",
        },
        "clean_prefixes": ["BDMBIS", "UPBDFS"],
    },
    {
        "package": "Weekly Membership",
        "amount": "weekly",
        "is_membership": True,
        "prefixes": {
            "unipin": "BDMB-Q-S",
            "upgift": "UPBD-N-S",
        },
        "clean_prefixes": ["BDMBQS", "UPBDNS"],
    },
    {
        "package": "610 Diamond",
        "amount": "610",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-K-S",
            "upgift": "UPBD-H-S",
        },
        "clean_prefixes": ["BDMBKS", "UPBDHS"],
    },
    {
        "package": "Monthly Membership",
        "amount": "monthly",
        "is_membership": True,
        "prefixes": {
            "unipin": "BDMB-S-S",
            "upgift": "UPBD-P-S",
        },
        "clean_prefixes": ["BDMBSS", "UPBDPS"],
    },
    {
        "package": "1240 Diamond",
        "amount": "1240",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-L-S",
            "upgift": "UPBD-I-S",
        },
        "clean_prefixes": ["BDMBLS", "UPBDIS"],
    },
    {
        "package": "2530 Diamond",
        "amount": "2530",
        "is_membership": False,
        "prefixes": {
            "unipin": "BDMB-M-S",
            "upgift": "UPBD-J-S",
        },
        "clean_prefixes": ["BDMBMS", "UPBDJS"],
    },
]

def _clean_prefix_string(voucher_or_prefix: str) -> str:
    """
    Extract the first token of the voucher and strip any non-alphanumeric characters,
    uppercasing the result for robust prefix matching.
    """
    if not voucher_or_prefix:
        return ""
    # Split on whitespace or comma to grab serial part
    parts = re.split(r"[\s,]+", str(voucher_or_prefix).strip(), maxsplit=1)
    token = parts[0] if parts else ""
    return re.sub(r"[^A-Za-z0-9]", "", token).upper()

def detect_package_info(voucher: str) -> Optional[Dict[str, Any]]:
    """
    Detect full package metadata from a voucher code or prefix string.
    Returns dict with keys: package, amount, is_membership, provider, detected_prefix.
    """
    clean = _clean_prefix_string(voucher)
    if not clean:
        return None

    for defn in PACKAGE_DEFINITIONS:
        for pfx in defn["clean_prefixes"]:
            if clean.startswith(pfx):
                provider = "unipin" if clean.startswith("BDMB") else "upgift"
                return {
                    "package": defn["package"],
                    "amount": defn["amount"],
                    "is_membership": defn["is_membership"],
                    "provider": provider,
                    "matched_prefix": pfx,
                }
    return None

def detect_package(voucher: str) -> Optional[str]:
    """
    Returns the human-readable package label, e.g. '25 Diamond' or 'Weekly Membership'.
    """
    info = detect_package_info(voucher)
    return info["package"] if info else None

def detect_package_amount(voucher: str) -> Optional[str]:
    """
    Returns the package value/amount string, e.g. '25', '115', 'weekly', 'monthly'.
    """
    info = detect_package_info(voucher)
    return info["amount"] if info else None

def is_supported_prefix(voucher: str) -> bool:
    """
    Returns True if the voucher matches any recognized package prefix.
    """
    return detect_package_info(voucher) is not None

def list_supported_packages() -> List[Dict[str, Any]]:
    """
    Return all defined package mapping specifications.
    """
    return list(PACKAGE_DEFINITIONS)
