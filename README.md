🌐 **English** | [Čeština](README.cs.md)

# Antigravity Assistant (AI Employee in Docker)

A personal and work autonomous assistant inspired by the **AI Employee** concept, built on top of **Google Antigravity CLI (`agy`)** in headless mode and ready for 24/7 operation inside a Docker container on a VPS.

Designed as **single-user**: 1 container = 1 user. Deploy another container for another user.

## Features

- **Bilingual (`en` / `cs`) with file-based translations (`app/locales/*.json`):** English by default, instant switching to Czech via the topbar toggle (`EN / CS`), Settings, Telegram (`/lang cs`), or `LANGUAGE=cs` environment variable. Adding another language only requires dropping a new JSON file into `app/locales/`.
- **Telegram Bot (`aiogram`):** mobile & desktop control with user ID whitelist, self-healing polling (30s watchdog), file/photo/voice attachments, and commands `/auto`, `/work`, `/personal`, `/context`, `/model`, `/lang`, `/new`, `/cleanup`, `/usage`, `/memory`, `/heartbeat`, `/briefing`, `/status`.
- **Modern Web UI & REST API (`FastAPI`):** dashboard on port `8080` (dark & light theme, zero npm build) with **Chat**, **Memory**, **Scheduler & History**, and **Settings** tabs, protected by password + optional **2FA (TOTP)**.
- **Fast responses:** persistent warm `agy --input-format stream-json --output-format stream-json` process, startup memory preloading, and automatic fallback to one-shot `agy -p`.
- **Persistent Markdown memory (`workspace/`):**
  - `AGENTS.md` – operating manual and **Work** vs. **Personal** separation rules
  - `SOUL.md` – assistant identity and communication style
  - `USER.md` – user profile and preferences
  - `MEMORY.md` + `memory/*.md` – curated long-term, thematic, and daily memory
  - `HEARTBEAT.md` – checklist for periodic background checks
- **Scheduler (`APScheduler`):** silent *Heartbeat*, *Morning Briefing*, **nightly memory cleanup & conversation reset** (default `00:15`), plus custom recurring or one-time tasks (expired one-time tasks clean up automatically).
- **MCP Servers:** managed in Web UI and filtered per chat area (`All` / `Work` / `Personal`).

## Project Structure

- `app/` – Python orchestrator (`main.py`, `agy_runner.py`, `telegram_bot.py`, `web.py`, `scheduler.py`, `memory.py`, `db.py`, `config.py`, `i18n.py`, `runtime_config.py`, `user_env.py`, `auth_session.py`, `security.py`, `attachments.py`, `version_check.py`)
- `app/locales/` – JSON translation files (`en.json`, `cs.json`)
- `app/static/` – Web UI (`index.html`, `styles.css`, `app.js`)
- `workspace_template/` – default English memory & skill templates (copied to persistent `workspace/` on first start)
- `workspace_template_cs/` – Czech memory & skill templates used when initialized with `LANGUAGE=cs`
- `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` – VPS containerization
- `tests/` – automated test suite (`pytest`)

---

## Language Switching & Adding Translations

- **Default:** `en` (English).
- **In Web UI:** click `EN` / `CS` in the top right bar (before or after login) or change it under **Settings → Language**.
- **In Telegram:** send `/lang cs` or `/lang en`.
- **At container startup:** set `LANGUAGE=cs` in environment variables (see below) to initialize both the UI and Czech `workspace_template_cs/` templates.
- **Add a new language:** copy `app/locales/en.json` to e.g. `app/locales/de.json`, update `_meta` (`code`, `name`, `short`, `date_locale`) and translate the strings. It is picked up automatically by both Web UI and `/lang`.

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
    environment:
      LANGUAGE: "en" # or "cs" for Czech
      TZ: "Europe/Prague"
      TG_TOKEN: "xxx"
      TG_ID: "xxx"
    volumes:
      - /opt/docker/asistent/home:/home/asistent
    ports:
      - "8080:8080"
    mem_limit: 2g
    cpus: 1.5
```

*(Alternatively, build the image manually on your server: `docker build -t asistent:latest https://github.com/petrbla/agy-assistant.git#main` and use `image: asistent:latest` with `pull_policy: never`.)*

### Step 2: First Launch
1. Open `http://<VPS-IP>:8080` and create your admin email, username, and password.
2. Go to **Settings → Antigravity CLI & Sign-in**, click **Start OAuth sign-in (agy)**, open the displayed URL, and paste the authorization code *(or enter your own Gemini API key)*.
3. Under **Settings → Account Security**, enabling **2FA** is strongly recommended.

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

## Connecting MCP Servers

1. **Recommended:** add servers in Web UI under **Settings → MCP Servers** and assign each to an area (`All chats`, `Work`, `Personal`).
2. Optionally set `MCP_WORK_*` / `MCP_PERSONAL_*` (or legacy Czech `MCP_PRACE_*` / `MCP_OSOBNI_*`) in `.env` before the very first startup – `docker-entrypoint.sh` generates `~/.gemini/config/mcp_config.json` automatically.

Context separation rules are defined in `workspace_template/AGENTS.md` (or `workspace_template_cs/AGENTS.md`). In Telegram, switch areas with `/work` (`/pracovni`), `/personal` (`/osobni`), or `/auto`.

## Development & Tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```
