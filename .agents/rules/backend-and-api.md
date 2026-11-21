---
trigger: model_decision
description: >-
  Pravidla a konvence pro vývoj Python backendu v app/, REST API, Web UI
  (app/static/), SQLite databáze a testů v tests/. Aktivuj při úpravách
  Python kódu, API endpointů, UI nebo testů.
---

# Pravidla pro Backend, API, Web UI a Testy

1. **Asynchronní model a serializace `agy`:**
   - Všechny vstupy (Telegram, Web UI `/api/chat`, plánovač) volají stejný `AgyRunner.run(...)`, který drží jeden `asyncio.Lock()`.
   - Příkazy sestavuj jako seznam argumentů bez shellu (`create_subprocess_exec`).
   - Výsledek (úspěch, chyba, timeout, `FileNotFoundError`) vždy loguj přes `AssistantDB.log_run()`.
   - Při změně modelu, Gemini klíče, MCP serverů nebo resetu konverzace ukonči teplé procesy (`close_session()` / `close_all()`).

2. **Kontexty a session klíče:**
   - Platné kontexty: `ContextMode = Literal["auto", "pracovni", "osobni"]` (`app/config.py`).
   - `session_key`: `tg:<chat_id>:<kanál>` (Telegram), `web:<kanál>` (Web UI), `cron:heartbeat`, `cron:briefing`, `task:<id>`.
   - Plánované úlohy volají `runner.run(..., use_conversation=False)`; noční úklid jako jediný pracuje s aktivními konverzacemi.

3. **Zabezpečení Web API a paměti:**
   - Každý nový endpoint v `app/web.py` (kromě `/`, `/api/health`, `/api/auth/state`, `/api/auth/initial-setup`, `/api/login*`, `/api/logout`) musí mít `Depends(require_auth)`.
   - Práce se soubory v `workspace/` musí jít přes `WorkspaceMemory`.
   - Do `/api/settings` ani `/api/status` nevracej surové tajné hodnoty – jen booleany `has_*`.

4. **Testování:**
   - Používej `tmp_path` a mockuj `asyncio.create_subprocess_exec` nebo `AgyRunner`.
   - `async def test_*` fungují bez dekorátoru (`asyncio_mode = auto`).
   - V testech používej jen smyšlená data (žádné skutečné firmy, jména ani tokeny).
