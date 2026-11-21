# Checklist oprav a vylepšení (Audit kódu)

Výstup read-only auditu repozitáře **Asistent Antigravity** (`asistentantigravity`).

---

## 1. Bezpečnost (vysoká priorita)

- [ ] **Rate limiting pro přihlášení, 2FA a ověření pozvánek** (`app/web.py`, ř. 492–570 a 1201–1216)
  - Omezit počet neúspěšných pokusů (např. max 5–10 pokusů / minutu na IP + účet) na endpointech `/api/login`, `/api/login/verify-2fa` a `/api/invites/check`.
- [ ] **Omezení `stdio` MCP serverů jen pro admina** (`app/web.py`, ř. 801–835)
  - Běžným uživatelům (`role="user"`) povolit pouze `http` MCP servery, případně přidat volitelný přepínač v `app/config.py`.
- [ ] **Limit rozbalené velikosti XML u příloh `.xlsx` / `.docx` / `.odt`** (`app/attachments.py`, ř. 97–200)
  - Před `zf.read(...)` ověřit `zf.getinfo(...).file_size <= 20 * 1024 * 1024`, aby obří příloha nevyčerpala paměť kontejneru (`mem_limit: 2g`).
- [ ] **Pojistka oprávnění při ručním ukončení PTY přihlášení** (`app/web.py`, ř. 898–905)
  - V `POST /api/auth/stop` pojistně zavolat `await ensure_os_user(settings, current_user.id)`.

---

## 2. Opravy drobností a výkon (střední priorita)

- [ ] **Smazat duplicitní funkci `applyEffortVisual`** (`app/static/index.html`, ř. 1746–1764)
  - Odstranit druhou identickou definici funkce v JavaScriptu.
- [ ] **Sjednotit výchozí hodnotu `AGY_DANGEROUSLY_SKIP_PERMISSIONS`** (`.env.example`, ř. 13 vs. `docker-compose.yml`, ř. 24)
  - Sladit výchozí hodnotu a doplnit jasnou poznámku v `.env.example` i `README.md`.
- [ ] **Přidat krátkou cache do `AgyRunner.check_version()`** (`app/agy_runner.py`, ř. 533–546)
  - Cachovat výstup `agy --version` (např. na 60 s) a smazat cache při `update_cli()`, aby `/api/status` nespouštěl podproces při každém načtení stránky.
- [ ] **Automatický úklid expirovaných `auth_sessions` v SQLite** (`app/db.py`, `app/main.py`)
  - Přidat metodu `cleanup_expired_auth_sessions()` a volat ji při startu aplikace (příp. periodicky v plánovači).

---

## 3. Provoz, CI a dokumentace (nízká priorita)

- [ ] **Přidat `workflow_dispatch:` do GitHub Actions** (`.github/workflows/docker-image.yml`, ř. 3–6)
  - Umožní spustit rebuild Docker image s novou verzí `agy` jedním kliknutím na GitHubu bez nutnosti dělat nový commit.
- [ ] **Přidat jednoduchý CLI skript pro reset hesla** (např. `app/reset_password.py`)
  - Pro pohodlné nastavení nového hesla v Portainer Console bez psaní dlouhého jednořádkového Python příkazu.
- [ ] **Aktualizovat pravidla v `.agents/rules/backend-and-api.md`**
  - Opravit zastaralé zmínky o `Depends(verify_auth)` a `ContextMode` tak, aby odpovídaly aktuálnímu kódu (`get_current_user` / `require_admin` a 5 režimům kontextu).

---

## 4. Ověření po dokončení úprav

- [ ] **Doplnit unit testy** v `tests/` pro rate limiting, limit velikosti příloh a úklid `auth_sessions`.
- [ ] **Spustit testy / kontrolu syntaxe** (`python -m pytest -v` nebo `python -m py_compile`).
- [ ] **Commitnout změny** s českou commit zprávou.
