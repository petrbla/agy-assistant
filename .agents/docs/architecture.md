# Architektura systému Asistent Antigravity

## Přehled

Asistent Antigravity je **single-user** orchestrátor v Pythonu (`app/`), který obaluje lokálně nainstalované **Google Antigravity CLI (`agy`)** a vystavuje ho přes tři kanály:
1. **Telegram bot** (`app/telegram_bot.py`, `aiogram` polling se samoopravnou smyčkou a 30s watchdogem v `TelegramBotController`)
2. **Web UI & REST API** (`app/web.py` + `app/static/{index.html,styles.css,app.js}`, FastAPI / Uvicorn na portu `8080`)
3. **Plánovač na pozadí** (`app/scheduler.py`, `APScheduler` `AsyncIOScheduler`)
4. **Lokalizace ze souborů** (`app/i18n.py` + `app/locales/{en,cs}.json`, výchozí `en` s přepínačem do `cs`, `GET /api/i18n/{lang}`, `/lang`)

Všechny kanály sdílejí jednu instanci `AgyRunner`, jednu SQLite databázi `AssistantDB` a jeden pracovní prostor `/home/asistent/workspace`.

## Datový tok

```text
Telegram (tg:<chat>:<kanál>)   Web UI (web:<kanál>)   Plánovač (cron:*, task:*)
              \                      |                      /
               +----------> AgyRunner (asyncio.Lock) <-----+
                            - conversation_id + context_mode + language z DB
                            - prompt = [Source] + language_hint + context_hint + paměť
                            - teplý proces: agy --input-format stream-json --output-format stream-json
                            - fallback: agy -p ... --output-format json
                                   |
             +---------------------+----------------------+
             v                     v                      v
     AssistantDB             workspace/ (paměť)     ~/.gemini/ (OAuth, settings.json,
   data/assistant.db                                 config/mcp_config.json)
```

## Start aplikace (`app/main.py`)

1. Načte `Settings()` a aplikuje `data/runtime_config.json`.
2. `migrate_legacy_home_if_needed()` – jednorázově přenese OAuth ze starší cesty `users/1/home/.gemini`.
3. `AssistantDB(...)` – při inicializaci jednorázově migruje starší multi-user schéma, doplní sloupec `language` a normalizuje názvy kontextů.
4. `ensure_owner()`, `cleanup_expired_auth_sessions()`, `import_global_mcp_if_needed()`, `ensure_workspace()` (vybere `workspace_template/` pro `en` nebo `workspace_template_cs/` pro `cs`), `prepare_agy_home()`.
5. Vytvoří `AgyRunner`, `AgyAuthManager`, `AssistantScheduler`, `TelegramBotController` a FastAPI aplikaci.
6. Spustí plánovač, Telegram bota a Uvicorn. Při ukončení zavře teplé procesy `agy`, PTY přihlášení, plánovač i bota.

## Oddělení kontextu (Pracovní vs. Osobní)

1. **Kanály chatu:** `auto`, `pracovni` (`work`), `osobni` (`personal`) – každý má vlastní `session_key` a vlastní `conversation_id`.
2. **Filtrování MCP:** `prepare_agy_home()` → `sync_mcp_config()` uvnitř `AgyRunner._lock` zapíše do `mcp_config.json` jen servery se `scope` `all` nebo shodným s aktivním kanálem.
3. **Prompt prefix:** `get_context_hint()` z `app/i18n.py` připojí instrukci používat jen pracovní / osobní nástroje v aktivním jazyce.
4. **Operační manuál:** `workspace_template/AGENTS.md` (EN) a `workspace_template_cs/AGENTS.md` (CS) ukládají povinnost doptat se při nejistotě.

## SQLite schéma (`app/db.py`)

- `owner_config` (1 řádek, `id = 1`): identita, jazyk (`language`), heslo, 2FA, Telegram, model, Gemini klíč, nastavení plánovače.
- `auth_sessions`: hash přihlašovacích tokenů Web UI + expirace.
- `mcp_servers`: konfigurace MCP serverů (`scope`, `transport`, `url`, `auth_token`, `command`, …).
- `plugin_configs`: oprávnění a stav OAuth připojení nainstalovaných Antigravity pluginů (`permissions_json`, `connected`, `connected_at`).
- `sessions`: `session_key`, `conversation_id`, `context_mode`.
- `runs`: historie běhů vč. tokenů.
- `scheduled_tasks`: vlastní úlohy (`cron` / `once`).

## Pluginy a jemná oprávnění (`app/plugins.py`, `/api/plugins*`)

- Správa oficiálních i partnerských `agy` pluginů (vč. Google Workspace: `gmail`, `gcalendar`, `gdrive`, `gdocs`, `gsheets`, `gslides`, a partnerských pluginů či GitHub URL) přes `agy plugin ...` a interaktivní PTY connect tok (`/plugin` → `Sign in`) s přísně validovaným předáním lokálního `http://127.0.0.1:<port>/callback` v backendu.
- Přepínače „Co asistent smí“ se propisují do `permissions.deny` v `settings.json`, `AGY_UNATTENDED=1` (pro Gmail bez oprávnění k odesílání) a do prompt politiky v `AgyRunner`.

## Bezpečnost

- **Telegram whitelist:** bot odpoví jen ID z `telegram_allowed_users`.
- **Web UI:** heslo (PBKDF2-SHA256), volitelné TOTP 2FA se záložními kódy, session tokeny v `auth_sessions`; záložní statický `WEB_AUTH_TOKEN` je při aktivním 2FA zablokovaný.
- **Souborový systém:** `WorkspaceMemory._resolve_safe()` blokuje path traversal a nepovolené přípony.
- **Procesy:** `agy` se spouští výhradně přes `create_subprocess_exec` (bez shellu) s timeoutem.
- **Kontejner:** běží pod non-root uživatelem `asistent` (UID 1000) přes `gosu`.
