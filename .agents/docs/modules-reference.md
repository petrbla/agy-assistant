# Reference modulů a REST API (`app/`)

## Moduly

### `app/config.py`
- `ContextMode = Literal["auto", "pracovni", "osobni"]`, `normalize_chat_channel()` (neznámé hodnoty → `auto`).
- `Settings` – viz `configuration-and-deployment.md`. Properties `effective_home_dir`, `db_path`, `runtime_config_path`, `allowed_telegram_user_ids`, `notify_chat_id`, metoda `subprocess_env()`.

### `app/db.py`
- Dataclassy `OwnerConfig`, `McpServerRecord`, `RunRecord`, `ActivityStats`, `ScheduledTaskRecord`.
- `AssistantDB`:
  - vlastník: `ensure_owner()`, `get_owner()`, `update_owner(**kw)`, `match_owner_login()`, `match_owner_token()`
  - přihlášení: `create_auth_session()`, `verify_auth_session()`, `revoke_auth_session()`, `revoke_all_auth_sessions()`
  - MCP: `list_mcp_servers()`, `upsert_mcp_server()`, `delete_mcp_server()`
  - úlohy: `list_scheduled_tasks()`, `get_scheduled_task()`, `upsert_scheduled_task()`, `update_scheduled_task()`, `delete_scheduled_task()`, `delete_scheduled_task_by_name()`, `cleanup_expired_once_tasks()`
  - relace: `get_session()`, `set_conversation_id()`, `set_context_mode()`, `reset_conversation()`, `reset_all_sessions()`, `list_active_sessions()`, `get_conversation_stats()`
  - historie: `log_run()`, `list_runs()`, `get_activity_stats(days=None)`
  - migrace: `_migrate_from_legacy_multiuser()`, `_normalize_legacy_context_modes()`

### `app/user_env.py`
- `ensure_workspace()`, `reset_workspace()`, `get_memory()`
- `build_mcp_config_dict()`, `sync_mcp_config()`, `import_global_mcp_if_needed()`, `prepare_agy_home()`
- `has_agy_oauth()`, `clear_agy_oauth()`, `migrate_legacy_home_if_needed()`

### `app/agy_runner.py`
- `CONTEXT_HINTS` (klíče `pracovni`, `osobni`), `build_startup_memory_block()`, `parse_agy_details()`, `parse_models_output()`, `extract_token_usage()`.
- `_WarmAgyProcess` – teplý proces `stream-json` (vstup `{"event":"user","message":{"content":...}}`, čtení `init` / `step_update` / `result`).
- `AgyRunner`: `run()`, `build_command()`, `build_stream_command()`, `format_prompt()`, `list_models()`, `check_version()`, `check_latest_version()`, `get_version_info()`, `update_cli()`, `close_session()`, `close_all()`.

### `app/scheduler.py`
- `HEARTBEAT_PROMPT`, `MORNING_BRIEFING_PROMPT`, `NIGHTLY_MEMORY_FLUSH_PROMPT`, `is_heartbeat_ok()`.
- `AssistantScheduler`: `start()`, `sync_jobs()`, `shutdown()`, `list_jobs()`, `run_heartbeat()`, `run_morning_briefing()`, `run_nightly_reset()`, `run_custom_task()`.

### `app/telegram_bot.py`
- `markdown_to_telegram_html()`, `split_telegram_message()`, `format_elapsed_seconds()`, `split_model_effort()`, `build_model_selection_keyboard()`.
- `TelegramAssistantBot` – handlery příkazů, příloh a textu, deduplikace zpráv, `_polling_loop()`.
- `TelegramBotController` – `reload()`, `send_notification()`, `stop()` a 30s watchdog.

### Další
- `app/auth_session.py` – `AgyAuthManager` (interaktivní OAuth `agy` v PTY, jen Linux).
- `app/security.py` – hesla (PBKDF2), TOTP, záložní kódy, podepsané 2FA výzvy.
- `app/memory.py` – `WorkspaceMemory` (bezpečné čtení/zápis paměti).
- `app/attachments.py` – ukládání a extrakce textu z příloh.
- `app/runtime_config.py` – `runtime_config.json` a synchronizace `settings.json`.

## REST API (`app/web.py`)

| Metoda | Cesta | Auth | Popis |
| :--- | :--- | :--- | :--- |
| `GET` | `/`, `/api/health` | Ne | Web UI, healthcheck |
| `GET` | `/api/auth/state` | Ne | Stav prvního nastavení |
| `POST` | `/api/auth/initial-setup` | Ne (1×) | Nastavení e-mailu, jména a hesla |
| `POST` | `/api/login`, `/api/login/verify-2fa` | Ne | Přihlášení (heslo / token, 2FA) |
| `POST` | `/api/logout` | – | Zruší session token |
| `POST` | `/api/account/email`, `/api/account/password` | Ano | Účet |
| `POST` | `/api/2fa/setup`, `/api/2fa/enable`, `/api/2fa/disable` | Ano | 2FA |
| `GET` | `/api/status` | Ano | Stav `agy`, přihlášení, Telegramu, skillů a úloh |
| `GET`/`POST` | `/api/settings` | Ano | Nastavení vlastníka |
| `GET`/`POST`/`DELETE` | `/api/mcp`, `/api/mcp/{id}` | Ano | MCP servery |
| `POST`/`GET` | `/api/auth/start`, `/status`, `/input`, `/stop`, `/logout` | Ano | OAuth přihlášení `agy` |
| `GET`/`POST` | `/api/agy/version`, `/api/agy/update` | Ano | Verze a aktualizace `agy` |
| `GET` | `/api/models` | Ano | Dostupné modely |
| `POST` | `/api/chat`, `/api/chat/reset` | Ano | Chat (`web:auto` / `web:pracovni` / `web:osobni`) |
| `GET`/`PUT` | `/api/files`, `/api/files/{path}` | Ano | Paměť |
| `POST` | `/api/workspace/reset` | Ano | Obnova šablon |
| `GET` | `/api/runs`, `/api/usage` | Ano | Historie a spotřeba tokenů |
| `POST` | `/api/heartbeat/run`, `/api/briefing/run`, `/api/nightly-reset/run` | Ano | Ruční spuštění |
| `GET`/`POST`/`PATCH`/`DELETE` | `/api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/run` | Ano | Vlastní úlohy |

Web UI ukládá session token do `localStorage` pod klíčem `agy_web_token`.
