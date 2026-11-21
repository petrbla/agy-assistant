---
trigger: always_on
---

# Kontext projektu: Asistent Antigravity (`asistentantigravity`)

Tento repozitář je **single-user** Python orchestrátor pro autonomního osobního a pracovního AI asistenta („AI zaměstnance“) běžícího nad **Google Antigravity CLI (`agy`)** v headless režimu uvnitř Docker kontejneru na VPS. 1 kontejner = 1 uživatel.

## Mapa repozitáře

- `app/` – Python orchestrátor (Python 3.12, FastAPI, Uvicorn, aiogram 3, APScheduler, Pydantic Settings, SQLite):
  - `main.py` – vstupní bod `serve()`, migrace, inicializace komponent a `TelegramBotController`.
  - `config.py` – `Settings` z ENV/`.env` (vč. aliasů `LANGUAGE`, `JMENO`, `TG_TOKEN`, `TG_ID`) a `ContextMode` (`auto | pracovni | osobni`).
  - `i18n.py` + `locales/*.json` – souborová lokalizace (`en.json`, `cs.json`) sdílená pro Web UI, Telegram bota, plánovač i prompty.
  - `agy_runner.py` – `AgyRunner`: teplý proces `agy` (`stream-json`) s fallbackem na `agy -p`, přednačtení paměti, jeden `asyncio.Lock`.
  - `auth_session.py` – `AgyAuthManager`, interaktivní OAuth přihlášení `agy` přes POSIX `pty` (na Windows nepodporováno).
  - `db.py` – `AssistantDB`: tabulky `owner_config` (vč. sloupce `language`), `auth_sessions`, `mcp_servers`, `sessions`, `runs`, `scheduled_tasks` + jednorázová migrace ze starší multi-user DB.
  - `user_env.py` – workspace ze šablon (`en` / `cs`), `mcp_config.json`, `settings.json`, OAuth detekce a migrace legacy HOME.
  - `memory.py` – `WorkspaceMemory`, bezpečná správa souborů paměti a skillů.
  - `runtime_config.py` – `data/runtime_config.json` a synchronizace `settings.json`.
  - `scheduler.py` – Heartbeat, Ranní briefing, noční úklid paměti s resetem konverzace a vlastní úlohy.
  - `security.py` – PBKDF2 hesla, TOTP 2FA, záložní kódy a podepisování výzev.
  - `attachments.py` – bezpečné ukládání a extrakce textu z příloh Telegramu (`workspace/inbox/`).
  - `telegram_bot.py` – `TelegramAssistantBot` a `TelegramBotController` (samoopravný polling, watchdog, `/lang`).
  - `web.py` + `static/{index.html,styles.css,app.js}` – REST API (vč. `/api/i18n/{lang}`) a moderní Web UI (heslo + 2FA, EN/CS přepínač).
- `workspace_template/` & `workspace_template_cs/` – výchozí anglické a české šablony paměti (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `HEARTBEAT.md`, `memory/`) a `.agents/skills/` běžícího asistenta.
- `tests/` – testy v `pytest` (`asyncio_mode = auto`).
- `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`, `README.md`, `README.cs.md`, `.github/workflows/docker-image.yml` – kontejnerizace (1 volume `/home/asistent`) a CI build na `ghcr.io`.

## Zásady práce v tomto projektu

1. **Nezaměňuj `.agents/` a `workspace_template/.agents/`:** Kořenové `.agents/` slouží pro vývoj tohoto kódu; `workspace_template/.agents/` je šablona pro nasazeného asistenta.
2. **Jazyk a styl:** Uživatelské texty, logy, docstrings, UI i commit zprávy jsou psány **česky**.
3. **Bezpečnost spouštění:** `AgyRunner` spouští `agy` výhradně přes `asyncio.create_subprocess_exec` (bez shellu) pod `asyncio.Lock`. `WorkspaceMemory` blokuje path traversal a nepovolené přípony.
4. **Konzistence konfigurací:** Při změně proměnných prostředí nebo chování zkontroluj soulad mezi `app/config.py`, `app/runtime_config.py`, `.env.example`, `docker-compose.yml`, `docker-entrypoint.sh` a `README.md`.
5. **Žádná osobní ani firemní data v repozitáři:** Repozitář je určen ke zveřejnění – nepoužívej skutečné názvy firem, jména, e-maily ani tokeny (ani v testech a příkladech).
6. **Detailní podklady:** Architektura systému je v `.agents/docs/architecture.md`.
