from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.db import AssistantDB, OwnerConfig
from app.memory import WorkspaceMemory
from app.plugins import (
    detect_installed_plugins_from_disk,
    normalize_plugin_permissions,
    parse_tool_permissions_raw,
)

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_BACKUP_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class GitHubBackupResult:
    """Výsledek jedné zálohy agenta do GitHub repozitáře."""

    success: bool
    changed: bool
    commit_sha: str
    file_count: int
    repo_url: str
    branch: str
    message: str
    ran_at: str


def normalize_github_repo_url(raw: str) -> str:
    """Ověří a normalizuje GitHub repozitář (`owner/repo` nebo HTTPS URL) na kanonickou URL."""
    cleaned = (raw or "").strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned) or cleaned.startswith("-"):
        raise ValueError("Neplatný název nebo URL GitHub repozitáře.")

    owner = ""
    repo = ""
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
            raise ValueError("Podporovány jsou pouze HTTPS adresy na github.com (nebo formát vlastnik/repo).")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("URL repozitáře nesmí obsahovat přihlašovací údaje ani query parametry.")
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) != 2:
            raise ValueError("Adresa repozitáře musí mít tvar https://github.com/vlastnik/repo.")
        owner, repo = parts
    else:
        parts = cleaned.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("Zadejte repozitář ve tvaru vlastnik/repo nebo https://github.com/vlastnik/repo.")
        owner, repo = parts

    if repo.endswith(".git"):
        repo = repo[:-4]
    if (
        not _SEGMENT_RE.fullmatch(owner)
        or not _SEGMENT_RE.fullmatch(repo)
        or ".." in owner
        or ".." in repo
    ):
        raise ValueError("Neplatný název vlastníka nebo repozitáře na GitHubu.")
    return f"https://github.com/{owner}/{repo}.git"


def validate_github_branch(raw: str | None) -> str:
    """Ověří bezpečný název Git větve (výchozí `main`)."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return "main"
    if (
        not _BRANCH_RE.fullmatch(cleaned)
        or ".." in cleaned
        or "//" in cleaned
        or cleaned.endswith("/")
        or cleaned.endswith(".lock")
    ):
        raise ValueError(f"Neplatný název větve: {raw!r}")
    return cleaned


def _redact_secret(text: str, token: str) -> str:
    if not text or not token:
        return text
    cleaned = text.replace(token, "***")
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return cleaned.replace(basic, "***")


def _safe_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def collect_agent_backup_files(
    settings: Any,
    db: AssistantDB,
    owner: OwnerConfig,
    *,
    app_version: str = "",
) -> dict[str, str]:
    """Poskládá kompletní sanitizovanou zálohu workspace, skillů a konfigurace agenta (bez hesel a tokenů)."""
    files: dict[str, str] = {}
    tz = _safe_zone(owner.tz or settings.tz)
    now_iso = datetime.now(tz=tz).isoformat()

    mem = WorkspaceMemory(settings.workspace_dir, timezone_name=str(tz))
    for path in mem.list_all_exportable_files():
        rel = path.relative_to(mem.workspace_dir).as_posix()
        try:
            files[f"workspace/{rel}"] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

    agent_cfg = {
        "assistant_name": owner.assistant_name,
        "display_name": owner.display_name,
        "language": owner.language,
        "tz": owner.tz,
        "default_context_mode": owner.default_context_mode,
        "agy_model": owner.agy_model,
        "heartbeat_enabled": owner.heartbeat_enabled,
        "heartbeat_interval_minutes": owner.heartbeat_interval_minutes,
        "morning_briefing_enabled": owner.morning_briefing_enabled,
        "morning_briefing_cron": owner.morning_briefing_cron,
        "nightly_reset_enabled": owner.nightly_reset_enabled,
        "nightly_reset_cron": owner.nightly_reset_cron,
        "github_backup_enabled": owner.github_backup_enabled,
        "github_backup_repo": owner.github_backup_repo,
        "github_backup_branch": owner.github_backup_branch,
        "github_backup_cron": owner.github_backup_cron,
    }
    files["config/agent_config.json"] = json.dumps(agent_cfg, indent=2, ensure_ascii=False) + "\n"

    tasks_out = [
        {
            "name": task.name,
            "prompt": task.prompt,
            "schedule_type": task.schedule_type,
            "cron_expr": task.cron_expr,
            "run_at": task.run_at,
            "action_type": task.action_type,
            "enabled": task.enabled,
        }
        for task in db.list_scheduled_tasks()
    ]
    files["config/scheduled_tasks.json"] = json.dumps(tasks_out, indent=2, ensure_ascii=False) + "\n"

    mcp_out: list[dict[str, Any]] = []
    for srv in db.list_mcp_servers():
        try:
            args = json.loads(srv.args_json)
            if not isinstance(args, list):
                args = []
        except json.JSONDecodeError:
            args = []
        mcp_out.append(
            {
                "name": srv.name,
                "scope": srv.scope,
                "transport": srv.transport,
                "url": srv.url,
                "command": srv.command,
                "args": args,
                "enabled": srv.enabled,
                "has_auth_token": bool(srv.auth_token.strip()),
                "tool_permissions": parse_tool_permissions_raw(srv.tool_permissions_json),
            }
        )
    files["config/mcp_servers.json"] = json.dumps(mcp_out, indent=2, ensure_ascii=False) + "\n"

    installed = detect_installed_plugins_from_disk(settings.effective_home_dir)
    accounts_out = [
        {
            "plugin_name": acct.plugin_name,
            "label": acct.label,
            "scope": acct.scope,
            "profile_key": acct.profile_key,
            "account_email": acct.account_email,
            "enabled": acct.enabled,
            "connected": acct.connected,
            "permissions": normalize_plugin_permissions(acct.plugin_name, acct.permissions_json),
            "tool_permissions": parse_tool_permissions_raw(acct.tool_permissions_json),
            "custom_rules": acct.custom_rules,
        }
        for acct in db.list_plugin_accounts()
    ]
    plugins_payload = {
        "installed": [
            {
                "name": info["name"],
                "display_name": info["display_name"],
                "version": info["version"],
                "enabled": info["enabled"],
                "mcp_servers": info.get("mcp_servers") or [],
            }
            for info in installed.values()
        ],
        "accounts": accounts_out,
    }
    files["config/plugins.json"] = json.dumps(plugins_payload, indent=2, ensure_ascii=False) + "\n"

    manifest = {
        "format": "agy-assistant-github-backup-v1",
        "backed_up_at": now_iso,
        "app_version": app_version,
        "assistant_name": owner.assistant_name,
        "file_count": len(files) + 2,
    }
    files["backup_manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    files["README.md"] = (
        f"# Záloha AI asistenta ({owner.assistant_name})\n\n"
        "Tento repozitář obsahuje automatickou sanitizovanou zálohu vlastního AI asistenta:\n"
        "- `workspace/` – kompletní paměť (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `HEARTBEAT.md`, `memory/`) a vlastní skilly (`.agents/skills/`)\n"
        "- `config/` – nastavení asistenta, plánované úlohy, definice MCP serverů a oprávnění pluginů\n"
        "- `backup_manifest.json` – metadata poslední zálohy\n\n"
        "> **Bezpečnostní upozornění:** Hesla, 2FA klíče, Gemini API klíč, Telegram Bot Token, "
        "MCP Bearer tokeny, GitHub PAT ani OAuth tokeny se do zálohy z bezpečnostních důvodů nikdy neukládají.\n"
    )
    return files


async def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str],
    token: str = "",
    timeout: float = 90.0,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    out = _redact_secret(stdout_b.decode("utf-8", errors="replace").strip(), token)
    return int(proc.returncode or 0), out


def _build_git_env(repo_url: str, token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    if repo_url.startswith("https://github.com/") and token:
        basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def _is_auth_or_missing_repo_error(output: str) -> bool:
    low = (output or "").lower()
    return any(
        tok in low
        for tok in (
            "authentication failed",
            "could not read username",
            "repository not found",
            "requested url returned error: 403",
            "requested url returned error: 401",
            "the requested url returned error: 404",
            "permission denied",
        )
    )


async def run_github_backup(
    settings: Any,
    db: AssistantDB,
    *,
    app_version: str = "",
) -> GitHubBackupResult:
    """Provede kompletní sanitizovanou zálohu asistenta do nastaveného GitHub repozitáře."""
    async with _BACKUP_LOCK:
        owner = db.get_owner()
        if owner is None:
            owner = db.ensure_owner(settings)
        tz = _safe_zone(owner.tz or settings.tz)
        ran_at = datetime.now(tz=tz).isoformat()

        def _fail(msg: str, repo_url: str = "", branch: str = "main", file_count: int = 0) -> GitHubBackupResult:
            safe_msg = _redact_secret(msg, owner.github_backup_token)[:400]
            db.update_owner(
                github_backup_last_run_at=ran_at,
                github_backup_last_status="error",
                github_backup_last_message=safe_msg,
            )
            return GitHubBackupResult(
                success=False,
                changed=False,
                commit_sha="",
                file_count=file_count,
                repo_url=repo_url,
                branch=branch,
                message=safe_msg,
                ran_at=ran_at,
            )

        raw_repo = owner.github_backup_repo.strip()
        token = owner.github_backup_token.strip()
        if not raw_repo:
            return _fail("Není nastaven cílový GitHub repozitář.")
        if not token:
            return _fail("Není nastaven GitHub přístupový token (PAT).")

        try:
            repo_url = normalize_github_repo_url(raw_repo)
            branch = validate_github_branch(owner.github_backup_branch)
        except ValueError as exc:
            return _fail(str(exc))

        backup_files = collect_agent_backup_files(settings, db, owner, app_version=app_version)
        env = _build_git_env(repo_url, token)

        try:
            with tempfile.TemporaryDirectory(prefix="agy-gh-backup-") as tmp_dir:
                repo_dir = Path(tmp_dir) / "repo"
                rc, clone_out = await _run_git(
                    ["clone", "--depth", "1", "--branch", branch, repo_url, str(repo_dir)],
                    env=env,
                    token=token,
                )
                if rc != 0:
                    if _is_auth_or_missing_repo_error(clone_out):
                        return _fail(
                            f"GitHub odmítl přístup k repozitáři: {clone_out}",
                            repo_url=repo_url,
                            branch=branch,
                            file_count=len(backup_files),
                        )
                    if repo_dir.exists():
                        shutil.rmtree(repo_dir, ignore_errors=True)
                    repo_dir.mkdir(parents=True, exist_ok=True)
                    rc_init, init_out = await _run_git(
                        ["init", "-b", branch], cwd=repo_dir, env=env, token=token
                    )
                    if rc_init != 0:
                        return _fail(
                            f"Inicializace Git repozitáře selhala: {init_out}",
                            repo_url=repo_url,
                            branch=branch,
                            file_count=len(backup_files),
                        )
                    await _run_git(
                        ["remote", "add", "origin", repo_url],
                        cwd=repo_dir,
                        env=env,
                        token=token,
                    )

                # Vymaž staré soubory mimo .git/ a zapiš čerstvou zálohu
                for item in list(repo_dir.iterdir()):
                    if item.name == ".git":
                        continue
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink(missing_ok=True)

                for rel_path, content in backup_files.items():
                    dst = (repo_dir / rel_path).resolve()
                    if not dst.is_relative_to(repo_dir.resolve()):
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(content, encoding="utf-8")

                await _run_git(
                    ["config", "user.name", owner.assistant_name or "Assistant Backup"],
                    cwd=repo_dir,
                    env=env,
                    token=token,
                )
                await _run_git(
                    ["config", "user.email", "backup@asistentantigravity.local"],
                    cwd=repo_dir,
                    env=env,
                    token=token,
                )

                rc_add, add_out = await _run_git(["add", "-A"], cwd=repo_dir, env=env, token=token)
                if rc_add != 0:
                    return _fail(
                        f"Chyba při přípravě změn (git add): {add_out}",
                        repo_url=repo_url,
                        branch=branch,
                        file_count=len(backup_files),
                    )

                rc_head, _ = await _run_git(
                    ["rev-parse", "--verify", "HEAD"], cwd=repo_dir, env=env, token=token
                )
                status_args = (
                    ["status", "--porcelain", "--", ".", ":(exclude)backup_manifest.json"]
                    if rc_head == 0
                    else ["status", "--porcelain"]
                )
                rc_st, st_out = await _run_git(
                    status_args, cwd=repo_dir, env=env, token=token
                )
                if rc_st != 0:
                    return _fail(
                        f"Chyba při kontrole změn (git status): {st_out}",
                        repo_url=repo_url,
                        branch=branch,
                        file_count=len(backup_files),
                    )

                if not st_out.strip():
                    msg = "Žádné změny od poslední zálohy."
                    db.update_owner(
                        github_backup_last_run_at=ran_at,
                        github_backup_last_status="no_changes",
                        github_backup_last_message=msg,
                    )
                    return GitHubBackupResult(
                        success=True,
                        changed=False,
                        commit_sha="",
                        file_count=len(backup_files),
                        repo_url=repo_url,
                        branch=branch,
                        message=msg,
                        ran_at=ran_at,
                    )

                stamp = datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")
                rc_cm, cm_out = await _run_git(
                    ["commit", "-m", f"záloha: denní záloha agenta {stamp}"],
                    cwd=repo_dir,
                    env=env,
                    token=token,
                )
                if rc_cm != 0:
                    return _fail(
                        f"Commit zálohy selhal: {cm_out}",
                        repo_url=repo_url,
                        branch=branch,
                        file_count=len(backup_files),
                    )

                rc_push, push_out = await _run_git(
                    ["push", "-u", "origin", f"HEAD:{branch}"],
                    cwd=repo_dir,
                    env=env,
                    token=token,
                )
                if rc_push != 0:
                    return _fail(
                        f"Odeslání zálohy na GitHub selhalo: {push_out}",
                        repo_url=repo_url,
                        branch=branch,
                        file_count=len(backup_files),
                    )

                _, sha_out = await _run_git(
                    ["rev-parse", "--short", "HEAD"], cwd=repo_dir, env=env, token=token
                )
                commit_sha = sha_out.strip().splitlines()[0] if sha_out.strip() else ""
                ok_msg = f"Záloha odeslána ({len(backup_files)} souborů, commit {commit_sha})."
                db.update_owner(
                    github_backup_last_run_at=ran_at,
                    github_backup_last_status="ok",
                    github_backup_last_message=ok_msg,
                )
                return GitHubBackupResult(
                    success=True,
                    changed=True,
                    commit_sha=commit_sha,
                    file_count=len(backup_files),
                    repo_url=repo_url,
                    branch=branch,
                    message=ok_msg,
                    ran_at=ran_at,
                )
        except asyncio.TimeoutError:
            return _fail(
                "Záloha na GitHub vypršela (timeout při komunikaci s GitHubem).",
                repo_url=repo_url,
                branch=branch,
                file_count=len(backup_files),
            )
        except Exception as exc:
            logger.exception("Neočekávaná chyba při záloze na GitHub: %s", exc)
            return _fail(
                f"Neočekávaná chyba při záloze na GitHub: {exc}",
                repo_url=repo_url,
                branch=branch,
                file_count=len(backup_files),
            )
