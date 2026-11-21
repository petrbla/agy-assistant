# Testování, konvence a technické poznámky

## Testovací sada (`tests/`)

`pytest.ini`: `testpaths = tests`, `asyncio_mode = auto`.

```bash
.venv/Scripts/python -m pytest      # Windows
.venv/bin/python -m pytest          # Linux
```

Testy pracují výhradně v `tmp_path` a mockují `asyncio.create_subprocess_exec` nebo `AgyRunner`, takže nevolají skutečné `agy` ani Telegram.

### Přehled
1. `test_agy_runner.py` – parsování výstupu `agy`, sestavení příkazů a promptu, ukládání `conversation_id` a tokenů, přednačtení paměti, teplý proces `stream-json` a fallback na one-shot.
2. `test_attachments.py` – sanitizace názvů a extrakce textu z příloh, zpracování příloh v Telegram botu.
3. `test_memory.py` – čtení/zápis paměti a ochrana proti path traversal.
4. `test_runtime_and_auth.py` – ENV aliasy, `runtime_config.json`, PTY výstup, `/api/settings` a `/api/auth/start`.
5. `test_scheduler.py` – Heartbeat, noční úklid a reset, registrace úloh, mazání proběhlých jednorázových úloh.
6. `test_singleuser_and_migration.py` – migrace ze starší multi-user DB a legacy OAuth, normalizace kontextů, heslo + 2FA přihlášení.
7. `test_telegram_formatting.py` – Markdown → Telegram HTML, dlouhé zprávy, průběžná stavová zpráva.
8. `test_telegram_usage.py` – `/usage`, `/uklid`, formát času, deduplikace zpráv.
9. `test_web.py` – veřejné vs. chráněné endpointy, chat a soubory.

## Konvence

- `from __future__ import annotations` a typování Python 3.12.
- Čeština v dokumentaci, komentářích, logách, UI i commit zprávách (`novinka:`, `oprava:`, `přepracování:` …).
- Na Windows neexistuje POSIX `pty` (`AgyAuthManager` vrací chybu); výchozí cesty v `Settings` míří na Linux kontejner, v testech se používá `tmp_path`.
- `.gitattributes` vynucuje `eol=lf` pro `*.sh`; `Dockerfile` navíc odstraňuje `\r`.
- Gitem ignorováno: `.env`, `.env.*`, `*.key`, `*.pem`, `secrets/`, `data/`, `workspace/`, `.venv/`.
