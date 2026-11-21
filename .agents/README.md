# `.agents` – Znalostní báze a customizace pro vývoj projektu `asistentantigravity`

Tento adresář obsahuje kompletní rešerši projektu, architektonickou dokumentaci, pravidla pro AI agenty a pracovní postupy pro vývoj repozitáře **Asistent Antigravity**.

> **Pozor na rozlišení dvou `.agents` složek v repozitáři:**
> - **Kořenové `.agents/`** (tato složka): Customizace a dokumentace pro **vývoj tohoto repozitáře**.
> - **`workspace_template/.agents/`**: Výchozí šablona skillů a MCP konfigurace pro **běžícího AI zaměstnance** uvnitř Docker kontejneru (při startu se přes `docker-entrypoint.sh` kopíruje do `/home/asistent/workspace/.agents/`).

## Struktura `.agents/`

### Pravidla (`rules/`)
- [`rules/project-context.md`](./rules/project-context.md) (`always_on`) – základní mapa projektu, klíčové soubory, konvence a kritická omezení.
- [`rules/backend-and-api.md`](./rules/backend-and-api.md) (`model_decision`) – konvence pro Python orchestrátor v `app/`, databázi, Web UI a testy.
- [`rules/docker-and-deployment.md`](./rules/docker-and-deployment.md) (`model_decision`) – pravidla pro `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, Portainer a `workspace_template/`.

### Kompletní dokumentace projektu (`docs/`)
- [`docs/architecture.md`](./docs/architecture.md) – celková architektura systému, datové toky, SQLite schéma, správa kontextů a bezpečnost.
- [`docs/modules-reference.md`](./docs/modules-reference.md) – detailní reference všech modulů v `app/`, jejich tříd, metod, Pydantic modelů a REST API endpointů.
- [`docs/configuration-and-deployment.md`](./docs/configuration-and-deployment.md) – proměnné prostředí (vč. aliasů), priority konfigurace, `runtime_config.json`, `mcp_config.json`, Docker/Portainer, CI/CD a `workspace_template/`.
- [`docs/testing-and-conventions.md`](./docs/testing-and-conventions.md) – přehled testovací sady v `tests/`, postupy spouštění, testovací vzory a vývojové konvence.

### Dovednosti (`skills/`)
- [`skills/project-research-and-checks/SKILL.md`](./skills/project-research-and-checks/SKILL.md) – kontrolní postup pro ověření testů a konzistence konfigurací napříč kódem, šablonami a Dockerem.

