from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import signal
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
from app.db import AssistantDB, McpServerRecord, PluginAccountRecord, PluginConfigRecord

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
    "left": "\x1b[D",
    "right": "\x1b[C",
    "tab": "\t",
    "esc": "\x1b",
    "space": " ",
    "ctrl_c": "\x03",
}

_TERMINAL_AUTO_RESPONSE_RE = re.compile(
    r"^(?:"
    r"\x1b\[[IO]"  # Focus In / Focus Out (DECSET 1004)
    r"|\x1b\[\??\d+;\d+R"  # Cursor Position Report (CPR / DECXCPR)
    r"|\x1b\[\??\d+(?:;\d+)*n"  # Device Status Report (DSR)
    r"|\x1b\[[?>=]?\d*(?:;\d*)*c"  # Device Attributes (DA1 / DA2 / DA3)
    r"|\x1b\[\??\d+;\d+\$y"  # Mode Report (DECRPM)
    r"|\x1b\[\?\d+u"  # Kitty keyboard protocol query reply
    r"|\x1b\[(?:4|6|8);\d+;\d+t"  # XTWINOPS reply
    r"|\x1b\[<\d+;\d+;\d+[Mm]"  # SGR mouse motion / wheel / click
    r"|\x1b\][0-9]+;[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"  # OSC color/title replies (OSC 10/11/12...)
    r"|\x1bP[^\x1b]*\x1b\\"  # DCS replies (XTVERSION, DECRQSS, XTGETTCAP)
    r"|\x1b_[^\x1b]*\x1b\\"  # APC replies
    r")+$"
)


def is_terminal_auto_response(data: str) -> bool:
    """Vrátí True, pokud vstup z xterm.js obsahuje výhradně automatické odpovědi terminálu."""
    if not data:
        return False
    return bool(_TERMINAL_AUTO_RESPONSE_RE.fullmatch(data))

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


TOOL_PERMISSION_MODES: tuple[str, ...] = ("allow", "ask", "block")


@dataclass(frozen=True)
class PluginPermissionSpec:
    """Definice jednoho přepínače oprávnění pluginu."""

    key: str
    label_key: str
    default: bool
    tier: str = "basic"  # 'basic' | 'detail'


@dataclass(frozen=True)
class PluginToolSpec:
    """Definice jednoho granulárního nástroje (akce) v 3-stavovém oprávnění pluginu."""

    key: str
    group: str  # 'readonly' | 'write'
    summary_key: str
    title_en: str
    title_cs: str
    desc_en: str
    desc_cs: str
    deny_commands: tuple[str, ...] = ()
    default_mode: str | None = None


PLUGIN_TOOL_CATALOG: dict[str, tuple[PluginToolSpec, ...]] = {
    "gmail": (
        # Read-only (7)
        PluginToolSpec(
            "search_threads",
            "readonly",
            "read",
            "Search email threads",
            "Vyhledávat vlákna e-mailů",
            "Searches messages and threads in the mailbox using Gmail queries.",
            "Vyhledává zprávy a konverzace ve schránce podle dotazu.",
            ("command(gmail readonly search)",),
        ),
        PluginToolSpec(
            "get_message",
            "readonly",
            "read",
            "Get email message",
            "Číst obsah e-mailu",
            "Retrieves the full content, headers, and body of a specific email.",
            "Načte kompletní obsah, hlavičky a text konkrétní zprávy.",
            ("command(gmail readonly get-message)", "command(gmail readonly read)"),
        ),
        PluginToolSpec(
            "get_thread",
            "readonly",
            "read",
            "Get email thread",
            "Číst celé vlákno konverzace",
            "Retrieves all messages within an email conversation thread.",
            "Načte všechny zprávy v jednom konverzačním vlákně.",
            ("command(gmail readonly get-thread)",),
        ),
        PluginToolSpec(
            "list_drafts",
            "readonly",
            "read",
            "List draft emails",
            "Zobrazit seznam konceptů",
            "Lists saved draft emails in the mailbox.",
            "Vypíše seznam uložených konceptů zpráv.",
            ("command(gmail readonly list-drafts)",),
        ),
        PluginToolSpec(
            "get_draft",
            "readonly",
            "read",
            "Get draft email",
            "Číst uložený koncept",
            "Retrieves a specific saved draft email by its ID.",
            "Přečte obsah konkrétního uloženého konceptu e-mailu.",
            ("command(gmail readonly get-draft)",),
        ),
        PluginToolSpec(
            "list_labels",
            "readonly",
            "read",
            "List labels",
            "Zobrazit seznam štítků",
            "Lists all Gmail system and custom labels in the mailbox.",
            "Vypíše všechny systémové i vlastní štítky ve schránce.",
            ("command(gmail readonly list-labels)",),
        ),
        PluginToolSpec(
            "get_attachment",
            "readonly",
            "read",
            "Download attachment",
            "Stahovat přílohy z e-mailů",
            "Downloads or inspects an attachment from an email message.",
            "Stáhne nebo zobrazí přílohu z e-mailové zprávy.",
            ("command(gmail readonly get-attachment)", "command(gmail readonly download-attachment)"),
        ),
        # Write / delete (18)
        PluginToolSpec(
            "create_draft",
            "write",
            "draft",
            "Create draft email",
            "Vytvořit koncept e-mailu",
            "Saves a new draft email without sending it.",
            "Uloží nový koncept zprávy bez odeslání.",
            ("command(gmail mutate create-draft)",),
        ),
        PluginToolSpec(
            "update_draft",
            "write",
            "draft",
            "Update draft email",
            "Upravit uložený koncept",
            "Edits an existing draft email without sending it.",
            "Upraví text, předmět nebo příjemce existujícího konceptu.",
            ("command(gmail mutate update-draft)",),
        ),
        PluginToolSpec(
            "delete_draft",
            "write",
            "trash",
            "Delete draft email",
            "Smazat koncept e-mailu",
            "Permanently deletes an existing draft email.",
            "Trvale odstraní vybraný koncept e-mailu.",
            ("command(gmail mutate delete-draft)",),
        ),
        PluginToolSpec(
            "send_message",
            "write",
            "send",
            "Send email message",
            "Odeslat nový e-mail",
            "Sends a new email message immediately.",
            "Okamžitě odešle nový e-mail zadaným příjemcům.",
            ("command(gmail mutate send)",),
        ),
        PluginToolSpec(
            "send_with_attachment",
            "write",
            "send",
            "Send email with attachment",
            "Odeslat e-mail s přílohou",
            "Sends a new email message with attached files immediately.",
            "Okamžitě odešle nový e-mail včetně přiložených souborů.",
            ("command(gmail mutate send-with-attachment)",),
        ),
        PluginToolSpec(
            "send_draft",
            "write",
            "send",
            "Send draft email",
            "Odeslat uložený koncept",
            "Sends a previously saved draft email to recipients.",
            "Odešle dříve připravený koncept příjemcům.",
            ("command(gmail mutate send-draft)",),
        ),
        PluginToolSpec(
            "reply_email",
            "write",
            "reply_forward",
            "Reply to email",
            "Odpovědět na e-mail",
            "Replies directly to an existing email message in a thread.",
            "Odešle přímou odpověď na vybranou zprávu ve vlákně.",
            ("command(gmail mutate reply)",),
        ),
        PluginToolSpec(
            "forward_email",
            "write",
            "reply_forward",
            "Forward email",
            "Přeposlat e-mail",
            "Forwards an email message to another recipient.",
            "Přepošle vybraný e-mail jiným příjemcům.",
            ("command(gmail mutate forward)",),
        ),
        PluginToolSpec(
            "add_label",
            "write",
            "labels",
            "Add labels to message/thread",
            "Přidat štítky zprávě či vláknu",
            "Applies one or more labels to a message or thread.",
            "Přiřadí jeden nebo více štítků zprávě či celému vláknu.",
            ("command(gmail mutate add-label)",),
        ),
        PluginToolSpec(
            "remove_label",
            "write",
            "labels",
            "Remove labels from message/thread",
            "Odebrat štítky ze zprávy či vlákna",
            "Removes labels from a message or thread.",
            "Odebere vybrané štítky ze zprávy nebo konverzace.",
            ("command(gmail mutate remove-label)",),
        ),
        PluginToolSpec(
            "thread_modify",
            "write",
            "labels",
            "Modify thread labels",
            "Upravit štítky vlákna",
            "Modifies label assignments on an entire conversation thread.",
            "Hromadně upraví štítky celého konverzačního vlákna.",
            ("command(gmail mutate thread-modify)",),
        ),
        PluginToolSpec(
            "batch_modify",
            "write",
            "labels",
            "Batch modify messages",
            "Hromadná úprava zpráv",
            "Applies or removes labels across multiple messages at once.",
            "Hromadně upraví štítky nebo stav u více zpráv najednou.",
            ("command(gmail mutate batch-modify)",),
        ),
        PluginToolSpec(
            "mark_read_unread",
            "write",
            "labels",
            "Mark read / unread",
            "Označit přečtené / nepřečtené",
            "Changes the read or unread state of messages and threads.",
            "Přepne stav přečteno / nepřečteno u zpráv a vláken.",
            ("command(gmail mutate mark-read)", "command(gmail mutate mark-unread)"),
        ),
        PluginToolSpec(
            "create_update_label",
            "write",
            "labels",
            "Create or rename label",
            "Vytvořit či přejmenovat štítek",
            "Creates a new Gmail label or renames an existing one.",
            "Vytvoří nový štítek v Gmailu nebo upraví stávající.",
            ("command(gmail mutate create-label)", "command(gmail mutate update-label)"),
        ),
        PluginToolSpec(
            "archive_email",
            "write",
            "archive",
            "Archive email",
            "Archivovat e-mail",
            "Removes messages or threads from Inbox without deleting them.",
            "Přesune zprávy z Doručené pošty do archivu bez smazání.",
            ("command(gmail mutate archive)",),
        ),
        PluginToolSpec(
            "trash_or_spam",
            "write",
            "trash",
            "Move to Trash or mark Spam",
            "Přesunout do koše nebo spamu",
            "Moves a message or thread to Trash or marks it as Spam.",
            "Přesune zprávu či vlákno do koše nebo označí jako spam.",
            ("command(gmail mutate trash)",),
        ),
        PluginToolSpec(
            "delete_label",
            "write",
            "trash",
            "Delete label",
            "Smazat štítek",
            "Deletes a custom Gmail label.",
            "Odstraní vlastní štítek z účtu Gmail.",
            ("command(gmail mutate delete-label)",),
        ),
        PluginToolSpec(
            "email_reactions",
            "write",
            "reactions",
            "Send or remove emoji reactions",
            "Posílat a odebírat reakce",
            "Adds or removes emoji reactions on email messages.",
            "Přidá nebo odebere emoji reakci u e-mailové zprávy.",
            (
                "command(gmail mutate react)",
                "command(gmail mutate create-reaction)",
                "command(gmail mutate delete-reaction)",
                "command(gmail mutate unreact)",
            ),
        ),
    ),
    "gcalendar": (
        PluginToolSpec(
            "list_events",
            "readonly",
            "read",
            "List & search calendar events",
            "Vyhledávat a vypisovat události",
            "Lists and filters events in selected calendars.",
            "Vypíše a vyhledá události ve vybraných kalendářích.",
            ("command(gcalendar readonly list)",),
        ),
        PluginToolSpec(
            "get_event",
            "readonly",
            "read",
            "Get event details",
            "Číst detail události",
            "Retrieves complete details, attendees, and links of an event.",
            "Zobrazí detail, popis, účastníky a odkaz na schůzku.",
            ("command(gcalendar readonly get)",),
        ),
        PluginToolSpec(
            "freebusy",
            "readonly",
            "read",
            "Check availability (Free/Busy)",
            "Zkontrolovat dostupnost (Free/Busy)",
            "Checks free and busy time slots across calendars.",
            "Zjistí volné a obsazené termíny v kalendářích.",
            ("command(gcalendar readonly freebusy)",),
        ),
        PluginToolSpec(
            "create_event",
            "write",
            "create",
            "Create calendar event",
            "Vytvořit novou událost",
            "Creates a full calendar event with title, time, and guests.",
            "Vytvoří novou událost s časem, místem a hosty.",
            ("command(gcalendar mutate create)",),
        ),
        PluginToolSpec(
            "quick_add",
            "write",
            "create",
            "Quick-add event from text",
            "Rychle přidat událost z textu",
            "Creates an event parsed from natural language text.",
            "Rychle založí událost z přirozeného textového popisu.",
            ("command(gcalendar mutate quick-add)",),
        ),
        PluginToolSpec(
            "import_event",
            "write",
            "create",
            "Import external event",
            "Importovat externí událost",
            "Imports an ICS or external event into the calendar.",
            "Naimportuje externí nebo ICS událost do kalendáře.",
            ("command(gcalendar mutate import)",),
        ),
        PluginToolSpec(
            "update_event",
            "write",
            "update",
            "Update calendar event",
            "Upravit událost v kalendáři",
            "Modifies time, title, description, or attendees of an event.",
            "Změní čas, název, popis nebo hosty existující události.",
            ("command(gcalendar mutate update)",),
        ),
        PluginToolSpec(
            "move_event",
            "write",
            "update",
            "Move event between calendars",
            "Přesunout událost do jiného kalendáře",
            "Moves an existing event to another calendar.",
            "Přesune vybranou událost do jiného kalendáře.",
            ("command(gcalendar mutate move)",),
        ),
        PluginToolSpec(
            "rsvp_event",
            "write",
            "rsvp",
            "Respond to invitation (RSVP)",
            "Odpovědět na pozvánku (RSVP)",
            "Accepts, tentatively accepts, or declines event invitations.",
            "Přijme, nezávazně potvrdí nebo odmítne pozvánku.",
            ("command(gcalendar mutate rsvp)",),
        ),
        PluginToolSpec(
            "delete_event",
            "write",
            "delete",
            "Delete calendar event",
            "Smazat událost z kalendáře",
            "Deletes an event from the calendar permanently.",
            "Odstraní vybranou událost z kalendáře.",
            ("command(gcalendar mutate delete)",),
        ),
    ),
    "gdrive": (
        PluginToolSpec(
            "search_files",
            "readonly",
            "read",
            "Search & list files",
            "Hledat a vypisovat soubory",
            "Searches files and folders in Google Drive.",
            "Vyhledává soubory a složky na Google Disku.",
            ("command(gdrive readonly search)",),
        ),
        PluginToolSpec(
            "get_file_metadata",
            "readonly",
            "read",
            "Get file metadata & permissions",
            "Číst metadata a oprávnění souboru",
            "Inspects file info, owners, size, and sharing metadata.",
            "Zobrazí vlastnosti souboru, vlastníka a přehled sdílení.",
            ("command(gdrive readonly get)",),
        ),
        PluginToolSpec(
            "download_file",
            "readonly",
            "read",
            "Download & export files",
            "Stahovat a exportovat soubory",
            "Downloads or exports file content to local workspace.",
            "Stáhne nebo vyexportuje obsah souboru do pracovního prostoru.",
            ("command(gdrive readonly download)",),
        ),
        PluginToolSpec(
            "upload_file",
            "write",
            "write",
            "Upload new file",
            "Nahrát nový soubor",
            "Uploads a local file into Google Drive.",
            "Nahraje soubor z pracovního prostoru na Google Disk.",
            ("command(gdrive mutate upload)",),
        ),
        PluginToolSpec(
            "create_folder",
            "write",
            "write",
            "Create folder",
            "Vytvořit složku",
            "Creates a new folder in Google Drive.",
            "Založí novou složku na Google Disku.",
            ("command(gdrive mutate create)",),
        ),
        PluginToolSpec(
            "update_file",
            "write",
            "write",
            "Update file content",
            "Aktualizovat obsah souboru",
            "Uploads a new revision or updates content of an existing file.",
            "Aktualizuje obsah existujícího souboru na Google Disku.",
            ("command(gdrive mutate update)",),
        ),
        PluginToolSpec(
            "copy_move_file",
            "write",
            "write",
            "Copy or move file",
            "Kopírovat nebo přesunout soubor",
            "Copies or moves files across Drive folders.",
            "Zkopíruje nebo přesune soubory mezi složkami.",
            ("command(gdrive mutate copy)", "command(gdrive mutate move)"),
        ),
        PluginToolSpec(
            "rename_file",
            "write",
            "write",
            "Rename file or folder",
            "Přejmenovat soubor či složku",
            "Changes the name of a Drive file or folder.",
            "Změní název souboru nebo složky na Google Disku.",
            ("command(gdrive mutate rename)",),
        ),
        PluginToolSpec(
            "share_file",
            "write",
            "share",
            "Share or unshare file",
            "Sdílet / zrušit sdílení souboru",
            "Grants or revokes sharing access to files and folders.",
            "Nastaví nebo zruší sdílení souboru s dalšími lidmi.",
            ("command(gdrive mutate share)", "command(gdrive mutate unshare)"),
        ),
        PluginToolSpec(
            "manage_permissions",
            "write",
            "share",
            "Manage file permissions",
            "Spravovat podrobná práva přístupu",
            "Modifies roles and permission entries on a Drive item.",
            "Upraví role (čtenář/editor) a přístupová práva k položce.",
            ("command(gdrive mutate permissions)",),
        ),
        PluginToolSpec(
            "delete_file",
            "write",
            "delete",
            "Delete or trash file",
            "Smazat nebo přesunout do koše",
            "Moves files to Trash or permanently deletes them.",
            "Přesune soubory do koše nebo je trvale smaže.",
            ("command(gdrive mutate delete)", "command(gdrive mutate trash)"),
        ),
    ),
    "gdocs": (
        PluginToolSpec(
            "read_document",
            "readonly",
            "read",
            "Read Google Doc content",
            "Číst obsah Google Dokumentu",
            "Reads headings, paragraphs, and content of a Google Doc.",
            "Načte text, strukturu a nadpisy Google Dokumentu.",
            ("command(gdocs readonly read)", "command(gdocs readonly get)"),
        ),
        PluginToolSpec(
            "search_documents",
            "readonly",
            "read",
            "List & search Google Docs",
            "Vyhledávat Google Dokumenty",
            "Finds and lists Google Docs documents.",
            "Vyhledá a vypíše dostupné Google Dokumenty.",
            ("command(gdocs readonly search)", "command(gdocs readonly list)"),
        ),
        PluginToolSpec(
            "create_document",
            "write",
            "write",
            "Create Google Doc",
            "Vytvořit nový Google Dokument",
            "Creates a new Google Docs document.",
            "Založí nový prázdný nebo předvyplněný Google Dokument.",
            ("command(gdocs mutate create)",),
        ),
        PluginToolSpec(
            "edit_document",
            "write",
            "write",
            "Edit or append Google Doc",
            "Upravit nebo doplnit Google Dokument",
            "Modifies or appends text and formatting in a Google Doc.",
            "Upraví text nebo doplní nové odstavce do dokumentu.",
            ("command(gdocs mutate update)", "command(gdocs mutate edit)", "command(gdocs mutate append)"),
        ),
    ),
    "gsheets": (
        PluginToolSpec(
            "read_spreadsheet",
            "readonly",
            "read",
            "Read Google Sheet values",
            "Číst data z Google Tabulek",
            "Reads sheets, ranges, and cell values from a spreadsheet.",
            "Přečte listy, rozsahy a hodnoty buněk z Google Tabulky.",
            ("command(gsheets readonly read)", "command(gsheets readonly get)"),
        ),
        PluginToolSpec(
            "search_spreadsheets",
            "readonly",
            "read",
            "List & search Google Sheets",
            "Vyhledávat Google Tabulky",
            "Lists and searches spreadsheets in Drive.",
            "Vyhledá a vypíše dostupné tabulky Google Sheets.",
            ("command(gsheets readonly search)", "command(gsheets readonly list)"),
        ),
        PluginToolSpec(
            "create_spreadsheet",
            "write",
            "write",
            "Create Google Sheet",
            "Vytvořit novou Google Tabulku",
            "Creates a new spreadsheet workbook.",
            "Vytvoří novou tabulku v Google Sheets.",
            ("command(gsheets mutate create)",),
        ),
        PluginToolSpec(
            "edit_spreadsheet",
            "write",
            "write",
            "Write & format Google Sheet",
            "Zapisovat a formátovat buňky",
            "Writes cell values, appends rows, and updates sheet formatting.",
            "Zapíše hodnoty, přidá řádky a upraví formátování buněk.",
            ("command(gsheets mutate update)", "command(gsheets mutate append)", "command(gsheets mutate format)"),
        ),
    ),
    "gslides": (
        PluginToolSpec(
            "read_presentation",
            "readonly",
            "read",
            "Read Google Slides deck",
            "Číst obsah Google Prezentace",
            "Reads slides, titles, speaker notes, and text from a deck.",
            "Přečte snímky, texty a poznámky řečníka z prezentace.",
            ("command(gslides readonly read)", "command(gslides readonly get)"),
        ),
        PluginToolSpec(
            "export_presentation",
            "readonly",
            "read",
            "List & export Google Slides",
            "Vyhledávat a exportovat prezentace",
            "Lists available slide decks or exports them to PDF/images.",
            "Vypíše dostupné prezentace nebo je exportuje.",
            ("command(gslides readonly export)", "command(gslides readonly list)"),
        ),
        PluginToolSpec(
            "create_presentation",
            "write",
            "write",
            "Create Google Slides deck",
            "Vytvořit novou prezentaci",
            "Creates a new Google Slides presentation.",
            "Založí novou prezentaci v Google Slides.",
            ("command(gslides mutate create)",),
        ),
        PluginToolSpec(
            "edit_presentation",
            "write",
            "write",
            "Edit Google Slides",
            "Upravovat snímky prezentace",
            "Adds, edits, or updates slides in an existing presentation.",
            "Přidá nebo upraví snímky a texty ve stávající prezentaci.",
            ("command(gslides mutate update)", "command(gslides mutate edit)"),
        ),
    ),
}


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
    supports_custom_url: bool = False
    mcp_server_name: str = ""
    custom_url_placeholder: str = ""
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
        supports_custom_url=True,
        mcp_server_name="n8n",
        custom_url_placeholder="https://n8n.example.com/mcp-server/http",
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
        supports_custom_url=True,
        mcp_server_name="grafana",
        custom_url_placeholder="https://grafana.example.com/mcp",
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
        supports_custom_url=True,
        mcp_server_name="looker",
        custom_url_placeholder="https://looker.example.com/mcp",
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
        supports_custom_url=True,
        mcp_server_name="neo4j",
        custom_url_placeholder="https://neo4j.example.com/mcp",
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


def validate_custom_mcp_url(url: str) -> str:
    """Ověří URL vlastního HTTP MCP serveru (`http://` nebo `https://`)."""
    cleaned = (url or "").strip()
    if not cleaned or len(cleaned) > 1000 or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise ValueError("Neplatná URL MCP serveru.")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL musí začínat http:// nebo https:// a obsahovat adresu serveru.")
    return cleaned


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


_NON_AUTH_HOSTS = frozenset(
    {
        "policies.google.com",
        "support.google.com",
        "developers.google.com",
        "cloud.google.com",
    }
)
_NON_AUTH_PATH_SEGMENTS = (
    "/terms",
    "/privacy",
    "/legal",
    "/tos",
    "/eula",
    "/terms-of-service",
    "/privacy-policy",
)


def _is_non_auth_info_url(url: str) -> bool:
    """Vrátí True pro právní či informační odkazy (např. `/terms`), které nejsou OAuth loginem."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return True
    host = parsed.hostname.lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if "redirect_uri=" in query or "client_id=" in query or "response_type=code" in query:
        return False
    if host in _NON_AUTH_HOSTS:
        return True
    if host == "antigravity.google" and not path.startswith(
        ("/oauth", "/authorize", "/login", "/signin")
    ):
        return True
    return any(seg in path for seg in _NON_AUTH_PATH_SEGMENTS)


def _pick_best_oauth_url(urls: list[str]) -> str:
    """Vybere z extrahovaných odkazů reálnou OAuth přihlašovací URL (nikdy `/terms` apod.)."""
    clean_urls = [
        u.strip()
        for u in urls
        if isinstance(u, str) and u.strip() and not _is_non_auth_info_url(u)
    ]
    if not clean_urls:
        return ""
    for scorer in (
        lambda u: ("accounts.google.com/o/oauth2" in u and "redirect_uri=" in u),
        lambda u: "redirect_uri=" in u,
        lambda u: "accounts.google.com/o/oauth2" in u,
        lambda u: any(
            tok in u.lower()
            for tok in (
                "/oauth",
                "/authorize",
                "client_id=",
                "response_type=code",
                "/login/oauth",
                "/device",
                "/signin",
            )
        ),
    ):
        matches = [u for u in clean_urls if scorer(u)]
        if matches:
            return max(matches, key=len)
    return ""


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


def parse_tool_permissions_raw(raw: dict[str, Any] | str | None) -> dict[str, str]:
    """Bezpečně převede JSON/dict s 3-stavovými právy nástrojů na mapu validních módů."""
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

    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if isinstance(v, str) and v.strip().lower() in TOOL_PERMISSION_MODES:
            out[k.strip()] = v.strip().lower()
    return out


def normalize_tool_permissions(
    plugin_name: str,
    raw_tool_perms: dict[str, Any] | str | None,
    summary_perms: dict[str, bool] | None = None,
) -> dict[str, str]:
    """Sestaví úplnou mapu `{tool_key: allow|ask|block}` s fallbackem ze souhrnných přepínačů."""
    pname = plugin_name.strip().lower()
    specs = PLUGIN_TOOL_CATALOG.get(pname)
    if not specs:
        return parse_tool_permissions_raw(raw_tool_perms)

    summary = (
        normalize_plugin_permissions(pname, summary_perms)
        if summary_perms is not None
        else default_permissions_for_plugin(pname)
    )
    overrides = parse_tool_permissions_raw(raw_tool_perms)
    resolved: dict[str, str] = {}
    for spec in specs:
        if spec.key in overrides:
            resolved[spec.key] = overrides[spec.key]
        elif spec.default_mode in TOOL_PERMISSION_MODES:
            resolved[spec.key] = spec.default_mode  # type: ignore[assignment]
        else:
            resolved[spec.key] = "allow" if summary.get(spec.summary_key, False) else "block"
    return resolved


def sync_summary_from_tool_permissions(
    plugin_name: str,
    tool_perms: dict[str, str],
    current_summary: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Aktualizuje souhrnné boolean přepínače podle granulární 3-stavové mapy nástrojů."""
    pname = plugin_name.strip().lower()
    base = (
        normalize_plugin_permissions(pname, current_summary)
        if current_summary is not None
        else default_permissions_for_plugin(pname)
    )
    specs = PLUGIN_TOOL_CATALOG.get(pname)
    if not base or not specs:
        return base

    by_summary: dict[str, list[str]] = {}
    for spec in specs:
        by_summary.setdefault(spec.summary_key, []).append(spec.key)

    updated = dict(base)
    for sum_key, tool_keys in by_summary.items():
        if sum_key in updated:
            updated[sum_key] = any(tool_perms.get(tk, "block") != "block" for tk in tool_keys)
    return updated


def localize_tool_specs(
    plugin_name: str, tool_perms: dict[str, str], lang: str = "en"
) -> list[dict[str, Any]]:
    """Vrátí lokalizovaný seznam nástrojů pluginu včetně jejich aktuálního režimu."""
    specs = PLUGIN_TOOL_CATALOG.get(plugin_name.strip().lower())
    if not specs:
        return []
    use_cs = (lang or "en").strip().lower() in ("cs", "sk")
    out: list[dict[str, Any]] = []
    for spec in specs:
        out.append(
            {
                "key": spec.key,
                "group": spec.group,
                "summary_key": spec.summary_key,
                "title": spec.title_cs if use_cs else spec.title_en,
                "description": spec.desc_cs if use_cs else spec.desc_en,
                "mode": tool_perms.get(spec.key, "block"),
            }
        )
    return out


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


def _effective_tool_permissions(
    plugin_name: str,
    configs: dict[str, PluginConfigRecord],
    selected_accounts: dict[str, PluginAccountRecord] | None = None,
) -> dict[str, str]:
    summary = _effective_permissions(plugin_name, configs, selected_accounts)
    raw_tp: str | None = None
    if selected_accounts and plugin_name.lower() in selected_accounts:
        raw_tp = selected_accounts[plugin_name.lower()].tool_permissions_json
    return normalize_tool_permissions(plugin_name, raw_tp, summary)


def _mcp_server_matches_channel(scope: str, channel: str) -> bool:
    if channel == "auto":
        return True
    return scope in ("all", channel)


def classify_mcp_tool_group(name: str, annotations: dict[str, Any] | None = None) -> str:
    """Zařadí MCP nástroj do skupiny `readonly` nebo `write` podle anotací či názvu."""
    if isinstance(annotations, dict):
        if annotations.get("readOnlyHint") is True:
            return "readonly"
        if annotations.get("destructiveHint") is True:
            return "write"
    low = (name or "").strip().lower()
    if low in {"whoami", "status", "health", "version"} or low.startswith(
        (
            "get_",
            "list_",
            "search_",
            "read_",
            "find_",
            "fetch_",
            "query_",
            "describe_",
            "show_",
            "inspect_",
            "check_",
            "count_",
            "lookup_",
            "view_",
        )
    ):
        return "readonly"
    return "write"


def parse_mcp_jsonrpc_response(raw_bytes: bytes, content_type: str = "") -> dict[str, Any]:
    """Rozparsuje JSON-RPC odpověď MCP serveru jak z plain JSON, tak ze SSE `text/event-stream`."""
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return {}

    ct = (content_type or "").lower()
    if "text/event-stream" not in ct and (text.startswith("{") or text.startswith("[")):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
            if isinstance(loaded, list):
                for item in reversed(loaded):
                    if isinstance(item, dict) and ("result" in item or "error" in item):
                        return item
                for item in loaded:
                    if isinstance(item, dict):
                        return item
        except json.JSONDecodeError:
            pass

    # SSE event-stream: sbíráme bloky `data:` řádků oddělené prázdným řádkem
    candidates: list[dict[str, Any]] = []
    data_buf: list[str] = []

    def _flush_buf() -> None:
        if not data_buf:
            return
        payload_str = "\n".join(data_buf).strip()
        data_buf.clear()
        if not payload_str or payload_str == "[DONE]":
            return
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(payload_str)
            if isinstance(parsed, dict):
                candidates.append(parsed)
            elif isinstance(parsed, list):
                for sub in parsed:
                    if isinstance(sub, dict):
                        candidates.append(sub)

    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if not stripped:
            _flush_buf()
            continue
        if line.startswith("data:"):
            data_buf.append(line[5:].lstrip())
    _flush_buf()

    for cand in reversed(candidates):
        if "result" in cand or "error" in cand:
            return cand
    if candidates:
        return candidates[-1]

    # Poslední fallback: zkusit přímo JSON
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    return {}


def _post_mcp_jsonrpc_sync(
    url: str,
    payload: dict[str, Any],
    *,
    auth_token: str = "",
    headers: dict[str, str] | None = None,
    session_id: str = "",
    timeout: float = 10.0,
) -> tuple[int, str, dict[str, Any]]:
    """Odešle jeden JSON-RPC POST požadavek na HTTP MCP server bez externích závislostí."""
    req_headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "asistent-antigravity/1.0",
    }
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(k, str) and k.strip() and isinstance(v, str):
                req_headers[k.strip()] = v
    if auth_token.strip():
        req_headers["Authorization"] = f"Bearer {auth_token.strip()}"
    if session_id.strip():
        req_headers["Mcp-Session-Id"] = session_id.strip()

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            new_sid = str(resp.headers.get("Mcp-Session-Id") or session_id or "").strip()
            ctype = str(resp.headers.get("Content-Type") or "")
            raw = resp.read(2 * 1024 * 1024)
            return status, new_sid, parse_mcp_jsonrpc_response(raw, ctype)
    except urllib.error.HTTPError as exc:
        err_raw = b""
        with contextlib.suppress(Exception):
            err_raw = exc.read(4096)
        ctype = str(exc.headers.get("Content-Type") or "") if exc.headers else ""
        parsed = parse_mcp_jsonrpc_response(err_raw, ctype)
        err_detail = ""
        if isinstance(parsed.get("error"), dict):
            err_detail = str(parsed["error"].get("message") or "")
        if not err_detail:
            err_detail = err_raw.decode("utf-8", errors="replace").strip()[:250]
        if int(exc.code) in (401, 403):
            raise RuntimeError(
                f"MCP server odmítl autorizaci (HTTP {exc.code}). Zkontrolujte Bearer token."
            ) from exc
        new_sid = (
            str(exc.headers.get("Mcp-Session-Id") or session_id or "").strip()
            if exc.headers
            else session_id
        )
        if parsed and ("error" in parsed or "result" in parsed):
            return int(exc.code), new_sid, parsed
        raise RuntimeError(f"HTTP {exc.code}: {err_detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Nepodařilo se připojit k MCP serveru: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Chyba komunikace s MCP serverem: {exc}") from exc


def discover_http_mcp_tools_sync(
    url: str,
    *,
    auth_token: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> list[dict[str, str]]:
    """Načte seznam dostupných nástrojů ze Streamable HTTP MCP serveru (`tools/list`)."""
    clean_url = validate_custom_mcp_url(url)
    session_id = ""
    initialized_ok = False

    with contextlib.suppress(RuntimeError):
        status, sid, init_res = _post_mcp_jsonrpc_sync(
            clean_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "asistent-antigravity", "version": "1.0"},
                },
            },
            auth_token=auth_token,
            headers=headers,
            timeout=timeout,
        )
        session_id = sid
        if 200 <= status < 300 and isinstance(init_res.get("result"), dict):
            initialized_ok = True

    if initialized_ok:
        with contextlib.suppress(RuntimeError):
            _post_mcp_jsonrpc_sync(
                clean_url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                auth_token=auth_token,
                headers=headers,
                session_id=session_id,
                timeout=min(timeout, 4.0),
            )

    raw_tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    cursor: str | None = None
    for page in range(5):
        params: dict[str, Any] = {"cursor": cursor} if cursor else {}
        status, session_id, list_res = _post_mcp_jsonrpc_sync(
            clean_url,
            {"jsonrpc": "2.0", "id": 10 + page, "method": "tools/list", "params": params},
            auth_token=auth_token,
            headers=headers,
            session_id=session_id,
            timeout=timeout,
        )
        if isinstance(list_res.get("error"), dict):
            msg = str(list_res["error"].get("message") or list_res["error"])
            raise RuntimeError(f"MCP server vrátil chybu při tools/list: {msg}")
        result = list_res.get("result")
        if not isinstance(result, dict):
            if status >= 400:
                raise RuntimeError(f"MCP server vrátil neplatnou odpověď (HTTP {status}).")
            break
        batch = result.get("tools")
        if isinstance(batch, list):
            for item in batch:
                if not isinstance(item, dict):
                    continue
                tname = str(item.get("name") or "").strip()
                if not tname or tname in seen_names:
                    continue
                seen_names.add(tname)
                raw_tools.append(item)
                if len(raw_tools) >= 200:
                    break
        next_cursor = result.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor.strip() or len(raw_tools) >= 200:
            break
        cursor = next_cursor.strip()

    out: list[dict[str, str]] = []
    for item in raw_tools:
        tname = str(item.get("name") or "").strip()
        ann = item.get("annotations") if isinstance(item.get("annotations"), dict) else None
        group = classify_mcp_tool_group(tname, ann)
        title = str(item.get("title") or tname).strip()[:120]
        desc = " ".join(str(item.get("description") or "").split())[:300]
        out.append({"key": tname, "title": title, "description": desc, "group": group})
    return out


def build_mcp_tools_payload(srv: McpServerRecord) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Sestaví seznam nástrojů a jejich 3-stavových oprávnění pro jeden MCP server."""
    try:
        cached = json.loads(srv.cached_tools_json or "[]")
        if not isinstance(cached, list):
            cached = []
    except (json.JSONDecodeError, TypeError):
        cached = []

    saved_tp = parse_tool_permissions_raw(srv.tool_permissions_json)
    tools_out: list[dict[str, Any]] = []
    resolved_tp: dict[str, str] = {}
    seen_keys: set[str] = set()

    for entry in cached:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or entry.get("name") or "").strip()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        raw_grp = str(entry.get("group") or "").strip().lower()
        group = raw_grp if raw_grp in ("readonly", "write") else classify_mcp_tool_group(key)
        default_mode = "allow" if group == "readonly" else "ask"
        mode = saved_tp.get(key, default_mode)
        resolved_tp[key] = mode
        tools_out.append(
            {
                "key": key,
                "group": group,
                "summary_key": group,
                "title": str(entry.get("title") or key).strip(),
                "description": str(entry.get("description") or "").strip(),
                "mode": mode,
            }
        )

    for key, mode in saved_tp.items():
        if key not in resolved_tp:
            resolved_tp[key] = mode

    return tools_out, resolved_tp


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
            continue
        if cli_name not in enabled_low:
            continue

        tperms = _effective_tool_permissions(cli_name, configs, selected)
        specs = PLUGIN_TOOL_CATALOG.get(cli_name, ())
        ro_specs = [sp for sp in specs if sp.group == "readonly"]
        wr_specs = [sp for sp in specs if sp.group == "write"]

        if ro_specs and all(tperms.get(sp.key, "block") == "block" for sp in ro_specs):
            rules.append(f"command({cli_name} readonly)")
        else:
            for sp in ro_specs:
                if tperms.get(sp.key, "block") == "block":
                    rules.extend(sp.deny_commands)

        if wr_specs and all(tperms.get(sp.key, "block") == "block" for sp in wr_specs):
            rules.append(f"command({cli_name} mutate)")
        else:
            for sp in wr_specs:
                if tperms.get(sp.key, "block") == "block":
                    rules.extend(sp.deny_commands)

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
    tperms = _effective_tool_permissions("gmail", configs, selected)
    send_keys = (
        "send_message",
        "send_with_attachment",
        "send_draft",
        "reply_email",
        "forward_email",
    )
    return all(tperms.get(k, "block") == "block" for k in send_keys)


def build_plugin_permissions_prompt_hint(
    enabled_plugins: set[str],
    configs: dict[str, PluginConfigRecord],
    *,
    accounts: list[PluginAccountRecord] | None = None,
    context_mode: str = "auto",
    mcp_servers: list[McpServerRecord] | None = None,
) -> str:
    """Sestaví systémovou instrukci pro agenta vč. 3-stavových práv, účtů, MCP nástrojů a pravidel."""
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
            specs = PLUGIN_TOOL_CATALOG.get(pname, ())
            if specs:
                tperms = _effective_tool_permissions(pname, configs, selected)
                ask_keys = [sp.key for sp in specs if tperms.get(sp.key) == "ask"]
                mixed_blocked = [
                    sp.key
                    for sp in specs
                    if tperms.get(sp.key) == "block" and perms.get(sp.summary_key, False)
                ]
                if mixed_blocked:
                    parts.append(f"BLOCKED_TOOLS({', '.join(mixed_blocked)})")
                if ask_keys:
                    parts.append(f"NEEDS_APPROVAL({', '.join(ask_keys)})")
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

    if mcp_servers:
        channel = normalize_chat_channel(context_mode)
        for srv in mcp_servers:
            if not srv.enabled or not _mcp_server_matches_channel(srv.scope, channel):
                continue
            _, resolved_tp = build_mcp_tools_payload(srv)
            blocked_tools = [k for k, m in resolved_tp.items() if m == "block"]
            ask_tools = [k for k, m in resolved_tp.items() if m == "ask"]
            mcp_parts: list[str] = []
            if blocked_tools:
                mcp_parts.append(f"BLOCKED_TOOLS({', '.join(blocked_tools)})")
            if ask_tools:
                mcp_parts.append(f"NEEDS_APPROVAL({', '.join(ask_tools)})")
            if mcp_parts:
                entries.append(f"mcp:{srv.name}: {'; '.join(mcp_parts)}")

    if not entries:
        return ""
    joined = " | ".join(entries)
    has_ask = any("NEEDS_APPROVAL(" in e for e in entries)
    ask_note = (
        "For NEEDS_APPROVAL tools, ALWAYS show exact target/parameters in chat and wait for explicit "
        "user confirmation before executing; "
        if has_ask
        else ""
    )
    return (
        "[Plugin permissions (STRICTLY ENFORCED – never run denied or blocked actions or violate "
        f"custom RULES, {ask_note}inform the user they can change them in Settings → Integrations): "
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
        self._raw_total_len: int = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._pty_size: tuple[int, int] = (0, 0)
        self._reader_task: asyncio.Task[None] | None = None
        self._nav_task: asyncio.Task[None] | None = None
        self._connect_subscribers: set[asyncio.Queue[str | None]] = set()
        self._manual_override = False

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
        self,
        acct: PluginAccountRecord,
        *,
        connected_override: bool | None = None,
        lang: str = "en",
    ) -> dict[str, Any]:
        pname = acct.plugin_name.lower()
        perms = normalize_plugin_permissions(pname, acct.permissions_json)
        tperms = normalize_tool_permissions(pname, acct.tool_permissions_json, perms)
        tools = localize_tool_specs(pname, tperms, lang=lang)
        return {
            "id": acct.id,
            "plugin_name": acct.plugin_name,
            "label": acct.label,
            "scope": acct.scope,
            "profile_key": sanitize_profile_key(acct.profile_key),
            "account_email": acct.account_email,
            "permissions": perms,
            "tool_permissions": tperms,
            "tools": tools,
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

    def _custom_mcp_server_name(self, plugin_name: str) -> str:
        safe_name = validate_plugin_name(plugin_name).lower()
        item = _PLUGIN_CATALOG_BY_NAME.get(safe_name)
        if item is not None and item.mcp_server_name:
            return item.mcp_server_name.lower()
        return safe_name

    def _find_custom_mcp_server(
        self,
        plugin_name: str,
        servers: list[McpServerRecord] | None = None,
    ) -> McpServerRecord | None:
        safe_name = validate_plugin_name(plugin_name).lower()
        mcp_name = self._custom_mcp_server_name(safe_name)
        rows = servers if servers is not None else self.db.list_mcp_servers()
        by_name = {srv.name.strip().lower(): srv for srv in rows if srv.transport == "http"}
        return by_name.get(mcp_name) or by_name.get(safe_name)

    @staticmethod
    def _custom_mcp_public_dict(srv: McpServerRecord | None) -> dict[str, Any] | None:
        if srv is None:
            return None
        tools_list, tool_perms = build_mcp_tools_payload(srv)
        return {
            "id": srv.id,
            "name": srv.name,
            "url": srv.url,
            "scope": srv.scope,
            "has_auth_token": bool(srv.auth_token.strip()),
            "enabled": bool(srv.enabled),
            "tools": tools_list,
            "tool_permissions": tool_perms,
        }

    def configure_custom_url(
        self,
        plugin_name: str,
        *,
        url: str,
        auth_token: str = "",
        scope: str = "all",
        clear_token: bool = False,
    ) -> dict[str, Any]:
        """Uloží vlastní HTTP MCP URL (+ volitelný Bearer token) přímo ke kartě pluginu."""
        safe_name = validate_plugin_name(plugin_name).lower()
        clean_url = validate_custom_mcp_url(url)
        clean_scope = scope if scope in ("all", "pracovni", "osobni") else "all"
        mcp_name = self._custom_mcp_server_name(safe_name)
        existing = self._find_custom_mcp_server(safe_name)
        new_token = (auth_token or "").strip()
        if clear_token:
            eff_token = ""
        elif new_token:
            eff_token = new_token
        elif existing is not None:
            eff_token = existing.auth_token
        else:
            eff_token = ""
        saved = self.db.upsert_mcp_server(
            server_id=existing.id if existing is not None else None,
            name=existing.name if existing is not None else mcp_name,
            scope=clean_scope,
            transport="http",
            url=clean_url,
            auth_token=eff_token,
            headers_json=existing.headers_json if existing is not None else "{}",
            enabled=True,
        )
        return {
            "status": "ok",
            "plugin": safe_name,
            "custom_mcp": self._custom_mcp_public_dict(saved),
        }

    async def discover_plugin_mcp_tools(
        self, plugin_name: str, *, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Načte a uloží seznam nástrojů z připojeného HTTP MCP serveru daného pluginu."""
        safe_name = validate_plugin_name(plugin_name).lower()
        srv = self._find_custom_mcp_server(safe_name)
        if srv is None or not srv.url.strip():
            raise ValueError(f"Plugin '{safe_name}' nemá nakonfigurovanou vlastní HTTP MCP URL.")
        try:
            headers_raw = json.loads(srv.headers_json or "{}")
            headers = headers_raw if isinstance(headers_raw, dict) else {}
        except json.JSONDecodeError:
            headers = {}
        tools = await asyncio.to_thread(
            discover_http_mcp_tools_sync,
            srv.url,
            auth_token=srv.auth_token,
            headers=headers,
            timeout=timeout,
        )
        updated = self.db.update_mcp_server_tools(
            srv.id,
            cached_tools_json=json.dumps(tools, ensure_ascii=False),
        )
        return {
            "status": "ok",
            "plugin": safe_name,
            "count": len(tools),
            "custom_mcp": self._custom_mcp_public_dict(updated),
        }

    def delete_custom_url(self, plugin_name: str) -> dict[str, Any]:
        """Odebere vlastní HTTP MCP URL navázanou na daný plugin."""
        safe_name = validate_plugin_name(plugin_name).lower()
        existing = self._find_custom_mcp_server(safe_name)
        deleted = False
        if existing is not None:
            deleted = self.db.delete_mcp_server(existing.id)
        return {"status": "ok", "plugin": safe_name, "deleted": deleted}

    async def list_catalog_with_status(self, *, lang: str = "en") -> dict[str, Any]:
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
        mcp_servers = self.db.list_mcp_servers()

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
                out_accts.append(
                    self._account_public_dict(acct, connected_override=eff_conn, lang=lang)
                )

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
            custom_srv = (
                self._find_custom_mcp_server(low, mcp_servers) if item.supports_custom_url else None
            )
            custom_mcp_dict = self._custom_mcp_public_dict(custom_srv)
            custom_url_conn = bool(
                custom_mcp_dict and custom_mcp_dict["enabled"] and custom_mcp_dict["url"]
            )
            schema_tools_count = len(PLUGIN_TOOL_CATALOG.get(low, ()))
            mcp_tools_count = len((custom_mcp_dict or {}).get("tools") or [])
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
                    "supports_custom_url": bool(item.supports_custom_url),
                    "custom_url_placeholder": item.custom_url_placeholder,
                    "custom_mcp": custom_mcp_dict,
                    "custom_url_connected": custom_url_conn,
                    "installed": inst is not None,
                    "enabled": bool(inst["enabled"]) if inst is not None else False,
                    "version": str((inst or {}).get("version") or ""),
                    "connected": (any_conn if inst is not None else False) or custom_url_conn,
                    "connected_at": rec.connected_at if rec is not None else "",
                    "account_label": first_label,
                    "accounts": accts_out,
                    "permissions_schema": [asdict(sp) for sp in item.permissions],
                    "permissions": primary_perms,
                    "tools_count": schema_tools_count or mcp_tools_count,
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
            custom_srv = self._find_custom_mcp_server(low, mcp_servers)
            custom_mcp_dict = self._custom_mcp_public_dict(custom_srv)
            custom_url_conn = bool(
                custom_mcp_dict and custom_mcp_dict["enabled"] and custom_mcp_dict["url"]
            )
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
                    "supports_custom_url": True,
                    "custom_url_placeholder": "https://mcp.example.com/mcp",
                    "custom_mcp": custom_mcp_dict,
                    "custom_url_connected": custom_url_conn,
                    "installed": True,
                    "enabled": bool(inst.get("enabled", True)),
                    "version": str(inst.get("version") or ""),
                    "connected": any_conn or custom_url_conn,
                    "connected_at": rec.connected_at if rec is not None else "",
                    "account_label": first_label,
                    "accounts": accts_out,
                    "permissions_schema": [],
                    "permissions": {},
                    "tools_count": len((custom_mcp_dict or {}).get("tools") or []),
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
        lang: str = "en",
    ) -> dict[str, Any]:
        """Uloží nastavení, oblast, oprávnění a pravidla konkrétního účtu pluginu."""
        safe_name = validate_plugin_name(plugin_name).lower()
        acct = self.db.get_plugin_account(account_id)
        if acct is None or acct.plugin_name.lower() != safe_name:
            raise ValueError(f"Účet pluginu '{safe_name}' (#{account_id}) nebyl nalezen.")

        perms_json: str | None = None
        tool_perms_json: str | None = None
        if permissions is not None:
            defaults = default_permissions_for_plugin(safe_name)
            if defaults:
                old_summary = normalize_plugin_permissions(safe_name, acct.permissions_json)
                merged = {
                    **old_summary,
                    **{k: bool(v) for k, v in permissions.items() if isinstance(v, bool)},
                }
                norm = normalize_plugin_permissions(safe_name, merged)
                perms_json = json.dumps(norm, ensure_ascii=False)
                specs = PLUGIN_TOOL_CATALOG.get(safe_name, ())
                if specs:
                    changed_keys = {k for k, v in norm.items() if old_summary.get(k) != v}
                    if changed_keys:
                        cur_tp = parse_tool_permissions_raw(acct.tool_permissions_json)
                        for sp in specs:
                            if sp.summary_key in changed_keys:
                                cur_tp[sp.key] = (
                                    "allow" if norm.get(sp.summary_key, False) else "block"
                                )
                        tool_perms_json = json.dumps(cur_tp, ensure_ascii=False)
            else:
                perms_json = "{}"

        updated = self.db.update_plugin_account(
            account_id,
            label=label,
            scope=scope,
            enabled=enabled,
            permissions_json=perms_json,
            tool_permissions_json=tool_perms_json,
            custom_rules=custom_rules,
        )
        assert updated is not None
        if sanitize_profile_key(updated.profile_key) == "default":
            self.db.upsert_plugin_config(
                safe_name,
                permissions_json=updated.permissions_json,
            )
        return {"status": "ok", "account": self._account_public_dict(updated, lang=lang)}

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
        tool_perms_json: str | None = None
        specs = PLUGIN_TOOL_CATALOG.get(safe_name, ())
        if specs:
            cur_tp = {
                sp.key: ("allow" if normalized.get(sp.summary_key, False) else "block")
                for sp in specs
            }
            tool_perms_json = json.dumps(cur_tp, ensure_ascii=False)
        self.db.update_plugin_account(
            primary.id,
            permissions_json=perms_json,
            tool_permissions_json=tool_perms_json,
        )
        return {"status": "ok", "plugin": safe_name, "permissions": normalized}

    def update_tool_permissions(
        self,
        plugin_name: str,
        *,
        tool_permissions: dict[str, str],
        account_id: int | None = None,
        label: str | None = None,
        scope: str | None = None,
        enabled: bool | None = None,
        custom_rules: str | None = None,
        lang: str = "en",
    ) -> dict[str, Any]:
        """Uloží 3-stavová oprávnění nástrojů (`allow`/`ask`/`block`) pro účet nebo MCP server."""
        safe_name = validate_plugin_name(plugin_name).lower()
        for raw_k, raw_v in (tool_permissions or {}).items():
            if not isinstance(raw_k, str) or not raw_k.strip() or len(raw_k.strip()) > 120:
                raise ValueError(f"Neplatný název nástroje: {raw_k!r}")
            if not isinstance(raw_v, str) or raw_v.strip().lower() not in TOOL_PERMISSION_MODES:
                raise ValueError(
                    f"Neplatný režim oprávnění pro '{raw_k}': {raw_v!r} "
                    "(povoleno: allow, ask, block)."
                )

        if safe_name in PLUGIN_TOOL_CATALOG or account_id is not None:
            if account_id is not None:
                acct = self.db.get_plugin_account(account_id)
                if acct is None or acct.plugin_name.lower() != safe_name:
                    raise ValueError(f"Účet pluginu '{safe_name}' (#{account_id}) nebyl nalezen.")
            else:
                acct = self.ensure_default_account(safe_name)

            perms_json: str | None = None
            tool_perms_json: str | None = None
            if safe_name in PLUGIN_TOOL_CATALOG:
                cur_summary = normalize_plugin_permissions(safe_name, acct.permissions_json)
                cur_tp = normalize_tool_permissions(
                    safe_name, acct.tool_permissions_json, cur_summary
                )
                valid_keys = {sp.key for sp in PLUGIN_TOOL_CATALOG[safe_name]}
                for raw_k, raw_v in (tool_permissions or {}).items():
                    ck = raw_k.strip()
                    if ck in valid_keys:
                        cur_tp[ck] = raw_v.strip().lower()
                new_summary = sync_summary_from_tool_permissions(safe_name, cur_tp, cur_summary)
                perms_json = json.dumps(new_summary, ensure_ascii=False)
                tool_perms_json = json.dumps(cur_tp, ensure_ascii=False)

            updated = self.db.update_plugin_account(
                acct.id,
                label=label,
                scope=scope,
                enabled=enabled,
                permissions_json=perms_json,
                tool_permissions_json=tool_perms_json,
                custom_rules=custom_rules,
            )
            assert updated is not None
            if sanitize_profile_key(updated.profile_key) == "default" and perms_json is not None:
                self.db.upsert_plugin_config(safe_name, permissions_json=updated.permissions_json)
            return {
                "status": "ok",
                "plugin": safe_name,
                "account": self._account_public_dict(updated, lang=lang),
            }

        custom_srv = self._find_custom_mcp_server(safe_name)
        if custom_srv is None:
            raise ValueError(
                f"Plugin '{safe_name}' nemá nastavitelné nástroje ani připojenou MCP URL."
            )
        cur_tp = parse_tool_permissions_raw(custom_srv.tool_permissions_json)
        for raw_k, raw_v in (tool_permissions or {}).items():
            ck = raw_k.strip()
            if ck and not any(ch.isspace() for ch in ck):
                cur_tp[ck] = raw_v.strip().lower()
            else:
                raise ValueError(f"Neplatný identifikátor MCP nástroje: {raw_k!r}")
        updated_srv = self.db.update_mcp_server_tools(
            custom_srv.id,
            tool_permissions_json=json.dumps(cur_tp, ensure_ascii=False),
        )
        return {
            "status": "ok",
            "plugin": safe_name,
            "custom_mcp": self._custom_mcp_public_dict(updated_srv),
        }

    def subscribe_connect(self) -> tuple[str, asyncio.Queue[str | None]]:
        """Přihlásí odběratele k živému PTY proudu připojování pluginu a vrátí dosavadní výstup."""
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
        self._connect_subscribers.add(queue)
        return self._raw_output, queue

    def unsubscribe_connect(self, queue: asyncio.Queue[str | None]) -> None:
        """Odhlásí odběratele z živého PTY proudu připojování pluginu."""
        self._connect_subscribers.discard(queue)

    def _broadcast_connect(self, data: str | None) -> None:
        for queue in list(self._connect_subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(data)

    def write_connect_raw(self, data: str) -> None:
        """Zapíše surové znaky z webového terminálu do PTY (uživatelský vstup převezme ovládání)."""
        if not data or not self._connect_state.running or self._master_fd is None:
            return
        if not is_terminal_auto_response(data):
            self._manual_override = True
        with contextlib.suppress(OSError):
            os.write(self._master_fd, data.encode("utf-8"))

    def resize_connect(self, cols: int, rows: int) -> None:
        """Upraví velikost PTY okna podle rozměrů xterm.js v modalu."""
        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))
        if (eff_cols, eff_rows) == self._pty_size:
            return
        self._pty_size = (eff_cols, eff_rows)
        if self._master_fd is not None and fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", eff_rows, eff_cols, 0, 0),
                )
            if (
                self._proc is not None
                and self._proc.poll() is None
                and hasattr(os, "killpg")
                and hasattr(signal, "SIGWINCH")
            ):
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGWINCH)

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
        cols: int = 100,
        rows: int = 26,
    ) -> PluginConnectState:
        """Spustí interaktivní `agy` relaci v PTY pro OAuth přihlášení nebo MCP konfiguraci."""
        safe_name = validate_plugin_name(plugin_name)
        await self.stop_connect()
        self._manual_override = False

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
        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))

        master_fd, slave_fd = pty.openpty()
        if fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(
                    slave_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", eff_rows, eff_cols, 0, 0),
                )

        env = self.settings.subprocess_env(gemini_api_key="", home_dir=eff_home)
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = str(eff_cols)
        env["LINES"] = str(eff_rows)

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
        self._raw_total_len = 0
        self._pty_size = (eff_cols, eff_rows)
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
                self._broadcast_connect(raw_text)
                self._raw_output = (self._raw_output + raw_text)[-48000:]
                self._raw_total_len += len(raw_text)
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
            self._broadcast_connect(None)

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
        """Automaticky projde případného průvodce, otevře `/plugin`, vybere plugin a spustí OAuth."""

        def _raw_cursor() -> int:
            return self._raw_total_len if self._raw_total_len > 0 else len(self._raw_output)

        def _recent_lower(mark: int) -> str:
            if self._raw_total_len > 0:
                delta = self._raw_total_len - mark
                if delta <= 0 or not self._raw_output:
                    return ""
                tail = self._raw_output[-min(delta, len(self._raw_output)) :]
                return clean_terminal_output(tail).lower()
            if mark <= 0 and not self._raw_output and self._connect_state.output:
                return self._connect_state.output.lower()
            if mark > len(self._raw_output):
                return self._connect_state.output.lower()
            return clean_terminal_output(self._raw_output[mark:]).lower()

        _tui_ready_markers = (
            "for shortcuts",
            "auto mode",
            "shift+tab",
            "plan mode",
            "ask mode",
            "choose your color scheme",
            "[next]",
            "[done]",
            "terms of service",
            "antigravity.google/terms",
            "trust this",
            "do you trust",
            "trust workspace",
            "press enter",
            "accept and continue",
            "installed",
            "discover",
            "marketplace:",
            "sign in",
            "sign-in:",
            "ready >",
        )

        try:
            # 1. Počkej, až se v TUI vykreslí připravená obrazovka nebo průvodce (ne jen první ANSI bajty)
            visible_ticks = 0
            for _ in range(50):
                if (
                    not self._connect_state.running
                    or self._master_fd is None
                    or self._manual_override
                ):
                    return
                vis = (
                    self._connect_state.output or clean_terminal_output(self._raw_output)
                ).lower()
                if any(tok in vis for tok in _tui_ready_markers):
                    await asyncio.sleep(0.5)
                    break
                if len(vis.strip()) >= 20:
                    visible_ticks += 1
                    if visible_ticks >= 12:
                        await asyncio.sleep(0.5)
                        break
                await asyncio.sleep(0.2)
            else:
                await asyncio.sleep(0.5)

            # 1b. Projdi případného 2-krokového průvodce agy (výběr barev [Next] -> Terms [Done] -> Trust)
            screen_mark = 0
            next_attempts = 0
            done_attempts = 0
            for _ in range(6):
                if (
                    not self._connect_state.running
                    or self._connect_state.auth_url
                    or self._manual_override
                ):
                    return
                recent = _recent_lower(screen_mark)
                if any(tok in recent for tok in ("installed", "discover", "marketplace:")):
                    break
                is_step1_color = (
                    "choose your color scheme" in recent
                    or ("welcome to antigravity cli" in recent and "[next]" in recent)
                )
                is_step2_done = "[done]" in recent and any(
                    tok in recent
                    for tok in (
                        "[previous]",
                        "terms of service",
                        "privacy policy",
                        "antigravity.google/terms",
                        "opt out",
                    )
                )
                is_simple_prompt = any(
                    tok in recent
                    for tok in (
                        "antigravity.google/terms",
                        "terms of service",
                        "accept and continue",
                        "press enter to continue",
                        "trust this",
                        "do you trust",
                        "trust workspace",
                    )
                )
                if is_step1_color:
                    screen_mark = _raw_cursor()
                    self._write_pty("\r" if next_attempts == 0 else "\t\r")
                    next_attempts += 1
                    await asyncio.sleep(1.3)
                    continue
                if is_step2_done:
                    screen_mark = _raw_cursor()
                    seq = ("\r", "\x1b[C\r", "\t\r")[min(done_attempts, 2)]
                    self._write_pty(seq)
                    done_attempts += 1
                    await asyncio.sleep(1.3)
                    continue
                if is_simple_prompt:
                    screen_mark = _raw_cursor()
                    self._write_pty("\r")
                    await asyncio.sleep(1.2)
                    continue
                break

            if (
                not self._connect_state.running
                or self._connect_state.auth_url
                or self._manual_override
            ):
                return
            plugin_mark = _raw_cursor()
            self._write_pty("/plugin\r")

            # 2. Vyčkej na obrazovku Správce pluginů (pozor na autocomplete řádek "Manage plugins")
            for step in range(40):
                if (
                    not self._connect_state.running
                    or self._connect_state.auth_url
                    or self._manual_override
                ):
                    return
                low = _recent_lower(plugin_mark)
                if "installed" in low or "discover" in low or "marketplaces" in low:
                    break
                if step == 8 and "/plugin" in low:
                    self._write_pty("\r")
                elif step == 18:
                    self._write_pty("\x1b")
                    await asyncio.sleep(0.2)
                    if (
                        not self._connect_state.running
                        or self._connect_state.auth_url
                        or self._manual_override
                    ):
                        return
                    self._write_pty("\x15")
                    await asyncio.sleep(0.1)
                    if (
                        not self._connect_state.running
                        or self._connect_state.auth_url
                        or self._manual_override
                    ):
                        return
                    plugin_mark = _raw_cursor()
                    self._write_pty("/plugin\r")
                elif step == 28 and "/plugin" in low:
                    self._write_pty("\r")
                await asyncio.sleep(0.2)

            if (
                not self._connect_state.running
                or self._connect_state.auth_url
                or self._manual_override
            ):
                return
            await asyncio.sleep(0.5)
            detail_mark = _raw_cursor()
            # Napiš název pluginu pro odfiltrování v záložce Installed a rozbal detail
            self._write_pty(plugin_name)
            await asyncio.sleep(0.6)
            if (
                not self._connect_state.running
                or self._connect_state.auth_url
                or self._manual_override
            ):
                return
            self._write_pty("\r")

            # 3. Vyčkej až 4 s na rozbalení detailu (nebo rovnou OAuth URL)
            for _ in range(20):
                if (
                    not self._connect_state.running
                    or self._connect_state.auth_url
                    or self._manual_override
                ):
                    return
                low = _recent_lower(detail_mark) or self._connect_state.output.lower()
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

            if (
                not self._connect_state.running
                or self._connect_state.auth_url
                or self._manual_override
            ):
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
                    if not self._connect_state.running or self._manual_override:
                        return
                    if "confirm sign out" in self._connect_state.output.lower():
                        break
                    await asyncio.sleep(0.2)
                if not self._connect_state.running or self._manual_override:
                    return
                self._write_pty("y")
                await asyncio.sleep(1.5)
                self._raw_output = ""
                self._raw_total_len = 0
                self._connect_state.output = ""
                if (
                    not self._connect_state.running
                    or self._connect_state.auth_url
                    or self._manual_override
                ):
                    return

            # 4. Spusť přihlášení (`Sign in`) a čekej na OAuth URL
            self._write_pty("\r")
            nudged_count = 0
            for step in range(30):
                if (
                    not self._connect_state.running
                    or self._connect_state.auth_url
                    or self._manual_override
                ):
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
                if nudged_count == 0 and step >= 12:
                    self._write_pty("\x1b[B\r")
                    nudged_count = 1
                elif nudged_count == 1 and step >= 22:
                    self._write_pty("\x1b[B\r")
                    nudged_count = 2
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
        self._manual_override = True
        os.write(self._master_fd, seq.encode("utf-8"))
        await asyncio.sleep(0.35)
        return self.get_connect_state()

    async def send_connect_input(self, value: str = "") -> PluginConnectState:
        """Pošle textový vstup (např. autorizační kód nebo volbu) do PTY relace."""
        if not self._connect_state.running or self._master_fd is None:
            raise RuntimeError("Relace připojování pluginu právě neběží.")
        self._manual_override = True
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
        res = await self.finalize_browser_oauth()
        return {
            **res,
            "http_status": http_status,
            "location": location,
        }

    async def finalize_browser_oauth(self, wait_seconds: float = 1.5) -> dict[str, Any]:
        """Dokončí OAuth relaci po přijetí callbacku v prohlížeči a uloží stav účtu."""
        state = self.get_connect_state()
        await asyncio.sleep(wait_seconds)
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
        }

    def _close_fd(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def stop_connect(self) -> None:
        """Bezpečně ukončí běžící PTY relaci připojování pluginu."""
        self._manual_override = False
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
        self._raw_total_len = 0
        self._pty_size = (0, 0)
        self._connect_state.running = False
        self._broadcast_connect(None)
