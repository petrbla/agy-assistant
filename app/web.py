from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.agy_runner import AgyRunner, is_version_up_to_date
from app.auth_session import AgyAuthManager
from app.config import ContextMode, Settings, normalize_chat_channel, normalize_language
from app.db import AssistantDB, McpServerRecord, OwnerConfig
from app.i18n import get_ui_bundle, list_available_languages, t
from app.memory import WorkspaceMemory
from app.plugins import PluginManager
from app.runtime_config import save_runtime_overrides
from app.scheduler import AssistantScheduler
from app.version_check import AppVersionChecker
from app.security import (
    build_otpauth_uri,
    create_2fa_challenge,
    generate_recovery_codes,
    generate_totp_secret,
    hash_password,
    is_valid_email,
    load_or_create_signing_key,
    normalize_email,
    verify_2fa_challenge,
    verify_and_consume_recovery_code,
    verify_password,
    verify_totp_code,
)
from app.user_env import (
    clear_agy_oauth,
    ensure_workspace,
    get_memory,
    has_agy_oauth,
    prepare_agy_home,
    reset_workspace,
    sync_soul_assistant_name,
)

STATIC_DIR = Path(__file__).parent / "static"


def _static_asset_url(path: str) -> str:
    """Doplní query parametr verze a času úpravy pro cache-busting statických souborů."""
    rel = path.removeprefix("/static/").lstrip("/")
    asset_file = STATIC_DIR / rel
    try:
        mtime = int(asset_file.stat().st_mtime)
    except OSError:
        return f"{path}?v={__version__}"
    return f"{path}?v={__version__}-{mtime}"


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    session_key: str = Field(default="web:default", min_length=1, max_length=120)
    context_mode: ContextMode | None = None
    model: str | None = Field(default=None, max_length=120)


class ResetRequest(BaseModel):
    session_key: str = Field(default="web:default", min_length=1, max_length=120)
    context_mode: ContextMode | None = None


class FileUpdateRequest(BaseModel):
    content: str


class SettingsUpdateRequest(BaseModel):
    gemini_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_allowed_users: str | None = None
    telegram_notify_chat_id: str | None = None
    web_auth_token: str | None = None
    web_token: str | None = None
    assistant_name: str | None = None
    display_name: str | None = None
    language: str | None = Field(default=None, max_length=16)
    tz: str | None = None
    default_context_mode: ContextMode | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    morning_briefing_enabled: bool | None = None
    morning_briefing_cron: str | None = None
    nightly_reset_enabled: bool | None = None
    nightly_reset_cron: str | None = None
    agy_model: str | None = Field(default=None, max_length=120)


class McpServerUpsertRequest(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=80)
    scope: Literal["all", "pracovni", "osobni"] = "all"
    transport: Literal["http", "stdio"] = "http"
    url: str = Field(default="", max_length=1000)
    auth_token: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] | None = None
    command: str = Field(default="", max_length=300)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    enabled: bool = True


class AuthStartRequest(BaseModel):
    mode: Literal["auth_login", "interactive"] = "interactive"


class AuthInputRequest(BaseModel):
    value: str = Field(default="", max_length=2000)


class PluginInstallRequest(BaseModel):
    target: str = Field(min_length=1, max_length=300)


class PluginToggleRequest(BaseModel):
    enabled: bool


class PluginPermissionsRequest(BaseModel):
    permissions: dict[str, bool]


class PluginConnectStartRequest(BaseModel):
    install_target: str | None = Field(default=None, max_length=300)
    force_reconnect: bool = False
    account_id: int | None = None
    interactive_config: bool = False


class PluginAccountCreateRequest(BaseModel):
    label: str = Field(default="", max_length=80)
    scope: Literal["all", "pracovni", "osobni"] = "all"
    permissions: dict[str, bool] | None = None
    custom_rules: str = Field(default="", max_length=1000)


class PluginAccountUpdateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    scope: Literal["all", "pracovni", "osobni"] | None = None
    enabled: bool | None = None
    permissions: dict[str, bool] | None = None
    custom_rules: str | None = Field(default=None, max_length=1000)


class PluginConnectCallbackRequest(BaseModel):
    callback_url: str = Field(min_length=1, max_length=4000)


class PluginConnectInputRequest(BaseModel):
    value: str = Field(default="", max_length=2000)
    key: str = Field(default="", max_length=32)


class ScheduledTaskUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    schedule_type: Literal["cron", "once"] = "cron"
    cron_expr: str = Field(default="", max_length=120)
    run_at: str = Field(default="", max_length=64)
    action_type: Literal["message", "agent"] = "message"
    enabled: bool = True


class ScheduledTaskPatchRequest(BaseModel):
    enabled: bool


class LoginRequest(BaseModel):
    identifier: str = Field(default="", max_length=200)
    username: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)
    web_token: str = Field(default="", max_length=300)
    totp_code: str = Field(default="", max_length=32)


class InitialSetupRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    username: str = Field(default="admin", min_length=2, max_length=50, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(default="", max_length=100)
    language: str = Field(default="en", max_length=16)
    password: str = Field(min_length=8, max_length=200)


class AccountEmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)


class Login2FARequest(BaseModel):
    challenge_token: str = Field(min_length=10, max_length=500)
    code: str = Field(min_length=4, max_length=32)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(default="", max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class TwoFactorVerifyRequest(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    password: str = Field(default="", max_length=200)


def _owner_public_dict(owner: OwnerConfig, settings: Settings) -> dict[str, object]:
    has_oauth = has_agy_oauth(settings)
    has_key = bool(owner.gemini_api_key.strip())
    return {
        "username": owner.username,
        "email": owner.email,
        "display_name": owner.display_name,
        "assistant_name": owner.assistant_name,
        "language": normalize_language(owner.language),
        "tz": owner.tz,
        "default_context_mode": owner.default_context_mode,
        "has_telegram_bot_token": bool(owner.telegram_bot_token.strip()),
        "telegram_allowed_users": owner.telegram_allowed_users,
        "telegram_notify_chat_id": owner.telegram_notify_chat_id,
        "heartbeat_enabled": owner.heartbeat_enabled,
        "heartbeat_interval_minutes": owner.heartbeat_interval_minutes,
        "morning_briefing_enabled": owner.morning_briefing_enabled,
        "morning_briefing_cron": owner.morning_briefing_cron,
        "nightly_reset_enabled": owner.nightly_reset_enabled,
        "nightly_reset_cron": owner.nightly_reset_cron,
        "agy_model": owner.agy_model,
        "has_gemini_api_key": has_key,
        "has_agy_oauth": has_oauth,
        "agy_authenticated": has_key or has_oauth,
        "has_web_token": bool(owner.web_token.strip() or settings.web_auth_token.strip()),
        "has_web_auth_token": bool(owner.web_token.strip() or settings.web_auth_token.strip()),
        "has_password": bool(owner.password_hash.strip()),
        "totp_enabled": bool(owner.totp_enabled),
        "created_at": owner.created_at,
    }


def _mcp_public_dict(srv: McpServerRecord) -> dict[str, object]:
    try:
        headers = json.loads(srv.headers_json)
    except json.JSONDecodeError:
        headers = {}
    try:
        args = json.loads(srv.args_json)
    except json.JSONDecodeError:
        args = []
    try:
        env = json.loads(srv.env_json)
    except json.JSONDecodeError:
        env = {}
    return {
        "id": srv.id,
        "name": srv.name,
        "scope": srv.scope,
        "transport": srv.transport,
        "url": srv.url,
        "has_auth_token": bool(srv.auth_token.strip()),
        "headers": headers if isinstance(headers, dict) else {},
        "command": srv.command,
        "args": args if isinstance(args, list) else [],
        "env": env if isinstance(env, dict) else {},
        "enabled": srv.enabled,
        "created_at": srv.created_at,
    }


def create_app(
    settings: Settings,
    db: AssistantDB,
    memory: WorkspaceMemory,
    runner: AgyRunner,
    scheduler: AssistantScheduler | None = None,
    auth_manager: AgyAuthManager | None = None,
    plugin_manager: PluginManager | None = None,
    app_version_checker: AppVersionChecker | None = None,
    on_settings_updated: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Vytvoří čistou single-user FastAPI aplikaci pro Web UI a REST API."""
    app = FastAPI(title="Asistent Antigravity", version=__version__)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    db.ensure_owner(settings)
    if plugin_manager is None:
        plugin_manager = PluginManager(settings, db)
    if app_version_checker is None:
        app_version_checker = AppVersionChecker(settings)
    signing_key = load_or_create_signing_key(settings.data_dir)
    if (
        scheduler is not None
        and isinstance(runner, AgyRunner)
        and runner.on_schedule_updated is None
    ):
        runner.on_schedule_updated = scheduler.sync_jobs

    async def _notify_settings_updated() -> None:
        if on_settings_updated is not None:
            await on_settings_updated()

    def _sync_all_plugin_contexts() -> None:
        for ctx in ("auto", "pracovni", "osobni"):
            prepare_agy_home(settings, db, context_mode=ctx)

    def _extract_bearer(authorization: str | None, x_auth_token: str | None) -> str:
        bearer = ""
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
        return bearer or (x_auth_token or "").strip()

    def _get_owner() -> OwnerConfig:
        owner = db.get_owner()
        if owner is None:
            owner = db.ensure_owner(settings)
        return owner

    async def require_auth(
        authorization: str | None = Header(default=None),
        x_auth_token: str | None = Header(default=None),
    ) -> OwnerConfig:
        provided = _extract_bearer(authorization, x_auth_token)
        owner = _get_owner()

        if provided:
            if db.verify_auth_session(provided):
                return owner
            matched = db.match_owner_token(provided, settings.web_auth_token)
            if matched is not None:
                if matched.totp_enabled:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Účet má aktivní 2FA – přihlaste se přes formulář s 2FA kódem.",
                    )
                return matched
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Neplatný nebo vypršelý přístupový token.",
            )

        has_protection = bool(
            owner.password_hash.strip()
            or owner.web_token.strip()
            or settings.web_auth_token.strip()
        )
        if has_protection:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Vyžadováno přihlášení.",
            )
        return owner

    def _verify_second_factor(owner: OwnerConfig, code: str) -> bool:
        if verify_totp_code(owner.totp_secret, code):
            return True
        try:
            hashes = json.loads(owner.totp_recovery_hashes_json or "[]")
            if not isinstance(hashes, list):
                hashes = []
        except json.JSONDecodeError:
            hashes = []
        matched, remaining = verify_and_consume_recovery_code(code, [str(h) for h in hashes])
        if matched:
            db.update_owner(totp_recovery_hashes_json=json.dumps(remaining))
            return True
        return False

    @app.get("/", response_model=None)
    async def index() -> HTMLResponse | dict[str, str]:
        index_file = STATIC_DIR / "index.html"
        if index_file.is_file():
            html = index_file.read_text(encoding="utf-8")
            html = (
                html.replace(
                    'href="/static/favicon.svg"',
                    f'href="{_static_asset_url("/static/favicon.svg")}"',
                )
                .replace(
                    'href="/static/styles.css"',
                    f'href="{_static_asset_url("/static/styles.css")}"',
                )
                .replace(
                    'src="/static/app.js"',
                    f'src="{_static_asset_url("/static/app.js")}"',
                )
            )
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        return {"status": "ok", "message": "Asistent Antigravity API běží."}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        icon_file = STATIC_DIR / "favicon.svg"
        if icon_file.is_file():
            return FileResponse(icon_file, media_type="image/svg+xml")
        raise HTTPException(status_code=404, detail="Favicon nenalezen.")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/i18n")
    async def list_i18n_languages() -> dict[str, object]:
        return {
            "default_language": "en",
            "available_languages": list_available_languages(),
        }

    @app.get("/api/i18n/{lang}")
    async def get_i18n_bundle(lang: str) -> dict[str, object]:
        return get_ui_bundle(lang)

    @app.get("/api/auth/state")
    async def auth_state() -> dict[str, object]:
        owner = _get_owner()
        has_pwd = bool(owner.password_hash.strip())
        has_tok = bool(owner.web_token.strip() or settings.web_auth_token.strip())
        return {
            "needs_initial_setup": not has_pwd and not has_tok,
            "has_password": has_pwd,
            "has_token_configured": has_tok,
            "totp_enabled": bool(owner.totp_enabled),
            "assistant_name": owner.assistant_name,
            "language": normalize_language(owner.language),
            "available_languages": list_available_languages(),
            "app_version": __version__,
        }

    @app.post("/api/auth/initial-setup")
    async def initial_setup(payload: InitialSetupRequest) -> dict[str, object]:
        owner = _get_owner()
        if owner.password_hash.strip():
            raise HTTPException(status_code=409, detail="Účet je již nastaven.")
        if not is_valid_email(payload.email):
            raise HTTPException(
                status_code=400, detail="Zadejte platnou e-mailovou adresu (např. vas@email.cz)."
            )
        prev_lang = normalize_language(owner.language)
        lang = normalize_language(payload.language)
        disp = payload.display_name.strip() or t("backend.default_display_name", lang)
        updated = db.update_owner(
            username=payload.username.strip().lower(),
            email=normalize_email(payload.email),
            display_name=disp,
            language=lang,
            password_hash=hash_password(payload.password),
        )
        if lang != prev_lang and not db.list_runs(limit=1):
            reset_workspace(settings, updated)
        session_token = db.create_auth_session()
        return {
            "status": "ok",
            "session_token": session_token,
            "owner": _owner_public_dict(updated, settings),
        }

    @app.post("/api/login")
    async def login(payload: LoginRequest) -> dict[str, object]:
        owner = _get_owner()
        if payload.web_token.strip():
            matched = db.match_owner_token(payload.web_token, settings.web_auth_token)
            if matched is None:
                raise HTTPException(status_code=401, detail="Neplatný Web Token.")
            if matched.totp_enabled:
                if payload.totp_code.strip():
                    if not _verify_second_factor(matched, payload.totp_code):
                        raise HTTPException(status_code=401, detail="Neplatný 2FA kód.")
                else:
                    return {
                        "requires_2fa": True,
                        "challenge_token": create_2fa_challenge(1, signing_key),
                        "username": matched.username,
                    }
            return {
                "status": "ok",
                "requires_2fa": False,
                "session_token": db.create_auth_session(),
                "owner": _owner_public_dict(matched, settings),
            }

        ident = payload.identifier.strip() or payload.username.strip()
        if not ident or not payload.password:
            raise HTTPException(status_code=400, detail="Zadejte přihlašovací jméno/e-mail a heslo.")
        matched = db.match_owner_login(ident)
        if matched is None or not verify_password(payload.password, matched.password_hash):
            raise HTTPException(status_code=401, detail="Nesprávný e-mail/uživatelské jméno nebo heslo.")

        if matched.totp_enabled:
            if payload.totp_code.strip():
                if not _verify_second_factor(matched, payload.totp_code):
                    raise HTTPException(status_code=401, detail="Neplatný 2FA kód.")
            else:
                return {
                    "requires_2fa": True,
                    "challenge_token": create_2fa_challenge(1, signing_key),
                    "username": matched.username,
                }

        return {
            "status": "ok",
            "requires_2fa": False,
            "session_token": db.create_auth_session(),
            "owner": _owner_public_dict(matched, settings),
        }

    @app.post("/api/login/verify-2fa")
    async def login_verify_2fa(payload: Login2FARequest) -> dict[str, object]:
        uid = verify_2fa_challenge(payload.challenge_token, signing_key)
        if uid is None:
            raise HTTPException(status_code=401, detail="Platnost 2FA výzvy vypršela. Přihlaste se znovu.")
        owner = _get_owner()
        if not owner.totp_enabled or not _verify_second_factor(owner, payload.code):
            raise HTTPException(status_code=401, detail="Neplatný 2FA nebo záložní kód.")
        return {
            "status": "ok",
            "session_token": db.create_auth_session(),
            "owner": _owner_public_dict(owner, settings),
        }

    @app.post("/api/logout")
    async def logout(
        authorization: str | None = Header(default=None),
        x_auth_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        provided = _extract_bearer(authorization, x_auth_token)
        if provided:
            db.revoke_auth_session(provided)
        return {"status": "ok"}

    @app.post("/api/account/email")
    async def update_account_email(
        payload: AccountEmailRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if not is_valid_email(payload.email):
            raise HTTPException(status_code=400, detail="Zadejte platnou e-mailovou adresu.")
        updated = db.update_owner(email=normalize_email(payload.email))
        return {"status": "ok", "owner": _owner_public_dict(updated, settings)}

    @app.post("/api/account/password")
    async def update_account_password(
        payload: PasswordChangeRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if owner.password_hash.strip() and not verify_password(
            payload.current_password, owner.password_hash
        ):
            raise HTTPException(status_code=400, detail="Současné heslo nesouhlasí.")
        updated = db.update_owner(password_hash=hash_password(payload.new_password))
        db.revoke_all_auth_sessions()
        session_token = db.create_auth_session()
        return {
            "status": "ok",
            "session_token": session_token,
            "owner": _owner_public_dict(updated, settings),
        }

    @app.post("/api/2fa/setup")
    async def setup_2fa(owner: OwnerConfig = Depends(require_auth)) -> dict[str, str]:
        secret = generate_totp_secret()
        db.update_owner(totp_pending_secret=secret)
        label = owner.email or owner.username
        return {"secret": secret, "otpauth_uri": build_otpauth_uri(secret, label)}

    @app.post("/api/2fa/enable")
    async def enable_2fa(
        payload: TwoFactorVerifyRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        pending = owner.totp_pending_secret.strip()
        if not pending:
            raise HTTPException(status_code=400, detail="Nejprve vygenerujte QR kód pro 2FA.")
        if not verify_totp_code(pending, payload.code):
            raise HTTPException(status_code=400, detail="Neplatný 6místný kód z autentizační aplikace.")
        plain_codes, hashed_codes = generate_recovery_codes(8)
        updated = db.update_owner(
            totp_enabled=True,
            totp_secret=pending,
            totp_pending_secret="",
            totp_recovery_hashes_json=json.dumps(hashed_codes),
        )
        return {
            "status": "ok",
            "recovery_codes": plain_codes,
            "session_token": db.create_auth_session(),
            "owner": _owner_public_dict(updated, settings),
        }

    @app.post("/api/2fa/disable")
    async def disable_2fa(
        payload: TwoFactorVerifyRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if not owner.totp_enabled:
            return {"status": "ok", "owner": _owner_public_dict(owner, settings)}
        if owner.password_hash.strip() and not verify_password(payload.password, owner.password_hash):
            raise HTTPException(status_code=401, detail="Nesprávné heslo účtu.")
        if not _verify_second_factor(owner, payload.code):
            raise HTTPException(status_code=401, detail="Neplatný 2FA nebo záložní kód.")
        updated = db.update_owner(
            totp_enabled=False,
            totp_secret="",
            totp_pending_secret="",
            totp_recovery_hashes_json="[]",
        )
        return {"status": "ok", "owner": _owner_public_dict(updated, settings)}

    @app.get("/api/status")
    async def get_status(owner: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        async def _load_agy_ver() -> dict[str, object]:
            if isinstance(runner, AgyRunner):
                return await runner.get_version_info(force_refresh=False)
            ver = await runner.check_version()
            return {
                "agy_available": ver is not None,
                "agy_version": ver,
                "agy_latest_version": None,
                "agy_up_to_date": None,
                "agy_update_available": False,
            }

        ver_info, app_ver_info = await asyncio.gather(
            _load_agy_ver(),
            app_version_checker.get_version_info(force_refresh=False),
        )
        mem = get_memory(settings, owner)
        _, web_ctx = db.get_session("web:default", default_context=owner.default_context_mode)
        jobs = scheduler.list_jobs() if scheduler else []
        return {
            **_owner_public_dict(owner, settings),
            **ver_info,
            **app_ver_info,
            "telegram_enabled": bool(owner.telegram_bot_token.strip()),
            "allowed_telegram_users_count": len(owner.allowed_telegram_user_ids),
            "web_context_mode": web_ctx,
            "skills": mem.list_skills(),
            "jobs": [asdict(j) for j in jobs],
        }

    @app.get("/api/settings")
    async def get_settings(owner: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        return _owner_public_dict(owner, settings)

    @app.post("/api/settings")
    async def update_settings(
        payload: SettingsUpdateRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        raw_updates = payload.model_dump(exclude_unset=True)
        owner_updates: dict[str, Any] = {}
        for field in (
            "display_name",
            "assistant_name",
            "language",
            "tz",
            "default_context_mode",
            "telegram_bot_token",
            "telegram_allowed_users",
            "telegram_notify_chat_id",
            "heartbeat_enabled",
            "heartbeat_interval_minutes",
            "morning_briefing_enabled",
            "morning_briefing_cron",
            "nightly_reset_enabled",
            "nightly_reset_cron",
            "agy_model",
            "gemini_api_key",
        ):
            if field in raw_updates and raw_updates[field] is not None:
                owner_updates[field] = (
                    normalize_language(str(raw_updates[field]))
                    if field == "language"
                    else raw_updates[field]
                )

        for cron_field, label in (
            ("morning_briefing_cron", "ranního briefingu"),
            ("nightly_reset_cron", "nočního resetu"),
        ):
            if cron_field in owner_updates:
                cron_val = str(owner_updates[cron_field]).strip()
                if not cron_val:
                    raise HTTPException(
                        status_code=400, detail=f"Cron výraz pro {label} nesmí být prázdný."
                    )
                try:
                    CronTrigger.from_crontab(cron_val)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400, detail=f"Neplatný cron výraz pro {label}: {exc}"
                    ) from exc

        runtime_updates: dict[str, str | None] = {}
        if "web_token" in raw_updates and raw_updates["web_token"]:
            owner_updates["web_token"] = raw_updates["web_token"]
            runtime_updates["web_auth_token"] = raw_updates["web_token"]
        if "web_auth_token" in raw_updates and raw_updates["web_auth_token"] is not None:
            owner_updates["web_token"] = raw_updates["web_auth_token"]
            runtime_updates["web_auth_token"] = raw_updates["web_auth_token"]
        if "gemini_api_key" in raw_updates and raw_updates["gemini_api_key"] is not None:
            runtime_updates["gemini_api_key"] = raw_updates["gemini_api_key"]
        if "telegram_bot_token" in raw_updates:
            runtime_updates["telegram_bot_token"] = raw_updates["telegram_bot_token"]
        if "telegram_allowed_users" in raw_updates:
            runtime_updates["telegram_allowed_users"] = raw_updates["telegram_allowed_users"]
        if runtime_updates:
            save_runtime_overrides(settings, runtime_updates)

        updated = db.update_owner(**owner_updates)
        ensure_workspace(settings, updated)
        if "assistant_name" in owner_updates:
            sync_soul_assistant_name(
                settings.workspace_dir, updated.assistant_name, overwrite_existing=True
            )
        prepare_agy_home(settings, db)
        if isinstance(runner, AgyRunner):
            if "gemini_api_key" in owner_updates:
                runner.invalidate_models_cache()
            if any(
                k in owner_updates
                for k in ("agy_model", "gemini_api_key", "default_context_mode", "language")
            ):
                await runner.close_all()
        await _notify_settings_updated()
        return {"status": "ok", **_owner_public_dict(updated, settings)}

    @app.get("/api/mcp")
    async def list_mcp_servers(_: OwnerConfig = Depends(require_auth)) -> list[dict[str, object]]:
        return [_mcp_public_dict(s) for s in db.list_mcp_servers()]

    @app.post("/api/mcp")
    async def upsert_mcp_server(
        payload: McpServerUpsertRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        existing_token = ""
        if payload.id is not None:
            for item in db.list_mcp_servers():
                if item.id == payload.id:
                    existing_token = item.auth_token
                    break
        effective_token = payload.auth_token if payload.auth_token is not None else existing_token
        try:
            saved = db.upsert_mcp_server(
                server_id=payload.id,
                name=payload.name,
                scope=payload.scope,
                transport=payload.transport,
                url=payload.url,
                auth_token=effective_token,
                headers_json=json.dumps(payload.headers or {}, ensure_ascii=False),
                command=payload.command,
                args_json=json.dumps(payload.args or [], ensure_ascii=False),
                env_json=json.dumps(payload.env or {}, ensure_ascii=False),
                enabled=payload.enabled,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        prepare_agy_home(settings, db)
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return _mcp_public_dict(saved)

    @app.delete("/api/mcp/{server_id}")
    async def delete_mcp_server(
        server_id: int,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, str]:
        if not db.delete_mcp_server(server_id):
            raise HTTPException(status_code=404, detail="MCP server nebyl nalezen.")
        prepare_agy_home(settings, db)
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return {"status": "ok"}

    @app.get("/api/plugins")
    async def list_plugins(_: OwnerConfig = Depends(require_auth)) -> dict[str, Any]:
        return await plugin_manager.list_catalog_with_status()

    @app.post("/api/plugins/install")
    async def install_plugin(
        payload: PluginInstallRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = await plugin_manager.install_plugin(payload.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.get("/api/plugins/connect/status")
    async def plugin_connect_status(_: OwnerConfig = Depends(require_auth)) -> dict[str, Any]:
        return asdict(plugin_manager.get_connect_state())

    @app.post("/api/plugins/connect/callback")
    async def plugin_connect_callback(
        payload: PluginConnectCallbackRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = await plugin_manager.submit_callback_url(payload.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.post("/api/plugins/connect/input")
    async def plugin_connect_input(
        payload: PluginConnectInputRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            if payload.key.strip():
                state = await plugin_manager.send_connect_key(payload.key)
            else:
                state = await plugin_manager.send_connect_input(payload.value)
            return asdict(state)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/plugins/connect/stop")
    async def plugin_connect_stop(_: OwnerConfig = Depends(require_auth)) -> dict[str, str]:
        await plugin_manager.stop_connect()
        return {"status": "ok"}

    @app.post("/api/plugins/{plugin_name}/toggle")
    async def toggle_plugin(
        plugin_name: str,
        payload: PluginToggleRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = await plugin_manager.set_plugin_enabled(plugin_name, payload.enabled)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.put("/api/plugins/{plugin_name}/permissions")
    async def update_plugin_permissions(
        plugin_name: str,
        payload: PluginPermissionsRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = plugin_manager.update_permissions(plugin_name, payload.permissions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.post("/api/plugins/{plugin_name}/accounts")
    async def create_plugin_account(
        plugin_name: str,
        payload: PluginAccountCreateRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = plugin_manager.create_account(
                plugin_name,
                label=payload.label,
                scope=payload.scope,
                permissions=payload.permissions,
                custom_rules=payload.custom_rules,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.put("/api/plugins/{plugin_name}/accounts/{account_id}")
    async def update_plugin_account(
        plugin_name: str,
        account_id: int,
        payload: PluginAccountUpdateRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = plugin_manager.update_account(
                plugin_name,
                account_id,
                label=payload.label,
                scope=payload.scope,
                enabled=payload.enabled,
                permissions=payload.permissions,
                custom_rules=payload.custom_rules,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.delete("/api/plugins/{plugin_name}/accounts/{account_id}")
    async def delete_plugin_account(
        plugin_name: str,
        account_id: int,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = plugin_manager.delete_account(plugin_name, account_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.post("/api/plugins/{plugin_name}/connect/start")
    async def plugin_connect_start(
        plugin_name: str,
        payload: PluginConnectStartRequest = Body(default_factory=PluginConnectStartRequest),
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            _sync_all_plugin_contexts()
            state = await plugin_manager.start_connect(
                plugin_name,
                install_target=payload.install_target,
                force_reconnect=payload.force_reconnect,
                account_id=payload.account_id,
                interactive_config=payload.interactive_config,
            )
            return asdict(state)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/plugins/{plugin_name}")
    async def uninstall_plugin(
        plugin_name: str,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            res = await plugin_manager.uninstall_plugin(plugin_name)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_all_plugin_contexts()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        return res

    @app.post("/api/auth/start")
    async def auth_start(
        payload: AuthStartRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if auth_manager is None:
            raise HTTPException(status_code=503, detail="Správce přihlášení není aktivní.")
        ensure_workspace(settings, owner)
        prepare_agy_home(settings, db)
        if isinstance(runner, AgyRunner):
            runner.invalidate_models_cache()
        state = await auth_manager.start(mode=payload.mode)
        return asdict(state)

    @app.get("/api/auth/status")
    async def auth_status(owner: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if auth_manager is None:
            raise HTTPException(status_code=503, detail="Správce přihlášení není aktivní.")
        has_oauth = has_agy_oauth(settings)
        has_key = bool(owner.gemini_api_key.strip())
        return {
            **asdict(auth_manager.get_state()),
            "has_agy_oauth": has_oauth,
            "has_gemini_api_key": has_key,
            "agy_authenticated": has_oauth or has_key,
        }

    @app.post("/api/auth/input")
    async def auth_input(
        payload: AuthInputRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if auth_manager is None:
            raise HTTPException(status_code=503, detail="Správce přihlášení není aktivní.")
        try:
            state = await auth_manager.send_input(payload.value)
            return asdict(state)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/stop")
    async def auth_stop(_: OwnerConfig = Depends(require_auth)) -> dict[str, str]:
        if auth_manager is not None:
            await auth_manager.stop()
        return {"status": "ok"}

    @app.post("/api/auth/logout")
    async def auth_logout(_: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if auth_manager is not None:
            await auth_manager.stop()
        clear_agy_oauth(settings)
        save_runtime_overrides(settings, {"gemini_api_key": ""})
        db.update_owner(gemini_api_key="")
        prepare_agy_home(settings, db)
        if isinstance(runner, AgyRunner):
            runner.invalidate_models_cache()
            await runner.close_all()
        return {
            "status": "ok",
            "has_agy_oauth": False,
            "has_gemini_api_key": False,
            "agy_authenticated": False,
        }

    @app.get("/api/app/version")
    async def get_app_version(
        refresh: bool = True,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        return await app_version_checker.get_version_info(force_refresh=refresh)

    @app.get("/api/agy/version")
    async def get_agy_version(
        refresh: bool = True,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if isinstance(runner, AgyRunner):
            return await runner.get_version_info(force_refresh=refresh)
        ver = await runner.check_version()
        return {
            "agy_available": ver is not None,
            "agy_version": ver,
            "agy_latest_version": None,
            "agy_up_to_date": None,
            "agy_update_available": False,
        }

    @app.post("/api/agy/update")
    async def post_agy_update(_: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if not isinstance(runner, AgyRunner):
            raise HTTPException(status_code=503, detail="Aktualizace agy není v tomto režimu dostupná.")
        result = await runner.update_cli()
        if not result.get("success"):
            raise HTTPException(
                status_code=500, detail=str(result.get("output") or "Aktualizace agy selhala.")
            )
        return result

    @app.get("/api/models")
    async def list_models(
        refresh: bool = False,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        ensure_workspace(settings, owner)
        prepare_agy_home(settings, db)
        models = await runner.list_models(force_refresh=refresh)
        return {"current_model": owner.agy_model, "models": models}

    @app.post("/api/chat")
    async def post_chat(
        payload: ChatRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        ensure_workspace(settings, owner)
        default_ctx: ContextMode = owner.default_context_mode  # type: ignore[assignment]
        effective_ctx = payload.context_mode or default_ctx
        channel = normalize_chat_channel(effective_ctx)
        session_key = f"web:{channel}" if payload.session_key == "web:default" else payload.session_key
        effective_model = payload.model if payload.model is not None else owner.agy_model
        result = await runner.run(
            payload.prompt,
            session_key=session_key,
            source="web",
            context_mode=effective_ctx,
            model=effective_model,
        )
        data = asdict(result)
        data["assistant_name"] = _get_owner().assistant_name
        return data

    @app.post("/api/chat/reset")
    async def post_chat_reset(
        payload: ResetRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, Literal["ok"]]:
        effective_ctx = payload.context_mode or owner.default_context_mode
        channel = normalize_chat_channel(effective_ctx)
        session_key = f"web:{channel}" if payload.session_key == "web:default" else payload.session_key
        db.reset_conversation(session_key, default_context=effective_ctx)
        if isinstance(runner, AgyRunner):
            await runner.close_session(session_key)
        return {"status": "ok"}

    @app.post("/api/workspace/reset")
    async def post_workspace_reset(
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        reset_workspace(settings, owner)
        db.reset_all_sessions()
        if isinstance(runner, AgyRunner):
            await runner.close_all()
        mem = get_memory(settings, owner)
        return {"status": "ok", "files": [asdict(f) for f in mem.list_files()]}

    @app.get("/api/files")
    async def list_files(owner: OwnerConfig = Depends(require_auth)) -> list[dict[str, object]]:
        mem = get_memory(settings, owner)
        return [asdict(f) for f in mem.list_files()]

    @app.get("/api/files/{file_path:path}")
    async def read_file(
        file_path: str,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, str]:
        mem = get_memory(settings, owner)
        try:
            content = mem.read_file(file_path)
            return {"path": file_path, "content": content}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/files/{file_path:path}")
    async def write_file(
        file_path: str,
        payload: FileUpdateRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        mem = get_memory(settings, owner)
        try:
            info = mem.write_file(file_path, payload.content)
            return asdict(info)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs")
    async def list_runs(
        limit: int = 30,
        session_key: str | None = None,
        _: OwnerConfig = Depends(require_auth),
    ) -> list[dict[str, object]]:
        return [asdict(r) for r in db.list_runs(limit=limit, session_key=session_key)]

    @app.get("/api/usage")
    async def get_usage(
        days: int | None = None,
        hours: int | None = None,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        return asdict(db.get_activity_stats(days=days, hours=hours))

    @app.post("/api/heartbeat/run")
    async def trigger_heartbeat(_: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if not scheduler:
            raise HTTPException(status_code=503, detail="Plánovač není aktivní.")
        return asdict(await scheduler.run_heartbeat(notify=False))

    @app.post("/api/briefing/run")
    async def trigger_briefing(_: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if not scheduler:
            raise HTTPException(status_code=503, detail="Plánovač není aktivní.")
        return asdict(await scheduler.run_morning_briefing(notify=False))

    @app.post("/api/nightly-reset/run")
    async def trigger_nightly_reset(_: OwnerConfig = Depends(require_auth)) -> dict[str, object]:
        if not scheduler:
            raise HTTPException(status_code=503, detail="Plánovač není aktivní.")
        return await scheduler.run_nightly_reset()

    @app.get("/api/tasks")
    async def list_scheduled_tasks(_: OwnerConfig = Depends(require_auth)) -> list[dict[str, object]]:
        return [asdict(t) for t in db.list_scheduled_tasks()]

    @app.post("/api/tasks")
    async def create_scheduled_task(
        payload: ScheduledTaskUpsertRequest,
        owner: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        try:
            tz = ZoneInfo(owner.tz or settings.tz)
        except Exception:
            tz = ZoneInfo("UTC")

        if payload.schedule_type == "cron":
            if not payload.cron_expr.strip():
                raise HTTPException(status_code=400, detail="Cron výraz nesmí být prázdný.")
            try:
                CronTrigger.from_crontab(payload.cron_expr.strip(), timezone=tz)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Neplatný cron výraz: {exc}") from exc
        else:
            if not payload.run_at.strip():
                raise HTTPException(status_code=400, detail="Datum a čas spuštění nesmí být prázdné.")
            try:
                datetime.fromisoformat(payload.run_at.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Neplatný formát data: {exc}") from exc

        task = db.upsert_scheduled_task(
            name=payload.name,
            prompt=payload.prompt,
            schedule_type=payload.schedule_type,
            cron_expr=payload.cron_expr,
            run_at=payload.run_at,
            action_type=payload.action_type,
            enabled=payload.enabled,
        )
        if scheduler:
            scheduler.sync_jobs()
        return asdict(task)

    @app.patch("/api/tasks/{task_id}")
    async def patch_scheduled_task(
        task_id: int,
        payload: ScheduledTaskPatchRequest,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        updated = db.update_scheduled_task(task_id, enabled=payload.enabled)
        if updated is None:
            raise HTTPException(status_code=404, detail="Úloha nebyla nalezena.")
        if scheduler:
            scheduler.sync_jobs()
        return asdict(updated)

    @app.delete("/api/tasks/{task_id}")
    async def delete_scheduled_task(
        task_id: int,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, str]:
        if not db.delete_scheduled_task(task_id):
            raise HTTPException(status_code=404, detail="Úloha nebyla nalezena.")
        if scheduler:
            scheduler.sync_jobs()
        return {"status": "ok"}

    @app.post("/api/tasks/{task_id}/run")
    async def run_scheduled_task_now(
        task_id: int,
        _: OwnerConfig = Depends(require_auth),
    ) -> dict[str, object]:
        if not scheduler:
            raise HTTPException(status_code=503, detail="Plánovač není aktivní.")
        if db.get_scheduled_task(task_id) is None:
            raise HTTPException(status_code=404, detail="Úloha nebyla nalezena.")
        return asdict(await scheduler.run_custom_task(task_id=task_id, notify=False))

    return app
