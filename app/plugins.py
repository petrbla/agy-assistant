from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.auth_session import clean_terminal_output, extract_osc8_urls, extract_urls
from app.config import Settings, normalize_chat_channel
from app.db import AssistantDB, PluginAccountRecord, PluginConfigRecord

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows nemá POSIX pty/fcntl/termios
    fcntl = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_PLUGIN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(?:\.git)?/?$"
)
_CONNECT_KEYS: dict[str, str] = {
    "enter": "\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "tab": "\t",
    "esc": "\x1b",
    "space": " ",
}

FEATURED_PLUGIN_NAMES: tuple[str, ...] = ("gmail", "gcalendar", "gdrive")
_GOOGLE_CLI_PLUGINS: tuple[str, ...] = (
    "gmail",
    "gcalendar",
    "gdrive",
    "gdocs",
    "gsheets",
    "gslides",
)
_PROFILE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

MANAGED_PLUGIN_DENY_PREFIXES: tuple[str, ...] = (
    "command(gmail ",
    "command(gcalendar ",
    "command(gdrive ",
    "command(gdocs ",
    "command(gsheets ",
    "command(gslides ",
)


@dataclass(frozen=True)
class PluginPermissionSpec:
    """Definice jednoho přepínače oprávnění pluginu."""

    key: str
    label_key: str
    default: bool
    tier: str = "basic"  # 'basic' | 'detail'


@dataclass(frozen=True)
class PluginCatalogItem:
    """Kurátorovaná položka katalogu Antigravity pluginů."""

    name: str
    display_name: str
    install_target: str
    marketplace: str
    category: str
    icon: str
    desc_key: str
    description: str
    logo_url: str = ""
    supports_oauth: bool = False
    cli_bin: str = ""
    permissions: tuple[PluginPermissionSpec, ...] = ()


@dataclass
class PluginConnectState:
    """Stav interaktivního připojování účtu pluginu v PTY."""

    running: bool = False
    plugin_name: str = ""
    flow_type: str = ""
    auth_url: str = ""
    urls: list[str] = field(default_factory=list)
    callback_port: int | None = None
    callback_path: str = "/callback"
    expected_state: str = ""
    output: str = ""
    connected: bool = False
    already_connected: bool = False
    account: str = ""
    account_id: int | None = None
    profile_key: str = "default"
    exit_code: int | None = None
    error: str | None = None


_READ_WRITE_PERMS_TEMPLATE: dict[str, tuple[PluginPermissionSpec, ...]] = {
    "gdrive": (
        PluginPermissionSpec("read", "perm_drive_read", True, "basic"),
        PluginPermissionSpec("write", "perm_drive_write", False, "basic"),
        PluginPermissionSpec("share", "perm_drive_share", False, "detail"),
        PluginPermissionSpec("delete", "perm_drive_delete", False, "detail"),
    ),
    "gdocs": (
        PluginPermissionSpec("read", "perm_docs_read", True, "basic"),
        PluginPermissionSpec("write", "perm_docs_write", False, "basic"),
    ),
    "gsheets": (
        PluginPermissionSpec("read", "perm_sheets_read", True, "basic"),
        PluginPermissionSpec("write", "perm_sheets_write", False, "basic"),
    ),
    "gslides": (
        PluginPermissionSpec("read", "perm_slides_read", True, "basic"),
        PluginPermissionSpec("write", "perm_slides_write", False, "basic"),
    ),
}

PLUGIN_CATALOG: tuple[PluginCatalogItem, ...] = (
    # Google Workspace (antigravity-plugins-official)
    PluginCatalogItem(
        name="gmail",
        display_name="Gmail",
        install_target="gmail",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="✉️",
        logo_url="https://www.gstatic.com/images/branding/productlogos/gmail_2020q4/v11/192px.svg",
        desc_key="plugin_desc_gmail",
        description="Search, read, draft, and send emails in Gmail.",
        supports_oauth=True,
        cli_bin="gmail",
        permissions=(
            PluginPermissionSpec("read", "perm_gmail_read", True, "basic"),
            PluginPermissionSpec("draft", "perm_gmail_draft", True, "basic"),
            PluginPermissionSpec("send", "perm_gmail_send", False, "basic"),
            PluginPermissionSpec("labels", "perm_gmail_labels", False, "basic"),
            PluginPermissionSpec("reply_forward", "perm_gmail_reply_forward", False, "detail"),
            PluginPermissionSpec("archive", "perm_gmail_archive", False, "detail"),
            PluginPermissionSpec("trash", "perm_gmail_trash", False, "detail"),
            PluginPermissionSpec("reactions", "perm_gmail_reactions", False, "detail"),
        ),
    ),
    PluginCatalogItem(
        name="gcalendar",
        display_name="Google Calendar",
        install_target="gcalendar",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="📅",
        logo_url="https://www.gstatic.com/images/branding/productlogos/calendar_2020q4/v13/192px.svg",
        desc_key="plugin_desc_gcalendar",
        description="Check availability, schedule events, and RSVP.",
        supports_oauth=True,
        cli_bin="gcalendar",
        permissions=(
            PluginPermissionSpec("read", "perm_gcal_read", True, "basic"),
            PluginPermissionSpec("create", "perm_gcal_create", True, "basic"),
            PluginPermissionSpec("delete", "perm_gcal_delete", False, "basic"),
            PluginPermissionSpec("update", "perm_gcal_update", True, "detail"),
            PluginPermissionSpec("rsvp", "perm_gcal_rsvp", False, "detail"),
        ),
    ),
    PluginCatalogItem(
        name="gdrive",
        display_name="Google Drive",
        install_target="gdrive",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="📁",
        logo_url="https://www.gstatic.com/images/branding/productlogos/drive_2020q4/v10/192px.svg",
        desc_key="plugin_desc_gdrive",
        description="Search, upload, download, and share files.",
        supports_oauth=True,
        cli_bin="gdrive",
        permissions=_READ_WRITE_PERMS_TEMPLATE["gdrive"],
    ),
    PluginCatalogItem(
        name="gdocs",
        display_name="Google Docs",
        install_target="gdocs",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="📝",
        logo_url="https://www.gstatic.com/images/branding/productlogos/docs_2020q4/v12/192px.svg",
        desc_key="plugin_desc_gdocs",
        description="Read, draft, and edit docs.",
        supports_oauth=True,
        cli_bin="gdocs",
        permissions=_READ_WRITE_PERMS_TEMPLATE["gdocs"],
    ),
    PluginCatalogItem(
        name="gsheets",
        display_name="Google Sheets",
        install_target="gsheets",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="📊",
        logo_url="https://www.gstatic.com/images/branding/productlogos/sheets_2020q4/v11/192px.svg",
        desc_key="plugin_desc_gsheets",
        description="Read, write, and format spreadsheets.",
        supports_oauth=True,
        cli_bin="gsheets",
        permissions=_READ_WRITE_PERMS_TEMPLATE["gsheets"],
    ),
    PluginCatalogItem(
        name="gslides",
        display_name="Google Slides",
        install_target="gslides",
        marketplace="antigravity-plugins-official",
        category="google",
        icon="📽️",
        logo_url="https://www.gstatic.com/images/branding/productlogos/slides_2020q4/v12/192px.svg",
        desc_key="plugin_desc_gslides",
        description="Read, edit, and export presentations.",
        supports_oauth=True,
        cli_bin="gslides",
        permissions=_READ_WRITE_PERMS_TEMPLATE["gslides"],
    ),
    # Produktivita (antigravity-plugins-partners)
    PluginCatalogItem(
        name="notion",
        display_name="Notion",
        install_target="notion@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="📓",
        logo_url="https://cdn.simpleicons.org/notion",
        desc_key="plugin_desc_notion",
        description="Connect your Notion workspace for notes, docs, and databases.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="clickup",
        display_name="ClickUp",
        install_target="clickup@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="✅",
        logo_url="https://cdn.simpleicons.org/clickup",
        desc_key="plugin_desc_clickup",
        description="Manage tasks, lists, and time tracking in ClickUp.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="linear",
        display_name="Linear",
        install_target="linear@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="📐",
        logo_url="https://cdn.simpleicons.org/linear",
        desc_key="plugin_desc_linear",
        description="Manage issues, projects, and cycles across your Linear workspace.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="atlassian-twg-cli",
        display_name="Atlassian (Jira & Confluence)",
        install_target="atlassian-twg-cli@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="🗂️",
        logo_url="https://cdn.simpleicons.org/atlassian",
        desc_key="plugin_desc_atlassian",
        description="Search and manage Jira issues, Confluence pages, and Bitbucket PRs.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="wispr-flow",
        display_name="Wispr Flow",
        install_target="wispr-flow@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="🎙️",
        logo_url="https://www.google.com/s2/favicons?domain=wisprflow.ai&sz=128",
        desc_key="plugin_desc_wispr",
        description="Search and read meeting notes, transcripts, and scratchpad notes.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="zoho-projects",
        display_name="Zoho Projects",
        install_target="zoho-projects@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="productivity",
        icon="📋",
        logo_url="https://cdn.simpleicons.org/zoho",
        desc_key="plugin_desc_zoho_projects",
        description="Access project tasks, issues, and milestones in Zoho Projects.",
        supports_oauth=True,
    ),
    # Byznys, automatizace & design (antigravity-plugins-partners)
    PluginCatalogItem(
        name="n8n-skills",
        display_name="n8n",
        install_target="n8n-skills@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="⚡",
        logo_url="https://cdn.simpleicons.org/n8n",
        desc_key="plugin_desc_n8n",
        description="Build, test, and debug n8n workflows and automations.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="canva",
        display_name="Canva",
        install_target="canva@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="🎨",
        logo_url="https://www.google.com/s2/favicons?domain=canva.com&sz=128",
        desc_key="plugin_desc_canva",
        description="Create, edit, resize, and review Canva designs.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="figma",
        display_name="Figma",
        install_target="figma@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="🖌️",
        logo_url="https://cdn.simpleicons.org/figma",
        desc_key="plugin_desc_figma",
        description="Read and inspect Figma designs and convert components to code.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="stripe",
        display_name="Stripe",
        install_target="stripe@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="💳",
        logo_url="https://cdn.simpleicons.org/stripe",
        desc_key="plugin_desc_stripe",
        description="Manage customers, invoices, subscriptions, and payment links.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="zoho-crm-plugin",
        display_name="Zoho CRM",
        install_target="zoho-crm-plugin@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="💼",
        logo_url="https://cdn.simpleicons.org/zoho",
        desc_key="plugin_desc_zoho_crm",
        description="Query and work with CRM contacts, leads, and deals.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="zoho-bigin",
        display_name="Bigin CRM",
        install_target="zoho-bigin@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="📇",
        logo_url="https://www.google.com/s2/favicons?domain=bigin.com&sz=128",
        desc_key="plugin_desc_bigin",
        description="Manage Bigin CRM pipelines, contacts, deals, and follow-ups.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="intercom",
        display_name="Intercom",
        install_target="intercom@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="business",
        icon="💬",
        logo_url="https://cdn.simpleicons.org/intercom",
        desc_key="plugin_desc_intercom",
        description="Search Intercom customer conversations, contacts, and support trends.",
        supports_oauth=True,
    ),
    # Vývoj & Cloud
    PluginCatalogItem(
        name="firebase",
        display_name="Firebase",
        install_target="firebase",
        marketplace="antigravity-plugins-official",
        category="dev",
        icon="🔥",
        logo_url="https://cdn.simpleicons.org/firebase",
        desc_key="plugin_desc_firebase",
        description="Build and operate apps with Firebase backend and AI tools.",
        supports_oauth=False,
    ),
    PluginCatalogItem(
        name="cloudflare",
        display_name="Cloudflare",
        install_target="cloudflare@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="dev",
        icon="☁️",
        logo_url="https://cdn.simpleicons.org/cloudflare",
        desc_key="plugin_desc_cloudflare",
        description="Manage Cloudflare Workers, storage, and edge deployments.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="netlify",
        display_name="Netlify",
        install_target="netlify@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="dev",
        icon="🌐",
        logo_url="https://cdn.simpleicons.org/netlify",
        desc_key="plugin_desc_netlify",
        description="Deploy and manage websites, functions, and forms on Netlify.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="grafana-cloud-mcp",
        display_name="Grafana Cloud",
        install_target="grafana-cloud-mcp@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="dev",
        icon="📈",
        logo_url="https://cdn.simpleicons.org/grafana",
        desc_key="plugin_desc_grafana",
        description="Query Grafana metrics, logs, dashboards, and alerts.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="google-cloud-developer",
        display_name="Google Cloud Developer",
        install_target="google-cloud-developer",
        marketplace="antigravity-plugins-official",
        category="dev",
        icon="🛠️",
        logo_url="https://cdn.simpleicons.org/googlecloud",
        desc_key="plugin_desc_gcloud",
        description="Google Cloud guidance, gcloud validation, and developer knowledge.",
        supports_oauth=False,
    ),
    PluginCatalogItem(
        name="chrome-devtools-plugin",
        display_name="Chrome DevTools",
        install_target="chrome-devtools-plugin",
        marketplace="antigravity-plugins-official",
        category="dev",
        icon="🧭",
        logo_url="https://cdn.simpleicons.org/googlechrome",
        desc_key="plugin_desc_chrome",
        description="Browser debugging, automation, and performance analysis.",
        supports_oauth=False,
    ),
    PluginCatalogItem(
        name="looker",
        display_name="Looker",
        install_target="looker",
        marketplace="antigravity-plugins-official",
        category="dev",
        icon="📉",
        logo_url="https://cdn.simpleicons.org/looker",
        desc_key="plugin_desc_looker",
        description="Explore Looker data, run Looks, and build dashboards.",
        supports_oauth=True,
    ),
    PluginCatalogItem(
        name="neo4j",
        display_name="Neo4j",
        install_target="neo4j@antigravity-plugins-partners",
        marketplace="antigravity-plugins-partners",
        category="dev",
        icon="🕸️",
        logo_url="https://cdn.simpleicons.org/neo4j",
        desc_key="plugin_desc_neo4j",
        description="Write Cypher queries, design graph models, and manage Neo4j Aura.",
        supports_oauth=False,
    ),
)

_PLUGIN_CATALOG_BY_NAME: dict[str, PluginCatalogItem] = {
    item.name.lower(): item for item in PLUGIN_CATALOG
}


def validate_plugin_name(name: str) -> str:
    """Ověří bezpečný název pluginu (bez flag injection a path traversal)."""
    cleaned = (name or "").strip()
    if not cleaned or ".." in cleaned or not _PLUGIN_NAME_RE.fullmatch(cleaned):
        raise ValueError(f"Neplatný název pluginu: {name!r}")
    return cleaned


def validate_plugin_target(target: str) -> str:
    """Ověří instalační cíl pluginu (`name`, `name@marketplace`, nebo HTTPS GitHub URL)."""
    cleaned = (target or "").strip()
    if not cleaned or cleaned.startswith("-") or ".." in cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError(f"Neplatný cíl pluginu: {target!r}")
    if _GITHUB_URL_RE.fullmatch(cleaned):
        return cleaned
    if "@" in cleaned:
        parts = cleaned.split("@")
        if len(parts) != 2:
            raise ValueError(f"Neplatný formát plugin@marketplace: {target!r}")
        validate_plugin_name(parts[0])
        validate_plugin_name(parts[1])
        return cleaned
    return validate_plugin_name(cleaned)


_SIGNED_IN_LINE_RE = re.compile(
    r"(?i)(?:sign-in\s*:\s*(?:✓\s*)?|✓\s*)signed\s+in([^\r\n]*)"
)
_SIGNED_IN_AS_RE = re.compile(r"(?i)(?:^|\s)as\s+([^\s\r\n│|]+)")


def extract_signed_in_account(text: str) -> tuple[bool, str]:
    """Rozpozná z výstupu TUI, že je plugin k účtu již přihlášen, a vrátí případný účet/e-mail."""
    if not text:
        return False, ""
    matches = list(_SIGNED_IN_LINE_RE.finditer(text))
    for match in reversed(matches):
        prefix = text[max(0, match.start() - 16) : match.start()].lower()
        if "not " in prefix:
            continue
        rest = (match.group(1) or "").split("│", 1)[0]
        as_match = _SIGNED_IN_AS_RE.search(rest)
        return True, (as_match.group(1).strip() if as_match else "")
    return False, ""


def _pick_best_oauth_url(urls: list[str]) -> str:
    """Vybere z extrahovaných odkazů nejpravděpodobnější OAuth přihlašovací URL."""
    clean_urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not clean_urls:
        return ""
    for scorer in (
        lambda u: ("accounts.google.com/o/oauth2" in u and "redirect_uri=" in u),
        lambda u: "redirect_uri=" in u,
        lambda u: "accounts.google.com/o/oauth2" in u,
        lambda u: ("/oauth" in u.lower() or "/authorize" in u.lower()),
    ):
        matches = [u for u in clean_urls if scorer(u)]
        if matches:
            return max(matches, key=len)
    return clean_urls[0]


def extract_oauth_callback_info(urls: list[str]) -> dict[str, Any]:
    """Rozpozná typ OAuth toku a parametry localhost callbacku z nalezených URL."""
    auth_url = _pick_best_oauth_url(urls)
    info: dict[str, Any] = {
        "auth_url": auth_url,
        "flow_type": "unknown" if auth_url else "",
        "callback_port": None,
        "callback_path": "/callback",
        "expected_state": "",
    }
    if not auth_url:
        return info

    parsed = urlparse(auth_url)
    qs = parse_qs(parsed.query, keep_blank_values=False)
    state = (qs.get("state") or [""])[0].strip()
    redirect_uri = (qs.get("redirect_uri") or [""])[0].strip()
    info["expected_state"] = state

    if redirect_uri:
        r_parsed = urlparse(redirect_uri)
        if (
            r_parsed.scheme == "http"
            and r_parsed.hostname in {"127.0.0.1", "localhost"}
            and r_parsed.port is not None
            and 1024 <= int(r_parsed.port) <= 65535
        ):
            info["flow_type"] = "localhost_callback"
            info["callback_port"] = int(r_parsed.port)
            info["callback_path"] = r_parsed.path or "/callback"
            return info
        if redirect_uri.startswith("https://antigravity.google/oauth-callback"):
            info["flow_type"] = "code_paste"
            return info
    return info


def validate_localhost_callback_url(
    raw_url: str,
    *,
    expected_port: int,
    expected_path: str = "/callback",
    expected_state: str = "",
) -> str:
    """Přísně ověří OAuth callback URL mířící výhradně na lokální port agy (ochrana proti SSRF)."""
    cleaned = (raw_url or "").strip()
    if not cleaned:
        raise ValueError("Vložte celou adresu z adresního řádku prohlížeče.")
    if not isinstance(expected_port, int) or not (1024 <= expected_port <= 65535):
        raise ValueError("Neplatný cílový port lokálního OAuth callbacku.")

    parsed = urlparse(cleaned)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Adresa musí začínat http://127.0.0.1:… nebo http://localhost:…")
    if parsed.port != expected_port:
        raise ValueError(f"Port v adrese neodpovídá čekající relaci (očekáván port {expected_port}).")

    norm_expected_path = expected_path or "/callback"
    if (parsed.path or "/") != norm_expected_path:
        raise ValueError(f"Neplatná cesta callbacku (očekáváno {norm_expected_path}).")

    qs = parse_qs(parsed.query, keep_blank_values=False)
    err = (qs.get("error") or [""])[0].strip()
    if err:
        raise ValueError(f"Google vrátil chybu autorizace: {err}")

    code = (qs.get("code") or [""])[0].strip()
    if not code:
        raise ValueError("V adrese chybí parametr ?code=… – zkopírujte celou URL z prohlížeče.")

    if expected_state:
        got_state = (qs.get("state") or [""])[0].strip()
        if got_state != expected_state:
            raise ValueError("Parametr state neodpovídá aktuální přihlašovací relaci. Spusťte připojení znovu.")

    return f"http://127.0.0.1:{expected_port}{norm_expected_path}?{parsed.query}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Zabraňuje automatickému následování HTTP přesměrování pryč z 127.0.0.1."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def forward_localhost_callback_sync(validated_url: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Předá ověřený callback lokálnímu listeneru agy bez následování přesměrování."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(validated_url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            location = str(resp.headers.get("Location") or "")
            body = resp.read(500).decode("utf-8", errors="replace")
            return status, location, body
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read(500).decode("utf-8", errors="replace")
        location = str(exc.headers.get("Location") or "") if exc.headers else ""
        return int(exc.code), location, body


def is_mcp_config_required_error(text: str) -> bool:
    """Rozpozná chybu CLI vyžadující nejprve konfiguraci MCP serveru v `/plugin`."""
    low = (text or "").lower()
    return (
        "need configuring first" in low
        or "needs configuring" in low
        or ("open /plugin" in low and "mcp" in low)
    )


def sanitize_profile_key(raw: str | None) -> str:
    """Ověří bezpečný klíč profilu účtu (`default`, `pracovni`, `osobni`, ...)."""
    cleaned = (raw or "").strip().lower()
    if cleaned and _PROFILE_KEY_RE.fullmatch(cleaned):
        return cleaned
    return "default"


def get_profile_home_dir(settings: Settings, profile_key: str) -> Path:
    """Vrátí izolovaný HOME adresář pro zadaný profil pluginů."""
    main_home = settings.effective_home_dir
    safe_key = sanitize_profile_key(profile_key)
    if safe_key == "default":
        return main_home
    profiles_root = (main_home / "profiles").resolve()
    candidate = (main_home / "profiles" / safe_key).resolve()
    if not candidate.is_relative_to(profiles_root):
        return main_home
    return main_home / "profiles" / safe_key


def allocate_profile_key(
    plugin_name: str,
    scope: str,
    existing_accounts: list[PluginAccountRecord],
) -> str:
    """Vybere vhodný profilový klíč (sdílí stejnou oblast napříč pluginy, nekoliduje v téže)."""
    pname = plugin_name.strip().lower()
    sc = scope if scope in ("all", "pracovni", "osobni") else "all"

    used_by_same_plugin = {
        sanitize_profile_key(a.profile_key)
        for a in existing_accounts
        if a.plugin_name.lower() == pname
    }
    used_by_other_scope = {
        sanitize_profile_key(a.profile_key)
        for a in existing_accounts
        if a.scope != sc
    }

    # Preferuj existující profilový klíč stejné oblasti z jiného pluginu
    for acct in existing_accounts:
        if acct.scope == sc:
            cand = sanitize_profile_key(acct.profile_key)
            if cand not in used_by_same_plugin and cand not in used_by_other_scope:
                return cand

    if sc == "all":
        if "default" not in used_by_same_plugin and "default" not in used_by_other_scope:
            return "default"
        base = "account"
    else:
        if sc not in used_by_same_plugin and sc not in used_by_other_scope:
            return sc
        base = sc

    idx = 2
    while True:
        cand = f"{base}_{idx}"
        if cand not in used_by_same_plugin and cand not in used_by_other_scope:
            return cand
        idx += 1


def _pick_preferred_account(candidates: list[PluginAccountRecord]) -> PluginAccountRecord:
    """Vybere nejvhodnější účet (preferuje již připojený, pak výchozí/nejstarší)."""
    for acct in candidates:
        if acct.connected:
            return acct
    return candidates[0]


def select_active_accounts_for_context(
    enabled_plugins: set[str],
    accounts: list[PluginAccountRecord],
    context_mode: str = "auto",
) -> tuple[str, dict[str, PluginAccountRecord], set[str]]:
    """Vybere aktivní účty a cílový profilový HOME pro kontext chatu (`auto`/`pracovni`/`osobni`)."""
    channel = normalize_chat_channel(context_mode)
    enabled_low = {p.lower() for p in enabled_plugins}
    by_plugin: dict[str, list[PluginAccountRecord]] = {}
    for acct in accounts:
        by_plugin.setdefault(acct.plugin_name.lower(), []).append(acct)

    selected: dict[str, PluginAccountRecord] = {}
    blocked: set[str] = set()

    for pname in sorted(enabled_low):
        acct_list = by_plugin.get(pname)
        if not acct_list:
            continue
        active = [a for a in acct_list if a.enabled]
        if not active:
            blocked.add(pname)
            continue

        chosen: PluginAccountRecord | None = None
        if channel in ("pracovni", "osobni"):
            exact = [a for a in active if a.scope == channel]
            shared = [a for a in active if a.scope == "all"]
            if exact:
                chosen = _pick_preferred_account(exact)
            elif shared:
                chosen = _pick_preferred_account(shared)
            else:
                blocked.add(pname)
        else:
            shared = [a for a in active if a.scope == "all"]
            if shared:
                chosen = _pick_preferred_account(shared)
            else:
                default_prof = [
                    a for a in active if sanitize_profile_key(a.profile_key) == "default"
                ]
                chosen = _pick_preferred_account(default_prof or active)

        if chosen is not None:
            selected[pname] = chosen

    target_profile_key = "default"
    if channel in ("pracovni", "osobni"):
        exact_selected = [a for a in selected.values() if a.scope == channel]
        if exact_selected:
            target_profile_key = sanitize_profile_key(
                _pick_preferred_account(exact_selected).profile_key
            )
        elif selected:
            target_profile_key = sanitize_profile_key(
                _pick_preferred_account(list(selected.values())).profile_key
            )
    elif selected:
        default_selected = [
            a for a in selected.values() if sanitize_profile_key(a.profile_key) == "default"
        ]
        if default_selected:
            target_profile_key = "default"
        else:
            target_profile_key = sanitize_profile_key(
                _pick_preferred_account(list(selected.values())).profile_key
            )

    return target_profile_key, selected, blocked


def default_permissions_for_plugin(plugin_name: str) -> dict[str, bool]:
    """Vrátí výchozí bezpečné hodnoty přepínačů oprávnění pro daný plugin."""
    item = _PLUGIN_CATALOG_BY_NAME.get(plugin_name.strip().lower())
    if item is None:
        return {}
    return {spec.key: spec.default for spec in item.permissions}


def normalize_plugin_permissions(
    plugin_name: str, raw: dict[str, Any] | str | None
) -> dict[str, bool]:
    """Sloučí uložená oprávnění s výchozí definicí (vč. zpětné migrace starších klíčů)."""
    pname = plugin_name.strip().lower()
    defaults = default_permissions_for_plugin(pname)
    if not defaults:
        return {}
    parsed: dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = {}
    elif isinstance(raw, dict):
        parsed = raw

    resolved = {
        key: bool(parsed[key]) if key in parsed and isinstance(parsed[key], bool) else default_val
        for key, default_val in defaults.items()
    }

    if pname == "gmail":
        if "reply_forward" not in parsed and isinstance(parsed.get("send"), bool):
            resolved["reply_forward"] = bool(parsed["send"])
        if isinstance(parsed.get("organize"), bool):
            org_val = bool(parsed["organize"])
            for key in ("labels", "archive", "trash", "reactions"):
                if key not in parsed:
                    resolved[key] = org_val
    elif pname == "gcalendar":
        if isinstance(parsed.get("write"), bool):
            w_val = bool(parsed["write"])
            for key in ("create", "update", "rsvp"):
                if key not in parsed:
                    resolved[key] = w_val
    elif pname == "gdrive":
        if "share" not in parsed and isinstance(parsed.get("write"), bool):
            resolved["share"] = bool(parsed["write"])

    return resolved


def _effective_permissions(
    plugin_name: str,
    configs: dict[str, PluginConfigRecord],
    selected_accounts: dict[str, PluginAccountRecord] | None = None,
) -> dict[str, bool]:
    if selected_accounts and plugin_name.lower() in selected_accounts:
        acct = selected_accounts[plugin_name.lower()]
        return normalize_plugin_permissions(plugin_name, acct.permissions_json)
    rec = configs.get(plugin_name.lower())
    return normalize_plugin_permissions(plugin_name, rec.permissions_json if rec is not None else None)


def compute_plugin_deny_rules(
    enabled_plugins: set[str],
    configs: dict[str, PluginConfigRecord],
    *,
    accounts: list[PluginAccountRecord] | None = None,
    context_mode: str = "auto",
) -> list[str]:
    """Vypočte `permissions.deny` pravidla pro `settings.json` podle účtů a kontextu chatu."""
    enabled_low = {p.lower() for p in enabled_plugins}
    selected: dict[str, PluginAccountRecord] = {}
    blocked: set[str] = set()
    if accounts is not None:
        _, selected, blocked = select_active_accounts_for_context(
            enabled_low, accounts, context_mode
        )

    rules: list[str] = []

    for cli_name in _GOOGLE_CLI_PLUGINS:
        if cli_name in blocked:
            rules.extend([f"command({cli_name} readonly)", f"command({cli_name} mutate)"])

    if "gmail" in enabled_low and "gmail" not in blocked:
        p = _effective_permissions("gmail", configs, selected)
        if not p.get("read", True):
            rules.append("command(gmail readonly)")
        if not p.get("draft", True):
            rules.extend(
                [
                    "command(gmail mutate create-draft)",
                    "command(gmail mutate update-draft)",
                ]
            )
        if not p.get("send", False):
            rules.extend(
                [
                    "command(gmail mutate send)",
                    "command(gmail mutate send-with-attachment)",
                    "command(gmail mutate send-draft)",
                ]
            )
        if not p.get("reply_forward", False):
            rules.extend(
                [
                    "command(gmail mutate reply)",
                    "command(gmail mutate forward)",
                ]
            )
        if not p.get("labels", False):
            rules.extend(
                [
                    "command(gmail mutate add-label)",
                    "command(gmail mutate remove-label)",
                    "command(gmail mutate create-label)",
                    "command(gmail mutate update-label)",
                    "command(gmail mutate mark-read)",
                    "command(gmail mutate mark-unread)",
                    "command(gmail mutate thread-modify)",
                    "command(gmail mutate batch-modify)",
                ]
            )
        if not p.get("archive", False):
            rules.append("command(gmail mutate archive)")
        if not p.get("trash", False):
            rules.extend(
                [
                    "command(gmail mutate trash)",
                    "command(gmail mutate delete-draft)",
                    "command(gmail mutate delete-label)",
                ]
            )
        if not p.get("reactions", False):
            rules.extend(
                [
                    "command(gmail mutate react)",
                    "command(gmail mutate create-reaction)",
                    "command(gmail mutate delete-reaction)",
                    "command(gmail mutate unreact)",
                ]
            )
        if not any(
            p.get(k, False)
            for k in ("draft", "send", "reply_forward", "labels", "archive", "trash", "reactions")
        ):
            rules.append("command(gmail mutate)")

    if "gcalendar" in enabled_low and "gcalendar" not in blocked:
        p = _effective_permissions("gcalendar", configs, selected)
        if not p.get("read", True):
            rules.append("command(gcalendar readonly)")
        create_ok = p.get("create", True)
        update_ok = p.get("update", True)
        rsvp_ok = p.get("rsvp", False)
        delete_ok = p.get("delete", False)
        if not create_ok and not update_ok and not rsvp_ok and not delete_ok:
            rules.append("command(gcalendar mutate)")
        else:
            if not create_ok:
                rules.extend(
                    [
                        "command(gcalendar mutate create)",
                        "command(gcalendar mutate quick-add)",
                        "command(gcalendar mutate import)",
                    ]
                )
            if not update_ok:
                rules.extend(
                    [
                        "command(gcalendar mutate update)",
                        "command(gcalendar mutate move)",
                    ]
                )
            if not rsvp_ok:
                rules.append("command(gcalendar mutate rsvp)")
            if not delete_ok:
                rules.append("command(gcalendar mutate delete)")

    if "gdrive" in enabled_low and "gdrive" not in blocked:
        p = _effective_permissions("gdrive", configs, selected)
        if not p.get("read", True):
            rules.append("command(gdrive readonly)")
        write_ok = p.get("write", False)
        share_ok = p.get("share", False)
        delete_ok = p.get("delete", False)
        if not write_ok and not share_ok and not delete_ok:
            rules.append("command(gdrive mutate)")
        else:
            if not write_ok:
                rules.extend(
                    [
                        "command(gdrive mutate upload)",
                        "command(gdrive mutate create)",
                        "command(gdrive mutate update)",
                        "command(gdrive mutate copy)",
                        "command(gdrive mutate move)",
                        "command(gdrive mutate rename)",
                    ]
                )
            if not share_ok:
                rules.extend(
                    [
                        "command(gdrive mutate share)",
                        "command(gdrive mutate unshare)",
                        "command(gdrive mutate permissions)",
                    ]
                )
            if not delete_ok:
                rules.extend(
                    [
                        "command(gdrive mutate delete)",
                        "command(gdrive mutate trash)",
                    ]
                )

    for name in ("gdocs", "gsheets", "gslides"):
        if name in enabled_low and name not in blocked:
            p = _effective_permissions(name, configs, selected)
            if not p.get("read", True):
                rules.append(f"command({name} readonly)")
            if not p.get("write", False):
                rules.append(f"command({name} mutate)")

    return list(dict.fromkeys(rules))


def should_set_agy_unattended(
    enabled_plugins: set[str],
    configs: dict[str, PluginConfigRecord],
    *,
    accounts: list[PluginAccountRecord] | None = None,
    context_mode: str = "auto",
) -> bool:
    """Vrátí True, pokud má být nastaveno `AGY_UNATTENDED=1` (tvrdá CLI blokace odesílání Gmailu)."""
    enabled_low = {p.lower() for p in enabled_plugins}
    if "gmail" not in enabled_low:
        return False
    selected: dict[str, PluginAccountRecord] = {}
    if accounts is not None:
        _, selected, blocked = select_active_accounts_for_context(
            enabled_low, accounts, context_mode
        )
        if "gmail" in blocked:
            return True
    perms = _effective_permissions("gmail", configs, selected)
    return not perms.get("send", False) and not perms.get("reply_forward", False)


def build_plugin_permissions_prompt_hint(
    enabled_plugins: set[str],
    configs: dict[str, PluginConfigRecord],
    *,
    accounts: list[PluginAccountRecord] | None = None,
    context_mode: str = "auto",
) -> str:
    """Sestaví stručnou systémovou instrukci pro agenta vč. kontextového účtu a vlastních pravidel."""
    enabled_low = {p.lower() for p in enabled_plugins}
    selected: dict[str, PluginAccountRecord] = {}
    blocked: set[str] = set()
    if accounts is not None:
        _, selected, blocked = select_active_accounts_for_context(
            enabled_low, accounts, context_mode
        )

    entries: list[str] = []
    seen_names: set[str] = set()

    def _append_entry(pname: str, has_perm_schema: bool) -> None:
        if pname in blocked:
            entries.append(f"{pname}: DISABLED in this chat area")
            return
        acct = selected.get(pname)
        parts: list[str] = []
        if acct is not None:
            ident = " / ".join(x for x in (acct.label, acct.account_email) if x)
            if ident:
                parts.append(f"account={ident}")
        if has_perm_schema:
            perms = _effective_permissions(pname, configs, selected)
            allowed = [k for k, v in perms.items() if v]
            denied = [k for k, v in perms.items() if not v]
            if denied:
                allow_s = ", ".join(allowed) if allowed else "none"
                deny_s = ", ".join(denied)
                parts.append(f"ALLOWED({allow_s}), DENIED({deny_s})")
        if acct is not None and acct.custom_rules.strip():
            clean_rule = " ".join(acct.custom_rules.split())[:500]
            parts.append(f"RULES: {clean_rule}")
        if parts:
            entries.append(f"{pname}: {'; '.join(parts)}")

    for item in PLUGIN_CATALOG:
        low = item.name.lower()
        if low not in enabled_low:
            continue
        seen_names.add(low)
        _append_entry(low, bool(item.permissions))

    for pname in sorted(enabled_low - seen_names):
        if pname in selected or pname in blocked:
            _append_entry(pname, False)

    if not entries:
        return ""
    joined = " | ".join(entries)
    return (
        "[Plugin permissions (STRICTLY ENFORCED – never run denied actions or violate custom RULES, "
        "inform the user they can change them in Settings → Integrations): "
        f"{joined}]"
    )


_LOGO_MIME_BY_EXT: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_manifest_logo(plugin_dir: Path, raw_logo: Any) -> str:
    """Bezpečně převede hodnotu `logo` z `plugin.json` na HTTPS URL nebo lokální `data:` URI."""
    if not isinstance(raw_logo, str):
        return ""
    cleaned = raw_logo.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("https://"):
        return cleaned[:500]
    if cleaned.startswith(("/", "\\", "http://")) or ".." in cleaned:
        return ""
    try:
        root_resolved = plugin_dir.resolve()
        candidate = plugin_dir / cleaned
        if candidate.is_symlink() or not candidate.is_file():
            return ""
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root_resolved):
            return ""
        mime = _LOGO_MIME_BY_EXT.get(resolved.suffix.lower())
        if not mime:
            return ""
        raw_bytes = resolved.read_bytes()
        if not raw_bytes or len(raw_bytes) > 65536:
            return ""
        encoded = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return ""


def detect_installed_plugins_from_disk(home_dir: Path) -> dict[str, dict[str, Any]]:
    """Načte nainstalované pluginy přímo z `~/.gemini/config/plugins/`."""
    plugins_root = home_dir / ".gemini" / "config" / "plugins"
    if not plugins_root.is_dir() or plugins_root.is_symlink():
        return {}

    found: dict[str, dict[str, Any]] = {}
    try:
        entries = sorted(plugins_root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return {}

    for folder in entries:
        if not folder.is_dir() or folder.is_symlink():
            continue
        manifest_path = folder / "plugin.json"
        manifest: dict[str, Any] = {}
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, json.JSONDecodeError):
                manifest = {}

        name = str(manifest.get("name") or folder.name).strip()
        if not name:
            continue
        mcp_names: list[str] = []
        mcp_path = folder / "mcp_config.json"
        if mcp_path.is_file() and not mcp_path.is_symlink():
            try:
                mcp_raw = json.loads(mcp_path.read_text(encoding="utf-8"))
                srvs = mcp_raw.get("mcpServers") if isinstance(mcp_raw, dict) else None
                if isinstance(srvs, dict):
                    mcp_names = [str(k) for k in srvs.keys()]
            except (OSError, json.JSONDecodeError):
                mcp_names = []

        found[name.lower()] = {
            "name": name,
            "display_name": str(manifest.get("displayName") or name),
            "version": str(manifest.get("version") or ""),
            "description": str(manifest.get("description") or ""),
            "logo_url": _resolve_manifest_logo(folder, manifest.get("logo")),
            "enabled": not bool(manifest.get("disabled", False)),
            "path": str(folder),
            "has_auth_json": (folder / "auth.json").is_file(),
            "mcp_servers": mcp_names,
        }
    return found


def detect_mcp_oauth_connected_plugins(home_dir: Path) -> set[str]:
    """Bezpečně přečte názvy OAuth token klíčů pro detekci připojených MCP pluginů."""
    connected: set[str] = set()
    candidates = (
        home_dir / ".gemini" / "antigravity" / "mcp_oauth_tokens.json",
        home_dir / ".gemini" / "antigravity-cli" / "mcp_oauth_tokens.json",
    )
    for token_file in candidates:
        if not token_file.is_file() or token_file.is_symlink():
            continue
        try:
            data = json.loads(token_file.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(key, str) and key.strip() and val:
                    connected.add(key.strip().lower())
    return connected


class PluginManager:
    """Správa Antigravity pluginů, jejich OAuth připojení v PTY a oprávnění."""

    def __init__(self, settings: Settings, db: AssistantDB) -> None:
        self.settings = settings
        self.db = db
        self._connect_state = PluginConnectState()
        self._raw_output = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._nav_task: asyncio.Task[None] | None = None

    async def _run_agy_plugin_cmd(
        self, *cli_args: str, timeout: float = 45.0
    ) -> tuple[int, str]:
        """Spustí `agy plugin <cli_args...>` bezpečně bez shellu s prostředím asistenta."""
        eff_home = self.settings.effective_home_dir
        eff_ws = self.settings.workspace_dir
        eff_home.mkdir(parents=True, exist_ok=True)
        eff_ws.mkdir(parents=True, exist_ok=True)
        env = self.settings.subprocess_env(gemini_api_key="", home_dir=eff_home)

        proc = await asyncio.create_subprocess_exec(
            self.settings.agy_bin,
            "plugin",
            *cli_args,
            cwd=str(eff_ws),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = clean_terminal_output(stdout_b.decode("utf-8", errors="replace")).strip()
        return int(proc.returncode or 0), out

    async def list_installed(self) -> list[dict[str, Any]]:
        """Vrátí seznam nainstalovaných pluginů z `agy plugin list --json` s diskovým fallbackem."""
        disk_map = detect_installed_plugins_from_disk(self.settings.effective_home_dir)
        try:
            rc, out = await self._run_agy_plugin_cmd("list", "--json", timeout=15.0)
            if rc == 0 and out:
                parsed = json.loads(out)
                if isinstance(parsed, list):
                    result: list[dict[str, Any]] = []
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        raw_name = str(item.get("name") or "").strip()
                        if not raw_name:
                            continue
                        disk_info = disk_map.get(raw_name.lower(), {})
                        result.append(
                            {
                                "name": raw_name,
                                "enabled": bool(item.get("enabled", True)),
                                "version": str(item.get("version") or disk_info.get("version") or ""),
                                "source": str(item.get("source") or ""),
                                "path": str(item.get("path") or disk_info.get("path") or ""),
                                "skills": item.get("skills") if isinstance(item.get("skills"), list) else [],
                                "mcpServers": item.get("mcpServers") if isinstance(item.get("mcpServers"), list) else [],
                                "display_name": disk_info.get("display_name") or raw_name,
                                "description": disk_info.get("description") or "",
                                "logo_url": disk_info.get("logo_url") or "",
                                "has_auth_json": bool(disk_info.get("has_auth_json", False)),
                                "disk_mcp_servers": disk_info.get("mcp_servers") or [],
                            }
                        )
                    return result
        except (FileNotFoundError, asyncio.TimeoutError, OSError, json.JSONDecodeError):
            pass

        return [
            {
                "name": info["name"],
                "enabled": info["enabled"],
                "version": info["version"],
                "source": "",
                "path": info["path"],
                "skills": [],
                "mcpServers": [],
                "display_name": info["display_name"],
                "description": info["description"],
                "logo_url": info["logo_url"],
                "has_auth_json": info["has_auth_json"],
                "disk_mcp_servers": info["mcp_servers"],
            }
            for info in disk_map.values()
        ]

    def _account_public_dict(
        self, acct: PluginAccountRecord, *, connected_override: bool | None = None
    ) -> dict[str, Any]:
        pname = acct.plugin_name.lower()
        perms = normalize_plugin_permissions(pname, acct.permissions_json)
        return {
            "id": acct.id,
            "plugin_name": acct.plugin_name,
            "label": acct.label,
            "scope": acct.scope,
            "profile_key": sanitize_profile_key(acct.profile_key),
            "account_email": acct.account_email,
            "permissions": perms,
            "custom_rules": acct.custom_rules,
            "connected": acct.connected if connected_override is None else connected_override,
            "connected_at": acct.connected_at,
            "enabled": acct.enabled,
        }

    def ensure_default_account(self, plugin_name: str) -> PluginAccountRecord:
        """Zajistí alespoň jeden výchozí účet v `plugin_accounts` (s migrací z `plugin_configs`)."""
        safe_name = validate_plugin_name(plugin_name).lower()
        existing = self.db.list_plugin_accounts(safe_name)
        if existing:
            return existing[0]
        rec = self.db.get_plugin_config(safe_name)
        if rec is not None:
            perms = normalize_plugin_permissions(safe_name, rec.permissions_json)
            perms_json = json.dumps(perms, ensure_ascii=False) if perms else "{}"
            return self.db.create_plugin_account(
                safe_name,
                label="",
                scope="all",
                profile_key="default",
                account_email=rec.account_label,
                permissions_json=perms_json,
                custom_rules="",
                connected=rec.connected,
                connected_at=rec.connected_at,
                enabled=True,
            )
        defaults = default_permissions_for_plugin(safe_name)
        perms_json = json.dumps(defaults, ensure_ascii=False) if defaults else "{}"
        return self.db.create_plugin_account(
            safe_name,
            label="",
            scope="all",
            profile_key="default",
            permissions_json=perms_json,
            enabled=True,
        )

    def _cleanup_orphan_profile_dir(self, profile_key: str) -> None:
        """Smaže izolovanou profilovou složku, pokud ji už nepoužívá žádný účet."""
        safe_key = sanitize_profile_key(profile_key)
        if safe_key == "default":
            return
        remaining = self.db.list_plugin_accounts()
        if any(sanitize_profile_key(a.profile_key) == safe_key for a in remaining):
            return
        prof_dir = get_profile_home_dir(self.settings, safe_key)
        if prof_dir != self.settings.effective_home_dir and prof_dir.is_dir():
            shutil.rmtree(prof_dir, ignore_errors=True)

    async def list_catalog_with_status(self) -> dict[str, Any]:
        """Sloučí kurátorovaný katalog, nainstalované pluginy, účty a stav připojení."""
        installed_list = await self.list_installed()
        installed_by_name = {item["name"].lower(): item for item in installed_list}
        disk_map = detect_installed_plugins_from_disk(self.settings.effective_home_dir)
        db_configs = self.db.list_plugin_configs()
        all_accounts = self.db.list_plugin_accounts()
        accounts_by_plugin: dict[str, list[PluginAccountRecord]] = {}
        for acct in all_accounts:
            accounts_by_plugin.setdefault(acct.plugin_name.lower(), []).append(acct)
        oauth_tokens = detect_mcp_oauth_connected_plugins(self.settings.effective_home_dir)

        plugins_out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _is_connected(name_low: str, inst_info: dict[str, Any] | None, disk_info: dict[str, Any] | None) -> bool:
            rec = db_configs.get(name_low)
            if rec is not None and rec.connected:
                return True
            if name_low in oauth_tokens:
                return True
            for mcp_key in (inst_info or {}).get("disk_mcp_servers") or (disk_info or {}).get("mcp_servers") or []:
                low_key = str(mcp_key).lower()
                if low_key in oauth_tokens or f"{name_low}_{low_key}" in oauth_tokens:
                    return True
            return False

        def _build_accounts_payload(
            pname_low: str, inst_info: dict[str, Any] | None, disk_info: dict[str, Any] | None
        ) -> tuple[list[dict[str, Any]], bool, str]:
            accts = list(accounts_by_plugin.get(pname_low) or [])
            if inst_info is not None and not accts:
                created = self.ensure_default_account(pname_low)
                accts = [created]
                accounts_by_plugin[pname_low] = accts

            disk_connected = _is_connected(pname_low, inst_info, disk_info) if inst_info is not None else False
            out_accts: list[dict[str, Any]] = []
            for acct in accts:
                eff_conn = acct.connected or (
                    len(accts) == 1 and acct.profile_key == "default" and disk_connected
                )
                out_accts.append(self._account_public_dict(acct, connected_override=eff_conn))

            any_conn = any(a["connected"] for a in out_accts) or disk_connected
            first_label = ""
            for a in out_accts:
                if a["account_email"] or a["label"]:
                    first_label = a["account_email"] or a["label"]
                    break
            if not first_label:
                rec = db_configs.get(pname_low)
                first_label = rec.account_label if rec is not None else ""
            return out_accts, any_conn, first_label

        for item in PLUGIN_CATALOG:
            low = item.name.lower()
            seen.add(low)
            inst = installed_by_name.get(low)
            disk = disk_map.get(low)
            rec = db_configs.get(low)
            accts_out, any_conn, first_label = _build_accounts_payload(low, inst, disk)
            primary_perms = (
                accts_out[0]["permissions"]
                if accts_out
                else normalize_plugin_permissions(item.name, rec.permissions_json if rec else None)
            )
            plugins_out.append(
                {
                    "name": item.name,
                    "display_name": item.display_name,
                    "install_target": item.install_target,
                    "marketplace": item.marketplace,
                    "category": item.category,
                    "featured": low in FEATURED_PLUGIN_NAMES,
                    "icon": item.icon,
                    "logo_url": str(
                        (inst or {}).get("logo_url")
                        or (disk or {}).get("logo_url")
                        or item.logo_url
                    ),
                    "desc_key": item.desc_key,
                    "description": item.description,
                    "supports_oauth": item.supports_oauth,
                    "installed": inst is not None,
                    "enabled": bool(inst["enabled"]) if inst is not None else False,
                    "version": str((inst or {}).get("version") or ""),
                    "connected": any_conn if inst is not None else False,
                    "connected_at": rec.connected_at if rec is not None else "",
                    "account_label": first_label,
                    "accounts": accts_out,
                    "permissions_schema": [asdict(sp) for sp in item.permissions],
                    "permissions": primary_perms,
                    "custom": False,
                }
            )

        for low, inst in installed_by_name.items():
            if low in seen:
                continue
            disk = disk_map.get(low, {})
            rec = db_configs.get(low)
            has_mcp = bool(inst.get("mcpServers") or disk.get("mcp_servers"))
            supports_oauth = bool(inst.get("has_auth_json") or has_mcp)
            accts_out, any_conn, first_label = _build_accounts_payload(low, inst, disk)
            plugins_out.append(
                {
                    "name": inst["name"],
                    "display_name": inst.get("display_name") or inst["name"],
                    "install_target": inst["name"],
                    "marketplace": str(inst.get("source") or "custom"),
                    "category": "custom",
                    "featured": False,
                    "icon": "🧩",
                    "logo_url": str(inst.get("logo_url") or disk.get("logo_url") or ""),
                    "desc_key": "",
                    "description": str(inst.get("description") or ""),
                    "supports_oauth": supports_oauth,
                    "installed": True,
                    "enabled": bool(inst.get("enabled", True)),
                    "version": str(inst.get("version") or ""),
                    "connected": any_conn,
                    "connected_at": rec.connected_at if rec is not None else "",
                    "account_label": first_label,
                    "accounts": accts_out,
                    "permissions_schema": [],
                    "permissions": {},
                    "custom": True,
                }
            )

        return {"plugins": plugins_out}

    async def install_plugin(self, target: str) -> dict[str, Any]:
        """Nainstaluje plugin pomocí `agy plugin install` a zapíše výchozí oprávnění i účet."""
        safe_target = validate_plugin_target(target)
        try:
            rc, out = await self._run_agy_plugin_cmd("install", safe_target, timeout=60.0)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Binárka '{self.settings.agy_bin}' nebyla nalezena.") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Instalace pluginu vypršela (timeout).") from exc
        if rc != 0:
            raise RuntimeError(out or f"Instalace pluginu '{safe_target}' selhala (kód {rc}).")

        base_name = safe_target.split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if base_name.endswith(".git"):
            base_name = base_name[:-4]
        low = base_name.lower()
        defaults = default_permissions_for_plugin(low)
        if defaults and self.db.get_plugin_config(low) is None:
            self.db.upsert_plugin_config(
                low, permissions_json=json.dumps(defaults, ensure_ascii=False)
            )
        self.ensure_default_account(low)
        return {"status": "ok", "target": safe_target, "output": out}

    async def uninstall_plugin(self, name: str) -> dict[str, Any]:
        """Odinstaluje plugin a smaže jeho účty, konfiguraci v DB i případné osiřelé profily."""
        safe_name = validate_plugin_name(name)
        existing_accounts = self.db.list_plugin_accounts(safe_name)
        profile_keys = {acct.profile_key for acct in existing_accounts}
        try:
            rc, out = await self._run_agy_plugin_cmd("uninstall", safe_name, timeout=30.0)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Binárka '{self.settings.agy_bin}' nebyla nalezena.") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Odinstalace pluginu vypršela.") from exc
        if rc != 0:
            raise RuntimeError(out or f"Odinstalace pluginu '{safe_name}' selhala (kód {rc}).")
        self.db.delete_plugin_accounts_for_plugin(safe_name)
        self.db.delete_plugin_config(safe_name)
        for pkey in profile_keys:
            self._cleanup_orphan_profile_dir(pkey)
        return {"status": "ok", "plugin": safe_name, "output": out}

    async def set_plugin_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """Zapne nebo vypne nainstalovaný plugin v `agy` (rozpozná nutnost MCP konfigurace)."""
        safe_name = validate_plugin_name(name)
        subcmd = "enable" if enabled else "disable"
        try:
            rc, out = await self._run_agy_plugin_cmd(subcmd, safe_name, timeout=30.0)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Binárka '{self.settings.agy_bin}' nebyla nalezena.") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Změna stavu pluginu vypršela.") from exc
        if rc != 0:
            if enabled and is_mcp_config_required_error(out):
                return {
                    "status": "needs_config",
                    "needs_config": True,
                    "plugin": safe_name,
                    "enabled": False,
                    "detail": out,
                }
            raise RuntimeError(out or f"Operace '{subcmd}' pro '{safe_name}' selhala (kód {rc}).")
        return {"status": "ok", "plugin": safe_name, "enabled": enabled, "output": out}

    def create_account(
        self,
        plugin_name: str,
        *,
        label: str = "",
        scope: str = "all",
        permissions: dict[str, bool] | None = None,
        custom_rules: str = "",
    ) -> dict[str, Any]:
        """Přidá další účet pluginu s automatickým nekonfliktním profilovým klíčem."""
        safe_name = validate_plugin_name(plugin_name).lower()
        clean_scope = scope if scope in ("all", "pracovni", "osobni") else "all"
        existing = self.db.list_plugin_accounts()
        profile_key = allocate_profile_key(safe_name, clean_scope, existing)
        defaults = default_permissions_for_plugin(safe_name)
        if defaults:
            perms = normalize_plugin_permissions(safe_name, permissions)
            perms_json = json.dumps(perms, ensure_ascii=False)
        else:
            perms_json = "{}"

        created = self.db.create_plugin_account(
            safe_name,
            label=label,
            scope=clean_scope,
            profile_key=profile_key,
            permissions_json=perms_json,
            custom_rules=custom_rules,
            enabled=True,
        )
        return {"status": "ok", "account": self._account_public_dict(created)}

    def update_account(
        self,
        plugin_name: str,
        account_id: int,
        *,
        label: str | None = None,
        scope: str | None = None,
        enabled: bool | None = None,
        permissions: dict[str, bool] | None = None,
        custom_rules: str | None = None,
    ) -> dict[str, Any]:
        """Uloží nastavení, oblast, oprávnění a pravidla konkrétního účtu pluginu."""
        safe_name = validate_plugin_name(plugin_name).lower()
        acct = self.db.get_plugin_account(account_id)
        if acct is None or acct.plugin_name.lower() != safe_name:
            raise ValueError(f"Účet pluginu '{safe_name}' (#{account_id}) nebyl nalezen.")

        perms_json: str | None = None
        if permissions is not None:
            defaults = default_permissions_for_plugin(safe_name)
            if defaults:
                merged = {
                    **normalize_plugin_permissions(safe_name, acct.permissions_json),
                    **{k: bool(v) for k, v in permissions.items() if isinstance(v, bool)},
                }
                norm = normalize_plugin_permissions(safe_name, merged)
                perms_json = json.dumps(norm, ensure_ascii=False)
            else:
                perms_json = "{}"

        updated = self.db.update_plugin_account(
            account_id,
            label=label,
            scope=scope,
            enabled=enabled,
            permissions_json=perms_json,
            custom_rules=custom_rules,
        )
        assert updated is not None
        if sanitize_profile_key(updated.profile_key) == "default":
            self.db.upsert_plugin_config(
                safe_name,
                permissions_json=updated.permissions_json,
            )
        return {"status": "ok", "account": self._account_public_dict(updated)}

    def delete_account(self, plugin_name: str, account_id: int) -> dict[str, Any]:
        """Smaže účet pluginu a uklidí izolovanou profilovou složku, pokud ji už nikdo nepoužívá."""
        safe_name = validate_plugin_name(plugin_name).lower()
        acct = self.db.get_plugin_account(account_id)
        if acct is None or acct.plugin_name.lower() != safe_name:
            raise ValueError(f"Účet pluginu '{safe_name}' (#{account_id}) nebyl nalezen.")
        self.db.delete_plugin_account(account_id)
        self._cleanup_orphan_profile_dir(acct.profile_key)
        return {"status": "ok", "plugin": safe_name, "deleted_account_id": account_id}

    def update_permissions(self, name: str, permissions: dict[str, bool]) -> dict[str, Any]:
        """Uloží přepínače oprávnění pro daný plugin (primární účet i `plugin_configs`)."""
        safe_name = validate_plugin_name(name).lower()
        normalized = normalize_plugin_permissions(safe_name, permissions)
        if not normalized:
            raise ValueError(f"Plugin '{safe_name}' nemá nastavitelné přepínače oprávnění.")
        perms_json = json.dumps(normalized, ensure_ascii=False)
        self.db.upsert_plugin_config(safe_name, permissions_json=perms_json)
        primary = self.ensure_default_account(safe_name)
        self.db.update_plugin_account(primary.id, permissions_json=perms_json)
        return {"status": "ok", "plugin": safe_name, "permissions": normalized}

    def get_connect_state(self) -> PluginConnectState:
        """Vrátí aktuální stav PTY relace připojování účtu k pluginu."""
        if self._proc is not None and self._proc.poll() is not None:
            self._connect_state.running = False
            self._connect_state.exit_code = self._proc.returncode
        return PluginConnectState(
            running=self._connect_state.running,
            plugin_name=self._connect_state.plugin_name,
            flow_type=self._connect_state.flow_type,
            auth_url=self._connect_state.auth_url,
            urls=list(self._connect_state.urls),
            callback_port=self._connect_state.callback_port,
            callback_path=self._connect_state.callback_path,
            expected_state=self._connect_state.expected_state,
            output=self._connect_state.output,
            connected=self._connect_state.connected,
            already_connected=self._connect_state.already_connected,
            account=self._connect_state.account,
            account_id=self._connect_state.account_id,
            profile_key=self._connect_state.profile_key,
            exit_code=self._connect_state.exit_code,
            error=self._connect_state.error,
        )

    async def start_connect(
        self,
        plugin_name: str,
        *,
        install_target: str | None = None,
        force_reconnect: bool = False,
        account_id: int | None = None,
        interactive_config: bool = False,
    ) -> PluginConnectState:
        """Spustí interaktivní `agy` relaci v PTY pro OAuth přihlášení nebo MCP konfiguraci."""
        safe_name = validate_plugin_name(plugin_name)
        await self.stop_connect()

        if pty is None:
            self._connect_state = PluginConnectState(
                running=False,
                plugin_name=safe_name,
                error="Interaktivní PTY připojení účtu pluginu vyžaduje Linux/Docker kontejner.",
            )
            return self.get_connect_state()

        # Zajisti instalaci (a případně povolení, pokud nevyžaduje interaktivní MCP konfiguraci)
        installed = {item["name"].lower(): item for item in await self.list_installed()}
        inst = installed.get(safe_name.lower())
        if inst is None:
            target = install_target or (
                _PLUGIN_CATALOG_BY_NAME[safe_name.lower()].install_target
                if safe_name.lower() in _PLUGIN_CATALOG_BY_NAME
                else safe_name
            )
            await self.install_plugin(target)
            installed = {item["name"].lower(): item for item in await self.list_installed()}
            inst = installed.get(safe_name.lower())
        if inst is not None and not inst.get("enabled", True) and not interactive_config:
            toggle_res = await self.set_plugin_enabled(safe_name, True)
            if toggle_res.get("needs_config"):
                interactive_config = True

        if account_id is not None:
            acct = self.db.get_plugin_account(account_id)
            if acct is None or acct.plugin_name.lower() != safe_name.lower():
                raise ValueError(f"Účet pluginu '{safe_name}' (#{account_id}) nebyl nalezen.")
        else:
            acct = self.ensure_default_account(safe_name)

        prof_key = sanitize_profile_key(acct.profile_key)
        from app.user_env import ensure_profile_home_seeded

        eff_home = ensure_profile_home_seeded(self.settings, prof_key)
        eff_ws = self.settings.workspace_dir
        eff_home.mkdir(parents=True, exist_ok=True)
        eff_ws.mkdir(parents=True, exist_ok=True)

        master_fd, slave_fd = pty.openpty()
        if fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 2000, 0, 0))

        env = self.settings.subprocess_env(gemini_api_key="", home_dir=eff_home)
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = "2000"
        env["LINES"] = "40"

        try:
            proc = subprocess.Popen(
                [self.settings.agy_bin],
                cwd=str(eff_ws),
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            self._connect_state = PluginConnectState(
                running=False,
                plugin_name=safe_name,
                account_id=acct.id,
                profile_key=prof_key,
                error=f"Nepodařilo se spustit agy: {exc}",
            )
            return self.get_connect_state()

        os.close(slave_fd)
        self._proc = proc
        self._master_fd = master_fd
        self._raw_output = ""
        self._connect_state = PluginConnectState(
            running=True,
            plugin_name=safe_name,
            flow_type="interactive_config" if interactive_config else "",
            account_id=acct.id,
            profile_key=prof_key,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        eff_force_reconnect = bool(force_reconnect or (prof_key != "default" and not acct.connected))
        self._nav_task = asyncio.create_task(
            self._auto_navigate_to_signin(
                safe_name,
                force_reconnect=eff_force_reconnect,
                account_id=acct.id,
                profile_key=prof_key,
                interactive_config=interactive_config,
            )
        )
        return self.get_connect_state()

    async def _read_loop(self) -> None:
        assert self._master_fd is not None
        assert self._proc is not None
        loop = asyncio.get_running_loop()

        try:
            while True:
                try:
                    chunk = await loop.run_in_executor(None, os.read, self._master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                raw_text = chunk.decode("utf-8", errors="replace")
                self._raw_output = (self._raw_output + raw_text)[-48000:]
                cleaned = clean_terminal_output(self._raw_output)[-16000:]
                self._connect_state.output = cleaned
                urls = extract_urls(cleaned)
                osc_urls = extract_osc8_urls(self._raw_output)
                merged = list(dict.fromkeys([*osc_urls, *urls]))
                self._connect_state.urls = merged
                info = extract_oauth_callback_info(merged)
                if info["auth_url"]:
                    self._connect_state.auth_url = info["auth_url"]
                    self._connect_state.flow_type = info["flow_type"]
                    self._connect_state.callback_port = info["callback_port"]
                    self._connect_state.callback_path = info["callback_path"]
                    self._connect_state.expected_state = info["expected_state"]
                if self._proc.poll() is not None:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        finally:
            if self._proc is not None:
                rc = self._proc.poll()
                if rc is None:
                    rc = await loop.run_in_executor(None, self._proc.wait)
                self._connect_state.exit_code = rc
            self._connect_state.running = False
            self._close_fd()

    def _write_pty(self, data: str) -> None:
        if self._master_fd is not None and self._connect_state.running:
            os.write(self._master_fd, data.encode("utf-8"))

    def _persist_account_connected(
        self,
        plugin_name: str,
        *,
        account_id: int | None,
        profile_key: str,
        account_email: str = "",
    ) -> None:
        if account_id is not None:
            kwargs: dict[str, Any] = {"connected": True}
            if account_email.strip():
                kwargs["account_email"] = account_email.strip()
            self.db.update_plugin_account(account_id, **kwargs)
        if sanitize_profile_key(profile_key) == "default":
            self.db.upsert_plugin_config(
                plugin_name,
                connected=True,
                account_label=account_email if account_email.strip() else None,
            )

    async def _auto_navigate_to_signin(
        self,
        plugin_name: str,
        *,
        force_reconnect: bool = False,
        account_id: int | None = None,
        profile_key: str = "default",
        interactive_config: bool = False,
    ) -> None:
        """Automaticky otevře `/plugin`, vybere plugin a spustí OAuth přihlášení nebo MCP nastavení."""
        try:
            # 1. Počkej, až TUI naběhne (první výstup + krátká prodleva nebo max 8 s)
            for _ in range(40):
                if not self._connect_state.running or self._master_fd is None:
                    return
                if self._connect_state.output.strip():
                    await asyncio.sleep(1.2)
                    break
                await asyncio.sleep(0.2)
            else:
                await asyncio.sleep(0.5)

            if not self._connect_state.running or self._connect_state.auth_url:
                return
            self._write_pty("/plugin\r")

            # 2. Vyčkej na obrazovku Správce pluginů
            for _ in range(40):
                if not self._connect_state.running or self._connect_state.auth_url:
                    return
                low = self._connect_state.output.lower()
                if "installed" in low or "discover" in low or "plugins" in low:
                    break
                await asyncio.sleep(0.2)

            if not self._connect_state.running or self._connect_state.auth_url:
                return
            await asyncio.sleep(0.5)
            # Napiš název pluginu pro odfiltrování v záložce Installed a rozbal detail
            self._write_pty(plugin_name)
            await asyncio.sleep(0.6)
            if not self._connect_state.running or self._connect_state.auth_url:
                return
            self._write_pty("\r")

            # 3. Vyčkej až 4 s na rozbalení detailu (nebo rovnou OAuth URL)
            for _ in range(20):
                if not self._connect_state.running or self._connect_state.auth_url:
                    return
                low = self._connect_state.output.lower()
                if any(
                    tok in low
                    for tok in (
                        "marketplace:",
                        "path:",
                        "version:",
                        "sign-in:",
                        "sign in",
                        "sign out",
                        "enable",
                        "mcp",
                    )
                ):
                    await asyncio.sleep(0.3)
                    break
                await asyncio.sleep(0.2)

            if interactive_config:
                # U pluginů vyžadujících konfiguraci MCP (např. n8n) necháme PTY otevřené pro uživatele
                return

            if not self._connect_state.running or self._connect_state.auth_url:
                return

            is_signed_in, account = extract_signed_in_account(self._connect_state.output)
            if is_signed_in and not force_reconnect:
                self._persist_account_connected(
                    plugin_name,
                    account_id=account_id,
                    profile_key=profile_key,
                    account_email=account,
                )
                self._connect_state.connected = True
                self._connect_state.already_connected = True
                self._connect_state.account = account
                if self._proc is not None and self._proc.poll() is None:
                    with contextlib.suppress(OSError):
                        self._proc.terminate()
                return

            if is_signed_in and force_reconnect:
                # Stávající účet odhlásit (`Confirm Sign Out` -> `y`) a pak spustit nové přihlášení
                self._write_pty("\r")
                for _ in range(15):
                    if not self._connect_state.running:
                        return
                    if "confirm sign out" in self._connect_state.output.lower():
                        break
                    await asyncio.sleep(0.2)
                self._write_pty("y")
                await asyncio.sleep(1.5)
                self._raw_output = ""
                self._connect_state.output = ""
                if not self._connect_state.running or self._connect_state.auth_url:
                    return

            # 4. Spusť přihlášení (`Sign in`) a čekej na OAuth URL
            self._write_pty("\r")
            nudged_down = False
            for step in range(25):
                if not self._connect_state.running or self._connect_state.auth_url:
                    return
                if not force_reconnect:
                    signed_now, acct_now = extract_signed_in_account(self._connect_state.output)
                    if signed_now:
                        self._persist_account_connected(
                            plugin_name,
                            account_id=account_id,
                            profile_key=profile_key,
                            account_email=acct_now,
                        )
                        self._connect_state.connected = True
                        self._connect_state.already_connected = True
                        self._connect_state.account = acct_now
                        if self._proc is not None and self._proc.poll() is None:
                            with contextlib.suppress(OSError):
                                self._proc.terminate()
                        return
                if not nudged_down and step >= 12:
                    self._write_pty("\x1b[B\r")
                    nudged_down = True
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return
        except OSError:
            return

    async def send_connect_key(self, key: str) -> PluginConnectState:
        """Pošle povolenou ovládací klávesu do běžící connect relace."""
        seq = _CONNECT_KEYS.get((key or "").strip().lower())
        if seq is None:
            raise ValueError(f"Nepodporovaná klávesa: {key!r}")
        if not self._connect_state.running or self._master_fd is None:
            raise RuntimeError("Relace připojování pluginu právě neběží.")
        os.write(self._master_fd, seq.encode("utf-8"))
        await asyncio.sleep(0.35)
        return self.get_connect_state()

    async def send_connect_input(self, value: str = "") -> PluginConnectState:
        """Pošle textový vstup (např. autorizační kód nebo volbu) do PTY relace."""
        if not self._connect_state.running or self._master_fd is None:
            raise RuntimeError("Relace připojování pluginu právě neběží.")
        payload = (value.rstrip("\r\n") + "\r").encode("utf-8")
        os.write(self._master_fd, payload)
        await asyncio.sleep(0.5)
        return self.get_connect_state()

    async def submit_callback_url(self, callback_url: str) -> dict[str, Any]:
        """Ověří a lokálně předá `http://127.0.0.1:<port>/callback?...` běžícímu `agy`."""
        state = self.get_connect_state()
        if not state.running or state.callback_port is None:
            raise ValueError(
                "Aktuálně nečeká žádný lokální přihlašovací port. Spusťte nejprve Připojit u pluginu."
            )
        validated = validate_localhost_callback_url(
            callback_url,
            expected_port=state.callback_port,
            expected_path=state.callback_path or "/callback",
            expected_state=state.expected_state,
        )
        try:
            http_status, location, body = await asyncio.to_thread(
                forward_localhost_callback_sync, validated
            )
        except OSError as exc:
            raise RuntimeError(
                f"Nepodařilo se spojit s lokálním přihlašovacím portem agy ({state.callback_port}): {exc}"
            ) from exc

        if http_status >= 400 or "error=" in location.lower():
            raise RuntimeError(
                f"Dokončení přihlášení selhalo (HTTP {http_status}): {body or location}"
            )

        # Dej agy krátký čas na výměnu kódu za token a zapsání na disk
        await asyncio.sleep(1.5)
        plugin_name = state.plugin_name
        if plugin_name:
            self._persist_account_connected(
                plugin_name,
                account_id=state.account_id,
                profile_key=state.profile_key,
                account_email=state.account,
            )
        self._connect_state.connected = True
        await self.stop_connect()
        return {
            "status": "ok",
            "plugin": plugin_name,
            "connected": True,
            "http_status": http_status,
            "location": location,
        }

    def _close_fd(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def stop_connect(self) -> None:
        """Bezpečně ukončí běžící PTY relaci připojování pluginu."""
        if self._nav_task is not None and not self._nav_task.done():
            self._nav_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._nav_task
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(OSError):
                self._proc.terminate()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._close_fd()
        self._proc = None
        self._reader_task = None
        self._nav_task = None
        self._raw_output = ""
        self._connect_state.running = False
