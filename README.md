# Asistent Antigravity (AI zaměstnanec v Dockeru)

Osobní a pracovní autonomní asistent inspirovaný konceptem **AI zaměstnance**, postavený na **Google Antigravity CLI (`agy`)** v headless režimu a připravený k nepřetržitému provozu 24/7 v Docker kontejneru na VPS.

Projekt je navržený jako **single-user**: 1 kontejner = 1 uživatel. Pro dalšího člověka stačí nasadit další kopii.

## Co umí

- **Telegram bot (`aiogram`):** ovládání z mobilu i PC s whitelistem povolených Telegram ID, samoopravným pollingem (watchdog) a příkazy `/auto`, `/pracovni`, `/osobni`, `/kontext`, `/model`, `/new`, `/uklid`, `/usage`, `/memory`, `/heartbeat`, `/briefing`, `/status`.
- **Moderní Web UI & REST API (`FastAPI`):** dashboard na portu `8080` (tmavý i světlý režim, bez npm buildu) se záložkami **Chat**, **Paměť**, **Plánovač & Historie** a **Nastavení**. Přihlášení heslem + volitelné **2FA (TOTP)**.
- **Rychlé odpovědi:** trvale spuštěný („teplý“) proces `agy --input-format stream-json --output-format stream-json`, přednačtení paměti při startu konverzace a automatický fallback na jednorázové `agy -p`.
- **Trvalá souborová paměť (`workspace/`):**
  - `AGENTS.md` – operační manuál a pravidla oddělení **pracovní** vs. **osobní** agendy
  - `SOUL.md` – osobnost a styl komunikace
  - `USER.md` – profil a preference uživatele
  - `MEMORY.md` + `memory/*.md` – dlouhodobá, tematická a denní paměť
  - `HEARTBEAT.md` – kontrolní seznam pro pravidelné probuzení na pozadí
- **Plánovač (`APScheduler`):** tichý *Heartbeat*, *Ranní briefing*, **noční úklid paměti a reset konverzace** (výchozí `00:15`) a vlastní opakované i jednorázové úlohy (proběhlé jednorázové úlohy se samy mažou).
- **MCP servery:** spravované ve Web UI a filtrované podle oblasti chatu (`Všechny` / `Pracovní` / `Osobní`).

## Struktura projektu

- `app/` – Python orchestrátor (`main.py`, `agy_runner.py`, `telegram_bot.py`, `web.py`, `scheduler.py`, `memory.py`, `db.py`, `config.py`, `user_env.py`, `auth_session.py`, `security.py`, `attachments.py`)
- `app/static/` – Web UI (`index.html`, `styles.css`, `app.js`)
- `workspace_template/` – výchozí šablony paměti a skillů (při startu se zkopírují do persistentního `workspace/`, pokud tam ještě nejsou)
- `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` – kontejnerizace pro VPS
- `tests/` – automatické testy (`pytest`)

---

## Nasazení na VPS přes Portainer (1 volume `/home/asistent`)

Všechna trvalá data (`workspace/`, `data/`, `.gemini/`) žijí v jediném adresáři **`/home/asistent`**.

### Krok 1: Build image
```bash
docker build -t asistent:latest https://github.com/petrbla/agy-assistant.git#main
```
*(nebo použijte předbuildovaný image `ghcr.io/petrbla/agy-assistant:latest`, který staví GitHub Actions při každém pushi do `main`)*

### Krok 2: Portainer → Stacks → Add stack → Web editor
```yaml
services:
  asistent:
    image: asistent:latest
    pull_policy: never
    container_name: asistent
    restart: unless-stopped
    init: true
    environment:
      JMENO: "Jarvis"
      TG_TOKEN: "xxx"
      TG_ID: "xxx"
    volumes:
      - /opt/docker/asistent/home:/home/asistent
    ports:
      - "8080:8080"
    mem_limit: 2g
    cpus: 1.5
```

### Krok 3: První spuštění
1. Otevřete `http://<IP-VPS>:8080` a nastavte si e-mail, uživatelské jméno a heslo.
2. V **Nastavení → Antigravity CLI & Přihlášení** klikněte na **Spustit OAuth přihlášení (agy)**, otevřete vypsanou URL a vložte ověřovací kód. *(Alternativně lze vložit vlastní Gemini API klíč.)*
3. V **Nastavení → Zabezpečení účtu** si doporučeně zapněte **2FA**.

> Pro vystavení do internetu doporučujeme před kontejner reverzní proxy s HTTPS (Caddy, Nginx, Traefik).

---

## Nasazení přes terminál (Docker Compose)

```bash
git clone https://github.com/petrbla/agy-assistant.git
cd agy-assistant
cp .env.example .env   # volitelně vyplňte TELEGRAM_BOT_TOKEN a TELEGRAM_ALLOWED_USERS
docker compose up -d
```

Chcete-li image sestavit lokálně ze zdrojových kódů místo stažení z GHCR:
```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
*(Pro jistotu nejnovější verze `agy` při lokálním buildu použijte `--no-cache`.)*

---

## Napojení MCP serverů

1. **Doporučeno:** přidat servery ve Web UI v **Nastavení → MCP servery** a u každého zvolit oblast (`Všechny`, `Pracovní`, `Osobní`).
2. Volitelně vyplnit `MCP_PRACE_*` / `MCP_OSOBNI_*` v `.env` ještě před prvním spuštěním – `docker-entrypoint.sh` z nich jednorázově vygeneruje `~/.gemini/config/mcp_config.json`.

Pravidla oddělení pracovních a osobních nástrojů jsou v `workspace_template/AGENTS.md`. V Telegramu se oblast přepíná příkazy `/pracovni`, `/osobni` nebo `/auto`.

## Vývoj a testy

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```
