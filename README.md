# FF TopUp Automation — Bangladesh

A Free Fire diamond top-up automation service for the Bangladesh server, featuring a **FastAPI** web API, an interactive **Telegram bot**, a sleek **Web UI**, and a high-performance **Playwright** browser automation engine.

Supports **Auto-Package Detection** (18 voucher prefixes), **Multi-Voucher Batch Redemptions** (single-session browser tab switching), a **Sequential Execution Queue**, and **Live Telegram Message Updates**.

All top-up requests are **asynchronous**: the API returns immediately with an `order_id`, and the caller polls `/api/order/{id}` for real-time progress and final results. Orders are persisted in `orders.json` and survive restarts (pending orders are auto-canceled on boot).

---

## 📑 Table of Contents

1. [Features](#-features)
2. [Architecture](#-architecture)
3. [Auto-Package Detection & Voucher Prefixes](#-auto-package-detection--voucher-prefixes)
4. [Multi-Voucher Batch Redemption](#-multi-voucher-batch-redemption)
5. [Order Lifecycle](#-order-lifecycle)
6. [Supported Environments](#-supported-environments)
7. [File Structure](#-file-structure)
8. [Installation](#-installation)
   - [Linux (Debian/Ubuntu)](#linux-debianubuntu)
   - [Linux (Fedora/RHEL/Arch)](#linux-fedorarhelarch)
   - [macOS](#macos)
   - [Windows 10/11](#windows-1011)
   - [Docker](#docker)
   - [Railway / Fly.io / Render](#railway--flyio--render)
9. [Configuration (.env)](#-configuration-env)
10. [API Reference](#-api-reference)
11. [Telegram Bot](#-telegram-bot)
12. [Troubleshooting](#-troubleshooting)
13. [Security Notes](#-security-notes)
14. [Credits](#-credits)

---

## ✨ Features

- **⚡ Auto-Package Detection** — Automatically determines diamond tier or membership from the voucher serial prefix across 18 supported patterns. Manual package selection is optional!
- **📦 Multi-Voucher Batch Support** — Redeem 1 to N vouchers (up to `MAX_VOUCHER_REDEMPTIONS`, default 10) in a single request across Web UI, Telegram Bot, and API.
- **📑 Single-Session Tab Navigation** — For multi-voucher redemptions with the same UID, the browser context stays open. The first voucher enters UID & logs in; subsequent vouchers open a new tab, cleanly close old tabs, **skip entering UID**, click **"Proceed to Payment"**, and redeem immediately.
- **🔄 Sequential Job Queue** — Thread-safe asynchronous queue (`asyncio.Queue`) prevents browser concurrency collisions and guarantees ordered execution.
- **🤖 Live Telegram Updates & Receipts** — Telegram bot edits the status message in real-time as each voucher is processed (`⏳ Processing...`, `⏱ Queued`, `✅ Done`, `❌ Consumed Voucher`), and renders a full receipt upon completion.
- **🎮 Centralized Player Name Lookup** — Dedicated `namecheck.py` service queries multiple endpoints (e.g. `topupkoro.com`, Vercel) with Unicode-safe nickname parsing and fallback mechanisms.
- **⚡ Async Top-Up Flow** — Every request returns an `order_id`; poll `/api/order/{id}` for live per-voucher progress and final status.
- **🔀 HTTP 307 Redirect Flow** — `/topup?api=KEY&uid=..&code=..` redirects smoothly to `/topup?api=KEY&id=N`.
- **💾 Persistent Order Store** — Orders are persisted in `orders.json` and survive service restarts.
- **🛡 Startup & Crash Recovery** — Stale `pending` orders are automatically flipped to `canceled` with reason `INTERRUPTED` on boot. Mid-flow browser closures are detected and handled gracefully.
- **🎭 Stealth Browser Automation** — Playwright with stealth fingerprinting, header randomization, tracker blocking, and idle Chromium rotation.
- **🔑 API Key & Quota Management** — JSON-backed key store (`apikeys.json`) tracking remaining balance and usage counters. Only successful top-ups consume quota.
- **🌐 Web UI** — Modern, responsive, single-file frontend served at `/` with auto-detected package badges, UID lookup, and live batch progress polling.

---

## 🏗 Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                           FastAPI (main.py)                            │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  GET /topup?api=KEY&uid=..&code=..                                     │
│      ├─ validate key & enforce MAX_VOUCHER_REDEMPTIONS                 │
│      ├─ create order (status=pending) in orders.json                   │
│      └─ 307 redirect → /topup?api=KEY&id=N                             │
│                                                                        │
│  GET /topup?api=KEY&id=N                                               │
│      └─ enqueue to _JOB_QUEUE, return {order_id, status:pending}       │
│                                                                        │
│  GET /api/order/{id}            → poll live per-voucher progress       │
│  GET /api/namecheck?uid={uid}   → player nickname lookup               │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   asyncio.Queue (_JOB_QUEUE)                                           │
│         │                                                              │
│         ▼                                                              │
│   Sequential Worker ──► automation.py ──► browser_pool ──► Playwright  │
│                               │                                        │
│                               ├─ pkg.py (Auto-Package Detection)       │
│                               └─ namecheck.py (Player Name Service)    │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    Garena shop (shop.garena.my)
                    apikeys.json   (API keys & balance)
                    orders.json    (Order tracking & history)
                    .env           (Environment config)
```

### Key Modules

| Module | Description |
|---|---|
| `main.py` | FastAPI application, API endpoints, sequential `_JOB_QUEUE`, order persistence, and lifecycle handlers. |
| `automation.py` | Playwright browser automation engine. Handles multi-voucher batching, tab management, UID login, package selection, and voucher submission. |
| `pkg.py` | Auto-package detection engine mapping 18 prefix pairs (`BDMB-*` and `UPBD-*`) to exact Garena diamond packages and payment channels. |
| `namecheck.py` | Centralized player name lookup service querying upstream APIs with fallback handling, error filtering, and Unicode normalization. |
| `browser_pool.py` | Reusable Chromium pool featuring stealth fingerprinting, idle rotator, and page context recycling. |
| `bot.py` | Telegram bot with interactive dialog, direct command parsing, live in-place message editing, and detailed receipt generation. |
| `index.html` | Frontend single-page application featuring live package detection badges, UID verification, and real-time progress card. |
| `apikeys.json` | API key authorization store and credit counter. |
| `orders.json` | Persistent order storage (automatically initialized and kept pruned). |

---

## 🎟 Auto-Package Detection & Voucher Prefixes

Vouchers from **UniPin** (`BDMB-*`) and **UP Gift** (`UPBD-*`) contain serial prefixes that identify the exact diamond package and payment method. When vouchers with these prefixes are supplied, manual package selection is bypassed and automatically resolved.

### Supported Prefix Mappings (18 Total)

| UniPin Prefix | UP Gift Prefix | Package | Diamonds / Item | Payment Provider |
|:---:|:---:|:---|:---:|:---:|
| `BDMB-T-S` | `UPBD-Q-S` | `25 Diamond` | 25 💎 | UniPin / UP Gift |
| `BDMB-U-S` | `UPBD-R-S` | `50 Diamond` | 50 💎 | UniPin / UP Gift |
| `BDMB-J-S` | `UPBD-G-S` | `115 Diamond` | 115 💎 | UniPin / UP Gift |
| `BDMB-I-S` | `UPBD-F-S` | `240 Diamond` | 240 💎 | UniPin / UP Gift |
| `BDMB-Q-S` | `UPBD-N-S` | `Weekly Membership` | Weekly Membership | UniPin / UP Gift |
| `BDMB-K-S` | `UPBD-H-S` | `610 Diamond` | 610 💎 | UniPin / UP Gift |
| `BDMB-S-S` | `UPBD-P-S` | `Monthly Membership` | Monthly Membership | UniPin / UP Gift |
| `BDMB-L-S` | `UPBD-I-S` | `1240 Diamond` | 1240 💎 | UniPin / UP Gift |
| `BDMB-M-S` | `UPBD-J-S` | `2530 Diamond` | 2530 💎 | UniPin / UP Gift |

### Accepted Voucher Input Formats

The parser automatically splits serial and PIN regardless of delimiters or spaces:

```text
# Standard space delimiter
UPBD-Q-S-01989133 1527-6324-9731-2625

# Comma delimiter
UPBD-Q-S-01989133,1527-6324-9731-2625

# Raw unformatted string
UPBDQS01989133 1527632497312625

# Separate serial and pin parameters in API
?code=UPBD-Q-S-01989133&pin=1527-6324-9731-2625
```

---

## 📦 Multi-Voucher Batch Redemption

You can submit multiple vouchers in a single order (separated by newlines or commas).

### How Multi-Voucher Automation Works
1. **Validation & Package Detection**: Each voucher is validated and matched against prefix tables in `pkg.py`. If total vouchers exceed `MAX_VOUCHER_REDEMPTIONS`, the batch is rejected before launching the browser.
2. **Voucher 1 (Initial Setup)**:
   - Navigates to `https://shop.garena.my/?app=100067&channel=202953`.
   - Types UID into the player ID field and logs in.
   - Clicks **"Proceed to Payment"** to navigate to the payment screen.
   - Selects the auto-detected package tile and payment provider (`UP Gift` or `UniPin`).
   - Fills serial and PIN, submits, and captures the result.
3. **Vouchers 2+ (Optimized Tab Reuse)**:
   - Opens `https://shop.garena.my/?app=100067&channel=202953` in a **new tab** and closes old tabs within the **same browser session**.
   - **Skips entering UID and login** since session cookies remain active.
   - Clicks **"Proceed to Payment"** directly to load the payment page.
   - Selects the detected package, submits the voucher, and records duration and outcome.
4. **Live Status**: Real-time progress is emitted to the API poller (`/api/order/{id}`) and Telegram bot after every voucher finishes.

---

## 🔄 Order Lifecycle

Every top-up transitions through deterministic states:

```
         ┌──────────┐
         │ pending  │  ← Created and enqueued in _JOB_QUEUE
         └────┬─────┘
              │
     ┌────────┼────────┬──────────────┐
     ▼        ▼        ▼              ▼
┌─────────┐ ┌───────┐ ┌──────────┐ ┌──────────┐
│complete │ │failed │ │ canceled │ │ canceled │
└─────────┘ └───────┘ └──────────┘ └──────────┘
   All/Any    Voucher   Browser        Server rebooted
   succeeded  invalid   closed         or graceful
              or used   mid-flow       shutdown
```

| Transition | Cause | `reason` Code |
|---|---|---|
| `pending → complete` | Browser confirmed redemption | `""` |
| `pending → failed` | Voucher invalid, consumed, or expired | e.g. `INVALID_VOUCHER`, `VOUCHER_USED` |
| `pending → canceled` | Browser process crashed or was closed | `BROWSER_CLOSED` |
| `pending → canceled` | Order manually cancelled or service shutdown | `CANCELLED`, `SHUTDOWN` |
| `pending → canceled` | Stale order detected on server startup | `INTERRUPTED` |

> 💡 **Quota Protection**: API key balance is only deducted when at least one voucher completes successfully.

---

## 💻 Supported Environments

| Environment | Supported | Notes |
|---|:---:|---|
| **Ubuntu / Debian** | ✅ Recommended | Primary production target |
| **Fedora / RHEL / Arch** | ✅ | Standard Playwright dependencies |
| **macOS (Intel / Apple Silicon)** | ✅ | Native ARM64/x86_64 Chromium |
| **Windows 10 / 11** | ✅ | Fully supported with PowerShell / CMD |
| **Docker** | ✅ | Production-ready with Microsoft Playwright image |
| **Railway / Fly.io / Render** | ✅ | Deployable via Dockerfile |
| **AWS Lambda / Cloudflare Workers** | ❌ | Serverless runtimes are unsupported due to persistent browser requirements |

**Python Version:** Python 3.10, 3.11, or 3.12 (3.12 recommended).

---

## 📁 File Structure

```
fftopup-automation/
├── main.py               # FastAPI entrypoint, router, and async sequential queue
├── automation.py         # Playwright multi-voucher automation & tab manager
├── pkg.py                # Auto-package detection engine (18 prefix pairs)
├── namecheck.py          # Player name lookup service with multi-API fallback
├── browser_pool.py       # Shared Chromium pool, stealth engine & idle rotator
├── bot.py                # Telegram bot with live message editing & receipts
├── index.html            # Web frontend UI (served at /)
├── apikeys.json          # API keys and usage quotas
├── orders.json           # Order history database (auto-created)
├── requirements.txt      # Python dependencies
├── railway.json          # Railway configuration
├── Dockerfile            # Container definition
├── .dockerignore         # Docker build ignore rules
├── .env                  # Runtime environment variables (DO NOT COMMIT)
└── README.md             # Project documentation
```

---

## 📦 Installation

### Common Prerequisites
- **Python 3.10+**
- **pip** and **venv**
- **~500 MB** disk space for Chromium
- Network access to `shop.garena.my`, `unipin.com`, and Telegram API

---

### Linux (Debian/Ubuntu)

```bash
# 1. System packages for Playwright
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

# 2. Clone repository & create virtual environment
git clone <repo-url> fftopup-automation && cd fftopup-automation
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python dependencies & Playwright Chromium
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
playwright install-deps

# 4. Configure environment
cp .env.example .env # or edit .env
nano .env

# 5. Start server
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Windows 10/11

```powershell
# In PowerShell (Run as Administrator if setting execution policy)
git clone <repo-url> fftopup-automation
cd fftopup-automation

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

# Edit .env
notepad .env

# Run server
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Docker

Build and run with volumes for `apikeys.json` and `orders.json`:

```bash
docker build -t ff-topup .
docker run -d --name ff-topup-instance \
  -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/apikeys.json:/app/apikeys.json" \
  -v "$(pwd)/orders.json:/app/orders.json" \
  ff-topup
```

---

## ⚙ Configuration (`.env`)

Create a `.env` file in the root directory:

```env
# ─── Telegram Bot ─────────────────────────────────────────────
TG_BOT_TOKEN=              # Telegram Bot API token from @BotFather
TG_CHAT_ID=                # Telegram admin chat ID for completion alerts
TG_ALLOWED_USERS=          # Comma-separated user IDs (blank = allow all)

# ─── Feature Toggles ──────────────────────────────────────────
ENABLE_FRONTEND=true       # Serve index.html at "/"
ENABLE_TELEGRAM_BOT=true   # Launch Telegram long-polling bot

# ─── Web Frontend API Key ─────────────────────────────────────
# Key used by index.html (must exist in apikeys.json)
WEB_API_KEY=fftpautoapiWEBfrontend000000000000000000000

# ─── Browser Automation ───────────────────────────────────────
HEADLESS=true              # true for server/production, false for visual debugging
WEBRENDER=false            # false = text-based result polling (faster)
BLOCK_TRACKERS=true        # Block tracking scripts & third-party analytics
ENABLE_WARMUP=true         # Pre-resolve DNS and warm up TLS connections

# ─── Multi-Voucher Redemptions Limit ──────────────────────────
MAX_VOUCHER_REDEMPTIONS=10 # Maximum vouchers allowed per batch (1-50)

# ─── Timeouts (milliseconds) ──────────────────────────────────
TIMEOUT_NAV=60000          # Page navigation timeout
TIMEOUT_SHORT=45000        # Element interaction timeout
TIMEOUT_RESULT=90000       # Maximum wait for redemption result

# ─── Fixed Pauses (milliseconds) ──────────────────────────────
PAUSE_AFTER_PAGE=800
PAUSE_AFTER_UID=2500
PAUSE_AFTER_LOGIN=600
PAUSE_AFTER_PROCEED=400
PAUSE_AFTER_DIAMOND=300
PAUSE_AFTER_TAB=300
PAUSE_AFTER_PROVIDER=300
PAUSE_AFTER_SERIAL=100
PAUSE_AFTER_PIN=100
PAUSE_BEFORE_CONFIRM=80

# ─── Result Polling (milliseconds) ────────────────────────────
RESULT_POLL_INTERVAL=100
RESULT_POLL_INTERVAL_FAST=50

# ─── Typing Delays (milliseconds) ─────────────────────────────
TYPE_DELAY_UID=15
TYPE_DELAY_PIN=10

# ─── Order Storage & Retention ────────────────────────────────
MAX_ORDERS_KEPT=1000       # Maximum completed orders stored in orders.json

# ─── Idle Fingerprint Rotation ────────────────────────────────
IDLE_ROTATE_SECONDS=300    # Rotate browser after N seconds idle
ROTATOR_INTERVAL=30        # Watchdog check interval

# ─── Player Name API ──────────────────────────────────────────
PLAYER_API_URLS=https://topupkoro.com/api/checkuid?uid={uid},https://ffnamecheck.vercel.app/info?uid={uid}
PLAYER_API_REGION=bd

# ─── Developer Credits ────────────────────────────────────────
DEV_NAME=S!XU
DEV_LINK=https://t.me/aura_sajid
DEV_LINK_TEXT=S!XU

# ─── Server Port ──────────────────────────────────────────────
PORT=8000
```

---

## 🌐 API Reference

Base URL: `http://localhost:8000`

### 1. Submit Top-Up (Redirect-based)

```http
GET /topup?api={KEY}&uid={UID}&code={VOUCHER}[&pkg={PKG}&pin={PIN}]
```

- **Vouchers**: Can be a single voucher or multiple vouchers separated by newline (`%0A`) or comma.
- **Pkg**: Optional if the voucher starts with a recognized prefix (`UPBD-*` or `BDMB-*`).
- **Response**: Returns a `307 Temporary Redirect` to `/topup?api={KEY}&id={order_id}`.

### 2. Submit Top-Up (Direct POST)

```http
POST /topup
Content-Type: application/json

{
  "api": "your_api_key",
  "uid": "3761036809",
  "code": "UPBD-P-S-03545008 2274-6243-2296-6132\nUPBD-P-S-03545057 1594-7535-4572-4643",
  "pkg": ""
}
```

Response:
```json
{
  "success": true,
  "order_id": 105,
  "status": "pending",
  "message": "Order queued. Poll /api/order/{order_id} for the result.",
  "poll_url": "/api/order/105"
}
```

---

### 3. Poll Order Status

```http
GET /api/order/{order_id}
```

#### In Progress (Multi-Voucher Live Progress):
```json
{
  "order_id": 105,
  "uid": "3761036809",
  "status": "pending",
  "total_vouchers": 2,
  "completed_vouchers": 1,
  "results": [
    {
      "index": 1,
      "success": true,
      "package": "Monthly Membership",
      "serial": "UPBDPS03545008",
      "pin": "2274624322966132",
      "duration": 18.2,
      "message": "Topup Completed Successfully!"
    }
  ],
  "duration": 18.2
}
```

#### Final Completed Batch:
```json
{
  "order_id": 105,
  "uid": "3761036809",
  "status": "complete",
  "multi": true,
  "total_vouchers": 2,
  "completed_vouchers": 2,
  "results": [
    {
      "index": 1,
      "success": true,
      "package": "Monthly Membership",
      "duration": 18.2,
      "message": "Topup Completed Successfully!"
    },
    {
      "index": 2,
      "success": false,
      "package": "Monthly Membership",
      "duration": 6.4,
      "reason": "VOUCHER_USED",
      "message": "Consumed Voucher"
    }
  ],
  "duration": 24.6
}
```

---

### 4. Player Name Lookup

```http
GET /api/namecheck?uid={uid}
```
Or alias: `GET /api/player-name?uid={uid}`

Response:
```json
{
  "ok": true,
  "uid": "4662030286",
  "nickname": "ᏣᎬᏕ-RATANツFF",
  "source": "https://topupkoro.com/api/checkuid?uid=3761036809"
}
```

---

### 5. Service Health

```http
GET /api/health
```

```json
{
  "ok": true,
  "service": "ff-topup-automation",
  "status": "online",
  "frontend": true,
  "telegram_bot": true,
  "queue_pending_jobs": 0,
  "orders_in_memory": 45,
  "next_order_id": 46
}
```

---

## 🤖 Telegram Bot

The built-in Telegram bot supports interactive conversations and high-speed direct orders.

### 1. Interactive Mode
1. Send `/start` or paste player UID: `3761036809`.
2. Bot fetches and displays the player's in-game name.
3. Paste 1 to `MAX_VOUCHER_REDEMPTIONS` vouchers (one per line).
4. Bot initiates redemption with live in-place status edits.

### 2. Direct Mode
Send UID followed by one or more vouchers in a single message:
```text
3761036809
UPBD-P-S-03545008 2274-6243-2296-6132
UPBD-P-S-03545057 1594-7535-4572-4643
```

### 3. Live Message Edits & Final Receipt
During processing, the bot edits the message per voucher:
- `⏳ Processing...`
- `⏱ Queued`
- `✅ Topup Completed Successfully!`
- `❌ Consumed Voucher`

When all vouchers finish, the bot updates the message with the final receipt:

```text
✗ Voucher Redeem Failed !!
--------------------------------
• FF NAME    : ᏣᎬᏕ-RATANツFF
• UID        : 3761036809
• TG USERNAME: aura_sajid
• TG ID      : --

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
➥ UPBD-P-S-03545057 1594-7535-4572-4643
| Monthly 💎 | ❌ Consumed Voucher
```

*(If all vouchers succeed, the header displays `💎 Diamond Top-Up Done!!`)*

---

## 🩺 Troubleshooting

### Chromium will not launch on Linux
```text
playwright._errors.Error: Executable doesn't exist
```
**Fix**:
```bash
playwright install chromium
sudo playwright install-deps
```

### Batch size rejected (`MAX_VOUCHER_REDEMPTIONS`)
```text
❌ Limit exceeded: Maximum allowed is 10 per batch.
```
**Fix**: Adjust `MAX_VOUCHER_REDEMPTIONS` in your `.env` file to your desired limit and restart the service.

### Premature package selection or tab navigation failure
- Ensure network access to `shop.garena.my` and `unipin.com`.
- Garena layout changes: check that `proceed_btn` selectors in `automation.py` match current Garena button texts (`Proceed to Payment`, `Login`).

### Stale orders after reboot
Pending orders are intentionally transitioned to `canceled` with reason `INTERRUPTED` on startup because their running browser tasks no longer exist.

---

## 🔒 Security Notes

1. **Keep `.env` private**: Never commit `.env` or paste your Telegram token or API keys into public forums.
2. **Web Key Quota**: The `WEB_API_KEY` is embedded into the client-side JavaScript of `index.html`. Set a sensible quota in `apikeys.json` and rotate it periodically.
3. **Admin APIs Restricted**: Key reload (`/api/keys/reload`) and manual order cancellations are restricted to localhost requests only.
4. **Queue Isolation**: Sequential processing through `asyncio.Queue` ensures no two top-ups interfere with each other or corrupt browser session state.

---

## 💬 Credits

Developed by **S!XU** — [Telegram Support](https://t.me/aura_sajid)
