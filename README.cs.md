🌐 [English](README.md) | **Čeština**

# Asistent Antigravity (AI zaměstnanec v Dockeru)

Osobní a pracovní autonomní asistent inspirovaný konceptem **AI zaměstnance**, postavený na **Google Antigravity CLI (`agy`)** v headless režimu a připravený k nepřetržitému provozu 24/7 v Docker kontejneru na VPS.

Projekt je navržený jako **single-user**: 1 kontejner = 1 uživatel. Pro dalšího člověka stačí nasadit další kopii.

## Co umí

- **Vícejazyčné rozhraní (`en`, `cs`, `de`, `sk`, `es`, `pl`) s jazykovými soubory (`app/locales/*.json`):** výchozí jazyk je angličtina, přepnutí přes výběr v horní liště, Nastavení, příkaz `/lang <kód>` (Telegram i webový chat) nebo proměnnou prostředí `LANGUAGE` (včetně příslušných šablon `workspace_template*`). Přidání dalšího jazyka vyžaduje pouze přidání nového JSON souboru do `app/locales/`.
- **Telegram bot (`aiogram`) & lomítkové příkazy ve webovém chatu:** ovládání z mobilu i PC s whitelistem povolených Telegram ID, samoopravným pollingem (30s watchdog), podporou příloh (fotky, PDF, Excel, Word, audio) a sdílenými příkazy (`/auto`, `/pracovni` (`/work`), `/osobni` (`/personal`), `/kontext` (`/context`), `/model`, `/lang`, `/new`, `/uklid` (`/cleanup`), `/usage` / `/quota` se zobrazením živého 5hodinového a týdenního limitu Antigravity, `/memory`, `/heartbeat`, `/briefing`, `/status`) funkčními v Telegramu i přímo ve Web UI chatu.
- **Moderní Web UI & REST API (`FastAPI`):** Liquid Glass rozhraní na portu `8080` (tmavý i světlý režim, bez npm buildu) se záložkami **Chat**, **Paměť**, **Plánovač & Historie** a **Nastavení** (rozděleno na *Obecné & Telegram*, *Integrace & MCP*, *Automatizace & Zálohy* a *Účet & Zabezpečení*), živý interaktivní terminál `agy`, přehled 5h/týdenní kvóty, 1-klik aktualizace `agy` CLI i celého kontejneru (přes sidecar `updater`) a přihlášení heslem + volitelné **2FA (TOTP)**.
- **Rychlé odpovědi:** trvale spuštěný („teplý“) proces `agy --input-format stream-json --output-format stream-json`, přednačtení paměti při startu konverzace a automatický fallback na jednorázové `agy -p`.
- **Trvalá souborová paměť (`workspace/`):**
  - `AGENTS.md` – operační manuál a pravidla oddělení **pracovní** vs. **osobní** agendy
  - `SOUL.md` – osobnost a styl komunikace
  - `USER.md` – profil a preference uživatele
  - `MEMORY.md` + `memory/*.md` – dlouhodobá, tematická a denní paměť
  - `HEARTBEAT.md` – kontrolní seznam pro pravidelné probuzení na pozadí
- **Plánovač (`APScheduler`):**
  - **Systémové úlohy:** tichý *Heartbeat*, *Ranní briefing*, *Noční úklid paměti a reset konverzace* (výchozí `00:15`), *Úklid při nečinnosti* (`idle_reset`), automatické promazávání starých denních poznámek a volitelná *Denní záloha na GitHub*.
  - **Vlastní úlohy (`cron` i `once`):** tři režimy — **Poslat zprávu** (`message`, 0 tokenů AI), **Spustit AI asistenta** (`agent`) nebo **Spustit skript / příkaz** (`script`, 0 tokenů AI; spustí lokální příkaz bezpečně bez shellu ve `workspace/` se správným `HOME`, `PATH` a `TZ`, výstup uloží do historie běhů, při úspěchu běží tiše a při chybě pošle upozornění na Telegram). Vše je uloženo v SQLite a přežije restart i aktualizaci kontejneru bez potřeby cronu na hostiteli nebo `setsid`.
- **Integrace, pluginy a MCP servery:**
  - **Antigravity pluginy:** správa oficiálních i partnerských pluginů (Gmail, Google Kalendář, Google Disk, Docs, Sheets, Slides, Notion, n8n, ClickUp, Linear, Figma, Stripe aj.) s interaktivním OAuth přihlášením ve Web UI, podporou **více účtů na jeden plugin** oddělených podle oblasti (`Všechny` / `Pracovní` / `Osobní`), **3-stavovým oprávněním nástrojů** (*Povoleno* / *Vyžaduje potvrzení* / *Zakázáno*) a vlastními textovými pravidly.
  - **Vlastní MCP servery:** Streamable HTTP i stdio servery s úpravou ve Web UI, vlastními HTTP hlavičkami (např. `x-api-key`), 1-klik importem JSON konfigurace, živým testem připojení i načtením nástrojů a 3-stavovým řízením jejich oprávnění.
- **Zálohování a přenositelnost:** export/import paměti, skillů i plánovače do ZIP archivu a volitelná automatická denní sanitizovaná záloha celého agenta (bez hesel, klíčů a tokenů) do soukromého GitHub repozitáře.

## Struktura projektu

- `app/` – Python orchestrátor (`main.py`, `agy_runner.py`, `telegram_bot.py`, `chat_commands.py`, `web.py`, `scheduler.py`, `memory.py`, `db.py`, `config.py`, `i18n.py`, `runtime_config.py`, `user_env.py`, `plugins.py`, `github_backup.py`, `auth_session.py`, `security.py`, `attachments.py`, `version_check.py`)
- `app/locales/` – jazykové JSON soubory (`en.json`, `cs.json`, `de.json`, `sk.json`, `es.json`, `pl.json`)
- `app/static/` – Web UI (`index.html`, `styles.css`, `app.js`)
- `workspace_template/` – výchozí anglické šablony paměti a skillů (při prvním startu se zkopírují do `workspace/`)
- `workspace_template_{cs,de,sk,es,pl}/` – lokalizované šablony paměti a skillů podle `LANGUAGE`
- `Dockerfile`, `docker/`, `docker-compose.yml`, `docker-compose.build.yml`, `docker-entrypoint.sh`, `VERSION` – kontejnerizace pro VPS a 1-klik aktualizace
- `tests/` – automatické testy (`pytest`)

---

## Přepínání jazyka a přidání nového jazyka

- **Výchozí jazyk:** `en` (Angličtina).
- **Dostupné jazyky:** `en`, `cs`, `de`, `sk`, `es`, `pl`.
- **Ve Web UI:** vyberte jazyk v horní liště (před i po přihlášení) nebo v **Nastavení → Obecné & Telegram**.
- **V Telegramu nebo Web Chatu:** napište `/lang cs`, `/lang en`, `/lang de`, `/lang sk`, `/lang es` nebo `/lang pl`.
- **Při startu kontejneru:** přidejte proměnnou prostředí `LANGUAGE=cs` (nebo jiný podporovaný kód), čímž se automaticky nasadí příslušné šablony paměti i rozhraní.
- **Vlastní překlad / další jazyk:** zkopírujte `app/locales/en.json` např. na `app/locales/fr.json`, upravte hlavičku `_meta` (`code`, `name`, `short`, `date_locale`) a přeložte texty. Nový jazyk se sám objeví ve Web UI i v `/lang`.

---

## Nasazení na VPS přes Portainer (1 volume `/home/asistent`)

Všechna trvalá data (`workspace/`, `data/`, `.gemini/`) žijí v jediném adresáři **`/home/asistent`**.

### Krok 1: Portainer → Stacks → Add stack → Web editor
Nejjednodušší je použít předbuildovaný image z GitHub Container Registry (`ghcr.io/petrbla/agy-assistant:latest`), který automaticky staví GitHub Actions při každém pushi do `main`:

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
      LANGUAGE: "cs" # podporované: en, cs, de, sk, es, pl
      TZ: "Europe/Prague"
      TG_TOKEN: "xxx"
      TG_ID: "xxx"
      APP_UPDATE_URL: "http://updater:8080/v1/update?async=true"
      APP_UPDATE_TOKEN: "zmente-si-tajny-token"
    volumes:
      - /opt/docker/asistent/home:/home/asistent
    ports:
      - "8080:8080"
    mem_limit: 2g
    cpus: 1.5

  # Volitelný lehký sidecar pro 1-klik aktualizace z Web UI (bez vystavených portů)
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
      WATCHTOWER_HTTP_API_TOKEN: "zmente-si-tajny-token"
      WATCHTOWER_LABEL_ENABLE: "true"
      WATCHTOWER_SCOPE: "asistent"
      WATCHTOWER_CLEANUP: "true"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    mem_limit: 128m
    cpus: 0.25
```

> Díky doprovodnému kontejneru `updater` se při vydání nové verze zobrazí přímo ve Web UI tlačítko **Aktualizovat asistenta** (v horním upozornění i v **Nastavení**). Po kliknutí se stáhne nový image z GHCR a kontejner se sám restartuje bez nutnosti chodit do Portaineru. Hodnoty `APP_UPDATE_TOKEN` a `WATCHTOWER_HTTP_API_TOKEN` se musí shodovat — nastavte si vlastní tajný řetězec.

*(Alternativně si můžete image sestavit i ručně na serveru: `docker build -t asistent:latest https://github.com/petrbla/agy-assistant.git#main` a v YAML pak použít `image: asistent:latest`, `pull_policy: never` a bez služby `updater`.)*

### Krok 2: První spuštění
1. Otevřete `http://<IP-VPS>:8080` a nastavte si e-mail, uživatelské jméno a heslo.
2. V **Nastavení → Obecné & Telegram → Antigravity CLI & Přihlášení** klikněte na **Spustit OAuth přihlášení (agy)**, otevřete vypsanou URL a vložte ověřovací kód. *(Alternativně lze vložit vlastní Gemini API klíč.)*
3. V **Nastavení → Účet & Zabezpečení** si doporučeně zapněte **2FA**.

> Pro vystavení do internetu doporučujeme před kontejner reverzní proxy s HTTPS (Caddy, Nginx Proxy Manager, Traefik).

---

## Nasazení přes terminál (Docker Compose)

```bash
git clone https://github.com/petrbla/agy-assistant.git
cd agy-assistant
cp .env.example .env   # volitelně vyplňte LANGUAGE, TELEGRAM_BOT_TOKEN a TELEGRAM_ALLOWED_USERS
docker compose up -d
```

Chcete-li image sestavit lokálně ze zdrojových kódů místo stažení z GHCR:
```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
*(Pro jistotu nejnovější verze `agy` při lokálním buildu použijte `--no-cache`.)*

---

## Integrace, MCP servery a plánované skripty

1. **Integrace a pluginy (doporučeno):** v **Nastavení → Integrace & MCP** můžete připojit Google Workspace i další partnerské pluginy, spravovat u nich více účtů podle oblasti (`Všechny`, `Pracovní`, `Osobní`) a nastavit jemná oprávnění nástrojů.
2. **Vlastní MCP servery:** přidejte Streamable HTTP nebo stdio server v **Nastavení → Integrace & MCP** (případně vložte celý JSON blok tlačítkem **Vložit z JSON konfigurace** — URL, vlastní HTTP hlavičky i typ připojení se vyplní automaticky), ověřte připojení a nastavte oprávnění jednotlivých nástrojů. Volitelně lze před prvním spuštěním vyplnit `MCP_WORK_*` / `MCP_PERSONAL_*` (nebo `MCP_PRACE_*` / `MCP_OSOBNI_*`) v `.env`.
3. **Plánování lokálních skriptů (`script`):** v záložce **Plánovač & Historie** (nebo z chatu přes `schedule_request.json`) zvolte **Spustit skript (0 tok.)**, zadejte např. cron `0 * * * *` a příkaz `python3 /home/asistent/workspace/scripts/job.py`. Skript běží uvnitř kontejneru se správným `HOME`, `PATH` a `TZ`, ukládá výstup do historie běhů, při chybě upozorní na Telegram a díky SQLite přežije restart i aktualizaci kontejneru bez cronu na hostiteli.

Pravidla oddělení pracovních a osobních nástrojů jsou v `workspace_template_cs/AGENTS.md` (příp. `workspace_template_<lang>/AGENTS.md`). V Telegramu i webovém chatu se oblast přepíná příkazy `/pracovni` (`/work`), `/osobni` (`/personal`) nebo `/auto`.

## Vývoj a testy

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```
