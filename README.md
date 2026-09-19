# FF TopUp Automation — Bangladesh

A Free Fire diamond top-up automation service for the Bangladesh server, with a
FastAPI web API, a Telegram bot, and a Playwright-driven browser automation layer.

---

## 📑 Table of Contents

1. [Features](#-features)
2. [Architecture](#-architecture)
3. [Supported Environments](#-supported-environments)
4. [File Structure](#-file-structure)
5. [Installation](#-installation)
   - [Linux (Debian/Ubuntu)](#linux-debianubuntu)
   - [Linux (Fedora/RHEL/Arch)](#linux-fedorarhelarch)
   - [macOS](#macos)
   - [Windows 10/11](#windows-1011)
   - [Docker](#docker)
   - [Railway / Fly.io / Render](#railway--flyio--render)
6. [Configuration (.env)](#-configuration-env)
7. [What to Change Per Environment](#-what-to-change-per-environment)
8. [API Reference](#-api-reference)
9. [Voucher Format](#-voucher-format)
10. [Package Values](#-package-values)
11. [Troubleshooting](#-troubleshooting)
12. [Security Notes](#-security-notes)

---

## ✨ Features

- **Browser automation** via Playwright (stealth + fingerprint rotation)
- **Shared Chromium pool** — one browser, many contexts, fast concurrent top-ups
- **Idle fingerprint rotator** — rotates Chromium after N seconds of inactivity
- **FastAPI HTTP API** with API-key protection for direct integrations
- **Telegram bot** with conversational flow + direct-command mode
- **Web UI** (`index.html`) served at `/`
- **DNS + TLS warm-up** to reduce first-request latency
- **Player name lookup** via configurable third-party APIs
- **Voucher auto-detection** (`UPBD*` → UP Gift, `BDMB*` → UniPin)

---

## 🏗 Architecture

```
                 ┌──────────────────┐
Browser ────────►│                  │
                 │   FastAPI (main) │────► automation ──► browser_pool ──► Playwright
Telegram ◄──────►│                  │              │
                 │   + bot polling  │              ▼
                 └──────────────────┘        Garena shop (shop.garena.my)
                        │
                        ▼
                 apikeys.json (usage counters)
                 .env          (config)
```

**Key modules:**

| File | Role |
|---|---|
| `main.py` | FastAPI app, HTTP endpoints, API-key auth, background tasks |
| `automation.py` | Playwright automation: opens Garena, fills UID, selects package, submits voucher, polls result |
| `browser_pool.py` | Shared Chromium pool, stealth fingerprint, idle rotator |
| `bot.py` | Telegram bot (long-polling) |
| `index.html` | Web frontend (single file, dark/light theme) |
| `apikeys.json` | Direct-API key + usage store |
| `.env` | Runtime configuration |

---

## 💻 Supported Environments

| Environment | Supported | Notes |
|---|---|---|
| **Ubuntu / Debian** | ✅ Recommended | Best tested, longest support window |
| **Fedora / RHEL** | ✅ | Slight package name differences |
| **Arch / Manjaro** | ✅ | Rolling release, works fine |
| **Alpine Linux** | ⚠️ | Playwright deps need extra work; use Ubuntu-based image instead |
| **macOS (Intel / Apple Silicon)** | ✅ | Chromium via Playwright works natively |
| **Windows 10 / 11** | ✅ | Requires PowerShell or CMD; not WSL-only |
| **Docker (any host)** | ✅ | Recommended for production |
| **Railway / Fly.io / Render** | ✅ | Use the Playwright base image, see Docker section |
| **Raspberry Pi (ARM64)** | ⚠️ | Playwright support is experimental; expect missing packages |
| **AWS Lambda / Cloud Functions** | ❌ | Not supported — no persistent filesystem or long-running processes |

**Python:** 3.10, 3.11, or 3.12 (3.12 recommended; 3.9 not tested with current deps).

---

## 📁 File Structure

The layout is **identical across all environments**:

```
ff-topup/
├── main.py               # FastAPI entrypoint
├── automation.py         # Playwright automation
├── browser_pool.py       # Shared Chromium pool
├── bot.py                # Telegram bot
├── index.html            # Web UI (served at /)
├── apikeys.json          # Direct-API key store (auto-created if missing)
├── requirements.txt      # Python dependencies
├── railway.json          # Railway deploy config (optional)
├── Dockerfile            # If using Docker
├── .env                  # Runtime config (NOT committed)
└── .env.example          # Template (committed)
```

---

## 📦 Installation

### Common Prerequisites (all platforms)

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

# 3. Install Python deps
pip install -r requirements.txt

# 4. Install Chromium for Playwright
playwright install chromium
playwright install-deps   # only needed once; requires sudo

# 5. Configure
cp .env.example .env
$EDITOR .env

# 6. Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

### Linux (Fedora/RHEL/Arch)

The Python workflow is identical. Only the **system packages** differ:

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

Then continue with the same `pip install` / `playwright install` steps.

---

### macOS

```bash
# 1. Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Python (system python3 works but brew python is safer)
brew install python@3.12

# 3. Clone & venv
git clone <your-repo> ff-topup && cd ff-topup
python3.12 -m venv .venv
source .venv/bin/activate

# 4. Python deps
pip install -r requirements.txt

# 5. Chromium for Playwright
playwright install chromium
# (no `install-deps` needed on macOS)

# 6. Configure
cp .env.example .env
$EDITOR .env

# 7. Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

> **Apple Silicon note:** Playwright ships native arm64 Chromium. No Rosetta required.

---

### Windows 10/11

Use **PowerShell** (not CMD) for the venv activation commands.

```powershell
# 1. Python from python.org (or Microsoft Store)
#    Ensure "Add Python to PATH" is checked during install.

# 2. Clone & venv
git clone <your-repo> ff-topup
cd ff-topup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 3. Python deps
pip install -r requirements.txt

# 4. Chromium for Playwright
playwright install chromium
# No `install-deps` on Windows; Chromium ships with its DLLs.

# 5. Configure
Copy-Item .env.example .env
notepad .env

# 6. Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

> **Windows PATH issue:** if `playwright` isn't found, run
> `.\.venv\Scripts\playwright.exe install chromium`.

> **Antivirus:** Windows Defender sometimes flags Chromium's headless mode.
> Add your project folder to the exclusion list.

---

### Docker

Use the **official Playwright base image** — it already has Chromium and
all OS deps:

```dockerfile
# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Playwright browsers already installed in base image
# (chromium + firefox + webkit are all present by default)

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

Build and run:

```bash
docker build -t ff-topup .
docker run --rm -p 8080:8080 --env-file .env ff-topup
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

> ⚠️ **Important:** bump `healthcheckTimeout` to **90** (not 30). Chromium
> takes 15–25s to launch on cold start, plus DNS+TLS warm-up.

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

[checks.health]
  type = "http"
  port = 8080
  path = "/api/health"
  interval = "30s"
  timeout = "90s"
  grace_period = "120s"
```

**Render:** use the "Docker" environment, point to your `Dockerfile`, and
set `healthCheckPath` to `/api/health`. Expect cold starts of 20–40s
because Render spins down free services.

> ⚠️ **Do NOT deploy to AWS Lambda / Cloudflare Workers / Vercel serverless.**
> Chromium can't run in those environments, and the top-up flow takes
> 30–120 seconds per request.

---

## ⚙ Configuration (`.env`)

The `.env` file **must be named exactly `.env`** (no `.txt`), and it must
sit next to `main.py`.

### Full Reference

```env
# ─── Telegram Bot ─────────────────────────────────────────────
TG_BOT_TOKEN=              # From @BotFather. Empty = bot disabled.
TG_CHAT_ID=                # Admin chat for notifications. Empty = silent.
TG_ALLOWED_USERS=          # Comma-separated Telegram user IDs. Empty = allow all.

# ─── Browser Mode ─────────────────────────────────────────────
HEADLESS=true              # true = invisible (server). false = visible (local debug).
WEBRENDER=false            # true = load CSS/images. false = plain-text (faster).
BLOCK_TRACKERS=true        # Block analytics domains.
ENABLE_WARMUP=true         # Pre-resolve DNS+TLS for Garena/UniPin before launch.

# ─── Timeouts (milliseconds) ──────────────────────────────────
TIMEOUT_NAV=60000          # Page navigation timeout.
TIMEOUT_SHORT=45000        # Short waits (button visible, etc.).
TIMEOUT_RESULT=90000       # Max time to poll for the top-up result.

# ─── Fixed Pauses (milliseconds) ──────────────────────────────
PAUSE_AFTER_PAGE=800       # After Garena home loads.
PAUSE_AFTER_UID=2500       # After typing the UID.
PAUSE_AFTER_LOGIN=600      # After clicking Login.
PAUSE_AFTER_PROCEED=400    # After "Proceed to Payment".
PAUSE_AFTER_DIAMOND=300    # After clicking a package tile.
PAUSE_AFTER_TAB=300        # After opening "Physical Vouchers".
PAUSE_AFTER_PROVIDER=300   # After selecting UniPin / UP Gift.
PAUSE_AFTER_SERIAL=100     # After typing the serial.
PAUSE_AFTER_PIN=100        # After typing the PIN.
PAUSE_BEFORE_CONFIRM=80    # Before clicking Confirm.

# ─── Result Polling (milliseconds) ────────────────────────────
RESULT_POLL_INTERVAL=100        # After the first 5 seconds.
RESULT_POLL_INTERVAL_FAST=50    # During the first 5 seconds.

# ─── Typing Speed (ms between keystrokes) ─────────────────────
TYPE_DELAY_UID=15
TYPE_DELAY_PIN=10

# ─── Fallback Package ─────────────────────────────────────────
FALLBACK_PACKAGE=25        # Used when a direct-API caller omits `pkg`.

# ─── Browser Fingerprint Rotation ─────────────────────────────
IDLE_ROTATE_SECONDS=300    # Rotate Chromium after N seconds idle.
ROTATOR_INTERVAL=30        # How often the rotator wakes up.

# ─── Player Name API ──────────────────────────────────────────
PLAYER_API_URLS=https://ffnamecheck.vercel.app/info?uid={uid}
PLAYER_API_REGION=bd       # Appended as `&region=bd` if not already in URL.

# ─── Developer Credit (footer) ────────────────────────────────
DEV_NAME=S!XU
DEV_LINK=https://t.me/aura_sajid
DEV_LINK_TEXT=S!XU

# ─── Server ───────────────────────────────────────────────────
PORT=8000                  # Local port (Railway/Fly override this).
```

### Booleans

All boolean values accept only `true` or `false` (lowercase). Anything
else is treated as `false` by `_bool()`.

### Comments

Inline `#` comments after a value are supported by `python-dotenv` but
**fragile**. Put comments on their own line.

---

## 🔧 What to Change Per Environment

### 1. `HEADLESS`

| Where | Recommended | Why |
|---|---|---|
| Local dev (any OS) | `false` | See the browser window for debugging |
| Server / VPS / Docker / Cloud | `true` | No display; headless is required |

### 2. `WEBRENDER`

| Where | Recommended | Why |
|---|---|---|
| Slow networks / cost-sensitive | `false` | Blocks images, CSS, fonts |
| Garena changed layout and packages won't click | `true` | Full render may fix selectors |
| Debugging UI issues | `true` | See what the page really looks like |

### 3. `ENABLE_WARMUP`

| Where | Recommended | Why |
|---|---|---|
| Cold-start platforms (Railway, Fly, Render) | `true` | Saves ~1s on first request |
| Frequently rotated browser pool | `false` | Warm-up runs on every rotation; slow |
| Very reliable network | `false` | Not needed |

### 4. `IDLE_ROTATE_SECONDS`

| Where | Recommended | Why |
|---|---|---|
| High traffic | `600–1200` | Avoid frequent browser restarts |
| Low traffic / security-sensitive | `120–300` | Rotate fingerprints faster |
| Debugging | `99999` | Effectively disable rotation |

### 5. `PORT`

| Where | Value |
|---|---|
| Local Linux/macOS/Windows | `8000` (or any free port) |
| Railway | leave unset — Railway injects `$PORT` |
| Fly.io | leave unset — Fly injects `$PORT` (internal 8080 by default) |
| Docker | whatever you `EXPOSE` (8080 in the Dockerfile above) |
| Render | leave unset — Render injects `$PORT` |

### 6. Player Name API

The default `https://ffnamecheck.vercel.app/info?uid={uid}` is a
third-party endpoint and may go offline or rate-limit you.

**Change it if:**
- You get "Player not found" for valid UIDs
- You see "HTTP Error" in responses
- You want to run your own lookup service

Set one or more URLs (comma-separated, tried left to right):

```env
PLAYER_API_URLS=https://api1.example/info?uid={uid},https://api2.example/check/{uid}
PLAYER_API_REGION=bd
```

The `{uid}` placeholder is replaced with the sanitized UID. If a URL
doesn't already contain `?region=` or `&region=`, `PLAYER_API_REGION` is
appended automatically.

### 7. Telegram Bot

| Where | Value |
|---|---|
| Production | Set `TG_BOT_TOKEN` + `TG_CHAT_ID` |
| Local dev, no bot needed | Leave `TG_BOT_TOKEN` empty → bot disabled automatically |
| Restricted access | Set `TG_ALLOWED_USERS=123456,789012` |

### 8. `FALLBACK_PACKAGE`

This is what a **direct-API caller** gets when they omit `pkg`.
Defaults:

- `main.py` defaults to `115` if `.env` doesn't set it
- `.env.example` ships with `25`

Pick a small value (e.g. `25`) so accidental fallbacks don't burn big
vouchers.

### 9. Filesystem Permissions

**Linux / macOS:** the directory containing `apikeys.json` must be
**writable** by the user running uvicorn. The app rewrites this file
whenever a key is consumed.

```bash
chmod 664 apikeys.json
chmod 755 .
```

**Windows:** ensure the process user has **Modify** permission on the
project folder (default for user-owned folders is fine).

**Docker:** mount a volume if you want usage counters to persist across
container restarts:

```bash
docker run -v $(pwd)/apikeys.json:/app/apikeys.json:rw ...
```

**Railway / Fly / Render:** the filesystem is **ephemeral** — usage
counters reset on every redeploy. If you need durable quota tracking,
move to Redis/Postgres/KV.

### 10. Static Frontend

`index.html` is served by FastAPI directly from disk. On **container
platforms**, ensure it's copied into the image (the Dockerfile above
does `COPY . .` which includes it).

If you serve the frontend from a separate domain (e.g. GitHub Pages),
you'll need to:

1. Set CORS origins in `main.py` (currently `allow_origins=["*"]`)
2. Point the frontend's `fetch('/api/...')` calls at the API's full URL

---

## 🌐 API Reference

Base URL (local): `http://localhost:8000`

### Health Check

```
GET /api/health
```

**Response:**
```json
{
  "ok": true,
  "service": "ff-topup-automation",
  "status": "online",
  "headless": "true",
  "webrender": "false",
  "warmup": "true",
  "idle_rotate_seconds": 300,
  "rotator_interval": 30,
  "tg_enabled": true,
  "api_keys_count": 4,
  "api_keys_enabled": 3,
  "fallback_package": "25"
}
```

### Public Config (used by `index.html`)

```
GET /api/config
```

**Response:**
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

**Response:**
```json
{
  "uptime_seconds": 3600,
  "total": 42,
  "success": 40,
  "failed": 2,
  "last_duration": 3.21,
  "average_duration": 3.85,
  "min_duration": 2.10,
  "max_duration": 12.44,
  "recent_durations": [3.21, 4.02, 2.88]
}
```

### Public Top-Up (no API key)

Used by `index.html`. **No authentication.**

```
GET  /api/topup?uid=<uid>&pkg=<pkg>&code=<voucher>[&pin=<pin>]
POST /api/topup
```

**GET query params:**

| Name | Type | Required | Description |
|---|---|---|---|
| `uid` | string | ✅ | Free Fire player UID (6–15 digits) |
| `pkg` | string | ✅ | Package: `25` / `115` / `weekly` / etc. |
| `code` | string | ✅ | Voucher serial, optionally with PIN |
| `pin` | string | ❌ | PIN if not embedded in `code` |

**POST body (JSON):**

```json
{
  "uid": "3761036809",
  "pkg": "115",
  "code": "UPBD-P-S-01989133 1527-6324-9731-2625",
  "pin": ""
}
```

**Success response:**

```json
{
  "success": true,
  "message": "Topup Completed Successfully!",
  "package_input": "115",
  "duration": 3.42
}
```

**Failure response:**

```json
{
  "success": false,
  "reason": "INVALID_VOUCHER",
  "message": "Invalid voucher code. Check the serial and PIN.",
  "package_input": "115",
  "duration": 2.88
}
```

### Direct API (API-key protected)

Same behavior as `/api/topup`, but requires an `api` key. Three aliases
are provided:

```
GET /api=<key>&uid=...&code=...&pkg=...[&pin=...]
GET /topup?api=<key>&uid=...&code=...&pkg=...[&pin=...]
GET /api/direct?api=<key>&uid=...&code=...&pkg=...[&pin=...]
```

**Query params:**

| Name | Type | Required | Description |
|---|---|---|---|
| `api` | string | ✅ | API key from `apikeys.json` |
| `uid` | string | ✅ | Player UID |
| `code` | string | ✅ | Voucher serial (+ PIN if embedded) |
| `pkg` | string | ❌ | Falls back to `FALLBACK_PACKAGE` |
| `pin` | string | ❌ | PIN |

**Success adds:**

```json
{
  "usage_remaining": 42,
  "usage_written": true
}
```

**401 response:**

```json
{
  "detail": "API key rejected: unknown key"
}
```

Possible `detail` values: `missing key`, `unknown key`,
`key disabled or exhausted`.

### Admin Endpoints (localhost only)

These return **403 Forbidden** if the caller's IP is not
`127.0.0.1`, `localhost`, or `::1`.

```
GET  /api/keys           # list keys (masked)
POST /api/keys/reload    # reload apikeys.json from disk
```

### Frontend

```
GET /      # index.html
```

---

## 🎟 Voucher Format

Supported serial prefixes:

| Prefix | Provider | Selector in Garena |
|---|---|---|
| `UPBD` | UP Gift | "UP Gift" tab |
| `BDMB` | UniPin | "UniPin" tab |

**Accepted input formats:**

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
| `weekly` | Weekly Membership (alias: `161`) |
| `monthly` | Monthly Membership (alias: `800`) |

Case-insensitive. `WEEKLY`, `Weekly`, `week`, `w` all resolve to
`weekly`.

---

## 🩺 Troubleshooting

### Chromium won't launch

```
playwright._impl._errors.Error: Executable doesn't exist at ...
```

**Fix:** `playwright install chromium`. On Linux, also run
`playwright install-deps` (requires sudo).

### `TypeError: process_freefire_topup() got an unexpected keyword argument`

You're running an old version of `bot.py` that calls the automation
with `headless=...`. Update `bot.py` — the current signature is
`process_freefire_topup(uid, pkg, voucher, pin="")`.

### Telegram bot disabled

```
⚠️ TG_BOT_TOKEN not set — Telegram bot disabled.
```

**Fix:** check that:
1. The file is literally named `.env` (not `.env.txt`)
2. It's in the same directory as `bot.py`
3. `TG_BOT_TOKEN=` is filled with a real token from @BotFather
4. You restarted the service

### Player lookup fails for valid UIDs

The default `ffnamecheck.vercel.app` API is a third-party service and
can be offline. Set `PLAYER_API_URLS` to a working endpoint.

### Package tile won't click

If Garena updated their UI:

1. Try `WEBRENDER=true`
2. Check the automation logs for "Could not click package tile"
3. Adjust the selectors in `automation.py::_click_tile_by_text`

### Railway healthcheck fails

```
Deploy failed: healthcheck timeout
```

Bump `healthcheckTimeout` in `railway.json` to **90** (Chromium cold
starts take 15–25s plus warm-up).

### HTTP request times out

`/api/topup` blocks for 30–120s. Some reverse proxies cut the connection
at 30s. Either:

- Increase the proxy timeout
- Wrap `automation.process_freefire_topup` in a background task and
  return a job ID (job-queue pattern)

### `apikeys.json` gets overwritten

By design — the app rewrites it whenever usage decrements. On ephemeral
filesystems (Railway/Fly/Render), usage resets on every redeploy.

---

## 🔒 Security Notes

1. **Never commit `.env`.** Add it to `.gitignore`.
2. **Rotate leaked tokens.** If `TG_BOT_TOKEN` was ever pasted into
   chat, a gist, or a screenshot, revoke it via @BotFather
   (`/revoke` → select your bot → generate a new one).
3. **`apikeys.json` is plain-text.** Treat it like a password store.
4. **The public `/api/topup` has no auth.** If you expose the service
   publicly, anyone can burn your vouchers. Options:
   - Put a reverse proxy (nginx/Caddy) in front with HTTP basic auth
   - Require an API key for `/api/topup` too (edit `main.py`)
   - Bind to `127.0.0.1` and use an SSH tunnel
5. **CORS is `allow_origins=["*"]`.** Tighten it in `main.py` for
   production:

   ```python
   app.add_middleware(
       CORSMiddleware,
       allow_origins=["https://yourdomain.com"],
       allow_credentials=True,
       allow_methods=["GET", "POST"],
       allow_headers=["*"],
   )
   ```

6. **Usage counters are not atomic.** Concurrent requests on multiple
   workers (uvicorn `--workers N`) can race on `apikeys.json`. Use a
   single worker, or move counters to a database.

---

## 📄 License

MIT

---

## 💬 Credits

Developed by **S!XU** — https://t.me/aura_sajid
