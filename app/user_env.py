from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings, normalize_chat_channel
from app.db import AssistantDB, McpServerRecord, OwnerConfig
from app.memory import WorkspaceMemory
from app.runtime_config import sync_agy_settings_file

logger = logging.getLogger(__name__)

# Složky v konfiguračním adresáři, které obsahují cache/runtime data `agy` a nikdy neznamenají OAuth
_SKIP_GEMINI_DIRS = frozenset(
    {
        "antigravity",
        "brain",
        "builtin",
        "cache",
        "code_tracker",
        "conversations",
        "crashes",
        "extensions",
        "implicit",
        "log",
        "logs",
        "node_modules",
        "plugins",
        "tmp",
        ".git",
    }
)
_NON_AUTH_FILES = frozenset({"settings.json", "mcp_config.json", "installation_id", "cli.log"})
_NON_AUTH_SUFFIXES = frozenset({".log", ".db", ".db-shm", ".db-wal", ".lock", ".tmp"})
_JETSKI_AUTH_RE = re.compile(
    r'(?i)\b(?:refresh_token|access_token|oauth_token|id_token|api_key)\s*:\s*"[^"\s]{8,}"'
    r'|\b(?:user_)?email\s*:\s*"[^"\s@]+@[^"\s@]+\.[^"\s@]+"'
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


def sync_soul_assistant_name(
    workspace_dir: Path,
    name: str,
    *,
    overwrite_existing: bool = False,
) -> None:
    """Propíše explicitně zadané jméno asistenta do `SOUL.md`."""
    cleaned = name.strip()
    if not cleaned or cleaned.lower() in {"assistant", "asistent", "assistent", "asistente", "asystent", "jarvis"}:
        return
    soul_path = workspace_dir / "SOUL.md"
    if not soul_path.is_file():
        return
    content = soul_path.read_text(encoding="utf-8")
    updated = content.replace("{{JMENO}}", cleaned).replace("{{NAME}}", cleaned)
    pattern = re.compile(
        r"^(\s*(?:[-*]\s*)?\*\*(?:Name|Jméno|Meno|Nombre|Imię):\*\*)\s*(.+?)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(updated)
    if match:
        current_val = match.group(2).lower()
        if (
            overwrite_existing
            or "[todo" in current_val
            or "[doplnit" in current_val
            or "doptej se" in current_val
            or "ask the user" in current_val
        ):
            updated = (
                updated[: match.start()]
                + f"{match.group(1)} {cleaned}"
                + updated[match.end() :]
            )
    if updated != content:
        soul_path.write_text(updated, encoding="utf-8")


def ensure_workspace(settings: Settings, owner: OwnerConfig | None = None) -> Path:
    """Inicializuje workspace ze šablony podle jazyka, pokud soubory ještě neexistují."""
    ws = settings.workspace_dir
    ws.mkdir(parents=True, exist_ok=True)
    lang = owner.language if owner is not None else settings.language
    tpl_dir = settings.template_dir_for_language(lang)
    if tpl_dir.is_dir():
        _copy_missing_tree(tpl_dir, ws)

    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)

    name = (owner.assistant_name if owner else "") or settings.assistant_name
    sync_soul_assistant_name(ws, name, overwrite_existing=False)
    return ws


def reset_workspace(settings: Settings, owner: OwnerConfig | None = None) -> Path:
    """Obnoví soubory paměti a šablony do výchozího stavu podle aktivního jazyka."""
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

    lang = owner.language if owner is not None else settings.language
    tpl_dir = settings.template_dir_for_language(lang)
    if tpl_dir.is_dir():
        _copy_overwrite_tree(tpl_dir, ws)

    memory_dir.mkdir(parents=True, exist_ok=True)
    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)

    name = (owner.assistant_name if owner else "") or settings.assistant_name
    sync_soul_assistant_name(ws, name, overwrite_existing=False)
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


def _is_oauth_credential_file(path: Path) -> bool:
    """Pozitivně ověří, zda jde o reálný OAuth / přihlašovací soubor `agy`, nikoliv cache či runtime stav."""
    if path.is_symlink() or not path.is_file():
        return False
    name = path.name.lower()
    if name in _NON_AUTH_FILES or any(name.endswith(sfx) for sfx in _NON_AUTH_SUFFIXES):
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False

    if name == "google_accounts.json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        active = data.get("active")
        accounts = data.get("accounts")
        old = data.get("old")
        return bool(
            (isinstance(active, str) and active.strip())
            or (isinstance(accounts, list) and len(accounts) > 0)
            or (isinstance(old, list) and len(old) > 0)
        )

    if name == "jetski_state.pbtxt":
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                sample = fh.read(65536)
        except OSError:
            return False
        return bool(_JETSKI_AUTH_RE.search(sample))

    if name in {"auth.json", "session_auth.json"}:
        return True
    return any(tok in name for tok in ("oauth", "cred", "token"))


def _iter_oauth_paths(gemini_dir: Path) -> list[Path]:
    """Vrátí existující reálné OAuth / přihlašovací soubory v zadaném konfiguračním adresáři."""
    if not gemini_dir.is_dir():
        return []
    found: list[Path] = []
    stack = [gemini_dir]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for item in entries:
            if item.is_symlink():
                continue
            if item.is_dir():
                if item.name not in _SKIP_GEMINI_DIRS:
                    stack.append(item)
            elif _is_oauth_credential_file(item):
                found.append(item)
    return found


def _iter_all_oauth_paths(settings: Settings) -> list[Path]:
    """Najde všechny OAuth soubory `agy` napříč známými konfiguračními složkami HOME."""
    home = settings.effective_home_dir
    candidates = (
        home / ".gemini",
        home / ".config" / "antigravity-cli",
        home / ".local" / "share" / "antigravity-cli",
    )
    paths: list[Path] = []
    for base in candidates:
        paths.extend(_iter_oauth_paths(base))
    return paths


def has_agy_oauth(settings: Settings) -> bool:
    """Zjistí, zda je v HOME uloženo OAuth přihlášení `agy`."""
    return bool(_iter_all_oauth_paths(settings))


def clear_agy_oauth(settings: Settings) -> None:
    """Odstraní OAuth přihlášení `agy` (bez smazání MCP, settings, builtin a DB)."""
    for path in _iter_all_oauth_paths(settings):
        with contextlib.suppress(OSError):
            path.unlink()
    jetski = settings.effective_home_dir / ".gemini" / "antigravity-cli" / "jetski_state.pbtxt"
    if jetski.is_file() and not jetski.is_symlink():
        with contextlib.suppress(OSError):
            jetski.unlink()


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
