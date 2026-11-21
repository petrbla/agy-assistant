---
name: project-research-and-checks
description: >-
  Kontrola konzistence konfigurace, testů a šablon v repozitáři asistentantigravity.
  Použij po úpravách Python orchestrátoru (app/), proměnných prostředí,
  Docker souborů nebo workspace_template/.
---

# Kontrola projektu Asistent Antigravity

Tento skill slouží k rychlému ověření, že změny v kódu, konfiguraci a šablonách zůstávají navzájem konzistentní.

## Postup ověření

1. **Kontrola testů (pokud je dostupný Python s nainstalovaným `pytest`):**
   - Spusť `python -m pytest -v` (po předchozím vysvětlení uživateli dle globálního pravidla).
   - Pokud v aktuálním prostředí chybí závislosti, ověř alespoň syntaxi modifikovaných modulů a zkontroluj, zda nové chování má odpovídající unit test v `tests/`.

2. **Kontrola konzistence proměnných prostředí a MCP názvů:**
   - Při přidání nebo úpravě konfiguračního pole v `app/config.py` zkontroluj:
     - `.env.example`
     - `docker-compose.yml`
     - `docker-entrypoint.sh`
     - `app/runtime_config.py` (pokud má jít editovat přes Web UI)
     - `README.md`
   - Při změně MCP serverů ověř shodu výchozích názvů a oddělení oblastí v:
     - `app/agy_runner.py` (`CONTEXT_HINTS`)
     - `docker-entrypoint.sh`
     - `workspace_template/AGENTS.md`
     - `workspace_template/.agents/mcp_config.example.json`

3. **Kontrola bezpečnosti a entrypointu:**
   - Nové endpointy v `app/web.py` (kromě veřejných `/`, `/api/health`, `/api/auth/state`, `/api/auth/initial-setup`, `/api/login*`, `/api/logout`) musí chránit `Depends(require_auth)`.
   - Přístup k souborům v `workspace/` musí procházet přes `WorkspaceMemory._resolve_safe()`.
   - Pokud upravuješ `.agents/` pro vývoj, nepleť si kořenovou složku `.agents/` se šablonou `workspace_template/.agents/`.

4. **Aktualizace znalostí v `.agents/`:**
   - Pokud změna mění architekturu, API endpointy, konfigurační proměnné nebo strukturu modulů, aktualizuj příslušný dokument v `.agents/docs/` (případně `.agents/rules/`).

