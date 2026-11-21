from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_LANGUAGE_ALIASES = {
    "cs": "cs",
    "cz": "cs",
    "czech": "cs",
    "cesky": "cs",
    "čeština": "cs",
    "cestina": "cs",
    "en": "en",
    "eng": "en",
    "english": "en",
    "anglicky": "en",
    "angličtina": "en",
    "anglictina": "en",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "deutsch": "de",
    "nemcina": "de",
    "němčina": "de",
    "nemecky": "de",
    "německy": "de",
    "sk": "sk",
    "svk": "sk",
    "slo": "sk",
    "slovak": "sk",
    "slovensky": "sk",
    "slovenčina": "sk",
    "slovencina": "sk",
    "slovenština": "sk",
    "slovenstina": "sk",
    "es": "es",
    "esp": "es",
    "spa": "es",
    "spanish": "es",
    "español": "es",
    "espanol": "es",
    "spanelstina": "es",
    "španělština": "es",
    "spanelsky": "es",
    "španělsky": "es",
    "pl": "pl",
    "pol": "pl",
    "polish": "pl",
    "polski": "pl",
    "polstina": "pl",
    "polština": "pl",
    "polsky": "pl",
}

_LOCALES_CACHE: dict[str, dict[str, Any]] | None = None


def reload_locales() -> dict[str, dict[str, Any]]:
    """Načte všechny jazykové JSON soubory ze složky `app/locales/` do paměti."""
    global _LOCALES_CACHE
    loaded: dict[str, dict[str, Any]] = {}
    if LOCALES_DIR.is_dir():
        for path in sorted(LOCALES_DIR.glob("*.json")):
            code = path.stem.strip().lower()
            if not code:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Nepodařilo se načíst jazykový soubor %s: %s", path.name, exc)
                continue
            if isinstance(raw, dict):
                loaded[code] = raw
    _LOCALES_CACHE = loaded
    return loaded


def _get_locales() -> dict[str, dict[str, Any]]:
    if _LOCALES_CACHE is None:
        return reload_locales()
    return _LOCALES_CACHE


def resolve_language(raw: str | None) -> str:
    """Bezpečně převede vstupní řetězec na kód dostupného jazyka (výchozí `en`)."""
    locales = _get_locales()
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in locales else next(iter(locales), DEFAULT_LANGUAGE)

    if cleaned in _LANGUAGE_ALIASES:
        candidate = _LANGUAGE_ALIASES[cleaned]
        if candidate in locales:
            return candidate

    primary = cleaned.replace("_", "-").split("-", 1)[0]
    if primary in _LANGUAGE_ALIASES:
        primary = _LANGUAGE_ALIASES[primary]
    if primary in locales:
        return primary
    if cleaned in locales:
        return cleaned
    return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in locales else next(iter(locales), DEFAULT_LANGUAGE)


def list_available_languages() -> list[dict[str, str]]:
    """Vrátí metadata všech dostupných jazyků (`en` je vždy první)."""
    locales = _get_locales()
    items: list[dict[str, str]] = []
    ordered_codes = sorted(locales.keys(), key=lambda c: (0 if c == DEFAULT_LANGUAGE else 1, c))
    for code in ordered_codes:
        meta = locales[code].get("_meta")
        meta_dict = meta if isinstance(meta, dict) else {}
        items.append(
            {
                "code": code,
                "name": str(meta_dict.get("name") or code.upper()),
                "short": str(meta_dict.get("short") or code.upper()),
                "date_locale": str(meta_dict.get("date_locale") or ("cs-CZ" if code == "cs" else "en-US")),
            }
        )
    return items


def get_ui_bundle(lang: str | None = None) -> dict[str, Any]:
    """Vrátí kompletní UI slovník pro daný jazyk s automatickým doplněním z výchozí `en`."""
    locales = _get_locales()
    resolved = resolve_language(lang)
    base_data = locales.get(DEFAULT_LANGUAGE, {})
    lang_data = locales.get(resolved, {})

    base_ui = base_data.get("ui") if isinstance(base_data.get("ui"), dict) else {}
    lang_ui = lang_data.get("ui") if isinstance(lang_data.get("ui"), dict) else {}
    merged_ui = {**base_ui, **lang_ui}

    meta_raw = lang_data.get("_meta") if isinstance(lang_data.get("_meta"), dict) else {}
    meta = {
        "code": resolved,
        "name": str(meta_raw.get("name") or resolved.upper()),
        "short": str(meta_raw.get("short") or resolved.upper()),
        "date_locale": str(meta_raw.get("date_locale") or ("cs-CZ" if resolved == "cs" else "en-US")),
    }
    return {
        "language": resolved,
        "meta": meta,
        "ui": merged_ui,
        "available_languages": list_available_languages(),
    }


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Vrátí lokalizovaný řetězec ze sekce `section.key` (výchozí sekce `backend`)."""
    if "." in key:
        section, item = key.split(".", 1)
    else:
        section, item = "backend", key

    locales = _get_locales()
    resolved = resolve_language(lang)

    template: str | None = None
    for code in (resolved, DEFAULT_LANGUAGE):
        data = locales.get(code)
        if not isinstance(data, dict):
            continue
        sec = data.get(section)
        if isinstance(sec, dict) and isinstance(sec.get(item), str):
            template = sec[item]
            break

    if template is None:
        return key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def get_language_name(lang: str | None = None) -> str:
    """Vrátí čitelný název jazyka podle `_meta.name`."""
    resolved = resolve_language(lang)
    for item in list_available_languages():
        if item["code"] == resolved:
            return item["name"]
    return resolved.upper()


def get_context_hint(context_mode: str, lang: str | None = None) -> str | None:
    """Vrátí lokalizovanou instrukci pro pracovní / osobní kontext."""
    if context_mode == "pracovni":
        return t("prompts.context_pracovni", lang)
    if context_mode == "osobni":
        return t("prompts.context_osobni", lang)
    return None


def get_language_hint(lang: str | None = None) -> str:
    """Vrátí instrukci komunikačního jazyka pro AI asistenta."""
    return t("prompts.language_hint", lang)


def channel_label(channel: str, lang: str | None = None) -> str:
    """Vrátí čitelný název kanálu chatu (`auto` / `pracovni` / `osobni`)."""
    cleaned = (channel or "auto").strip().lower()
    if cleaned not in {"auto", "pracovni", "osobni"}:
        cleaned = "auto"
    return t(f"backend.channel_{cleaned}", lang)
