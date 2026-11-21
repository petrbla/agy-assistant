from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings, normalize_chat_channel
from app.db import AssistantDB, McpServerRecord, OwnerConfig
from app.memory import WorkspaceMemory
from app.runtime_config import sync_agy_settings_file

logger = logging.getLogger(__name__)

# Složky v ~/.gemini/, které patří Antigravity IDE/cache a nesmí se považovat za OAuth
_SKIP_GEMINI_DIRS = frozenset(
    {
        "antigravity",
        "brain",
        "code_tracker",
        "extensions",
        "plugins",
        "logs",
        "tmp",
        "cache",
        "node_modules",
        ".git",
    }
)


def get_memory(settings: Settings, owner: OwnerConfig | None = None) -> WorkspaceMemory:
    """Vrátí správce paměti pro pracovní prostor asistenta."""
    tz_name = owner.tz if owner is not None else settings.tz
    return WorkspaceMemory(settings.workspace_dir, timezone_name=tz_name)


def _copy_missing_tree(src_dir: Path, dst_dir: Path) -> None:
    """Rekurzivně zkopíruje soubory ze šablony bez přepsání existujících."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if item.is_dir():
            _copy_missing_tree(item, target)
        elif not target.exists():
            shutil.copy2(item, target)


def _copy_overwrite_tree(src_dir: Path, dst_dir: Path) -> None:
    """Rekurzivně zkopíruje soubory ze šablony a přepíše existující soubory."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if item.is_dir():
            _copy_overwrite_tree(item, target)
        else:
            try:
                shutil.copyfile(item, target)
            except PermissionError:
                target.unlink(missing_ok=True)
                shutil.copyfile(item, target)


def migrate_legacy_home_if_needed(settings: Settings) -> bool:
    """Jednorázově přenese OAuth soubory ze starší cesty `users/1/home/.gemini` do aktivního HOME."""
    target_home = settings.effective_home_dir
    legacy_gemini = settings.data_dir.parent / "users" / "1" / "home" / ".gemini"
    target_gemini = target_home / ".gemini"
    if not legacy_gemini.is_dir() or has_agy_oauth(settings):
        return False

    migrated = False
    for src_file in _iter_oauth_paths(legacy_gemini):
        rel = src_file.relative_to(legacy_gemini)
        dst_file = target_gemini / rel
        if dst_file.exists():
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_file, dst_file)
            migrated = True
        except OSError as exc:
            logger.warning("Migrace OAuth souboru %s selhala: %s", rel, exc)
    if migrated:
        logger.info("OAuth přihlášení úspěšně přeneseno z legacy users/1/home/.gemini do %s.", target_gemini)
    return migrated


def ensure_workspace(settings: Settings, owner: OwnerConfig | None = None) -> Path:
    """Inicializuje workspace ze šablony, pokud soubory ještě neexistují."""
    ws = settings.workspace_dir
    ws.mkdir(parents=True, exist_ok=True)
    if settings.template_dir.is_dir():
        _copy_missing_tree(settings.template_dir, ws)

    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)

    soul_path = ws / "SOUL.md"
    if soul_path.is_file():
        content = soul_path.read_text(encoding="utf-8")
        if "{{JMENO}}" in content:
            name = (owner.assistant_name if owner else "") or settings.assistant_name
            soul_path.write_text(content.replace("{{JMENO}}", name), encoding="utf-8")
    return ws


def reset_workspace(settings: Settings, owner: OwnerConfig | None = None) -> Path:
    """Obnoví soubory paměti a šablony do výchozího stavu z `workspace_template/`."""
    ws = settings.workspace_dir
    ws.mkdir(parents=True, exist_ok=True)

    memory_dir = ws / "memory"
    if memory_dir.is_dir():
        for note in memory_dir.glob("*.md"):
            try:
                note.unlink()
            except OSError:
                pass
    (ws / "schedule_request.json").unlink(missing_ok=True)

    if settings.template_dir.is_dir():
        _copy_overwrite_tree(settings.template_dir, ws)

    memory_dir.mkdir(parents=True, exist_ok=True)
    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)

    soul_path = ws / "SOUL.md"
    if soul_path.is_file():
        content = soul_path.read_text(encoding="utf-8")
        if "{{JMENO}}" in content:
            name = (owner.assistant_name if owner else "") or settings.assistant_name
            soul_path.write_text(content.replace("{{JMENO}}", name), encoding="utf-8")
    return ws


def _safe_json_loads(raw: str, default: Any) -> Any:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) else default
    except (json.JSONDecodeError, TypeError):
        return default


def _mcp_matches_channel(scope: str, channel: str) -> bool:
    if channel == "auto":
        return True
    return scope in ("all", channel)


def build_mcp_config_dict(
    servers: list[McpServerRecord], context_mode: str = "auto"
) -> dict[str, Any]:
    """Sestaví obsah souboru `mcp_config.json` ze seznamu MCP serverů pro daný chat."""
    channel = normalize_chat_channel(context_mode)
    mcp_servers: dict[str, Any] = {}
    for srv in servers:
        if not srv.enabled or not _mcp_matches_channel(srv.scope, channel):
            continue
        if srv.transport == "stdio" and srv.command.strip():
            entry: dict[str, Any] = {
                "command": srv.command.strip(),
                "args": _safe_json_loads(srv.args_json, []),
            }
            env = _safe_json_loads(srv.env_json, {})
            if env:
                entry["env"] = env
            mcp_servers[srv.name] = entry
        elif srv.url.strip():
            entry = {"url": srv.url.strip()}
            headers: dict[str, str] = {
                str(k): str(v) for k, v in _safe_json_loads(srv.headers_json, {}).items()
            }
            if srv.auth_token.strip():
                headers["Authorization"] = f"Bearer {srv.auth_token.strip()}"
            if headers:
                entry["headers"] = headers
            mcp_servers[srv.name] = entry
    return {"mcpServers": mcp_servers}


def sync_mcp_config(
    settings: Settings, db: AssistantDB, context_mode: str = "auto"
) -> Path:
    """Zapíše `mcp_config.json` do HOME složky asistenta."""
    mcp_file = settings.effective_home_dir / ".gemini" / "config" / "mcp_config.json"
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    payload = build_mcp_config_dict(db.list_mcp_servers(), context_mode=context_mode)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        mcp_file.write_text(serialized, encoding="utf-8")
    except PermissionError:
        mcp_file.unlink(missing_ok=True)
        mcp_file.write_text(serialized, encoding="utf-8")
    return mcp_file


def import_global_mcp_if_needed(
    db: AssistantDB, home_dir: Path | None = None
) -> int:
    """Načte existující `mcp_config.json` do DB (jednorázově při prázdné tabulce)."""
    if db.list_mcp_servers():
        return 0
    base_home = home_dir or Path.home()
    mcp_file = base_home / ".gemini" / "config" / "mcp_config.json"
    if not mcp_file.is_file():
        return 0
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return 0

    imported = 0
    for name, cfg in servers.items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        if isinstance(cfg.get("command"), str) and cfg["command"].strip():
            args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
            env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
            db.upsert_mcp_server(
                name=name,
                transport="stdio",
                command=cfg["command"],
                args_json=json.dumps(args, ensure_ascii=False),
                env_json=json.dumps(env, ensure_ascii=False),
                enabled=True,
            )
            imported += 1
        elif isinstance(cfg.get("url"), str) and cfg["url"].strip():
            raw_headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
            headers = {str(k): str(v) for k, v in raw_headers.items()}
            auth_token = ""
            auth_val = headers.pop("Authorization", "")
            if auth_val.lower().startswith("bearer "):
                auth_token = auth_val[7:].strip()
            elif auth_val:
                headers["Authorization"] = auth_val
            db.upsert_mcp_server(
                name=name,
                transport="http",
                url=cfg["url"],
                auth_token=auth_token,
                headers_json=json.dumps(headers, ensure_ascii=False),
                enabled=True,
            )
            imported += 1
    return imported


def _iter_oauth_paths(gemini_dir: Path) -> list[Path]:
    """Vrátí existující OAuth / přihlašovací soubory v `.gemini/` adresáři."""
    if not gemini_dir.is_dir():
        return []
    found: list[Path] = []
    for item in gemini_dir.iterdir():
        if item.name in _SKIP_GEMINI_DIRS:
            continue
        if item.name == "config":
            if item.is_dir():
                for sub in item.iterdir():
                    if sub.name != "mcp_config.json" and sub.is_file():
                        found.append(sub)
            continue
        if item.name == "antigravity-cli":
            if item.is_dir():
                for sub in item.iterdir():
                    if sub.name == "settings.json" or sub.name in _SKIP_GEMINI_DIRS:
                        continue
                    if sub.is_file():
                        found.append(sub)
                    elif sub.is_dir():
                        found.extend(p for p in sub.rglob("*") if p.is_file())
            continue
        if item.is_file():
            found.append(item)
        elif item.is_dir():
            for sub in item.rglob("*"):
                if sub.is_file() and not any(part in _SKIP_GEMINI_DIRS for part in sub.parts):
                    found.append(sub)
    return found


def has_agy_oauth(settings: Settings) -> bool:
    """Zjistí, zda je v HOME uloženo OAuth přihlášení `agy`."""
    return bool(_iter_oauth_paths(settings.effective_home_dir / ".gemini"))


def clear_agy_oauth(settings: Settings) -> None:
    """Odstraní OAuth přihlášení `agy` (bez smazání MCP a settings)."""
    for path in _iter_oauth_paths(settings.effective_home_dir / ".gemini"):
        try:
            path.unlink()
        except OSError:
            pass


def prepare_agy_home(
    settings: Settings,
    db: AssistantDB,
    context_mode: str = "auto",
) -> Path:
    """Připraví HOME adresář pro `agy` (settings.json, mcp_config.json a model)."""
    home_dir = settings.effective_home_dir
    owner = db.get_owner()
    model = owner.agy_model if owner is not None else settings.agy_model
    api_key = owner.gemini_api_key if owner is not None else settings.gemini_api_key

    home_dir.mkdir(parents=True, exist_ok=True)
    sync_agy_settings_file(
        settings,
        home_dir=home_dir,
        model=model,
        gemini_api_key=api_key,
    )
    sync_mcp_config(settings, db, context_mode=context_mode)
    return home_dir
