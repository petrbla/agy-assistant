---
trigger: model_decision
description: >-
  Pravidla a kritické detaily pro Docker build, docker-entrypoint.sh,
  docker-compose.yml, Portainer nasazení, volume /home/asistent a šablony v
  workspace_template/. Aktivuj při úpravách Dockeru, entrypointu, .env.example
  nebo šablon asistenta.
---

# Pravidla pro Docker, nasazení a `workspace_template/`

1. **Jediný persistentní volume (`/home/asistent`):**
   - Veškerá trvalá data (`workspace/`, `data/`, `.gemini/`, `secrets/`) leží v `/home/asistent`.
   - **Kritické:** Binárka `agy` se instaluje do `/opt/agy-install` a symlinkuje do `/usr/local/bin/agy`, **nikdy** do `/home/asistent`, jinak by ji překryl volume.

2. **Entrypoint a idempotence (`docker-entrypoint.sh`):**
   - Kontejner startuje jako `root`, vytvoří složky, nakopíruje `workspace_template/` přes `cp -rn` (bez přepisu dat), dosadí `{{JMENO}}` do `SOUL.md`, vytvoří výchozí `settings.json` a `mcp_config.json` (jen pokud neexistují), nastaví `chown -R asistent:asistent /home/asistent` a spustí aplikaci přes `exec gosu asistent "$@"`.
   - `Dockerfile` odstraňuje CRLF ze shell skriptů (vývoj probíhá i na Windows).

3. **Krátké proměnné pro Portainer:**
   - `app/config.py` i `docker-compose.yml` podporují aliasy `JMENO`, `TG_TOKEN`, `TG_ID`. Zachovej je.

4. **Šablony asistenta (`workspace_template/`):**
   - Oddělení **pracovní** vs. **osobní** agendy je popsané obecně v `workspace_template/AGENTS.md` a doplněné v `app/agy_runner.py` (`CONTEXT_HINTS`).
   - Výchozí MCP názvy v `docker-entrypoint.sh` (`n8n-prace`, `nocodb-prace`, `n8n-osobni`, `nocodb-osobni`, `google-osobni`) a proměnné `MCP_PRACE_*` / `MCP_OSOBNI_*` v `.env.example` a `docker-compose.yml` musí zůstat sladěné.
   - Šablony musí zůstat obecné – žádná jména, firmy ani osobní údaje.
