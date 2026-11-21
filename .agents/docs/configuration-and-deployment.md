# Konfigurace, Docker, Nasazení a Šablony

## Proměnné prostředí (`.env.example`, `app/config.py`, `docker-compose.yml`)

| Proměnná (a aliasy) | Výchozí hodnota | Popis |
| :--- | :--- | :--- |
| `ASSISTANT_NAME` / `JMENO` | `Jarvis` | Jméno asistenta (dosazuje se do `SOUL.md` za `{{JMENO}}`) |
| `WORKSPACE_DIR` | `/home/asistent/workspace` | Pracovní prostor s pamětí a `.agents/skills/` |
| `DATA_DIR` | `/home/asistent/data` | `assistant.db`, `runtime_config.json`, `auth_secret.key` |
| `AGY_HOME_DIR` | `/home/asistent` | HOME pro `agy` (`.gemini/`) |
| `GEMINI_API_KEY` | `""` | Volitelný Gemini API klíč (alternativa k OAuth) |
| `AGY_BIN` | `agy` | Binárka Antigravity CLI |
| `AGY_MODE` | `auto` | Mód `--mode` (pokud není skip permissions) |
| `AGY_DANGEROUSLY_SKIP_PERMISSIONS` | `true` | Přidá `--dangerously-skip-permissions` |
| `AGY_TIMEOUT_SECONDS` | `300` | Časový limit jednoho volání |
| `AGY_PERSISTENT_SESSION` | `true` | Teplý proces `agy` přes `stream-json` |
| `TELEGRAM_BOT_TOKEN` / `TG_TOKEN` | `""` | Token bota od `@BotFather` |
| `TELEGRAM_ALLOWED_USERS` / `TG_ID` | `""` | Povolená Telegram ID (povinné) |
| `TELEGRAM_NOTIFY_CHAT_ID` | `""` | Cílové chat ID pro notifikace |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | Host a port Web UI |
| `WEB_AUTH_TOKEN` | `""` | Volitelný záložní statický token |
| `TZ` | `Europe/Prague` | Časové pásmo |
| `HEARTBEAT_ENABLED` / `HEARTBEAT_INTERVAL_MINUTES` | `true` / `60` | Heartbeat |
| `MORNING_BRIEFING_ENABLED` / `MORNING_BRIEFING_CRON` | `true` / `0 8 * * *` | Ranní briefing |
| `NIGHTLY_RESET_ENABLED` / `NIGHTLY_RESET_CRON` | `true` / `15 0 * * *` | Noční úklid paměti a reset konverzace |
| `DEFAULT_CONTEXT_MODE` | `auto` | `auto`, `pracovni`, `osobni` |
| `MCP_PRACE_N8N_*`, `MCP_PRACE_NOCODB_*` | `""` | Výchozí pracovní MCP (`n8n-prace`, `nocodb-prace`) |
| `MCP_OSOBNI_N8N_*`, `MCP_OSOBNI_NOCODB_*`, `MCP_OSOBNI_GOOGLE_*` | `""` | Výchozí osobní MCP (`n8n-osobni`, `nocodb-osobni`, `google-osobni`) |

Hodnoty z `.env` slouží jako výchozí při prvním startu. Poté je zdrojem pravdy tabulka `owner_config` (změny přes Web UI).

## Kontejnerizace

### `Dockerfile`
- `python:3.12-slim-bookworm`, non-root uživatel `asistent` (UID/GID 1000).
- `agy` se instaluje do `/opt/agy-install` a symlinkuje do `/usr/local/bin/agy` (mimo volume `/home/asistent`).
- Jediný root wrapper `docker/agy-cli-update` (přes `sudoers`) pro aktualizaci `agy` z Web UI.
- `HEALTHCHECK` na `/api/health`.

### `docker-entrypoint.sh`
1. Vytvoří `workspace/`, `data/`, `secrets/`, `.gemini/antigravity-cli/`, `.gemini/config/`.
2. Zkopíruje `workspace_template/` přes `cp -rn` a dosadí `{{JMENO}}` do `SOUL.md`.
3. Vytvoří výchozí `settings.json` a případně `mcp_config.json` z `MCP_PRACE_*` / `MCP_OSOBNI_*` (jen pokud ještě neexistují).
4. `chown -R asistent:asistent /home/asistent` a `exec gosu asistent "$@"`.

### `docker-compose.yml`
- Jediný volume `${VOLUME_HOME:-asistent_home}:/home/asistent`, `restart: unless-stopped`, `init: true`, `mem_limit: 2g`, `cpus: 1.5`.

## GitHub Actions (`.github/workflows/docker-image.yml`)
- Při pushi do `main` sestaví image a nahraje ho na `ghcr.io/${{ github.repository }}:latest` (vždy s nejnovější verzí `agy`).

## Šablona pracovního prostoru (`workspace_template/`)
- `AGENTS.md` – operační manuál (čtení paměti, pravidla zápisu, oddělení pracovní vs. osobní agendy, plánovač přes `schedule_request.json`, `HEARTBEAT_OK`).
- `SOUL.md` – osobnost asistenta (`{{JMENO}}`).
- `USER.md` – profil uživatele (vyplňuje se při úvodním doptávání).
- `MEMORY.md` – dlouhodobá paměť (cílově do ~150 řádků).
- `HEARTBEAT.md` – checklist pravidelného probuzení.
- `memory/` – tematické a denní poznámky.
- `.agents/skills/morning-briefing/` a `.agents/skills/memory-maintenance/`.
