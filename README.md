🌐 **English** | [Čeština](README.cs.md)

# Antigravity Assistant (AI Employee in Docker)

A personal and work autonomous assistant inspired by the **AI Employee** concept, built on top of **Google Antigravity CLI (`agy`)** in headless mode and ready for 24/7 operation inside a Docker container on a VPS.

Designed as **single-user**: 1 container = 1 user. Deploy another container for another user.

## Features

- **Multilingual (`en`, `cs`, `de`, `sk`, `es`, `pl`) with file-based translations (`app/locales/*.json`):** English by default, instant switching via the topbar selector, Settings, `/lang <code>` (Telegram & web chat), or the `LANGUAGE` environment variable (including matching localized `workspace_template*` memory & skill templates). Adding another language only requires dropping a new JSON file into `app/locales/`.
- **Telegram Bot (`aiogram`) & Web Slash Commands:** mobile & desktop control with user ID whitelist, self-healing polling (30s watchdog), file/photo/PDF/Office/audio attachments, and shared slash commands (`/auto`, `/work`, `/personal`, `/context`, `/model`, `/lang`, `/new`, `/cleanup`, `/usage` / `/quota` with live 5-hour and weekly Antigravity quota limits, `/memory`, `/heartbeat`, `/briefing`, `/status`) available in both Telegram and Web UI chat.
- **Modern Web UI & REST API (`FastAPI`):** Liquid Glass dashboard on port `8080` (dark & light theme, zero npm build) with **Chat**, **Memory**, **Scheduler & History**, and **Settings** (organized into *General & Telegram*, *Integrations & MCP*, *Automation & Backup*, and *Account & Security*), live interactive `agy` terminal, 5h/weekly quota overview, 1-click updates for both `agy` CLI and the assistant container (via optional `updater` sidecar), and password + optional **2FA (TOTP)** login.
- **Fast responses:** persistent warm `agy --input-format stream-json --output-format stream-json` process, startup memory preloading, and automatic fallback to one-shot `agy -p`.
- **Persistent Markdown memory (`workspace/`):**
  - `AGENTS.md` – operating manual and **Work** vs. **Personal** separation rules
  - `SOUL.md` – assistant identity and communication style
  - `USER.md` – user profile and preferences
  - `MEMORY.md` + `memory/*.md` – curated long-term, thematic, and daily memory
  - `HEARTBEAT.md` – checklist for periodic background checks
- **Scheduler (`APScheduler`):**
  - **System jobs:** silent *Heartbeat*, *Morning Briefing*, *Nightly memory cleanup & conversation reset* (default `00:15`), *Idle context cleanup* (`idle_reset`), automatic daily note retention pruning, and optional *Daily GitHub Backup*.
  - **Custom tasks (`cron` & `once`):** three execution modes — **Send message** (`message`, 0 AI tokens), **Run AI assistant** (`agent`), or **Run script / command** (`script`, 0 AI tokens; runs locally without a shell inside `workspace/` with proper `HOME`, `PATH`, and `TZ`, logs output to runs history, stays silent on success, and sends a Telegram alert on failure). Stored in SQLite so all tasks survive container restarts and updates without needing host cron or `setsid`.
- **Integrations, Plugins & MCP Servers:**
  - **Antigravity plugins:** manage official & partner plugins (Gmail, Google Calendar, Google Drive, Docs, Sheets, Slides, Notion, n8n, ClickUp, Linear, Figma, Stripe, etc.) with interactive OAuth sign-in from the Web UI, **multiple accounts per plugin** separated by area (`All chats` / `Work` / `Personal`), **3-state per-tool permissions** (*Always allow* / *Needs approval* / *Blocked*), and custom text rules.
  - **Custom MCP servers:** Streamable HTTP & stdio servers with Web UI editing, custom HTTP headers (e.g. `x-api-key`), 1-click JSON config import, live connection & tool discovery checks, and 3-state per-tool permissions.
- **Backup & Portability:** ZIP export/import of memory, skills, and scheduler configuration, plus optional automated daily sanitized backup of the entire assistant (excluding passwords, keys, and tokens) to a private GitHub repository.

## Project Structure

- `app/` – Python orchestrator (`main.py`, `agy_runner.py`, `telegram_bot.py`, `chat_commands.py`, `web.py`, `scheduler.py`, `memory.py`, `db.py`, `config.py`, `i18n.py`, `runtime_config.py`, `user_env.py`, `plugins.py`, `github_backup.py`, `auth_session.py`, `security.py`, `attachments.py`, `version_check.py`)
- `app/locales/` – JSON translation files (`en.json`, `cs.json`, `de.json`, `sk.json`, `es.json`, `pl.json`)
- `app/static/` – Web UI (`index.html`, `styles.css`, `app.js`)
- `workspace_template/` – default English memory & skill templates (copied to persistent `workspace/` on first start)
- `workspace_template_{cs,de,sk,es,pl}/` – localized memory & skill templates selected via `LANGUAGE`
- `Dockerfile`, `docker/`, `docker-compose.yml`, `docker-compose.build.yml`, `docker-entrypoint.sh`, `VERSION` – VPS containerization & 1-click update support
- `tests/` – automated test suite (`pytest`)

---

## Language Switching & Adding Translations

- **Default:** `en` (English).
- **Available out of the box:** `en`, `cs`, `de`, `sk`, `es`, `pl`.
- **In Web UI:** pick a language in the top right bar (before or after login) or change it under **Settings → General & Telegram**.
- **In Telegram / Web Chat:** send `/lang cs`, `/lang en`, `/lang de`, `/lang sk`, `/lang es`, or `/lang pl`.
- **At container startup:** set `LANGUAGE=cs` (or another supported code) in environment variables (see below) to initialize both the UI and the matching `workspace_template_<lang>/` templates.
- **Add a new language:** copy `app/locales/en.json` to e.g. `app/locales/fr.json`, update `_meta` (`code`, `name`, `short`, `date_locale`) and translate the strings. It is picked up automatically by both Web UI and `/lang`.

---

## Deployment on VPS via Portainer (Single Volume `/home/asistent`)

All persistent data (`workspace/`, `data/`, `.gemini/`) lives in a single directory: **`/home/asistent`**.

### Step 1: Portainer → Stacks → Add stack → Web editor
The easiest option is the prebuilt image from GitHub Container Registry (`ghcr.io/petrbla/agy-assistant:latest`), built automatically by GitHub Actions on every push to `main`:

```yaml
services:
  asistent:
    image: ghcr.io/petrbla/agy-assistant:latest
    pull_policy: always
    container_name: asistent
    restart: unless-stopped
    init: true
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.scope=asistent"
    environment:
      LANGUAGE: "en" # supported: en, cs, de, sk, es, pl
      TZ: "Europe/Prague"
      TG_TOKEN: "xxx"
      TG_ID: "xxx"
      APP_UPDATE_URL: "http://updater:8080/v1/update?async=true"
      APP_UPDATE_TOKEN: "change-me-updater-token"
    volumes:
      - /opt/docker/asistent/home:/home/asistent
    ports:
      - "8080:8080"
    mem_limit: 2g
    cpus: 1.5

  # Optional lightweight sidecar enabling 1-click updates from Web UI (no exposed ports)
  updater:
    image: nickfedor/watchtower:latest
    container_name: asistent-updater
    restart: unless-stopped
    labels:
      - "com.centurylinklabs.watchtower.enable=false"
      - "com.centurylinklabs.watchtower.scope=asistent"
    environment:
      TZ: "Europe/Prague"
      WATCHTOWER_HTTP_API_UPDATE: "true"
      WATCHTOWER_HTTP_API_PERIODIC_POLLS: "false"
      WATCHTOWER_HTTP_API_TOKEN: "change-me-updater-token"
      WATCHTOWER_LABEL_ENABLE: "true"
      WATCHTOWER_SCOPE: "asistent"
      WATCHTOWER_CLEANUP: "true"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    mem_limit: 128m
    cpus: 0.25
```

> With the `updater` sidecar included, whenever a new assistant version is released, an **Update assistant** button appears directly in the Web UI (in the top update banner and under **Settings**) — clicking it pulls the new image and restarts the container without opening Portainer. Make sure `APP_UPDATE_TOKEN` and `WATCHTOWER_HTTP_API_TOKEN` match and use your own secret value.

*(Alternatively, build the image manually on your server: `docker build -t asistent:latest https://github.com/petrbla/agy-assistant.git#main` and use `image: asistent:latest` with `pull_policy: never` and without the `updater` service.)*

### Step 2: First Launch
1. Open `http://<VPS-IP>:8080` and create your admin email, username, and password.
2. Go to **Settings → General & Telegram → Antigravity CLI & Sign-in**, click **Start OAuth sign-in (agy)**, open the displayed URL, and paste the authorization code *(or enter your own Gemini API key)*.
3. Under **Settings → Account & Security**, enabling **2FA** is strongly recommended.

> For public internet exposure, place the container behind a reverse proxy with HTTPS (Caddy, Nginx Proxy Manager, Traefik).

---

## Terminal Deployment (Docker Compose)

```bash
git clone https://github.com/petrbla/agy-assistant.git
cd agy-assistant
cp .env.example .env   # optionally set LANGUAGE, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS
docker compose up -d
```

To build the image locally from source instead of pulling from GHCR:
```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
*(Use `--no-cache` when building locally to fetch the latest `agy` CLI release.)*

---

## Connecting Integrations, MCP Servers & Scheduled Scripts

1. **Integrations & Plugins (recommended):** connect Google Workspace and partner plugins under **Settings → Integrations & MCP**, add multiple accounts per plugin if needed (`All chats`, `Work`, `Personal`), and configure per-tool permissions.
2. **Custom MCP Servers:** add Streamable HTTP or stdio servers in **Settings → Integrations & MCP** (or paste a full JSON snippet via **Paste from JSON config** — URL, custom HTTP headers, and transport are filled automatically), test live tool discovery, and manage per-tool permissions. Optionally set `MCP_WORK_*` / `MCP_PERSONAL_*` (or legacy Czech `MCP_PRACE_*` / `MCP_OSOBNI_*`) in `.env` before the very first startup.
3. **Scheduling local scripts (`script`):** in **Scheduler & History** (or via chat through `schedule_request.json`), choose **Run script (0 tok.)** with a cron expression such as `0 * * * *` and command `python3 /home/asistent/workspace/scripts/job.py`. Scripts run inside the container with `HOME`, `PATH`, and `TZ`, log output to run history, alert Telegram on error, and persist in SQLite across container restarts and updates without host cron.

Context separation rules are defined in `workspace_template/AGENTS.md` (or `workspace_template_<lang>/AGENTS.md`). In Telegram or Web Chat, switch areas with `/work` (`/pracovni`), `/personal` (`/osobni`), or `/auto`.

## Development & Tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```
