# FF TopUp Automation — Bangladesh

A Free Fire diamond top-up automation service for the Bangladesh server, with a
FastAPI web API, a Telegram bot, and a Playwright-driven browser automation layer.

All top-up requests are now **asynchronous**: the API returns immediately with
an `order_id`, and the caller polls `/api/order/{id}` for the result. Orders
are persisted in `orders.json` and survive restarts (pending orders are
auto-canceled on boot).

---

## 📑 Table of Contents

1. [Features](#-features)
2. [Architecture](#-architecture)
3. [Order Lifecycle](#-order-lifecycle)
4. [Supported Environments](#-supported-environments)
5. [File Structure](#-file-structure)
6. [Installation](#-installation)
   - [Linux (Debian/Ubuntu)](#linux-debianubuntu)
   - [Linux (Fedora/RHEL/Arch)](#linux-fedorarhelarch)
   - [macOS](#macos)
   - [Windows 10/11](#windows-1011)
   - [Docker](#docker)
   - [Railway / Fly.io / Render](#railway--flyio--render)
7. [Configuration (.env)](#-configuration-env)
8. [API Reference](#-api-reference)
9. [Voucher Format](#-voucher-format)
10. [Package Values](#-package-values)
11. [Troubleshooting](#-troubleshooting)
12. [Security Notes](#-security-notes)

---

## ✨ Features

- **Async top-up flow** — every request returns an `order_id`; poll for the result
- **HTTP 307 redirect** — `/topup?api=KEY&uid=..&pkg=..&code=..` → `/topup?api=KEY&id=N`
- **Persistent order tracking** — `orders.json` survives restarts
- **Startup recovery** — stale `pending` orders are auto-canceled on boot
- **Browser-close detection** — a browser that closes mid-flow flips the order to `canceled`
- **Browser automation** via Playwright (stealth + fingerprint rotation)
- **Shared Chromium pool** — one browser, many contexts, fast concurrent top-ups
- **Idle fingerprint rotator** — rotates Chromium after N seconds of inactivity
- **FastAPI HTTP API** with per-key usage tracking
- **Telegram bot** with conversational flow + direct-command mode (toggleable)
- **Web UI** (`index.html`) served at `/` (toggleable)
- **DNS + TLS warm-up** to reduce first-request latency
- **Player name lookup** via configurable third-party APIs
- **Voucher auto-detection** (`UPBD*` → UP Gift, `BDMB*` → UniPin)

---

## 🏗 Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          FastAPI (main.py)                         │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  GET /topup?api=KEY&uid=..&pkg=..&code=..                          │
│      ├─ validate key (401 if bad)                                  │
│      ├─ create order (status=pending) in orders.json               │
│      └─ 307 redirect → /topup?api=KEY&id=N                         │
│                                                                    │
│  GET /topup?api=KEY&id=N                                           │
│      └─ queue background job, return {order_id, status:pending}    │
│                                                                    │
│  GET /api/order/{id}            → poll status                      │
│  GET /api/orders                → list recent orders               │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│   Background worker ──► automation ──► browser_pool ──► Playwright │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                Garena shop (shop.garena.my)
                apikeys.json   (key + usage store)
                orders.json    (order history)
                .env           (config)
```

**Key modules:**

| File | Role |
|---|---|
| `main.py` | FastAPI app, redirect logic, order store, background workers, API-key auth |
| `automation.py` | Playwright automation: opens Garena, fills UID, selects package, submits voucher, polls result |
| `browser_pool.py` | Shared Chromium pool, stealth fingerprint, idle rotator |
| `bot.py` | Telegram bot (long-polling) |
| `index.html` | Web frontend (single file, dark/light theme) |
| `apikeys.json` | API key + usage store |
| `orders.json` | Order history (auto-created on first order) |
| `.env` | Runtime configuration |

---

## 🔄 Order Lifecycle

Every top-up creates an order that moves through these states:

```
         ┌──────────┐
         │ pending  │  ← created on enqueue
         └────┬─────┘
              │
     ┌────────┼────────┬──────────────┐
     ▼        ▼        ▼              ▼
┌─────────┐ ┌───────┐ ┌──────────┐ ┌──────────┐
│complete │ │failed │ │ canceled │ │ canceled │
└─────────┘ └───────┘ └──────────┘ └──────────┘
   browser    browser   browser        server
   returned   returned  closed         restarted
   success    a reason  mid-flow       / shut down
```

| Transition | Triggered by | `reason` value |
|---|---|---|
| `pending → complete` | Browser reported success | `""` |
| `pending → failed` | Browser reported a failure reason | e.g. `INVALID_VOUCHER` |
| `pending → canceled` | Playwright error: *"browser has been closed"* | `BROWSER_CLOSED` |
| `pending → canceled` | Task cancelled (shutdown / admin) | `CANCELLED` / `MANUAL_CANCEL` |
| `pending → canceled` | Server restart found order still pending | `INTERRUPTED` |
| `pending → canceled` | Server shutdown flushed in-flight orders | `SHUTDOWN` |

**Rule:** No `pending` order survives a restart or shutdown.

**API key usage is consumed only when an order reaches `complete`.** Failed and canceled orders do not decrement your quota.

---

## 💻 Supported Environments

| Environment | Supported | Notes |
|---|---|---|
| **Ubuntu / Debian** | ✅ Recommended | Best tested |
| **Fedora / RHEL** | ✅ | Slight package name differences |
| **Arch / Manjaro** | ✅ | Rolling release, works fine |
| **Alpine Linux** | ⚠️ | Playwright deps need extra work; use Ubuntu base image |
| **macOS (Intel / Apple Silicon)** | ✅ | Native arm64 Chromium |
| **Windows 10 / 11** | ✅ | PowerShell or CMD |
| **Docker (any host)** | ✅ | Recommended for production |
| **Railway / Fly.io / Render** | ✅ | Use the Playwright base image |
| **Raspberry Pi (ARM64)** | ⚠️ | Playwright support is experimental |
| **AWS Lambda / Cloudflare Workers** | ❌ | No persistent FS, no long-running processes |

**Python:** 3.10, 3.11, or 3.12 (3.12 recommended).

---

## 📁 File Structure

```
ff-topup/
├── main.py               # FastAPI entrypoint (redirect + order store)
├── automation.py         # Playwright automation
├── browser_pool.py       # Shared Chromium pool
├── bot.py                # Telegram bot
├── index.html            # Web UI (served at /)
├── apikeys.json          # API key store (auto-created if missing)
├── orders.json           # Order history (auto-created on first order)
├── requirements.txt      # Python dependencies
├── railway.json          # Railway deploy config
├── Dockerfile            # Container build
├── .dockerignore         # Exclude .env from the image
├── .env                  # Runtime config (NOT committed)
├── .env.example          # Template (committed)
└── README.md             # This file
```

---

## 📦 Installation

### Common Prerequisites

- **Python 3.10+**
- **pip** and **venv**
- **~500 MB** free disk (Chromium + deps)
- **Network access** to `shop.garena.my`, `unipin.com`, Telegram API

---

### Linux (Debian/Ubuntu)

```bash
# 1. System packages for Playwright
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

# 2. Clone & create venv
git clone <your-repo> ff-topup && cd ff-topup
python3 -m venv .venv
source .venv/bin/activate

# 3. Python deps
pip install -r requirements.txt

# 4. Chromium for Playwright
playwright install chromium
playwright install-deps   # requires sudo

# 5. Configure
cp .env.example .env
$EDITOR .env

# 6. Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Linux (Fedora/RHEL/Arch)

**Fedora / RHEL / CentOS:**
```bash
sudo dnf install -y nss nspr atk at-spi2-atk cups-libs libdrm \
    libXcomposite libXdamage libXfixes libXrandr mesa-libgbm alsa-lib
```

**Arch / Manjaro:**
```bash
sudo pacman -S --needed nss nspr atk at-spi2-atk cups libdrm \
    libxcomposite libxdamage libxfixes libxrandr mesa alsa-lib
```

Then follow the same `pip install` / `playwright install` steps.

---

### macOS

```bash
brew install python@3.12

git clone <your-repo> ff-topup && cd ff-topup
python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
# (no `install-deps` needed on macOS)

cp .env.example .env
$EDITOR .env

uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Windows 10/11

```powershell
# Ensure "Add Python to PATH" during install

git clone <your-repo> ff-topup
cd ff-topup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

pip install -r requirements.txt
playwright install chromium

Copy-Item .env.example .env
notepad .env

uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Docker

```dockerfile
# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=true

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD wget -qO- http://127.0.0.1:${PORT:-8080}/api/health || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

Build and run (mount `apikeys.json` and `orders.json` for persistence):

```bash
docker build -t ff-topup .
docker run --rm -p 8080:8080 \
  --env-file .env \
  -v "$(pwd)/apikeys.json:/app/apikeys.json" \
  -v "$(pwd)/orders.json:/app/orders.json" \
  ff-topup
```

**Add a `.dockerignore`** — critical for keeping `.env` out of the image:

```
Dockerfile
.dockerignore

.env
.env.*
!.env.example

__pycache__/
*.py[cod]
.venv/
venv/

.git/
.gitignore

.vscode/
.idea/

*.log
logs/

.DS_Store
Thumbs.db
```

---

### Railway / Fly.io / Render

**Railway (`railway.json`):**

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile"
  },
  "deploy": {
    "startCommand": "sh -c 'uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}'",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10,
    "healthcheckPath": "/api/health",
    "healthcheckTimeout": 90
  }
}
```

> ⚠️ **Important:** `healthcheckTimeout` must be **90**, not 30. Chromium cold start takes 15–25 s.

**Fly.io (`fly.toml`):**

```toml
app = "ff-topup"
primary_region = "sin"

[build]
  dockerfile = "Dockerfile"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true

[[mounts]]
  source = "ff_data"
  destination = "/app/data"

[checks.health]
  type = "http"
  port = 8080
  path = "/api/health"
  interval = "30s"
  timeout = "90s"
  grace_period = "120s"
```

**Render:** use the "Docker" environment. Set `healthCheckPath` to `/api/health`. Cold starts take 20–40 s.

> ⚠️ **Do NOT deploy to AWS Lambda / Cloudflare Workers / Vercel serverless.**

---

## ⚙ Configuration (`.env`)

The `.env` file **must be named exactly `.env`** (no `.txt`) next to `main.py`.

### Full Reference

```env
# ─── Telegram Bot ─────────────────────────────────────────────
TG_BOT_TOKEN=              # From @BotFather. Empty = bot disabled.
TG_CHAT_ID=                # Admin chat for notifications.
TG_ALLOWED_USERS=          # Comma-separated user IDs. Empty = allow all.

# ─── Feature toggles ──────────────────────────────────────────
ENABLE_FRONTEND=true       # Serve index.html at "/"
ENABLE_TELEGRAM_BOT=true   # Start the Telegram long-polling bot

# ─── Web frontend API key ─────────────────────────────────────
# This must match a key in apikeys.json.
# It is injected into index.html and used by the browser form.
WEB_API_KEY=fftpautoapiWEBfrontend000000000000000000000

# ─── Browser Mode ─────────────────────────────────────────────
HEADLESS=true              # true = server. false = visible (local debug).
WEBRENDER=false            # false = plain-text (faster).
BLOCK_TRACKERS=true
ENABLE_WARMUP=true

# ─── Timeouts (milliseconds) ──────────────────────────────────
TIMEOUT_NAV=60000
TIMEOUT_SHORT=45000
TIMEOUT_RESULT=90000

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

# ─── Typing Speed (ms) ────────────────────────────────────────
TYPE_DELAY_UID=15
TYPE_DELAY_PIN=10

# ─── Fallback package ─────────────────────────────────────────
FALLBACK_PACKAGE=25

# ─── Order store ──────────────────────────────────────────────
MAX_ORDERS_KEPT=1000       # Orders to retain in orders.json

# ─── Browser Fingerprint Rotation ─────────────────────────────
IDLE_ROTATE_SECONDS=300
ROTATOR_INTERVAL=30

# ─── Player Name API ──────────────────────────────────────────
PLAYER_API_URLS=https://ffnamecheck.vercel.app/info?uid={uid}
PLAYER_API_REGION=bd

# ─── Developer Credit ─────────────────────────────────────────
DEV_NAME=S!XU
DEV_LINK=https://t.me/aura_sajid
DEV_LINK_TEXT=S!XU

# ─── Server ───────────────────────────────────────────────────
PORT=8000
```

**Booleans** accept only `true` or `false` (lowercase). Anything else → `false`.

---

## 🌐 API Reference

Base URL (local): `http://localhost:8000`

### Health Check

```
GET /api/health
```

```json
{
  "ok": true,
  "service": "ff-topup-automation",
  "status": "online",
  "frontend": true,
  "telegram_bot": true,
  "web_api_key_set": true,
  "orders_in_memory": 12,
  "live_tasks": 1,
  "next_order_id": 13,
  "api_keys_count": 5,
  "api_keys_enabled": 4
}
```

### Public Config (used by `index.html`)

```
GET /api/config
```

```json
{
  "player_api_urls": ["https://ffnamecheck.vercel.app/info?uid={uid}"],
  "player_api_region": "bd",
  "dev_name": "S!XU",
  "dev_link": "https://t.me/aura_sajid",
  "dev_link_text": "S!XU"
}
```

### Runtime Stats

```
GET /api/stats
```

```json
{
  "uptime_seconds": 3600,
  "total": 42,
  "success": 40,
  "failed": 1,
  "canceled": 1,
  "average_duration": 18.85,
  "orders_in_memory": 42,
  "live_tasks": 1
}
```

---

### Main Top-Up Flow (redirect-based)

#### Step 1 — Submit a top-up

```
GET /topup?api=<KEY>&uid=<uid>&pkg=<pkg>&code=<voucher>[&pin=<pin>]
```

Server validates the key, creates an order, and **307-redirects** to:

```
GET /topup?api=<KEY>&id=<order_id>
```

**If the key is invalid:** `401 Unauthorized`, no order created, no redirect.
**The order is not created for the wrong key.**

#### Step 2 — The redirect target runs the job

```
GET /topup?api=<KEY>&id=<N>
```

The key must match the order's owner, otherwise `404 Not Found` (no leak).

Response:

```json
{
  "success": true,
  "order_id": 123,
  "status": "pending",
  "message": "Order queued. Poll /api/order/{order_id} for the result.",
  "poll_url": "/api/order/123"
}
```

#### Step 3 — Poll for the result

```
GET /api/order/<N>
```

While running:

```json
{
  "order_id": 123,
  "uid": "3761036809",
  "pkg": "115",
  "voucher": "UPBD-P-S-12345678 1234-5678-9012-3456",
  "source": "direct",
  "status": "pending",
  "duration": 0.0
}
```

After success:

```json
{
  "order_id": 123,
  "uid": "3761036809",
  "pkg": "115",
  "voucher": "UPBD-P-S-12345678 1234-5678-9012-3456",
  "source": "direct",
  "status": "complete",
  "reason": "",
  "message": "Topup Completed Successfully!",
  "duration": 18.42
}
```

After failure:

```json
{
  "order_id": 123,
  "status": "failed",
  "reason": "INVALID_VOUCHER",
  "message": "Invalid voucher code. Check the serial and PIN."
}
```

**Note:** `api_key` is never returned in any public response.

---

### Alternate Direct API (redirect-based)

```
GET /api=<KEY>&uid=...&code=...&pkg=...[&pin=...]
GET /api/direct?api=<KEY>&uid=...&code=...&pkg=...[&pin=...]
```

Both behave like the main `/topup` — they create an order and 307-redirect to `/topup?api=KEY&id=N`.

---

### POST `/topup` — no redirect

```
POST /topup
Content-Type: application/json

{
  "api": "fftpautotest",
  "uid": "3761036809",
  "pkg": "115",
  "code": "UPBD-P-S-12345678 1234-5678-9012-3456",
  "pin": ""
}
```

Returns the same JSON shape as Step 2, no redirect.

---

### Web frontend top-up

```
GET  /api/topup?api=<KEY>&uid=...&pkg=...&code=...
POST /api/topup  { "api":"KEY", "uid":"...", "pkg":"...", "code":"..." }
```

Used internally by `index.html`. Returns the same JSON shape as Step 2 (no redirect).

---

### List recent orders

```
GET /api/orders?limit=20
```

```json
{
  "count": 3,
  "orders": [
    { "order_id": 12, "uid": "...", "status": "complete", ... },
    { "order_id": 11, "uid": "...", "status": "canceled", ... }
  ]
}
```

The `api_key` field is stripped from every item.

---

### Admin Endpoints (localhost only)

Return `403 Forbidden` when called from a non-local IP.

```
GET  /api/keys                      # list keys (masked)
POST /api/keys/reload               # reload apikeys.json
POST /api/orders/reload             # reload orders.json
POST /api/orders/cancel/{order_id}  # force-cancel a live order
```

---

### Frontend

```
GET /      # index.html (with the WEB_API_KEY injected as window.__FFT_API_KEY__)
```

---

## 🎟 Voucher Format

| Prefix | Provider | Garena selector |
|---|---|---|
| `UPBD` | UP Gift | "UP Gift" tab |
| `BDMB` | UniPin | "UniPin" tab |

Accepted input:

```
UPBD-P-S-01989133 1527-6324-9731-2625
UPBD-P-S-01989133,1527-6324-9731-2625
UPBDPS01989133 1527632497312625
```

The parser strips all non-alphanumerics from both serial and PIN.

---

## 💎 Package Values

| Value | Meaning |
|---|---|
| `25` | 25 Diamonds |
| `50` | 50 Diamonds |
| `115` | 115 Diamonds |
| `240` | 240 Diamonds |
| `610` | 610 Diamonds |
| `1240` | 1240 Diamonds |
| `2530` | 2530 Diamonds |
| `weekly` | Weekly Membership |
| `monthly` | Monthly Membership |

Case-insensitive.

---

## 🩺 Troubleshooting

### Chromium won't launch

```
playwright._errors.Error: Executable doesn't exist
```

**Fix:** `playwright install chromium` (and `playwright install-deps` on Linux).

### `GET /topup?...` returns 401

Your API key is not in `apikeys.json` or has been exhausted.

```bash
curl "http://localhost:8000/topup?api=WRONGKEY&uid=...&code=..."
# → 401 {"detail":"API key rejected: unknown key"}
```

Add the key to `apikeys.json`, or call `POST /api/keys/reload` (localhost only).

### `GET /topup?api=KEY&id=N` returns 404

Either:
- The order does not exist, or
- The order belongs to a different key.

Both cases return the same 404 to avoid leaking existence.

### Stuck in `pending` after restart

This is intentional — the recovery routine flips `pending` orders to `canceled`
with `reason: "INTERRUPTED"` at startup, since the process that was running
them is gone.

### `orders.json` grows too large

`MAX_ORDERS_KEPT` (default 1000) trims the oldest orders on every write.

### Telegram bot disabled

```
⚠️ TG_BOT_TOKEN not set — Telegram bot disabled.
```

Checklist:
1. File is literally named `.env`
2. `TG_BOT_TOKEN=` is filled
3. `ENABLE_TELEGRAM_BOT=true`
4. Service restarted

### Railway healthcheck fails

Bump `healthcheckTimeout` in `railway.json` to **90**. Chromium cold start takes 15–25 s.

### `apikeys.json` / `orders.json` disappear on redeploy

Railway / Fly / Render use **ephemeral** filesystems. Mount a volume
(Fly: `[[mounts]]`, Railway: attach a volume) or move to a proper database.

---

## 🔒 Security Notes

1. **Never commit `.env`.** Add it to `.gitignore` and `.dockerignore`.
2. **Rotate leaked tokens** via @BotFather if `TG_BOT_TOKEN` ever leaked.
3. **`apikeys.json` and `orders.json` are plain-text.** Treat them as secrets.
4. **The web key (`WEB_API_KEY`) is public** in the browser — it's unavoidable for a web UI. Give it a finite quota (`10000`, not `unlimited`), rotate if it hits 90%, watch the logs.
5. **Per-key ownership is enforced.** `/api/order/{id}` returns 404 for keys that don't own the order.
6. **CORS is `allow_origins=["*"]`.** Tighten it for production.
7. **Usage counters are not atomic across workers.** Use `--workers 1`, or migrate counters to Redis/Postgres.

---

## 📄 License

MIT

---

## 💬 Credits

Developed by **S!XU** — https://t.me/aura_sajid
